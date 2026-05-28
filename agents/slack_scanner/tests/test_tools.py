"""Unit tests for the Slack Scanner Agent tools.

Tests cover the SlackMessageSource adapter and the shared scanning
algorithm via ``execute_channel_scan``, including channel history
retrieval, bot/alert message filtering, investigation window scoping,
and Slack API error handling.

Also covers ``_execute_capture_snapshot`` — the ``/status`` snapshot
surface — exercising happy / anomaly / error paths against a mock
:class:`slack_sdk.WebClient`.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from agents.slack_scanner.tools import (
    SlackMessageSource,
    _execute_capture_snapshot,
)
from shared.channel_scan import execute_channel_scan
from shared.models import Finding
from shared.tool_result import ToolResult, build_agent_result, severity_from_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_slack_response(messages: list[dict]) -> MagicMock:
    """Build a mock Slack API response containing *messages*."""
    resp = MagicMock()
    resp.get.side_effect = lambda key, default=None: (
        messages if key == "messages" else default
    )
    resp.__getitem__ = lambda self, key: messages if key == "messages" else None
    return resp


def _source(mock_client: MagicMock) -> SlackMessageSource:
    """Build a SlackMessageSource backed by a mock WebClient."""
    return SlackMessageSource(mock_client)


def _scan(alert_ts: str, channels: list[str], mock_client: MagicMock) -> ToolResult:
    """Shorthand: run execute_channel_scan with a mocked Slack source."""
    return execute_channel_scan(alert_ts, channels, _source(mock_client))


# ---------------------------------------------------------------------------
# SlackMessageSource.is_alert
# ---------------------------------------------------------------------------

class TestIsAlert:
    """Tests for the SlackMessageSource.is_alert method."""

    def setup_method(self):
        self.source = SlackMessageSource(MagicMock())

    def test_message_with_bot_id(self):
        assert self.source.is_alert({"bot_id": "B123", "text": "hello"}) is True

    def test_message_with_bot_subtype(self):
        assert self.source.is_alert({"subtype": "bot_message", "text": "deploy complete"}) is True

    def test_message_with_alert_keyword(self):
        assert self.source.is_alert({"text": "CRITICAL: CPU usage above 90%"}) is True

    def test_message_with_error_keyword(self):
        assert self.source.is_alert({"text": "Error connecting to database"}) is True

    def test_message_with_warning_keyword(self):
        assert self.source.is_alert({"text": "Warning: disk space low"}) is True

    def test_regular_user_message_excluded(self):
        assert self.source.is_alert({"text": "Hey team, standup in 5 minutes"}) is False

    def test_empty_text_no_bot_fields(self):
        assert self.source.is_alert({"text": ""}) is False

    def test_missing_text_field(self):
        assert self.source.is_alert({}) is False

    def test_bot_id_empty_string_not_treated_as_bot(self):
        assert self.source.is_alert({"bot_id": "", "text": "just chatting"}) is False


# ---------------------------------------------------------------------------
# severity_from_text
# ---------------------------------------------------------------------------

class TestDetermineSeverity:
    """Tests for the severity_from_text helper."""

    def test_critical_keyword(self):
        assert severity_from_text("CRITICAL: service down") == "critical"

    def test_error_keyword(self):
        assert severity_from_text("Error: connection refused") == "warning"

    def test_failure_keyword(self):
        assert severity_from_text("Build failure in pipeline") == "warning"

    def test_no_keyword_returns_info(self):
        assert severity_from_text("Deployment succeeded") == "info"

    def test_case_insensitive(self):
        assert severity_from_text("cRiTiCaL issue") == "critical"


# ---------------------------------------------------------------------------
# build_agent_result
# ---------------------------------------------------------------------------

class TestBuildAgentResult:
    """Tests for the build_agent_result helper."""

    def test_success_with_findings(self):
        scan = ToolResult(
            findings=[
                Finding(source="C001", timestamp="2025-01-15T14:30:00+00:00",
                        content="alert fired", severity="critical")
            ],
            scanned_items=["C001"],
        )
        result = build_agent_result("slack_scanner", scan)
        assert result.status == "success"
        assert result.agent_name == "slack_scanner"
        assert len(result.findings) == 1
        assert "1 finding" in result.summary

    def test_error_only_no_findings(self):
        scan = ToolResult(errors=["Slack API error for channel C001: channel_not_found"])
        result = build_agent_result("slack_scanner", scan)
        assert result.status == "error"
        assert result.error_message is not None
        assert "channel_not_found" in result.error_message

    def test_partial_success_with_errors(self):
        scan = ToolResult(
            findings=[Finding(source="C001", timestamp="2025-01-15T14:30:00+00:00",
                              content="alert", severity="info")],
            scanned_items=["C001"],
            errors=["Slack API error for channel C002: not_in_channel"],
        )
        result = build_agent_result("slack_scanner", scan)
        assert result.status == "success"
        assert result.error_message is not None
        assert len(result.findings) == 1

    def test_no_findings_no_errors(self):
        scan = ToolResult(scanned_items=["C001"])
        result = build_agent_result("slack_scanner", scan)
        assert result.status == "success"
        assert result.error_message is None
        assert "0 finding" in result.summary


# ---------------------------------------------------------------------------
# execute_channel_scan with SlackMessageSource
# ---------------------------------------------------------------------------

class TestExecuteScan:
    """Tests for execute_channel_scan with a mocked SlackMessageSource."""

    ALERT_TS = "2025-01-15T14:32:00+00:00"

    def test_retrieves_messages_within_window(self):
        mock_client = MagicMock()
        bot_msg = {"bot_id": "B001", "text": "Alert: high latency", "ts": "1736951520.000100"}
        mock_client.conversations_history.return_value = _make_slack_response([bot_msg])

        result = _scan(self.ALERT_TS, ["C001"], mock_client)

        assert len(result.findings) == 1
        assert result.findings[0].source == "C001"
        assert "C001" in result.scanned_items
        call_kwargs = mock_client.conversations_history.call_args.kwargs
        assert "oldest" in call_kwargs
        assert "latest" in call_kwargs

    def test_bot_id_messages_included(self):
        mock_client = MagicMock()
        messages = [
            {"bot_id": "B001", "text": "Deploy notification", "ts": "1736951520.000100"},
            {"text": "Hey team, lunch?", "ts": "1736951521.000200"},
        ]
        mock_client.conversations_history.return_value = _make_slack_response(messages)

        result = _scan(self.ALERT_TS, ["C001"], mock_client)

        assert len(result.findings) == 1
        assert result.findings[0].content == "Deploy notification"

    def test_bot_subtype_messages_included(self):
        mock_client = MagicMock()
        messages = [{"subtype": "bot_message", "text": "CI build passed", "ts": "1736951520.000100"}]
        mock_client.conversations_history.return_value = _make_slack_response(messages)

        result = _scan(self.ALERT_TS, ["C001"], mock_client)

        assert len(result.findings) == 1
        assert result.findings[0].content == "CI build passed"

    def test_alert_keyword_messages_included(self):
        mock_client = MagicMock()
        messages = [{"text": "CRITICAL: CPU spike on prod-web-01", "ts": "1736951520.000100"}]
        mock_client.conversations_history.return_value = _make_slack_response(messages)

        result = _scan(self.ALERT_TS, ["C001"], mock_client)

        assert len(result.findings) == 1
        assert result.findings[0].severity == "critical"

    def test_regular_messages_excluded(self):
        mock_client = MagicMock()
        messages = [
            {"text": "Good morning everyone!", "ts": "1736951520.000100"},
            {"text": "Let's sync after standup", "ts": "1736951521.000200"},
        ]
        mock_client.conversations_history.return_value = _make_slack_response(messages)

        result = _scan(self.ALERT_TS, ["C001"], mock_client)

        assert len(result.findings) == 0

    def test_channel_selection_limits_to_10(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = _make_slack_response([])

        channels = [f"C{i:03d}" for i in range(15)]
        result = _scan(self.ALERT_TS, channels, mock_client)

        assert len(result.scanned_items) == 10
        assert mock_client.conversations_history.call_count == 10

    def test_investigation_window_scoping(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = _make_slack_response([])

        _scan(self.ALERT_TS, ["C001"], mock_client)

        call_kwargs = mock_client.conversations_history.call_args.kwargs
        oldest = float(call_kwargs["oldest"])
        latest = float(call_kwargs["latest"])

        alert_dt = datetime.fromisoformat(self.ALERT_TS)
        expected_oldest = alert_dt.timestamp() - 5 * 60
        expected_latest = alert_dt.timestamp() + 5 * 60

        assert abs(oldest - expected_oldest) < 1
        assert abs(latest - expected_latest) < 1

    def test_slack_api_error_caught(self):
        mock_client = MagicMock()
        error_response = MagicMock()
        error_response.__getitem__ = lambda self, key: "channel_not_found" if key == "error" else None
        mock_client.conversations_history.side_effect = SlackApiError(
            message="channel_not_found", response=error_response,
        )

        result = _scan(self.ALERT_TS, ["C001"], mock_client)

        assert len(result.errors) == 1
        assert "channel_not_found" in result.errors[0]
        assert len(result.findings) == 0

    def test_multiple_channels_scanned(self):
        mock_client = MagicMock()
        ch1_msgs = [{"bot_id": "B001", "text": "Alert from ch1", "ts": "1736951520.000100"}]
        ch2_msgs = [{"bot_id": "B002", "text": "Alert from ch2", "ts": "1736951521.000200"}]
        mock_client.conversations_history.side_effect = [
            _make_slack_response(ch1_msgs),
            _make_slack_response(ch2_msgs),
        ]

        result = _scan(self.ALERT_TS, ["C001", "C002"], mock_client)

        assert len(result.scanned_items) == 2
        assert len(result.findings) == 2
        assert {f.source for f in result.findings} == {"C001", "C002"}

    def test_empty_channel_history(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = _make_slack_response([])

        result = _scan(self.ALERT_TS, ["C001"], mock_client)

        assert len(result.findings) == 0
        assert "C001" in result.scanned_items
        assert len(result.errors) == 0

    def test_invalid_alert_timestamp(self):
        mock_client = MagicMock()

        result = _scan("not-a-timestamp", ["C001"], mock_client)

        assert len(result.errors) == 1
        assert "Invalid alert_timestamp" in result.errors[0]
        assert len(result.findings) == 0

    def test_empty_channel_list(self):
        mock_client = MagicMock()

        result = _scan(self.ALERT_TS, [], mock_client)

        assert len(result.findings) == 0
        assert len(result.scanned_items) == 0
        assert len(result.errors) == 0
        mock_client.conversations_history.assert_not_called()

    def test_partial_api_failure_across_channels(self):
        mock_client = MagicMock()
        good_msgs = [{"bot_id": "B001", "text": "Alert: disk full", "ts": "1736951520.000100"}]
        error_response = MagicMock()
        error_response.__getitem__ = lambda self, key: "not_in_channel" if key == "error" else None
        mock_client.conversations_history.side_effect = [
            _make_slack_response(good_msgs),
            SlackApiError(message="not_in_channel", response=error_response),
        ]

        result = _scan(self.ALERT_TS, ["C001", "C002"], mock_client)

        assert len(result.findings) == 1
        assert len(result.errors) == 1
        assert "C001" in result.scanned_items


# ---------------------------------------------------------------------------
# capture_snapshot — /status path
# ---------------------------------------------------------------------------

REQUESTED_AT = "2026-05-28T19:00:00+00:00"


def _slack_api_error(error_code: str) -> SlackApiError:
    """Build a SlackApiError whose ``.response.get('error')`` returns *error_code*."""
    return SlackApiError(message=error_code, response={"error": error_code})


class TestCaptureSnapshotHappyPath:
    """Both probes succeed → no anomaly, two sections with informational lines."""

    def _client(self) -> MagicMock:
        client = MagicMock()
        client.auth_test.return_value = {
            "ok": True,
            "team": "Acme Corp",
            "team_id": "T12345",
            "user": "sre-bot",
            "user_id": "U67890",
            "url": "https://acme.slack.com/",
        }
        client.users_conversations.return_value = {
            "channels": [{"id": f"C{i:03d}"} for i in range(7)],
        }
        return client

    def test_no_anomaly(self):
        report = _execute_capture_snapshot(self._client(), requested_at=REQUESTED_AT)
        assert report.anomaly is False
        assert report.anomaly_summary is None

    def test_captured_at_is_requested_at(self):
        report = _execute_capture_snapshot(self._client(), requested_at=REQUESTED_AT)
        assert report.captured_at == REQUESTED_AT

    def test_agent_name_set(self):
        report = _execute_capture_snapshot(self._client(), requested_at=REQUESTED_AT)
        assert report.agent_name == "slack_scanner"

    def test_authentication_section_populated(self):
        report = _execute_capture_snapshot(self._client(), requested_at=REQUESTED_AT)
        auth = next(s for s in report.sections if s.label == "Authentication")
        joined = "\n".join(auth.lines)
        assert "Acme Corp" in joined
        assert "T12345" in joined
        assert "sre-bot" in joined
        assert "U67890" in joined
        assert "https://acme.slack.com/" in joined

    def test_channel_access_section_reports_count(self):
        report = _execute_capture_snapshot(self._client(), requested_at=REQUESTED_AT)
        access = next(s for s in report.sections if s.label == "Channel access")
        assert any("7 channel" in line for line in access.lines)

    def test_authentication_omits_url_line_when_empty(self):
        client = self._client()
        client.auth_test.return_value = {
            "ok": True,
            "team": "Acme Corp",
            "team_id": "T12345",
            "user": "sre-bot",
            "user_id": "U67890",
            # no url key
        }
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)
        auth = next(s for s in report.sections if s.label == "Authentication")
        # No "workspace URL:" line when Slack didn't return one
        assert not any("workspace URL" in line for line in auth.lines)


class TestCaptureSnapshotAuthAnomalyPaths:
    """auth.test failures must flip the report to anomaly=True."""

    def test_non_ok_auth_test_flags_anomaly(self):
        client = MagicMock()
        client.auth_test.return_value = {"ok": False, "error": "token_expired"}
        client.users_conversations.return_value = {"channels": []}

        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        assert report.anomaly is True
        assert report.anomaly_summary is not None
        assert "token_expired" in report.anomaly_summary
        # Authentication section carries an ❌ line
        auth = next(s for s in report.sections if s.label == "Authentication")
        assert any("❌" in line and "token_expired" in line for line in auth.lines)

    def test_slack_api_error_on_auth_flags_anomaly(self):
        client = MagicMock()
        client.auth_test.side_effect = _slack_api_error("invalid_auth")
        client.users_conversations.return_value = {"channels": []}

        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        assert report.anomaly is True
        assert "invalid_auth" in (report.anomaly_summary or "")

    def test_generic_exception_on_auth_flags_anomaly_no_raise(self):
        client = MagicMock()
        client.auth_test.side_effect = RuntimeError("network down")
        client.users_conversations.return_value = {"channels": []}

        # No raise — the tool honours the no-raise contract.
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        assert report.anomaly is True
        assert "network down" in (report.anomaly_summary or "")
        # Channel access probe still ran after the auth failure.
        access = next(s for s in report.sections if s.label == "Channel access")
        assert any("0 channel" in line for line in access.lines)

    def test_anomaly_section_lines_describe_the_failure(self):
        client = MagicMock()
        client.auth_test.side_effect = _slack_api_error("ratelimited")
        client.users_conversations.return_value = {"channels": []}

        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        auth = next(s for s in report.sections if s.label == "Authentication")
        assert any("ratelimited" in line for line in auth.lines)


class TestCaptureSnapshotChannelProbeFailures:
    """users.conversations failures surface in the section but do NOT trigger
    anomaly — auth is still healthy, channel listing is a softer signal."""

    def test_slack_api_error_on_channels_does_not_flag_anomaly(self):
        client = MagicMock()
        client.auth_test.return_value = {
            "ok": True,
            "team": "Acme",
            "team_id": "T1",
            "user": "bot",
            "user_id": "U1",
        }
        client.users_conversations.side_effect = _slack_api_error("missing_scope")

        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        assert report.anomaly is False
        assert report.anomaly_summary is None
        access = next(s for s in report.sections if s.label == "Channel access")
        assert any("❌" in line and "missing_scope" in line for line in access.lines)

    def test_generic_exception_on_channels_does_not_flag_anomaly(self):
        client = MagicMock()
        client.auth_test.return_value = {
            "ok": True,
            "team": "Acme",
            "team_id": "T1",
            "user": "bot",
            "user_id": "U1",
        }
        client.users_conversations.side_effect = RuntimeError("boom")

        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        assert report.anomaly is False
        access = next(s for s in report.sections if s.label == "Channel access")
        assert any("boom" in line for line in access.lines)


class TestCaptureSnapshotShape:
    """Structural invariants of the returned SnapshotReport."""

    def test_returns_two_sections_in_order_auth_then_channels(self):
        client = MagicMock()
        client.auth_test.return_value = {
            "ok": True,
            "team": "Acme",
            "team_id": "T1",
            "user": "bot",
            "user_id": "U1",
        }
        client.users_conversations.return_value = {"channels": []}

        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        assert [s.label for s in report.sections] == ["Authentication", "Channel access"]

    def test_metadata_defaults_to_empty(self):
        client = MagicMock()
        client.auth_test.return_value = {"ok": True, "team": "A", "team_id": "T", "user": "b", "user_id": "U"}
        client.users_conversations.return_value = {"channels": []}

        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)

        assert report.metadata.model_id is None
        assert report.metadata.input_tokens is None
