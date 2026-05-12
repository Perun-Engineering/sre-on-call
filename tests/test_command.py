"""Unit tests for the /postmortem slash command flow."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import boto3
import pytest
from moto import mock_aws

from lambda_adapter.adapters import SlackWebhookAdapter, DiscordWebhookAdapter
from lambda_adapter.handler import lambda_handler
from lambda_adapter.intake import _process_command
from shared.models import CommandRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNING_SECRET = "test_signing_secret_abc123"
SLACK_BOT_TOKEN = "xoxb-test-token"
DEDUP_TABLE = "test-dedup-table"
AGENT_ENDPOINT = "TESTAGENT123"


def _make_signature(secret: str, timestamp: str, body: str) -> str:
    sig_basestring = f"v0:{timestamp}:{body}"
    h = hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return f"v0={h}"


def _build_slack_command_body(
    command: str = "/postmortem",
    text: str = "",
    channel_id: str = "C12345",
    user_id: str = "U67890",
    thread_ts: str | None = "1700000000.000100",
    response_url: str = "https://hooks.slack.com/commands/T123/456/789",
) -> str:
    fields = {
        "command": command,
        "text": text,
        "channel_id": channel_id,
        "user_id": user_id,
        "response_url": response_url,
    }
    if thread_ts is not None:
        fields["thread_ts"] = thread_ts
    return urlencode(fields)


def _build_command_event(
    body: str,
    *,
    signing_secret: str = SIGNING_SECRET,
    timestamp: str | None = None,
) -> dict:
    if timestamp is None:
        timestamp = str(int(time.time()))
    signature = _make_signature(signing_secret, timestamp, body)
    return {
        "headers": {
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
            "content-type": "application/x-www-form-urlencoded",
        },
        "body": body,
        "isBase64Encoded": False,
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_BOT_TOKEN)
    monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
    monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", AGENT_ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# ---------------------------------------------------------------------------
# SlackWebhookAdapter command detection
# ---------------------------------------------------------------------------


class TestSlackCommandDetection:
    def test_is_command_true_for_form_encoded(self):
        adapter = SlackWebhookAdapter(signing_secret=SIGNING_SECRET)
        headers = {"content-type": "application/x-www-form-urlencoded"}
        body = _build_slack_command_body()
        assert adapter.is_command(headers, body) is True

    def test_is_command_false_for_json(self):
        adapter = SlackWebhookAdapter(signing_secret=SIGNING_SECRET)
        headers = {"content-type": "application/json"}
        body = json.dumps({"type": "event_callback"})
        assert adapter.is_command(headers, body) is False

    def test_is_command_false_when_no_command_field(self):
        adapter = SlackWebhookAdapter(signing_secret=SIGNING_SECRET)
        headers = {"content-type": "application/x-www-form-urlencoded"}
        body = urlencode({"text": "hello"})
        assert adapter.is_command(headers, body) is False


class TestSlackParseCommand:
    def test_parses_all_fields(self):
        adapter = SlackWebhookAdapter(signing_secret=SIGNING_SECRET)
        body = _build_slack_command_body(
            command="/postmortem",
            text="extra notes",
            channel_id="C99999",
            user_id="U11111",
            thread_ts="1700000000.000200",
            response_url="https://hooks.slack.com/test",
        )
        cmd = adapter.parse_command(body)
        assert cmd.platform == "slack"
        assert cmd.command == "/postmortem"
        assert cmd.text == "extra notes"
        assert cmd.channel_id == "C99999"
        assert cmd.user_id == "U11111"
        assert cmd.thread_ts == "1700000000.000200"
        assert cmd.response_url == "https://hooks.slack.com/test"

    def test_thread_ts_none_when_absent(self):
        adapter = SlackWebhookAdapter(signing_secret=SIGNING_SECRET)
        body = _build_slack_command_body(thread_ts=None)
        cmd = adapter.parse_command(body)
        assert cmd.thread_ts is None


# ---------------------------------------------------------------------------
# DiscordWebhookAdapter command detection
# ---------------------------------------------------------------------------


class TestDiscordCommandDetection:
    def test_is_command_true_for_interaction_type_2(self):
        adapter = DiscordWebhookAdapter(public_key="abc")
        headers = {"content-type": "application/json"}
        body = json.dumps({"type": 2, "data": {"name": "postmortem"}})
        assert adapter.is_command(headers, body) is True

    def test_is_command_false_for_ping(self):
        adapter = DiscordWebhookAdapter(public_key="abc")
        headers = {"content-type": "application/json"}
        body = json.dumps({"type": 1})
        assert adapter.is_command(headers, body) is False

    def test_is_command_false_for_invalid_json(self):
        adapter = DiscordWebhookAdapter(public_key="abc")
        headers = {"content-type": "application/json"}
        assert adapter.is_command(headers, "not json") is False


class TestDiscordParseCommand:
    def test_parses_interaction(self):
        adapter = DiscordWebhookAdapter(public_key="abc")
        body = json.dumps({
            "type": 2,
            "id": "interaction-123",
            "token": "tok-abc",
            "channel_id": "999888777",
            "member": {"user": {"id": "discord-user-1"}},
            "data": {"name": "postmortem", "options": [{"name": "text", "value": "notes"}]},
        })
        cmd = adapter.parse_command(body)
        assert cmd.platform == "discord"
        assert cmd.command == "/postmortem"
        assert cmd.text == "notes"
        assert cmd.channel_id == "999888777"
        assert cmd.user_id == "discord-user-1"
        assert cmd.platform_metadata["interaction_id"] == "interaction-123"


# ---------------------------------------------------------------------------
# Intake command routing (end-to-end through lambda_handler)
# ---------------------------------------------------------------------------


class TestCommandRouting:
    def test_command_detected_and_routed(self):
        """Slash command should bypass JSON parsing and dedup."""
        body = _build_slack_command_body()
        event = _build_command_event(body)

        with patch.object(
            SlackWebhookAdapter, "ack_command",
        ) as mock_ack, patch("lambda_adapter.intake.boto3.client") as mock_client:
            mock_runtime = MagicMock()
            mock_client.return_value = mock_runtime

            result = lambda_handler(event, None)

            assert result["statusCode"] == 200
            mock_ack.assert_called_once()
            assert "Post-Incident Report" in mock_ack.call_args[0][1]
            mock_runtime.invoke_agent_runtime.assert_called_once()

    def test_command_invokes_agent_with_pir_task(self):
        body = _build_slack_command_body(thread_ts="1700000000.000100")
        event = _build_command_event(body)

        with patch.object(
            SlackWebhookAdapter, "ack_command",
        ), patch("lambda_adapter.intake.boto3.client") as mock_client:
            mock_runtime = MagicMock()
            mock_client.return_value = mock_runtime

            lambda_handler(event, None)

            call_kwargs = mock_runtime.invoke_agent_runtime.call_args[1]
            envelope = json.loads(call_kwargs["payload"].decode("utf-8"))
            payload = json.loads(envelope["params"]["message"]["parts"][0]["text"])
            assert payload["task"] == "pir"
            assert payload["channel_id"] == "C12345"
            assert payload["thread_ts"] == "1700000000.000100"

    def test_command_without_thread_returns_error(self):
        body = _build_slack_command_body(thread_ts=None)
        event = _build_command_event(body)

        with patch.object(
            SlackWebhookAdapter, "ack_command",
        ) as mock_ack, patch("lambda_adapter.intake.boto3.client") as mock_client:
            mock_runtime = MagicMock()
            mock_client.return_value = mock_runtime

            result = lambda_handler(event, None)

            assert result["statusCode"] == 200
            mock_ack.assert_called_once()
            assert "inside an incident thread" in mock_ack.call_args[0][1]
            mock_runtime.invoke_agent_runtime.assert_not_called()

    def test_unknown_command_returns_error(self):
        body = _build_slack_command_body(command="/unknown")
        event = _build_command_event(body)

        with patch.object(
            SlackWebhookAdapter, "ack_command",
        ), patch("lambda_adapter.intake.boto3.client"):
            result = lambda_handler(event, None)

            assert result["statusCode"] == 200
            body_json = json.loads(result["body"])
            assert "Unknown command" in body_json.get("text", "")

    def test_invalid_signature_rejected_for_command(self):
        body = _build_slack_command_body()
        event = _build_command_event(body)
        event["headers"]["x-slack-signature"] = "v0=invalid"

        result = lambda_handler(event, None)
        assert result["statusCode"] == 401


# ---------------------------------------------------------------------------
# PIR formatting
# ---------------------------------------------------------------------------


class TestPIRFormatting:
    def test_format_pir_contains_all_sections(self):
        from agents.master.report_formatter import ReportFormatter
        from shared.models import AlertContext, AgentFailure, AgentResult, Finding

        formatter = ReportFormatter()
        ctx = AlertContext(
            investigation_id="inv-pir-001",
            platform="slack",
            channel_id="C12345",
            message_id="1700000000.000100",
            alert_text="High CPU on api-server",
            alert_timestamp="2025-01-15T14:32:00Z",
            investigation_window=("2025-01-15T14:27:00Z", "2025-01-15T14:37:00Z"),
        )
        results: dict[str, AgentResult | AgentFailure] = {
            "slack_scanner": AgentResult(
                agent_name="slack_scanner", status="success",
                findings=[Finding(source="#ops", timestamp="2025-01-15T14:30:00Z", content="Related alert", severity="info")],
                summary="Found related alerts",
            ),
            "prometheus": AgentResult(
                agent_name="prometheus", status="success",
                findings=[], summary="CPU at 95%",
            ),
            "cloudwatch_logs": AgentResult(
                agent_name="cloudwatch_logs", status="success",
                findings=[], summary="OOM errors",
            ),
            "eks": AgentResult(
                agent_name="eks", status="success",
                findings=[], summary="Pod restarts",
            ),
        }
        pir = formatter.format_pir(ctx, results)
        assert "Post-Incident Report" in pir
        assert "Timeline" in pir
        assert "Root Cause" in pir
        assert "Impact" in pir
        assert "Action Items" in pir
        assert "Lessons Learned" in pir
        assert "High CPU on api-server" in pir

    def test_pir_timeline_includes_findings(self):
        from agents.master.report_formatter import ReportFormatter
        from shared.models import AlertContext, AgentFailure, AgentResult, Finding

        formatter = ReportFormatter()
        ctx = AlertContext(
            investigation_id="inv-pir-002",
            platform="slack",
            channel_id="C12345",
            message_id="1700000000.000100",
            alert_text="Disk full",
            alert_timestamp="2025-01-15T14:32:00Z",
            investigation_window=("2025-01-15T14:27:00Z", "2025-01-15T14:37:00Z"),
        )
        results: dict[str, AgentResult | AgentFailure] = {
            "slack_scanner": AgentResult(
                agent_name="slack_scanner", status="success",
                findings=[Finding(source="#alerts", timestamp="2025-01-15T14:31:00Z", content="Disk warning", severity="warning")],
                summary="Disk warnings found",
            ),
            "prometheus": AgentResult(
                agent_name="prometheus", status="error",
                findings=[], summary="", error_message="timeout",
            ),
            "cloudwatch_logs": AgentResult(
                agent_name="cloudwatch_logs", status="success",
                findings=[], summary="No errors",
            ),
            "eks": AgentResult(
                agent_name="eks", status="success",
                findings=[], summary="Healthy",
            ),
        }
        pir = formatter.format_pir(ctx, results)
        assert "Disk warning" in pir
        assert "14:31:00" in pir


class TestPIRRendering:
    def test_slack_renderer_pir(self):
        from shared.report_renderer import PIRSections, SlackReportRenderer

        renderer = SlackReportRenderer()
        sections = PIRSections(
            incident_summary="CPU spike",
            timeline="- 14:30 Alert\n- 14:35 Resolved",
            root_cause="Memory leak",
            impact="5min downtime",
            action_items="1. Fix leak",
            lessons_learned="Add memory limits",
        )
        output = renderer.render_pir(sections)
        assert "*Post-Incident Report*" in output
        assert "CPU spike" in output
        assert "Memory leak" in output

    def test_discord_renderer_pir(self):
        from shared.report_renderer import PIRSections, DiscordReportRenderer

        renderer = DiscordReportRenderer()
        sections = PIRSections(
            incident_summary="CPU spike",
            timeline="- 14:30 Alert",
            root_cause="Memory leak",
            impact="5min downtime",
            action_items="1. Fix leak",
            lessons_learned="Add memory limits",
        )
        output = renderer.render_pir(sections)
        assert "**Post-Incident Report**" in output
        assert "CPU spike" in output
