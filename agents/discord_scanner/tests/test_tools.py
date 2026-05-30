"""Tests for Discord adapter components: signature, parser, handler, chat poster, and renderer."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from shared.platforms.discord import verify_discord_signature
from shared.platforms.discord import parse_alert_context as discord_parse
from lambda_adapter.handler import lambda_handler as discord_handler
from shared.platforms import for_platform
from shared.platforms.discord import DiscordChatPlatform
from shared.report_renderer import DiscordReportRenderer, ReportSections, EvidenceBlock, EnrichmentSections
from shared.models import AlertContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ed25519_keypair():
    """Generate an Ed25519 keypair for testing."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    pub_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_key, pub_bytes.hex()


def _sign_discord(private_key, timestamp: str, body: str) -> str:
    """Sign a Discord request body."""
    message = f"{timestamp}{body}".encode()
    sig = private_key.sign(message)
    return sig.hex()


def _discord_alert_context(**overrides) -> AlertContext:
    defaults = dict(
        investigation_id="inv-discord-001",
        platform="discord",
        channel_id="123456789",
        message_id="987654321",
        alert_text="ALERT: High CPU on prod",
        alert_timestamp="2025-01-15T14:32:00+00:00",
        investigation_window=("2025-01-15T14:27:00+00:00", "2025-01-15T14:37:00+00:00"),
        platform_metadata={"guild_id": "111222333", "message_id": "987654321"},
    )
    defaults.update(overrides)
    return AlertContext(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# Discord Signature Verification
# ===========================================================================


class TestDiscordSignature:
    def test_valid_signature_accepted(self):
        private_key, public_hex = _make_ed25519_keypair()
        ts = "1700000000"
        body = '{"type":1}'
        sig = _sign_discord(private_key, ts, body)
        assert verify_discord_signature(public_hex, ts, body, sig) is True

    def test_invalid_signature_rejected(self):
        _, public_hex = _make_ed25519_keypair()
        assert verify_discord_signature(public_hex, "123", "body", "00" * 64) is False

    def test_tampered_body_rejected(self):
        private_key, public_hex = _make_ed25519_keypair()
        ts = "1700000000"
        body = '{"type":1}'
        sig = _sign_discord(private_key, ts, body)
        assert verify_discord_signature(public_hex, ts, body + "x", sig) is False

    def test_invalid_public_key_rejected(self):
        assert verify_discord_signature("not_hex", "123", "body", "00" * 64) is False


# ===========================================================================
# Discord Parser
# ===========================================================================


class TestDiscordParser:
    def test_extracts_channel_id(self):
        payload = {"channel_id": "CH999", "id": "MSG001", "content": "alert", "timestamp": "2025-01-15T14:32:00+00:00"}
        ctx = discord_parse(payload)
        assert ctx.channel_id == "CH999"

    def test_extracts_message_id(self):
        payload = {"channel_id": "CH1", "id": 12345, "content": "alert", "timestamp": "2025-01-15T14:32:00+00:00"}
        ctx = discord_parse(payload)
        assert ctx.message_id == "12345"

    def test_platform_is_discord(self):
        payload = {"channel_id": "CH1", "id": "1", "content": "x", "timestamp": "2025-01-15T14:32:00+00:00"}
        ctx = discord_parse(payload)
        assert ctx.platform == "discord"

    def test_investigation_window_symmetric(self):
        payload = {"channel_id": "CH1", "id": "1", "content": "x", "timestamp": "2025-01-15T14:32:00+00:00"}
        ctx = discord_parse(payload)
        start, end = ctx.investigation_window
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        alert_dt = datetime.fromisoformat(ctx.alert_timestamp)
        assert abs((alert_dt - start_dt).total_seconds() - 300) < 1
        assert abs((end_dt - alert_dt).total_seconds() - 300) < 1

    def test_guild_id_in_metadata(self):
        payload = {"channel_id": "CH1", "id": "1", "content": "x", "timestamp": "2025-01-15T14:32:00+00:00", "guild_id": "G999"}
        ctx = discord_parse(payload)
        assert ctx.platform_metadata["guild_id"] == "G999"

    def test_investigation_id_is_valid_uuid(self):
        payload = {"channel_id": "CH1", "id": "1", "content": "x", "timestamp": "2025-01-15T14:32:00+00:00"}
        ctx = discord_parse(payload)
        uuid.UUID(ctx.investigation_id)

    def test_missing_channel_id_raises(self):
        with pytest.raises(KeyError):
            discord_parse({"id": "1", "content": "x"})


# ===========================================================================
# Discord Handler
# ===========================================================================


DEDUP_TABLE = "test-discord-dedup"


class TestDiscordHandler:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        private_key, public_hex = _make_ed25519_keypair()
        self._private_key = private_key
        self._public_hex = public_hex
        monkeypatch.setenv("DISCORD_PUBLIC_KEY", public_hex)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-bot-token")
        monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
        monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "TESTAGENT")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")

    def _build_event(self, payload: dict) -> dict:
        body = json.dumps(payload)
        ts = "1700000000"
        sig = _sign_discord(self._private_key, ts, body)
        return {
            "headers": {"x-signature-timestamp": ts, "x-signature-ed25519": sig},
            "body": body,
            "isBase64Encoded": False,
        }

    def test_ping_returns_type_1(self):
        event = self._build_event({"type": 1})
        result = discord_handler(event, None)
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["type"] == 1

    def test_invalid_signature_returns_401(self):
        event = self._build_event({"type": 1})
        event["headers"]["x-signature-ed25519"] = "00" * 64
        result = discord_handler(event, None)
        assert result["statusCode"] == 401

    @mock_aws
    def test_valid_event_invokes_agent(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=DEDUP_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        payload = {
            "channel_id": "CH001",
            "id": "MSG001",
            "content": "ALERT: disk full",
            "timestamp": "2025-01-15T14:32:00+00:00",
            "guild_id": "G001",
        }
        event = self._build_event(payload)

        with patch("lambda_adapter.intake.boto3.client") as mock_boto:
            mock_runtime = MagicMock()
            mock_boto.return_value = mock_runtime
            result = discord_handler(event, None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["ok"] is True
        mock_runtime.invoke_agent_runtime.assert_called_once()


# ===========================================================================
# ChatPoster Factory
# ===========================================================================


class TestChatPlatformRegistry:
    """The for_platform() factory replaces the legacy create_chat_poster()."""

    def test_creates_slack_platform(self):
        platform = for_platform("slack")
        assert type(platform).__name__ == "SlackChatPlatform"

    def test_creates_discord_platform(self):
        platform = for_platform("discord")
        assert isinstance(platform, DiscordChatPlatform)

    def test_unknown_platform_raises(self):
        with pytest.raises(ValueError, match="Unsupported platform"):
            for_platform("teams")


# ===========================================================================
# Discord Report Renderer
# ===========================================================================


class TestDiscordReportRenderer:
    def test_uses_double_asterisks_for_bold(self):
        renderer = DiscordReportRenderer()
        sections = ReportSections(
            severity="🔴 Critical",
            affected_services="api-server",
            time_of_detection="2025-01-15T14:32:00Z",
            summary="Test summary",
            root_cause="Unknown",
            evidence_blocks=[EvidenceBlock(emoji="📡", display_name="Test", lines=["finding 1"])],
            impact_assessment="High impact",
            recommended_actions="1. Fix it",
            links=[],
        )
        report = renderer.render_report(sections)
        assert "**Incident Report**" in report
        assert "**Severity:**" in report
        # Verify Discord markdown, not Slack mrkdwn single-asterisk bold
        assert report.startswith("🚨 **Incident Report**")

    def test_enrichment_uses_double_asterisks(self):
        renderer = DiscordReportRenderer()
        sections = EnrichmentSections(
            emoji="📊",
            display_name="Prometheus",
            findings_lines=["CPU spike"],
            updated_assessment="New data available",
        )
        update = renderer.render_enrichment(sections)
        assert "**Enrichment Update" in update
        assert "**New Findings:**" in update


# ---------------------------------------------------------------------------
# capture_snapshot — /status path
# ---------------------------------------------------------------------------

from agents.discord_scanner.tools import (
    DiscordRESTClient,
    _execute_capture_snapshot,
)


REQUESTED_AT = "2026-05-28T19:00:00+00:00"


def _stub_client(
    *,
    self_status: int = 200,
    self_body: dict | None = None,
    self_exception: Exception | None = None,
    guilds_status: int = 200,
    guilds_body: list | None = None,
    guilds_exception: Exception | None = None,
) -> MagicMock:
    """Build a mock DiscordRESTClient with controllable per-method responses."""
    client = MagicMock(spec=DiscordRESTClient)
    if self_exception is not None:
        client.get_user_self.side_effect = self_exception
    else:
        client.get_user_self.return_value = (
            self_status,
            self_body if self_body is not None else {
                "id": "987654321",
                "username": "sre-bot",
                "discriminator": "0001",
            },
        )
    if guilds_exception is not None:
        client.get_user_guilds.side_effect = guilds_exception
    else:
        client.get_user_guilds.return_value = (
            guilds_status,
            guilds_body if guilds_body is not None else [
                {"id": "111"},
                {"id": "222"},
                {"id": "333"},
            ],
        )
    return client


# ---- happy path -----------------------------------------------------------


class TestDiscordCaptureSnapshotHappyPath:
    def test_no_anomaly_when_both_probes_succeed(self):
        report = _execute_capture_snapshot(_stub_client(), requested_at=REQUESTED_AT)
        assert report.anomaly is False
        assert report.anomaly_summary is None

    def test_captured_at_is_requested_at(self):
        report = _execute_capture_snapshot(_stub_client(), requested_at=REQUESTED_AT)
        assert report.captured_at == REQUESTED_AT

    def test_agent_name_set(self):
        report = _execute_capture_snapshot(_stub_client(), requested_at=REQUESTED_AT)
        assert report.agent_name == "discord_scanner"

    def test_authentication_section_lists_bot_identity(self):
        report = _execute_capture_snapshot(_stub_client(), requested_at=REQUESTED_AT)
        auth = next(s for s in report.sections if s.label == "Authentication")
        joined = "\n".join(auth.lines)
        assert "987654321" in joined
        assert "sre-bot" in joined

    def test_guild_access_section_reports_count(self):
        report = _execute_capture_snapshot(_stub_client(), requested_at=REQUESTED_AT)
        guilds = next(s for s in report.sections if s.label == "Guild access")
        assert any("3 guild" in line for line in guilds.lines)


# ---- auth anomaly paths ---------------------------------------------------


class TestDiscordCaptureSnapshotAuthAnomalyPaths:
    def test_4xx_on_users_self_flips_anomaly(self):
        client = _stub_client(self_status=401, self_body={"message": "401: Unauthorized"})
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)
        assert report.anomaly is True
        assert "401" in (report.anomaly_summary or "")

    def test_5xx_on_users_self_flips_anomaly(self):
        client = _stub_client(self_status=503, self_body={"message": "service down"})
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)
        assert report.anomaly is True
        assert "503" in (report.anomaly_summary or "")

    def test_exception_during_users_self_flips_anomaly_no_raise(self):
        client = _stub_client(self_exception=RuntimeError("connection refused"))
        # Tool honours the no-raise contract
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)
        assert report.anomaly is True
        assert "connection refused" in (report.anomaly_summary or "")

    def test_non_object_body_on_users_self_flips_anomaly(self):
        client = _stub_client(self_body=[])  # type: ignore[arg-type]
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)
        assert report.anomaly is True


# ---- guild probe failures (informational, not anomaly) --------------------


class TestDiscordCaptureSnapshotGuildProbeFailures:
    def test_4xx_on_guilds_does_not_flag_anomaly(self):
        client = _stub_client(
            guilds_status=403,
            guilds_body={"message": "missing access"},
        )
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)
        assert report.anomaly is False
        guilds = next(s for s in report.sections if s.label == "Guild access")
        assert any("403" in line for line in guilds.lines)

    def test_exception_on_guilds_does_not_flag_anomaly(self):
        client = _stub_client(guilds_exception=RuntimeError("boom"))
        report = _execute_capture_snapshot(client, requested_at=REQUESTED_AT)
        assert report.anomaly is False
        guilds = next(s for s in report.sections if s.label == "Guild access")
        assert any("boom" in line for line in guilds.lines)


# ---- structural invariants -----------------------------------------------


class TestDiscordCaptureSnapshotShape:
    def test_sections_in_order_authentication_then_guild_access(self):
        report = _execute_capture_snapshot(_stub_client(), requested_at=REQUESTED_AT)
        assert [s.label for s in report.sections] == ["Authentication", "Guild access"]

    def test_metadata_defaults_to_empty(self):
        report = _execute_capture_snapshot(_stub_client(), requested_at=REQUESTED_AT)
        assert report.metadata.model_id is None
        assert report.metadata.input_tokens is None
