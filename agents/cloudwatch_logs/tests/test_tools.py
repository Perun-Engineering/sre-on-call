"""Unit tests for the CloudWatch Logs Agent tools.

Tests cover the core query logic in ``agents.cloudwatch_logs.tools``,
including log group derivation/validation, Logs Insights query execution,
skip behavior for non-existent log groups, and query timeout handling.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from agents.cloudwatch_logs.tools import (
    _bytes_per_group,
    _execute_capture_snapshot,
    _execute_query,
    _get_existing_log_groups,
    _humanize_bytes,
    _list_log_groups,
    _poll_query_results,
    _query_error_counts,
    _results_to_findings,
)
from shared.models import Finding, SnapshotReport
from shared.tool_result import ToolResult, build_agent_result, severity_from_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_describe_response(log_group_names: list[str]) -> dict:
    """Build a mock describe_log_groups response."""
    return {
        "logGroups": [
            {"logGroupName": name} for name in log_group_names
        ],
    }


def _make_query_results(rows: list[dict]) -> list[list[dict]]:
    """Convert simple dicts to Logs Insights result format.

    Each row is a dict like ``{"@timestamp": "...", "@message": "..."}``.
    Returns the nested list-of-dicts format used by the API.
    """
    return [
        [{"field": k, "value": v} for k, v in row.items()]
        for row in rows
    ]


def _client_error(code: str = "AccessDeniedException", message: str = "denied") -> ClientError:
    """Create a botocore ClientError."""
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


# ---------------------------------------------------------------------------
# _get_existing_log_groups
# ---------------------------------------------------------------------------

class TestGetExistingLogGroups:
    """Tests for log group existence validation."""

    def test_all_groups_exist(self):
        client = MagicMock()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/my-service"]
        )

        existing, missing = _get_existing_log_groups(
            client, ["/aws/lambda/my-service"]
        )

        assert existing == ["/aws/lambda/my-service"]
        assert missing == []

    def test_some_groups_missing(self):
        client = MagicMock()

        def describe_side_effect(**kwargs):
            prefix = kwargs["logGroupNamePrefix"]
            if prefix == "/aws/lambda/exists":
                return _make_describe_response(["/aws/lambda/exists"])
            return _make_describe_response([])

        client.describe_log_groups.side_effect = describe_side_effect

        existing, missing = _get_existing_log_groups(
            client, ["/aws/lambda/exists", "/aws/lambda/gone"]
        )

        assert existing == ["/aws/lambda/exists"]
        assert missing == ["/aws/lambda/gone"]

    def test_all_groups_missing(self):
        client = MagicMock()
        client.describe_log_groups.return_value = _make_describe_response([])

        existing, missing = _get_existing_log_groups(
            client, ["/aws/lambda/nope"]
        )

        assert existing == []
        assert missing == ["/aws/lambda/nope"]

    def test_client_error_treated_as_missing(self):
        client = MagicMock()
        client.describe_log_groups.side_effect = _client_error()

        existing, missing = _get_existing_log_groups(
            client, ["/aws/lambda/err"]
        )

        assert existing == []
        assert missing == ["/aws/lambda/err"]

    def test_empty_input(self):
        client = MagicMock()

        existing, missing = _get_existing_log_groups(client, [])

        assert existing == []
        assert missing == []
        client.describe_log_groups.assert_not_called()


# ---------------------------------------------------------------------------
# _poll_query_results
# ---------------------------------------------------------------------------

class TestPollQueryResults:
    """Tests for polling Logs Insights query results."""

    def test_complete_on_first_poll(self):
        client = MagicMock()
        client.get_query_results.return_value = {
            "status": "Complete",
            "results": _make_query_results([
                {"@timestamp": "2025-01-15T14:32:00", "@message": "error log"},
            ]),
        }

        status, results = _poll_query_results(client, "qid-123", timeout=5)

        assert status == "Complete"
        assert len(results) == 1

    def test_polls_until_complete(self):
        client = MagicMock()
        client.get_query_results.side_effect = [
            {"status": "Running", "results": []},
            {"status": "Running", "results": []},
            {
                "status": "Complete",
                "results": _make_query_results([
                    {"@timestamp": "2025-01-15T14:32:00", "@message": "done"},
                ]),
            },
        ]

        status, results = _poll_query_results(client, "qid-123", timeout=10)

        assert status == "Complete"
        assert len(results) == 1
        assert client.get_query_results.call_count == 3

    def test_failed_query(self):
        client = MagicMock()
        client.get_query_results.return_value = {
            "status": "Failed",
            "results": [],
        }

        status, results = _poll_query_results(client, "qid-123", timeout=5)

        assert status == "Failed"
        assert results == []

    def test_timeout_returns_partial(self):
        """When polling times out, returns Timeout status with whatever is available."""
        client = MagicMock()
        # Always return Running — will time out
        client.get_query_results.return_value = {
            "status": "Running",
            "results": _make_query_results([
                {"@timestamp": "2025-01-15T14:32:00", "@message": "partial"},
            ]),
        }

        status, results = _poll_query_results(client, "qid-123", timeout=0)

        assert status == "Timeout"


# ---------------------------------------------------------------------------
# _results_to_findings
# ---------------------------------------------------------------------------

class TestResultsToFindings:
    """Tests for converting Logs Insights results to Finding objects."""

    def test_basic_conversion(self):
        rows = _make_query_results([
            {"@timestamp": "2025-01-15T14:32:00", "@message": "ERROR: connection refused"},
        ])

        findings = _results_to_findings(rows, ["/aws/lambda/svc"])

        assert len(findings) == 1
        assert findings[0].source == "/aws/lambda/svc"
        assert findings[0].timestamp == "2025-01-15T14:32:00"
        assert "connection refused" in findings[0].content
        assert findings[0].severity == "warning"  # "error" keyword

    def test_multiple_log_groups_in_source(self):
        rows = _make_query_results([
            {"@timestamp": "2025-01-15T14:32:00", "@message": "test"},
        ])

        findings = _results_to_findings(rows, ["/aws/a", "/aws/b"])

        assert findings[0].source == "/aws/a, /aws/b"

    def test_empty_results(self):
        findings = _results_to_findings([], ["/aws/lambda/svc"])
        assert findings == []

    def test_extra_fields_in_metadata(self):
        rows = _make_query_results([
            {
                "@timestamp": "2025-01-15T14:32:00",
                "@message": "info msg",
                "@ptr": "ptr-123",
                "custom_field": "custom_value",
            },
        ])

        findings = _results_to_findings(rows, ["/aws/lambda/svc"])

        assert findings[0].metadata["ptr"] == "ptr-123"
        assert findings[0].metadata["custom_field"] == "custom_value"


# ---------------------------------------------------------------------------
# severity_from_text
# ---------------------------------------------------------------------------

class TestDetermineSeverity:
    """Tests for the severity heuristic."""

    def test_critical(self):
        assert severity_from_text("CRITICAL: out of memory") == "critical"

    def test_fatal(self):
        assert severity_from_text("FATAL: process crashed") == "critical"

    def test_error(self):
        assert severity_from_text("ERROR: timeout") == "warning"

    def test_exception(self):
        assert severity_from_text("NullPointerException at line 42") == "warning"

    def test_info(self):
        assert severity_from_text("Request completed successfully") == "info"


# ---------------------------------------------------------------------------
# _build_agent_result
# ---------------------------------------------------------------------------

class TestBuildAgentResult:
    """Tests for converting ToolResult to AgentResult."""

    def test_success_with_findings(self):
        cw = ToolResult(
            findings=[
                Finding(
                    source="/aws/lambda/svc",
                    timestamp="2025-01-15T14:32:00",
                    content="error log",
                    severity="warning",
                )
            ],
            scanned_items=["/aws/lambda/svc"],
        )

        result = build_agent_result("cloudwatch_logs", cw)

        assert result.status == "success"
        assert result.agent_name == "cloudwatch_logs"
        assert len(result.findings) == 1
        assert "1 item" in result.summary

    def test_error_no_queried_groups(self):
        cw = ToolResult(
            errors=["None of the requested log groups exist: /aws/nope"],
        )

        result = build_agent_result("cloudwatch_logs", cw)

        assert result.status == "error"
        assert result.error_message is not None

    def test_skipped_groups_appear_as_findings(self):
        cw = ToolResult(
            findings=[
                Finding(
                    source="/aws/gone",
                    timestamp="",
                    content="Log group '/aws/gone' does not exist — skipped.",
                    severity="warning",
                    metadata={"skipped": True},
                ),
            ],
            scanned_items=["/aws/exists"],
        )

        result = build_agent_result("cloudwatch_logs", cw)

        assert result.status == "success"
        skip_findings = [f for f in result.findings if f.metadata.get("skipped")]
        assert len(skip_findings) == 1

    def test_no_findings_no_errors(self):
        cw = ToolResult(
            scanned_items=["/aws/lambda/svc"],
        )

        result = build_agent_result("cloudwatch_logs", cw)

        assert result.status == "success"
        assert result.error_message is None


# ---------------------------------------------------------------------------
# _execute_query — core query logic
# ---------------------------------------------------------------------------

class TestExecuteQuery:
    """Tests for _execute_query — client is passed in directly."""

    START_TIME = 1736951220  # 2025-01-15T14:27:00 UTC
    END_TIME = 1736951820    # 2025-01-15T14:37:00 UTC
    QUERY = "fields @timestamp, @message | filter @message like /ERROR/"

    def _make_client(self) -> MagicMock:
        return MagicMock()

    def test_successful_query(self):
        client = self._make_client()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/svc"]
        )
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {
            "status": "Complete",
            "results": _make_query_results([
                {"@timestamp": "2025-01-15T14:32:00", "@message": "ERROR: timeout"},
            ]),
        }

        result = _execute_query(
            client, ["/aws/lambda/svc"], self.QUERY, self.START_TIME, self.END_TIME,
        )

        assert len(result.scanned_items) == 1
        assert not any(f.metadata.get("skipped") for f in result.findings)
        assert any("timeout" in f.content for f in result.findings)

    def test_non_existent_groups_skipped(self):
        client = self._make_client()

        def describe_side_effect(**kwargs):
            prefix = kwargs["logGroupNamePrefix"]
            if prefix == "/aws/lambda/exists":
                return _make_describe_response(["/aws/lambda/exists"])
            return _make_describe_response([])

        client.describe_log_groups.side_effect = describe_side_effect
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {
            "status": "Complete",
            "results": [],
        }

        result = _execute_query(
            client,
            ["/aws/lambda/exists", "/aws/lambda/gone"],
            self.QUERY,
            self.START_TIME,
            self.END_TIME,
        )

        assert result.scanned_items == ["/aws/lambda/exists"]
        skip_findings = [f for f in result.findings if f.metadata.get("skipped")]
        assert len(skip_findings) == 1
        assert "/aws/lambda/gone" in skip_findings[0].content

    def test_all_groups_missing_returns_error(self):
        client = self._make_client()
        client.describe_log_groups.return_value = _make_describe_response([])

        result = _execute_query(
            client, ["/aws/lambda/nope"], self.QUERY, self.START_TIME, self.END_TIME,
        )

        assert len(result.errors) >= 1
        assert "None of the requested log groups exist" in result.errors[-1]
        assert result.scanned_items == []

    def test_empty_log_group_list(self):
        client = self._make_client()

        result = _execute_query(client, [], self.QUERY, self.START_TIME, self.END_TIME)

        assert len(result.errors) == 1
        assert "No log group names" in result.errors[0]
        client.describe_log_groups.assert_not_called()

    def test_query_timeout_returns_partial(self):
        client = self._make_client()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/svc"]
        )
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {
            "status": "Running",
            "results": _make_query_results([
                {"@timestamp": "2025-01-15T14:32:00", "@message": "partial result"},
            ]),
        }

        with patch("agents.cloudwatch_logs.tools._QUERY_POLL_TIMEOUT_SECONDS", 0):
            result = _execute_query(
                client, ["/aws/lambda/svc"], self.QUERY, self.START_TIME, self.END_TIME,
            )

        timeout_findings = [f for f in result.findings if f.metadata.get("timeout")]
        assert len(timeout_findings) == 1

    def test_start_query_permission_error(self):
        client = self._make_client()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/svc"]
        )
        client.start_query.side_effect = _client_error(
            "AccessDeniedException", "Not authorized"
        )

        result = _execute_query(
            client, ["/aws/lambda/svc"], self.QUERY, self.START_TIME, self.END_TIME,
        )

        assert len(result.errors) >= 1
        assert "AccessDeniedException" in result.errors[-1]

    def test_failed_query_status(self):
        client = self._make_client()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/svc"]
        )
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {
            "status": "Failed",
            "results": [],
        }

        result = _execute_query(
            client, ["/aws/lambda/svc"], self.QUERY, self.START_TIME, self.END_TIME,
        )

        assert len(result.errors) >= 1
        assert "failed" in result.errors[-1].lower()


class TestExecuteQueryDeepLinks:
    """Result findings carry a Logs Insights deep link into their query/window."""

    START_TIME = 1736951220
    END_TIME = 1736951820
    QUERY = "fields @timestamp, @message | filter @message like /ERROR/"

    def _client_in_region(self, region: str = "us-east-1") -> MagicMock:
        client = MagicMock()
        client.meta.region_name = region
        return client

    def test_result_findings_get_deep_link(self):
        client = self._client_in_region()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/svc"]
        )
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {
            "status": "Complete",
            "results": _make_query_results([
                {"@timestamp": "2025-01-15T14:32:00", "@message": "ERROR: timeout"},
            ]),
        }

        result = _execute_query(
            client, ["/aws/lambda/svc"], self.QUERY, self.START_TIME, self.END_TIME,
        )

        finding = next(f for f in result.findings if "timeout" in f.content)
        assert finding.link is not None
        # Points at the Logs Insights console with the escaped query + group.
        assert finding.link.startswith(
            "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
        )
        assert "logs-insights" in finding.link
        assert "*2faws*2flambda*2fsvc" in finding.link

    def test_timeout_finding_gets_deep_link(self):
        client = self._client_in_region()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/svc"]
        )
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {
            "status": "Running",
            "results": [],
        }

        with patch("agents.cloudwatch_logs.tools._QUERY_POLL_TIMEOUT_SECONDS", 0):
            result = _execute_query(
                client, ["/aws/lambda/svc"], self.QUERY, self.START_TIME, self.END_TIME,
            )

        timeout_finding = next(f for f in result.findings if f.metadata.get("timeout"))
        assert timeout_finding.link is not None
        assert "logs-insights" in timeout_finding.link

    def test_skipped_group_finding_has_no_link(self):
        client = self._client_in_region()

        def describe_side_effect(**kwargs):
            if kwargs["logGroupNamePrefix"] == "/aws/lambda/exists":
                return _make_describe_response(["/aws/lambda/exists"])
            return _make_describe_response([])

        client.describe_log_groups.side_effect = describe_side_effect
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {"status": "Complete", "results": []}

        result = _execute_query(
            client,
            ["/aws/lambda/exists", "/aws/lambda/gone"],
            self.QUERY,
            self.START_TIME,
            self.END_TIME,
        )

        skipped = next(f for f in result.findings if f.metadata.get("skipped"))
        assert skipped.link is None

    def test_link_construction_failure_is_fail_open(self):
        # No region on the client => no link, but findings still come through.
        client = MagicMock()
        client.meta.region_name = None
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/svc"]
        )
        client.start_query.return_value = {"queryId": "qid-1"}
        client.get_query_results.return_value = {
            "status": "Complete",
            "results": _make_query_results([
                {"@timestamp": "2025-01-15T14:32:00", "@message": "ERROR: boom"},
            ]),
        }

        result = _execute_query(
            client, ["/aws/lambda/svc"], self.QUERY, self.START_TIME, self.END_TIME,
        )

        finding = next(f for f in result.findings if "boom" in f.content)
        assert finding.link is None


# ---------------------------------------------------------------------------
# capture_snapshot — /sre-snapshot path (i-a: IncomingBytes ranking + bounded Insights)
# ---------------------------------------------------------------------------


REQUESTED_AT = "2026-05-28T19:00:00+00:00"
NOW = datetime(2026, 5, 28, 19, 0, 0, tzinfo=timezone.utc)


# ---- helpers --------------------------------------------------------------


def _describe_response(group_names: list[str], next_token: str | None = None) -> dict:
    response = {"logGroups": [{"logGroupName": n} for n in group_names]}
    if next_token:
        response["nextToken"] = next_token
    return response


def _metric_data_response(volumes: dict[str, float], *, id_start: int = 0) -> dict:
    """Build a get_metric_data response from {qid: bytes} pairs.

    Helper that maps query IDs `q<id_start>..q<id_start+N-1>` to the order
    log groups were passed in. Tests construct {<group>: bytes} and the
    helper internally figures out the q-id mapping. ``id_start`` lets
    tests build successive batches with the correct offsets.
    """
    results = []
    for i, (_, vol) in enumerate(volumes.items()):
        results.append({
            "Id": f"q{id_start + i}",
            "Label": "IncomingBytes",
            "Timestamps": [NOW.isoformat()],
            "Values": [vol] if vol else [],
            "StatusCode": "Complete",
        })
    return {"MetricDataResults": results}


def _insights_results(counts: dict[str, int]) -> list[list[dict]]:
    return [
        [{"field": "@logGroup", "value": group}, {"field": "count()", "value": str(c)}]
        for group, c in counts.items()
    ]


def _setup_happy_clients(
    *,
    groups: list[str] | None = None,
    volumes: dict[str, float] | None = None,
    error_counts: dict[str, int] | None = None,
) -> tuple[MagicMock, MagicMock]:
    groups = groups or ["/aws/lambda/foo"]
    volumes = volumes or {g: 1024.0 for g in groups}
    error_counts = error_counts or {}

    logs_client = MagicMock()
    logs_client.describe_log_groups.return_value = _describe_response(groups)
    logs_client.start_query.return_value = {"queryId": "qid-1"}
    logs_client.get_query_results.return_value = {
        "status": "Complete",
        "results": _insights_results(error_counts),
    }

    cw_client = MagicMock()
    cw_client.get_metric_data.return_value = _metric_data_response(volumes)
    return logs_client, cw_client


def _section_lines(report: SnapshotReport, label_substring: str = "Top log groups") -> list[str]:
    for s in report.sections:
        if label_substring in s.label:
            return s.lines
    raise AssertionError(f"section matching {label_substring!r} not found")


# ---- _humanize_bytes ------------------------------------------------------


class TestHumanizeBytes:
    def test_zero_bytes(self):
        assert _humanize_bytes(0) == "0.0 B"

    def test_kilobytes(self):
        assert _humanize_bytes(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _humanize_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        assert _humanize_bytes(1.5 * 1024 ** 3) == "1.5 GB"


# ---- _list_log_groups -----------------------------------------------------


class TestListLogGroups:
    def test_single_page(self):
        client = MagicMock()
        client.describe_log_groups.return_value = _describe_response(["a", "b"])
        result = _list_log_groups(client, max_groups=500)
        assert result == ["a", "b"]

    def test_paginated(self):
        client = MagicMock()
        client.describe_log_groups.side_effect = [
            _describe_response(["a", "b"], next_token="t1"),
            _describe_response(["c"], next_token=None),
        ]
        result = _list_log_groups(client, max_groups=500)
        assert result == ["a", "b", "c"]
        assert client.describe_log_groups.call_count == 2

    def test_max_groups_caps_pagination(self):
        client = MagicMock()
        client.describe_log_groups.side_effect = [
            _describe_response(["a", "b", "c"], next_token="t1"),
            # Should never be called — cap reached after first page
        ]
        result = _list_log_groups(client, max_groups=2)
        assert result == ["a", "b"]


# ---- _bytes_per_group -----------------------------------------------------


class TestBytesPerGroup:
    def test_maps_results_back_to_group_names(self):
        cw = MagicMock()
        cw.get_metric_data.return_value = _metric_data_response(
            {"a": 100.0, "b": 200.0, "c": 0.0},
        )
        totals = _bytes_per_group(cw, ["a", "b", "c"], NOW, NOW)
        assert totals == {"a": 100.0, "b": 200.0, "c": 0.0}

    def test_chunks_in_500_query_batches(self):
        cw = MagicMock()
        cw.get_metric_data.side_effect = [
            _metric_data_response({f"g{i}": 1.0 for i in range(500)}, id_start=0),
            _metric_data_response({f"g{i}": 2.0 for i in range(500, 510)}, id_start=500),
        ]
        groups = [f"g{i}" for i in range(510)]
        totals = _bytes_per_group(cw, groups, NOW, NOW)
        assert len(totals) == 510
        assert cw.get_metric_data.call_count == 2


# ---- _query_error_counts --------------------------------------------------


class TestQueryErrorCounts:
    def test_parses_at_log_group_and_count(self):
        client = MagicMock()
        client.start_query.return_value = {"queryId": "q1"}
        client.get_query_results.return_value = {
            "status": "Complete",
            "results": _insights_results({"/aws/lambda/foo": 5, "/aws/lambda/bar": 0}),
        }
        counts = _query_error_counts(
            client, ["/aws/lambda/foo", "/aws/lambda/bar"], NOW, NOW,
        )
        assert counts == {"/aws/lambda/foo": 5, "/aws/lambda/bar": 0}

    def test_non_complete_status_returns_empty(self):
        client = MagicMock()
        client.start_query.return_value = {"queryId": "q1"}
        client.get_query_results.return_value = {
            "status": "Failed",
            "results": [],
        }
        counts = _query_error_counts(client, ["/aws/lambda/foo"], NOW, NOW)
        assert counts == {}


# ---- happy path -----------------------------------------------------------


class TestCaptureSnapshotHappyPath:
    def test_no_anomaly_with_zero_error_counts(self):
        logs, cw = _setup_happy_clients(
            groups=["/aws/lambda/foo", "/aws/lambda/bar"],
            volumes={"/aws/lambda/foo": 5_000_000, "/aws/lambda/bar": 1_000_000},
            error_counts={"/aws/lambda/foo": 0, "/aws/lambda/bar": 0},
        )
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        assert report.anomaly is False

    def test_top_n_lines_include_byte_volume_and_error_count(self):
        logs, cw = _setup_happy_clients(
            groups=["/aws/lambda/foo", "/aws/lambda/bar"],
            volumes={"/aws/lambda/foo": 5_242_880, "/aws/lambda/bar": 1_048_576},
            error_counts={"/aws/lambda/foo": 0, "/aws/lambda/bar": 0},
        )
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        joined = "\n".join(_section_lines(report))
        assert "/aws/lambda/foo · 5.0 MB · 0 errors" in joined
        assert "/aws/lambda/bar · 1.0 MB · 0 errors" in joined

    def test_results_sorted_by_volume_desc(self):
        logs, cw = _setup_happy_clients(
            groups=["small", "large", "medium"],
            volumes={"small": 100, "large": 10_000, "medium": 1_000},
            error_counts={"small": 0, "large": 0, "medium": 0},
        )
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        lines = _section_lines(report)
        # Order: large, medium, small
        assert lines[0].startswith("large")
        assert lines[1].startswith("medium")
        assert lines[2].startswith("small")

    def test_truncated_to_top_n(self):
        groups = [f"g{i}" for i in range(20)]
        volumes = {g: float(i) for i, g in enumerate(groups, start=1)}
        logs, cw = _setup_happy_clients(
            groups=groups,
            volumes=volumes,
            error_counts={g: 0 for g in groups},
        )
        report = _execute_capture_snapshot(
            logs, cw, requested_at=REQUESTED_AT, now=NOW, top_n=10,
        )
        lines = _section_lines(report)
        assert len(lines) == 10

    def test_zero_volume_groups_excluded(self):
        logs, cw = _setup_happy_clients(
            groups=["live", "dead"],
            volumes={"live": 1024, "dead": 0},
            error_counts={"live": 0},
        )
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        lines = _section_lines(report)
        # "dead" volume=0 is not in the top-N; "live" is
        assert any("live" in line for line in lines)
        assert not any("dead" in line for line in lines)


# ---- anomaly path ---------------------------------------------------------


class TestCaptureSnapshotAnomalyPath:
    def test_any_top_n_with_errors_flips_anomaly(self):
        logs, cw = _setup_happy_clients(
            groups=["a", "b"],
            volumes={"a": 1024, "b": 1024},
            error_counts={"a": 47, "b": 0},
        )
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        assert report.anomaly is True
        summary = report.anomaly_summary or ""
        assert "1 log group" in summary
        assert "errors" in summary

    def test_error_count_in_section_line(self):
        logs, cw = _setup_happy_clients(
            groups=["api"],
            volumes={"api": 5_242_880},
            error_counts={"api": 47},
        )
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        joined = "\n".join(_section_lines(report))
        assert "api · 5.0 MB · 47 errors" in joined


# ---- empty / soft-failure paths -------------------------------------------


class TestCaptureSnapshotEdgeCases:
    def test_no_log_groups_in_account(self):
        logs = MagicMock()
        logs.describe_log_groups.return_value = _describe_response([])
        cw = MagicMock()
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        assert report.anomaly is False
        joined = "\n".join(_section_lines(report))
        assert "no log groups" in joined.lower()
        # GetMetricData is never called when there are no groups
        cw.get_metric_data.assert_not_called()

    def test_no_traffic_in_window(self):
        logs, cw = _setup_happy_clients(
            groups=["idle"],
            volumes={"idle": 0.0},
            error_counts={},
        )
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        assert report.anomaly is False
        joined = "\n".join(_section_lines(report))
        assert "no log group received any traffic" in joined.lower()

    def test_insights_query_failure_is_soft(self):
        # Top-N still renders; error_count column is replaced with a notice.
        logs, cw = _setup_happy_clients(
            groups=["foo"],
            volumes={"foo": 1024},
        )
        logs.start_query.side_effect = _client_error("AccessDenied", "no logs perms")
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        # NOT anomaly — error count is informational
        assert report.anomaly is False
        joined = "\n".join(_section_lines(report))
        assert "foo · 1.0 KB" in joined
        assert "error count unavailable" in joined.lower()


# ---- primary-probe failures ----------------------------------------------


class TestCaptureSnapshotPrimaryFailures:
    def test_describe_log_groups_failure_is_anomaly(self):
        logs = MagicMock()
        logs.describe_log_groups.side_effect = _client_error("AccessDenied", "no")
        cw = MagicMock()
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        assert report.anomaly is True
        assert "describe_log_groups" in (report.anomaly_summary or "")
        cw.get_metric_data.assert_not_called()

    def test_get_metric_data_failure_is_anomaly(self):
        logs = MagicMock()
        logs.describe_log_groups.return_value = _describe_response(["foo"])
        cw = MagicMock()
        cw.get_metric_data.side_effect = _client_error("Throttling", "slow down")
        report = _execute_capture_snapshot(logs, cw, requested_at=REQUESTED_AT, now=NOW)
        assert report.anomaly is True
        assert "get_metric_data" in (report.anomaly_summary or "")
        # Insights query never attempted when ranking failed
        logs.start_query.assert_not_called()


# ---------------------------------------------------------------------------
# Chart descriptor + series emission
# ---------------------------------------------------------------------------


class TestChartEmission:
    def _client_with_rows(self, rows, status="Complete"):
        client = MagicMock()
        client.describe_log_groups.return_value = _make_describe_response(
            ["/aws/lambda/x"],
        )
        client.start_query.return_value = {"queryId": "qid"}
        client.get_query_results.return_value = {"status": status, "results": rows}
        return client

    def test_emits_descriptor_and_series(self, monkeypatch):
        monkeypatch.setenv("CHART_SNAPSHOTS_ENABLED", "true")
        rows = [[
            {"field": "@timestamp", "value": "2026-01-01T00:00:00"},
            {"field": "@message", "value": "boom"},
        ]]
        client = self._client_with_rows(rows)

        result = _execute_query(client, ["/aws/lambda/x"], "fields @message", 1000, 2000)

        charted = [f for f in result.findings if f.chart is not None]
        assert charted, "row findings should carry a chart descriptor"
        chart_id = charted[0].chart.chart_id
        assert chart_id in result.chart_series
        assert result.chart_series[chart_id].points[0]["@message"] == "boom"

    def test_no_chart_when_disabled(self, monkeypatch):
        monkeypatch.setenv("CHART_SNAPSHOTS_ENABLED", "false")
        rows = [[{"field": "@message", "value": "boom"}]]
        client = self._client_with_rows(rows)

        result = _execute_query(client, ["/aws/lambda/x"], "fields @message", 1000, 2000)

        assert result.chart_series == {}
        assert all(f.chart is None for f in result.findings)

    def test_no_chart_on_empty_results(self, monkeypatch):
        monkeypatch.setenv("CHART_SNAPSHOTS_ENABLED", "true")
        client = self._client_with_rows([])

        result = _execute_query(client, ["/aws/lambda/x"], "fields @message", 1000, 2000)

        assert result.chart_series == {}

    def test_series_capped_at_1000(self, monkeypatch):
        monkeypatch.setenv("CHART_SNAPSHOTS_ENABLED", "true")
        rows = [[{"field": "@message", "value": str(i)}] for i in range(1100)]
        client = self._client_with_rows(rows)

        result = _execute_query(client, ["/aws/lambda/x"], "fields @message", 1000, 2000)

        chart_id = next(iter(result.chart_series))
        series = result.chart_series[chart_id]
        assert series.truncated is True
        assert len(series.points) == 1000

    def test_binned_query_sets_series_kind(self, monkeypatch):
        monkeypatch.setenv("CHART_SNAPSHOTS_ENABLED", "true")
        rows = [[
            {"field": "bin", "value": "2026-01-01T00:00:00"},
            {"field": "count", "value": "5"},
        ]]
        client = self._client_with_rows(rows)

        result = _execute_query(
            client, ["/aws/lambda/x"],
            "filter @message like /err/ | stats count() by bin(5m)", 1000, 2000,
        )

        chart_id = next(iter(result.chart_series))
        assert result.chart_series[chart_id].series_kind == "binned"
