"""Smoke tests for ``shared.platforms``.

Verifies that the new :class:`ChatPlatform` skeleton dispatches correctly
to the underlying legacy adapters/posters/renderers. Behavioural depth
(signature edge cases, parser corners, retry semantics) is already
covered by the legacy tests; these tests exist to confirm the wiring.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.models import AlertContext, CommandRequest
from shared.platforms import (
    AlertWebhook,
    ChallengeWebhook,
    ChatPlatform,
    CommandWebhook,
    InvalidWebhook,
    detect_platform,
    for_platform,
)
from shared.platforms.discord import DiscordChatPlatform
from shared.platforms.slack import SlackChatPlatform
from shared.report_renderer import (
    EnrichmentSections,
    InvestigationStartedSections,
    PIRSections,
    ReportSections,
)


SIGNING_SECRET = "test_signing_secret_abc123"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "00" * 32)  # 32-byte hex
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-test")


def _slack_signature(secret: str, timestamp: str, body: str) -> str:
    h = hmac.new(
        secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"v0={h}"


def _slack_alert_payload() -> dict:
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C12345",
            "ts": "1700000000.000100",
            "text": "ALERT",
            "event_ts": "1700000000.000100",
        },
    }


def _slack_event(body: str) -> tuple[dict, str]:
    ts = str(int(time.time()))
    sig = _slack_signature(SIGNING_SECRET, ts, body)
    headers = {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": sig,
        "content-type": "application/json",
    }
    return headers, body


def _slack_command_event(command: str = "/postmortem") -> tuple[dict, str]:
    body = (
        f"command={command}&text=&"
        "channel_id=C1&user_id=U1&thread_ts=1700000000.0&"
        "response_url=https%3A%2F%2Fhooks.slack.com%2Fcb"
    )
    ts = str(int(time.time()))
    sig = _slack_signature(SIGNING_SECRET, ts, body)
    headers = {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": sig,
        "content-type": "application/x-www-form-urlencoded",
    }
    return headers, body


class TestRegistry:
    def test_for_platform_slack(self) -> None:
        platform = for_platform("slack")
        assert isinstance(platform, SlackChatPlatform)
        assert platform.name == "slack"

    def test_for_platform_discord(self) -> None:
        platform = for_platform("discord")
        assert isinstance(platform, DiscordChatPlatform)
        assert platform.name == "discord"

    def test_for_platform_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported platform"):
            for_platform("teams")

    def test_detect_platform_slack_headers(self) -> None:
        platform = detect_platform({"x-slack-signature": "v0=abc"})
        assert platform.name == "slack"

    def test_detect_platform_discord_headers(self) -> None:
        platform = detect_platform({"x-signature-ed25519": "abc"})
        assert platform.name == "discord"

    def test_detect_platform_no_headers_raises(self) -> None:
        with pytest.raises(ValueError, match="Unable to detect platform"):
            detect_platform({})


class TestSlackIngest:
    """Ingest dispatch — the load-bearing surface of step 1."""

    def test_invalid_signature_returns_invalid_401(self) -> None:
        platform = for_platform("slack")
        body = json.dumps(_slack_alert_payload())
        headers = {
            "x-slack-request-timestamp": str(int(time.time())),
            "x-slack-signature": "v0=0000",
            "content-type": "application/json",
        }
        event = platform.ingest(headers, body)
        assert isinstance(event, InvalidWebhook)
        assert event.status_code == 401

    def test_challenge_returns_challenge_event(self) -> None:
        platform = for_platform("slack")
        headers, body = _slack_event(
            json.dumps({"type": "url_verification", "challenge": "abc"})
        )
        event = platform.ingest(headers, body)
        assert isinstance(event, ChallengeWebhook)
        assert event.response == {"challenge": "abc"}

    def test_alert_returns_alert_event_with_context(self) -> None:
        platform = for_platform("slack")
        headers, body = _slack_event(json.dumps(_slack_alert_payload()))
        event = platform.ingest(headers, body)
        assert isinstance(event, AlertWebhook)
        assert event.context.platform == "slack"
        assert event.context.channel_id == "C12345"

    def test_command_returns_command_event(self) -> None:
        platform = for_platform("slack")
        headers, body = _slack_command_event()
        event = platform.ingest(headers, body)
        assert isinstance(event, CommandWebhook)
        assert event.command.command == "/postmortem"
        assert event.command.platform == "slack"

    def test_malformed_json_returns_invalid_400(self) -> None:
        platform = for_platform("slack")
        headers, body = _slack_event("{not valid json")
        event = platform.ingest(headers, body)
        assert isinstance(event, InvalidWebhook)
        assert event.status_code == 400


class TestDeliverDispatch:
    """Confirm deliver routes each section type to the right renderer call."""

    def _ctx(self) -> AlertContext:
        return AlertContext(
            investigation_id="inv-1",
            platform="slack",
            channel_id="C1",
            message_id="ts-1",
            alert_text="alert",
            alert_timestamp="2025-01-01T00:00:00+00:00",
            investigation_window=("2025-01-01T00:00:00+00:00", "2025-01-01T00:10:00+00:00"),
            platform_metadata={"thread_ts": "ts-1"},
        )

    def _platform_with_mocks(self) -> tuple[SlackChatPlatform, MagicMock, AsyncMock]:
        platform = SlackChatPlatform(signing_secret="x", bot_token="y")
        platform._renderer = MagicMock()
        platform._renderer.render_report.return_value = "RENDERED_REPORT"
        platform._renderer.render_enrichment.return_value = "RENDERED_ENRICHMENT"
        platform._renderer.render_investigation_started.return_value = "RENDERED_STARTED"
        platform._renderer.render_pir.return_value = "RENDERED_PIR"
        post_mock = AsyncMock()
        platform._post_reply = post_mock  # type: ignore[method-assign]
        return platform, platform._renderer, post_mock

    def test_report_sections_dispatches_to_render_report(self) -> None:
        platform, renderer, poster = self._platform_with_mocks()
        sections = ReportSections(
            severity="🔴", affected_services="svc", time_of_detection="t",
            summary="s", root_cause="r", evidence_blocks=[],
            impact_assessment="i", recommended_actions="a", links=[],
        )
        rendered = asyncio.run(platform.deliver(self._ctx(), sections))
        renderer.render_report.assert_called_once_with(sections)
        poster.assert_awaited_once_with(self._ctx(), "RENDERED_REPORT")
        assert rendered == "RENDERED_REPORT"

    def test_enrichment_sections_dispatches_to_render_enrichment(self) -> None:
        platform, renderer, poster = self._platform_with_mocks()
        sections = EnrichmentSections(
            emoji="📡", display_name="Slack Scanner",
            findings_lines=["found"], updated_assessment="a",
        )
        rendered = asyncio.run(platform.deliver(self._ctx(), sections))
        renderer.render_enrichment.assert_called_once_with(sections)
        poster.assert_awaited_once_with(self._ctx(), "RENDERED_ENRICHMENT")
        assert rendered == "RENDERED_ENRICHMENT"

    def test_started_sections_dispatches_to_render_started(self) -> None:
        platform, renderer, poster = self._platform_with_mocks()
        sections = InvestigationStartedSections(
            alert_text="a", investigation_id="i", dispatched=[("📡", "Slack Scanner")],
        )
        rendered = asyncio.run(platform.deliver(self._ctx(), sections))
        renderer.render_investigation_started.assert_called_once_with(sections)
        poster.assert_awaited_once_with(self._ctx(), "RENDERED_STARTED")
        assert rendered == "RENDERED_STARTED"

    def test_pir_sections_dispatches_to_render_pir(self) -> None:
        platform, renderer, poster = self._platform_with_mocks()
        sections = PIRSections(
            incident_summary="s", timeline="t", root_cause="r",
            impact="i", action_items="a", lessons_learned="l",
        )
        rendered = asyncio.run(platform.deliver(self._ctx(), sections))
        renderer.render_pir.assert_called_once_with(sections)
        poster.assert_awaited_once_with(self._ctx(), "RENDERED_PIR")
        assert rendered == "RENDERED_PIR"

    def test_unknown_payload_raises_typeerror(self) -> None:
        platform, _, _ = self._platform_with_mocks()
        with pytest.raises(TypeError, match="Unsupported deliver payload"):
            asyncio.run(platform.deliver(self._ctx(), object()))  # type: ignore[arg-type]


class TestAck:
    def test_slack_ack_posts_to_response_url(self) -> None:
        from unittest.mock import patch as _patch
        platform = SlackChatPlatform(signing_secret="x", bot_token="y")
        cmd = CommandRequest(
            platform="slack", command="/postmortem", text="",
            channel_id="C1", user_id="U1", thread_ts="ts-1",
            response_url="https://hooks/cb",
        )
        with _patch("urllib.request.urlopen") as mock_open:
            platform.ack(cmd, "hello")
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://hooks/cb"
        body = json.loads(req.data.decode())
        assert body == {"response_type": "ephemeral", "text": "hello"}

    def test_slack_ack_skips_when_no_response_url(self) -> None:
        from unittest.mock import patch as _patch
        platform = SlackChatPlatform(signing_secret="x", bot_token="y")
        cmd = CommandRequest(
            platform="slack", command="/postmortem", text="",
            channel_id="C1", user_id="U1", thread_ts=None,
            response_url="",
        )
        with _patch("urllib.request.urlopen") as mock_open:
            platform.ack(cmd, "hello")
        mock_open.assert_not_called()


class TestDiscordSmoke:
    """Light smoke checks for Discord ChatPlatform — full parser/signature
    behaviour is covered by the existing legacy tests."""

    def test_constructor_succeeds(self) -> None:
        platform = for_platform("discord")
        assert platform.name == "discord"

    def test_invalid_signature_returns_401(self) -> None:
        platform = for_platform("discord")
        # No signature headers at all → Ed25519 verifier fails
        event = platform.ingest({}, json.dumps({"type": 1}))
        assert isinstance(event, InvalidWebhook)
        assert event.status_code == 401

    def test_unknown_payload_raises(self) -> None:
        platform = DiscordChatPlatform(public_key="00" * 32, bot_token="x")
        platform._renderer = MagicMock()
        platform._post_reply = AsyncMock()  # type: ignore[method-assign]
        ctx = AlertContext(
            investigation_id="inv-1", platform="discord", channel_id="C1",
            message_id="m1", alert_text="a",
            alert_timestamp="2025-01-01T00:00:00+00:00",
            investigation_window=("2025-01-01T00:00:00+00:00", "2025-01-01T00:10:00+00:00"),
        )
        with pytest.raises(TypeError):
            asyncio.run(platform.deliver(ctx, object()))  # type: ignore[arg-type]


def test_chat_platform_protocol_runtime_check() -> None:
    """Confirm SlackChatPlatform and DiscordChatPlatform satisfy the Protocol."""
    slack: ChatPlatform = SlackChatPlatform()
    discord: ChatPlatform = DiscordChatPlatform()
    assert slack.name == "slack"
    assert discord.name == "discord"


class TestDiscordRenderingRegression:
    """Regression for the latent bug fixed in step 3 of the deepening migration.

    Before: ``ReportFormatter`` defaulted to ``SlackReportRenderer()``; the
    orchestrator never consulted ``alert_context.platform`` when picking a
    renderer, so Discord investigations rendered with Slack mrkdwn (single
    ``*bold*``, ``<url|label>`` links). Discord then displayed the raw
    markup verbatim.

    After: the orchestrator's ``_get_platform(ctx)`` calls
    ``for_platform(ctx.platform)``; Discord deliveries go through
    ``DiscordChatPlatform`` which renders with ``DiscordReportRenderer``
    (double ``**bold**``, ``[label](url)`` links).
    """

    def test_discord_uses_double_asterisk_bold(self) -> None:
        from shared.report_renderer import ReportSections
        platform = DiscordChatPlatform(public_key="00" * 32, bot_token="x")
        platform._post_reply = AsyncMock()  # type: ignore[method-assign]
        sections = ReportSections(
            severity="🔴", affected_services="api", time_of_detection="t",
            summary="s", root_cause="r", evidence_blocks=[],
            impact_assessment="i", recommended_actions="a", links=[],
        )
        ctx = AlertContext(
            investigation_id="inv-1", platform="discord", channel_id="C1",
            message_id="m1", alert_text="a",
            alert_timestamp="2025-01-01T00:00:00+00:00",
            investigation_window=("2025-01-01T00:00:00+00:00", "2025-01-01T00:10:00+00:00"),
        )
        rendered = asyncio.run(platform.deliver(ctx, sections))
        # Discord markdown uses **bold**; Slack mrkdwn uses *bold*.
        assert "**Severity:**" in rendered
        assert "*Severity:*" not in rendered.replace("**Severity:**", "")

    def test_slack_uses_single_asterisk_bold(self) -> None:
        from shared.report_renderer import ReportSections
        platform = SlackChatPlatform(signing_secret="x", bot_token="y")
        platform._post_reply = AsyncMock()  # type: ignore[method-assign]
        sections = ReportSections(
            severity="🔴", affected_services="api", time_of_detection="t",
            summary="s", root_cause="r", evidence_blocks=[],
            impact_assessment="i", recommended_actions="a", links=[],
        )
        ctx = AlertContext(
            investigation_id="inv-1", platform="slack", channel_id="C1",
            message_id="m1", alert_text="a",
            alert_timestamp="2025-01-01T00:00:00+00:00",
            investigation_window=("2025-01-01T00:00:00+00:00", "2025-01-01T00:10:00+00:00"),
        )
        rendered = asyncio.run(platform.deliver(ctx, sections))
        assert "*Severity:*" in rendered
        assert "**Severity:**" not in rendered

    def test_orchestrator_default_platform_selection_uses_alert_context(self) -> None:
        """When no chat_platform is injected, the orchestrator picks one
        from ``alert_context.platform`` — fixing the bug where Discord
        alerts rendered with Slack mrkdwn."""
        from agents.master.orchestrator import InvestigationOrchestrator
        from shared.agents import AgentRegistry
        from shared.config import AgentConfig, Defaults, ProjectConfig

        registry = AgentRegistry(
            ProjectConfig(
                project="test",
                environment="dev",
                defaults=Defaults(model_id="anthropic.claude-test"),
                agents={
                    "master": AgentConfig(skills=["investigate_alert"]),
                    "eks": AgentConfig(enabled=True, network_mode="VPC"),
                },
            )
        )
        orch = InvestigationOrchestrator(
            http_client=MagicMock(),
            registry=registry,
        )

        slack_ctx = AlertContext(
            investigation_id="i", platform="slack", channel_id="C1",
            message_id="m1", alert_text="a",
            alert_timestamp="2025-01-01T00:00:00+00:00",
            investigation_window=("2025-01-01T00:00:00+00:00", "2025-01-01T00:10:00+00:00"),
        )
        discord_ctx = AlertContext(
            investigation_id="i", platform="discord", channel_id="C2",
            message_id="m2", alert_text="a",
            alert_timestamp="2025-01-01T00:00:00+00:00",
            investigation_window=("2025-01-01T00:00:00+00:00", "2025-01-01T00:10:00+00:00"),
        )

        assert orch._get_platform(slack_ctx).name == "slack"
        assert orch._get_platform(discord_ctx).name == "discord"


class TestDeliverWithRetry:
    """deliver_with_retry: exponential backoff + return-value passthrough."""

    def _ctx(self) -> AlertContext:
        return AlertContext(
            investigation_id="inv-1", platform="slack", channel_id="C1",
            message_id="ts-1", alert_text="a",
            alert_timestamp="2025-01-01T00:00:00+00:00",
            investigation_window=("2025-01-01T00:00:00+00:00", "2025-01-01T00:10:00+00:00"),
        )

    def _sections(self) -> ReportSections:
        return ReportSections(
            severity="🔴", affected_services="svc", time_of_detection="t",
            summary="s", root_cause="r", evidence_blocks=[],
            impact_assessment="i", recommended_actions="a", links=[],
        )

    def test_returns_text_on_first_success(self) -> None:
        from shared.platforms import deliver_with_retry

        platform = MagicMock()
        platform.deliver = AsyncMock(return_value="OK")
        result = asyncio.run(
            deliver_with_retry(platform, self._ctx(), self._sections(), base_delay=0.0)
        )
        assert result == "OK"
        assert platform.deliver.await_count == 1

    def test_retries_then_returns_text(self) -> None:
        from shared.platforms import deliver_with_retry

        platform = MagicMock()
        # First two calls raise, third returns "DELIVERED".
        platform.deliver = AsyncMock(
            side_effect=[RuntimeError("fail1"), RuntimeError("fail2"), "DELIVERED"]
        )
        result = asyncio.run(
            deliver_with_retry(
                platform, self._ctx(), self._sections(),
                max_retries=3, base_delay=0.0,
            )
        )
        assert result == "DELIVERED"
        assert platform.deliver.await_count == 3

    def test_raises_after_exhausting_retries(self) -> None:
        from shared.platforms import deliver_with_retry

        platform = MagicMock()
        platform.deliver = AsyncMock(side_effect=RuntimeError("permanent"))
        with pytest.raises(RuntimeError, match="permanent"):
            asyncio.run(
                deliver_with_retry(
                    platform, self._ctx(), self._sections(),
                    max_retries=2, base_delay=0.0,
                )
            )
        assert platform.deliver.await_count == 3  # 1 initial + 2 retries
