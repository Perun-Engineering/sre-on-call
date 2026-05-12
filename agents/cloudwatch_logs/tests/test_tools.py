"""Unit tests for the CloudWatch Logs Agent tools.

Tests cover the core query logic in ``agents.cloudwatch_logs.tools``,
including log group derivation/validation, Logs Insights query execution,
skip behavior for non-existent log groups, and query timeout handling.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from agents.cloudwatch_logs.tools import (
    _execute_query,
    _get_existing_log_groups,
    _poll_query_results,
    _results_to_findings,
)
from shared.models import AgentResult, Finding
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
