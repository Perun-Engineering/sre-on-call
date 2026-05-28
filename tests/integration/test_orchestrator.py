"""Unit tests for the Master Agent InvestigationOrchestrator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.master.orchestrator import (
    AgentCoreClient,
    AsyncHTTPClient,
    InvestigationOrchestrator,
    _serialize_alert_context,
    _parse_agent_result,
)
from shared.a2a_protocol import build_a2a_request
from shared.agents import AgentRegistry
from shared.config import AgentConfig, Defaults, ProjectConfig
from shared.platforms import ChatPlatform
from shared.report_renderer import (
    EnrichmentSections,
    InvestigationStartedSections,
    PIRSections,
    ReportSections,
    SlackReportRenderer,
)
from agents.master.report_formatter import ReportFormatter
from shared.models import AgentFailure, AgentMetadata, AgentResult, AlertContext, Finding


# ---------------------------------------------------------------------------
# Endpoint constants — match the catalogue defaults so resolve_endpoint()
# returns these when the corresponding env vars are unset (which is what
# `_clean_env` ensures via the autouse fixture below).
# ---------------------------------------------------------------------------

SLACK_URL = "http://localhost:9001"
DISCORD_URL = "http://localhost:9002"
CLOUDWATCH_URL = "http://localhost:9004"
EKS_URL = "http://localhost:9005"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear runtime/URL env vars before each test so :meth:`Agent.resolve_endpoint`
    falls back to catalogue defaults. Tests that exercise specific overrides
    set them explicitly via monkeypatch."""
    for var in (
        "SLACK_SCANNER_AGENT_RUNTIME_ARN",
        "SLACK_SCANNER_AGENT_URL",
        "DISCORD_SCANNER_AGENT_RUNTIME_ARN",
        "DISCORD_SCANNER_AGENT_URL",
        "CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN",
        "CLOUDWATCH_LOGS_AGENT_URL",
        "EKS_AGENT_RUNTIME_ARN",
        "EKS_AGENT_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _build_registry(
    *,
    active_specialized: list[str] | None = None,
    disabled_specialized: list[str] | None = None,
) -> AgentRegistry:
    """Build an :class:`AgentRegistry` for tests with a custom deployment manifest.

    Default is the three specialized agents the legacy fixture used:
    slack_scanner, cloudwatch_logs, eks — all enabled.
    """
    if active_specialized is None:
        active_specialized = ["slack_scanner", "cloudwatch_logs", "eks"]
    if disabled_specialized is None:
        disabled_specialized = []

    agents: dict[str, AgentConfig] = {
        "master": AgentConfig(skills=["investigate_alert"]),
    }
    for aid in active_specialized:
        kwargs: dict = {"enabled": True}
        if aid == "eks":
            kwargs["network_mode"] = "VPC"
        agents[aid] = AgentConfig(**kwargs)
    for aid in disabled_specialized:
        kwargs = {"enabled": False}
        if aid == "eks":
            kwargs["network_mode"] = "VPC"
        agents[aid] = AgentConfig(**kwargs)

    return AgentRegistry(
        ProjectConfig(
            project="test",
            environment="dev",
            defaults=Defaults(model_id="anthropic.claude-test"),
            agents=agents,
        )
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def alert_context():
    return AlertContext(
        investigation_id="inv-test-001",
        platform="slack",
        channel_id="C12345",
        message_id="1705312320.000100",
        alert_text="High CPU usage on service-api",
        alert_timestamp="2025-01-15T14:32:00Z",
        investigation_window=("2025-01-15T14:27:00Z", "2025-01-15T14:37:00Z"),
        platform_metadata={"thread_ts": "1705312320.000100"},
    )


class FakeHTTPClient:
    """Controllable fake HTTP client for testing A2A calls."""

    def __init__(self, responses: dict[str, dict] | None = None, delay: float = 0.0):
        self.responses = responses or {}
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def post_json(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        # Return a matching response or a default success
        return self.responses.get(url, _default_a2a_response(payload))


class FakeChatPlatform:
    """Fake :class:`ChatPlatform` that renders payloads via Slack mrkdwn and
    records the rendered text alongside the original (ctx, payload) tuples.

    Exposes a ``messages`` property in the legacy ``(channel, thread, text)``
    shape so existing assertions about rendered content keep working.
    """

    name = "slack"

    def __init__(self) -> None:
        self._renderer = SlackReportRenderer()
        self.deliveries: list[tuple] = []  # (ctx, payload, rendered_text)

    def ingest(self, headers, raw_body):  # not exercised in orchestrator tests
        raise NotImplementedError

    def ack(self, command, text):  # not exercised in orchestrator tests
        raise NotImplementedError

    async def deliver(self, alert_context, payload) -> str:
        text = self._render(payload)
        self.deliveries.append((alert_context, payload, text))
        return text

    def _render(self, payload) -> str:
        if isinstance(payload, ReportSections):
            return self._renderer.render_report(payload)
        if isinstance(payload, EnrichmentSections):
            return self._renderer.render_enrichment(payload)
        if isinstance(payload, InvestigationStartedSections):
            return self._renderer.render_investigation_started(payload)
        if isinstance(payload, PIRSections):
            return self._renderer.render_pir(payload)
        raise TypeError(f"Unsupported deliver payload: {type(payload).__name__}")

    @property
    def messages(self) -> list[tuple[str, str, str]]:
        """Legacy compat: list of (channel_id, thread_ts, rendered_text) tuples."""
        return [
            (
                ctx.channel_id,
                ctx.platform_metadata.get("thread_ts", ctx.message_id),
                text,
            )
            for ctx, _, text in self.deliveries
        ]


def _find_report_msg(messages: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    """Pick the Incident Report out of a fake poster's recorded messages.

    The orchestrator now posts an "Investigation Started" notice before
    fan-out, so the report is no longer guaranteed to be at index 0.
    """
    for msg in messages:
        if "Incident Report" in msg[2]:
            return msg
    raise AssertionError(f"No Incident Report found in {len(messages)} messages")


def _default_a2a_response(request: dict) -> dict:
    """Build a default successful A2A response."""
    return {
        "jsonrpc": "2.0",
        "id": request.get("id", "unknown"),
        "result": {
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": "Agent analysis complete."}],
                "messageId": "resp-001",
            }
        },
    }


def _make_orchestrator(
    http_client: AsyncHTTPClient | None = None,
    chat_platform: ChatPlatform | None = None,
    initial_deadline: float = 0.1,
    hard_cutoff: float = 0.5,
    registry: AgentRegistry | None = None,
) -> InvestigationOrchestrator:
    """Create an orchestrator with short timeouts for testing.

    Endpoints come from the registry's :meth:`Agent.resolve_endpoint` —
    with env vars cleared by the autouse fixture, that resolves to the
    catalogue defaults (slack=9001, cloudwatch=9004, eks=9005).
    """
    registry = registry or _build_registry()
    orch = InvestigationOrchestrator(
        http_client=http_client or FakeHTTPClient(),
        chat_platform=chat_platform or FakeChatPlatform(),
        report_formatter=ReportFormatter(registry),
        registry=registry,
    )
    # Override deadlines for fast tests
    orch.INITIAL_DEADLINE_SECONDS = initial_deadline
    orch.HARD_CUTOFF_SECONDS = hard_cutoff
    return orch


# ---------------------------------------------------------------------------
# Tests: A2A request building
# ---------------------------------------------------------------------------


class TestBuildA2ARequest:
    def test_request_structure(self):
        req = build_a2a_request('{"alert": "test"}', "req-cloudwatch_logs-inv-001")

        assert req["jsonrpc"] == "2.0"
        assert req["method"] == "message/send"
        assert req["id"] == "req-cloudwatch_logs-inv-001"
        assert req["params"]["message"]["role"] == "user"
        assert len(req["params"]["message"]["parts"]) == 1
        assert req["params"]["message"]["parts"][0]["kind"] == "text"
        assert req["params"]["message"]["parts"][0]["text"] == '{"alert": "test"}'
        assert "messageId" in req["params"]["message"]

    def test_unique_message_ids(self):
        req1 = build_a2a_request("ctx1", "req-eks-inv-001")
        req2 = build_a2a_request("ctx2", "req-eks-inv-001")
        assert req1["params"]["message"]["messageId"] != req2["params"]["message"]["messageId"]


class TestSerializeAlertContext:
    def test_round_trip(self, alert_context):
        import json

        serialized = _serialize_alert_context(alert_context)
        data = json.loads(serialized)
        assert data["investigation_id"] == alert_context.investigation_id
        assert data["channel_id"] == alert_context.channel_id
        assert data["alert_text"] == alert_context.alert_text


class TestParseAgentResult:
    def test_success_response(self):
        response = {
            "jsonrpc": "2.0",
            "id": "req-cloudwatch_logs-inv-001",
            "result": {
                "message": {
                    "role": "agent",
                    "parts": [{"kind": "text", "text": "CPU spike detected."}],
                    "messageId": "resp-001",
                }
            },
        }
        result = _parse_agent_result("cloudwatch_logs", response)
        assert result.agent_name == "cloudwatch_logs"
        assert result.status == "success"
        assert "CPU spike detected" in result.summary

    def test_error_response(self):
        response = {
            "jsonrpc": "2.0",
            "id": "req-eks-inv-001",
            "error": {"code": -32000, "message": "Agent unavailable"},
        }
        result = _parse_agent_result("eks", response)
        assert result.agent_name == "eks"
        assert result.status == "error"
        assert result.error_message is not None
        assert "Agent unavailable" in result.error_message

    def test_malformed_response(self):
        result = _parse_agent_result("cloudwatch_logs", {"unexpected": True})
        assert result.agent_name == "cloudwatch_logs"
        # Should not crash — returns a result (possibly with error or empty summary)
        assert result.status in ("success", "error")

    def test_task_envelope_response(self):
        """Strands A2AServer wraps tool-driven agents as a completed Task.

        The canonical reply lives in artifacts[*].parts[*].text under an
        artifact named ``agent_response`` — not in ``result.message.parts``.
        Regression for the dict-repr leak where the parser fell through to
        ``str(result_data)`` and dumped the whole Task envelope into the
        Incident Report's Summary section.
        """
        canonical_text = "**Investigation Summary** — CPU spike on service-api at 14:32 UTC."
        response = {
            "jsonrpc": "2.0",
            "id": "req-slack_scanner-inv-001",
            "result": {
                "kind": "task",
                "id": "df20b7af-93d0-45a7-99ac-3c9c0a3baa87",
                "contextId": "8afcdb4e-e48e-4bce-8631-6041a60542a9",
                "status": {"state": "completed", "timestamp": "2026-05-09T03:05:28Z"},
                "artifacts": [
                    {
                        "artifactId": "b03d53ae-7f04-4f3c-b68b-5bb44e774ace",
                        "name": "agent_response",
                        "parts": [{"kind": "text", "text": canonical_text}],
                    }
                ],
                # Streaming chunks accumulate here; must NOT leak into summary.
                "history": [
                    {"role": "agent", "parts": [{"kind": "text", "text": "I'll scan"}]},
                    {"role": "agent", "parts": [{"kind": "text", "text": " the channels"}]},
                ],
            },
        }
        result = _parse_agent_result("slack_scanner", response)
        assert result.status == "success"
        assert result.summary == canonical_text
        # Hard guards against the regression: no dict-repr leakage.
        for marker in ("artifacts", "history", "taskId", "contextId", "kind"):
            assert marker not in result.summary, (
                f"summary leaked Task envelope key '{marker}': {result.summary!r}"
            )

    def test_task_envelope_picks_named_artifact(self):
        """When multiple artifacts exist, prefer the one named ``agent_response``."""
        response = {
            "jsonrpc": "2.0",
            "result": {
                "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [
                    {"name": "trace", "parts": [{"kind": "text", "text": "trace data"}]},
                    {"name": "agent_response", "parts": [{"kind": "text", "text": "the answer"}]},
                ],
            },
        }
        result = _parse_agent_result("eks", response)
        assert result.summary == "the answer"

    def test_task_envelope_with_metadata_footer(self):
        """Telemetry footer must still be stripped on the Task path."""
        from shared.agent_telemetry import METADATA_PREFIX, METADATA_SUFFIX

        footer = (
            f'{METADATA_PREFIX}'
            '{"model_id":"us.anthropic.claude-haiku-4-5-20251001-v1:0",'
            '"input_tokens":100,"output_tokens":50}'
            f'{METADATA_SUFFIX}'
        )
        response = {
            "jsonrpc": "2.0",
            "result": {
                "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "name": "agent_response",
                        "parts": [{"kind": "text", "text": f"clean text {footer}"}],
                    }
                ],
            },
        }
        result = _parse_agent_result("eks", response)
        assert result.summary == "clean text"
        assert result.metadata.input_tokens == 100
        assert result.metadata.output_tokens == 50

    def test_task_envelope_with_structured_agent_result(self):
        """Structured findings survive the A2A text seam."""
        structured_footer = (
            '<<<AGENT_RESULT '
            '{"agent_name":"eks","status":"success",'
            '"findings":[{"source":"pod/api-123",'
            '"timestamp":"2025-01-15T14:32:00Z",'
            '"content":"Pod api-123: phase=Failed",'
            '"severity":"critical",'
            '"metadata":{"kind":"pod_status","pod":"api-123"}}],'
            '"summary":"Inspected 1 item(s). Found 1 finding(s).",'
            '"error_message":null,"duration_seconds":0.0,'
            '"metadata":{"model_id":"model-from-agent"}}'
            ' AGENT_RESULT>>>'
        )
        response = {
            "jsonrpc": "2.0",
            "result": {
                "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "name": "agent_response",
                        "parts": [
                            {
                                "kind": "text",
                                "text": f"Inspected 1 item(s). Found 1 finding(s). {structured_footer}",
                            }
                        ],
                    }
                ],
            },
        }

        result = _parse_agent_result(
            "eks",
            response,
            AgentMetadata(started_at="2025-01-15T14:32:00+00:00"),
        )

        assert result.agent_name == "eks"
        assert result.status == "success"
        assert result.summary == "Inspected 1 item(s). Found 1 finding(s)."
        assert len(result.findings) == 1
        assert result.findings[0] == Finding(
            source="pod/api-123",
            timestamp="2025-01-15T14:32:00Z",
            content="Pod api-123: phase=Failed",
            severity="critical",
            metadata={"kind": "pod_status", "pod": "api-123"},
        )
        assert result.metadata.started_at == "2025-01-15T14:32:00+00:00"
        assert result.metadata.model_id == "model-from-agent"


# ---------------------------------------------------------------------------
# Tests: InvestigationOrchestrator
# ---------------------------------------------------------------------------


class TestAgentCoreClient:
    """AgentCoreClient invokes Bedrock AgentCore runtime ARNs via boto3."""

    @pytest.mark.asyncio
    async def test_invokes_runtime_with_arn_and_encoded_payload(self):
        fake_boto = MagicMock()
        fake_boto.invoke_agent_runtime.return_value = {
            "response": b'{"jsonrpc": "2.0", "id": "1", "result": {"ok": true}}',
            "contentType": "application/json",
            "statusCode": 200,
        }
        client = AgentCoreClient(client=fake_boto)
        arn = "arn:aws:bedrock-agentcore:us-east-1:111111111111:agent-runtime/abc"

        result = await client.post_json(arn, {"jsonrpc": "2.0", "method": "ping"})

        fake_boto.invoke_agent_runtime.assert_called_once()
        kwargs = fake_boto.invoke_agent_runtime.call_args.kwargs
        assert kwargs["agentRuntimeArn"] == arn
        assert kwargs["contentType"] == "application/json"
        assert kwargs["accept"] == "application/json"
        # payload is JSON-encoded bytes
        import json as _json

        assert _json.loads(kwargs["payload"].decode("utf-8")) == {
            "jsonrpc": "2.0",
            "method": "ping",
        }
        assert result == {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}

    @pytest.mark.asyncio
    async def test_decodes_streaming_body_response(self):
        class FakeStreamingBody:
            def __init__(self, data: bytes):
                self._data = data

            def read(self) -> bytes:
                return self._data

        fake_boto = MagicMock()
        fake_boto.invoke_agent_runtime.return_value = {
            "response": FakeStreamingBody(b'{"result": "streamed"}'),
        }
        client = AgentCoreClient(client=fake_boto)

        result = await client.post_json("arn:aws:bedrock-agentcore:us-east-1:0:r/x", {})

        assert result == {"result": "streamed"}

    @pytest.mark.asyncio
    async def test_decodes_str_response(self):
        fake_boto = MagicMock()
        fake_boto.invoke_agent_runtime.return_value = {
            "response": '{"result": "as-string"}',
        }
        client = AgentCoreClient(client=fake_boto)

        result = await client.post_json("arn:aws:bedrock-agentcore:us-east-1:0:r/x", {})

        assert result == {"result": "as-string"}


class TestOrchestratorFanOut:
    """Test that all configured agents are invoked in parallel."""

    @pytest.mark.asyncio
    async def test_all_agents_invoked(self, alert_context):
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client)

        await orch.investigate(alert_context)

        called_urls = {url for url, _ in http_client.calls}
        assert called_urls == {SLACK_URL, CLOUDWATCH_URL, EKS_URL}

    @pytest.mark.asyncio
    async def test_a2a_request_format(self, alert_context):
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client)

        await orch.investigate(alert_context)

        for _, payload in http_client.calls:
            assert payload["jsonrpc"] == "2.0"
            assert payload["method"] == "message/send"
            assert "params" in payload
            assert payload["params"]["message"]["role"] == "user"


class TestOrchestratorDeadlines:
    """Test deadline management."""

    @pytest.mark.asyncio
    async def test_initial_report_posted_at_deadline(self, alert_context):
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(
            chat_platform=chat_platform,
            initial_deadline=0.1,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        # The Incident Report follows the "Investigation Started" notice.
        channel, thread_ts, text = _find_report_msg(chat_platform.messages)
        assert channel == alert_context.channel_id
        assert thread_ts == alert_context.message_id
        assert "Incident Report" in text

    @pytest.mark.asyncio
    async def test_report_marks_slow_agents_pending(self, alert_context):
        """Agents that don't respond within the initial deadline render as ⏳ pending."""
        # Make all agents slow (longer than initial deadline)
        http_client = FakeHTTPClient(delay=0.3)
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat_platform,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        _, _, report_text = _find_report_msg(chat_platform.messages)
        assert "⏳" in report_text or "still investigating" in report_text.lower()

    @pytest.mark.asyncio
    async def test_hard_cutoff_terminates_investigation(self, alert_context):
        """Investigation terminates at the hard cutoff."""
        # Agents take longer than the hard cutoff
        http_client = FakeHTTPClient(delay=5.0)
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat_platform,
            initial_deadline=0.05,
            hard_cutoff=0.2,
        )

        import time
        start = time.monotonic()
        await orch.investigate(alert_context)
        elapsed = time.monotonic() - start

        # Should complete near the hard cutoff, not wait for slow agents
        assert elapsed < 1.0


class TestOrchestratorLateResults:
    """Test enrichment updates for late-arriving results."""

    @pytest.mark.asyncio
    async def test_enrichment_update_for_late_result(self, alert_context):
        """Late-arriving results trigger enrichment updates."""
        # Make some agents fast and some slow (but within hard cutoff)
        fast_response = _default_a2a_response({"id": "fast"})
        slow_delay = 0.15  # After initial deadline but before hard cutoff

        class MixedHTTPClient:
            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                if url in (EKS_URL, CLOUDWATCH_URL):
                    await asyncio.sleep(slow_delay)
                return _default_a2a_response(payload)

        http_client = MixedHTTPClient()
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat_platform,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        # Should have initial report + enrichment updates
        assert len(chat_platform.messages) >= 2  # At least report + 1 enrichment

    @pytest.mark.asyncio
    async def test_enrichment_update_format(self, alert_context):
        """Enrichment updates contain the expected format."""

        class SlowOneAgent:
            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                if url == EKS_URL:  # EKS is slow
                    await asyncio.sleep(0.15)
                return _default_a2a_response(payload)

        http_client = SlowOneAgent()
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat_platform,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        # Find enrichment update messages (not the initial report)
        enrichment_msgs = [
            text for _, _, text in chat_platform.messages
            if "Enrichment Update" in text
        ]
        # The slow agent should trigger an enrichment update
        assert len(enrichment_msgs) >= 1


class TestOrchestratorErrorHandling:
    """Test error handling in the orchestrator."""

    @pytest.mark.asyncio
    async def test_agent_http_error_handled_gracefully(self, alert_context):
        """HTTP errors from agents don't crash the orchestrator."""

        class FailingHTTPClient:
            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                if url == CLOUDWATCH_URL:  # cloudwatch_logs fails
                    raise ConnectionError("Connection refused")
                return _default_a2a_response(payload)

        http_client = FailingHTTPClient()
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat_platform,
        )

        # Should not raise
        await orch.investigate(alert_context)

        # Report should still be posted
        assert len(chat_platform.messages) >= 1

    @pytest.mark.asyncio
    async def test_slack_post_failure_does_not_crash(self, alert_context):
        """Chat post failures are logged but don't crash the orchestrator."""

        class FailingChatPlatform:
            name = "slack"

            def ingest(self, headers, raw_body):
                raise NotImplementedError

            def ack(self, command, text):
                raise NotImplementedError

            async def deliver(self, alert_context, payload):
                raise RuntimeError("Slack API error")

        orch = _make_orchestrator(
            chat_platform=FailingChatPlatform(),
        )

        # Should not raise
        await orch.investigate(alert_context)


class TestOrchestratorInvokeAgent:
    """Test the invoke_agent method directly."""

    @pytest.mark.asyncio
    async def test_invoke_agent_returns_result(self, alert_context):
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client)

        result = await orch.invoke_agent("cloudwatch_logs", alert_context)

        assert isinstance(result, AgentResult)
        assert result.agent_name == "cloudwatch_logs"
        assert result.status == "success"
        assert result.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_invoke_agent_sends_correct_payload(self, alert_context):
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client)

        await orch.invoke_agent("slack_scanner", alert_context)

        assert len(http_client.calls) == 1
        url, payload = http_client.calls[0]
        assert url == SLACK_URL
        assert payload["method"] == "message/send"
        assert "req-slack_scanner-inv-test-001" == payload["id"]


class TestOrchestratorEndpointConfig:
    """Test agent endpoint configuration via the registry.

    Most endpoint-resolution mechanics are tested directly in
    :mod:`tests.test_agent_registry` (see ``TestResolveEndpoint``); this
    class only covers the orchestrator's interaction with the registry.
    """

    def test_default_registry_includes_all_active_specialized(self):
        # The repo's config.yaml has slack_scanner, cloudwatch_logs, eks active
        # and discord_scanner disabled. The orchestrator's fan-out endpoints
        # should match exactly the active specialized agents.
        from shared.agents import reset_cache as reset_registry
        from shared.config import reset_cache as reset_cfg

        reset_cfg()
        reset_registry()
        try:
            orch = InvestigationOrchestrator(
                http_client=FakeHTTPClient(),
                chat_platform=FakeChatPlatform(),
            )
            assert sorted(orch.agent_endpoints.keys()) == [
                "cloudwatch_logs",
                "eks",
                "slack_scanner",
            ]
        finally:
            reset_cfg()
            reset_registry()

    def test_disabled_in_config_agent_surfaced_to_orchestrator(self):
        registry = _build_registry(
            active_specialized=["slack_scanner", "eks"],
            disabled_specialized=["discord_scanner"],
        )
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=FakeChatPlatform(),
            registry=registry,
        )
        # disabled agents are NOT dispatched to but ARE tracked for evidence
        assert sorted(orch.agent_endpoints.keys()) == ["eks", "slack_scanner"]
        assert orch.disabled_agents == {"discord_scanner"}

    def test_runtime_arn_env_var_overrides_url(self, monkeypatch):
        monkeypatch.setenv(
            "EKS_AGENT_RUNTIME_ARN",
            "arn:aws:bedrock-agentcore:us-east-1:0:agent-runtime/eks",
        )
        monkeypatch.setenv("EKS_AGENT_URL", "http://should-be-ignored:1234")

        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=FakeChatPlatform(),
            registry=_build_registry(active_specialized=["eks"]),
        )
        assert (
            orch.agent_endpoints["eks"]
            == "arn:aws:bedrock-agentcore:us-east-1:0:agent-runtime/eks"
        )

    def test_url_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("CLOUDWATCH_LOGS_AGENT_URL", "http://env-cw:9999")
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=FakeChatPlatform(),
            registry=_build_registry(active_specialized=["cloudwatch_logs"]),
        )
        assert orch.agent_endpoints["cloudwatch_logs"] == "http://env-cw:9999"

    def test_auto_selects_agentcore_client_for_arn_endpoints(self, monkeypatch):
        monkeypatch.setenv(
            "EKS_AGENT_RUNTIME_ARN",
            "arn:aws:bedrock-agentcore:us-east-1:0:runtime/x",
        )
        orch = InvestigationOrchestrator(
            chat_platform=FakeChatPlatform(),
            registry=_build_registry(active_specialized=["eks"]),
        )
        assert isinstance(orch.http_client, AgentCoreClient)

    def test_auto_selects_aiohttp_client_for_url_endpoints(self):
        orch = InvestigationOrchestrator(
            chat_platform=FakeChatPlatform(),
            registry=_build_registry(active_specialized=["eks"]),
        )
        from agents.master.orchestrator import AiohttpClient
        assert isinstance(orch.http_client, AiohttpClient)


# ---------------------------------------------------------------------------
# Tests: investigation-started announcement, late-failure enrichment, metadata
# ---------------------------------------------------------------------------


class TestInvestigationStartedAnnouncement:
    """The orchestrator posts a "started" notice listing dispatched agents."""

    @pytest.mark.asyncio
    async def test_started_message_posted_first(self, alert_context):
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(chat_platform=chat_platform)

        await orch.investigate(alert_context)

        first = chat_platform.messages[0][2]
        assert "Investigation Started" in first
        # Names every agent the orchestrator dispatched
        assert "Slack Scanner" in first
        assert "CloudWatch Logs" in first
        assert "EKS Cluster State" in first

    @pytest.mark.asyncio
    async def test_no_started_message_when_no_agents_configured(self, alert_context):
        chat_platform = FakeChatPlatform()
        # Empty registry — no specialized agents in the deployment manifest.
        empty_registry = AgentRegistry(
            ProjectConfig(
                project="test",
                environment="dev",
                defaults=Defaults(model_id="anthropic.claude-test"),
                agents={"master": AgentConfig(skills=["investigate_alert"])},
            )
        )
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=chat_platform,
            registry=empty_registry,
        )
        orch.INITIAL_DEADLINE_SECONDS = 0.05
        orch.HARD_CUTOFF_SECONDS = 0.1

        await orch.investigate(alert_context)

        # Only the Incident Report (which says no agents configured) — no
        # started notice when we'd be naming zero agents.
        for _, _, text in chat_platform.messages:
            assert "Investigation Started" not in text


class TestLateFailureEnrichment:
    """Failures that arrive after the initial deadline still get a chat update."""

    @pytest.mark.asyncio
    async def test_late_failure_posts_enrichment_with_error_marker(self, alert_context):
        class OneSlowFailingClient:
            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                if url == EKS_URL:  # slow + fails
                    await asyncio.sleep(0.15)
                    raise ConnectionError("boom")
                return _default_a2a_response(payload)

        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=OneSlowFailingClient(),
            chat_platform=chat_platform,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        late_msgs = [
            text for _, _, text in chat_platform.messages
            if "Late Result" in text or "Enrichment Update" in text
        ]
        assert any("Late Result (failed)" in m for m in late_msgs), (
            f"Expected a failure-flavoured late update; got: {late_msgs}"
        )


class TestAgentMetadataPropagation:
    """Per-invocation metadata flows from the agent footer into AgentResult."""

    @pytest.mark.asyncio
    async def test_metadata_footer_extracted_and_stripped_from_summary(
        self, alert_context,
    ):
        from shared.agent_telemetry import encode_metadata_footer
        from shared.models import AgentMetadata

        meta = AgentMetadata(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=120,
            output_tokens=42,
            total_tokens=162,
            cost_usd=0.00033,
        )
        footer = encode_metadata_footer(meta)

        async def post_json(url: str, payload: dict) -> dict:
            return {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "message": {
                        "role": "agent",
                        "parts": [
                            {"kind": "text", "text": "Pod healthy.\n\n" + footer},
                        ],
                        "messageId": "resp-meta",
                    }
                },
            }

        class StubClient:
            async def post_json(self, url, payload):
                return await post_json(url, payload)

        orch = InvestigationOrchestrator(
            http_client=StubClient(),
            chat_platform=FakeChatPlatform(),
            registry=_build_registry(active_specialized=["eks"]),
        )

        result = await orch.invoke_agent("eks", alert_context)

        assert footer not in result.summary
        assert "Pod healthy" in result.summary
        assert result.metadata.model_id == meta.model_id
        assert result.metadata.input_tokens == 120
        assert result.metadata.output_tokens == 42
        assert result.metadata.cost_usd == 0.00033
        # Orchestrator stamps timing wall-clock around the call
        assert result.metadata.started_at is not None
        assert result.metadata.completed_at is not None



class TestDisabledInConfigPropagation:
    """Disabled-in-config agents are excluded from fan-out but rendered in
    the Incident Report's evidence section as 🚫 disabled blocks."""

    @pytest.mark.asyncio
    async def test_disabled_agent_skipped_from_fan_out(self, alert_context):
        registry = _build_registry(
            active_specialized=["eks"],
            disabled_specialized=["discord_scanner"],
        )
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client, registry=registry)

        await orch.investigate(alert_context)

        # Only EKS was dispatched; discord_scanner was disabled.
        called_urls = {url for url, _ in http_client.calls}
        assert called_urls == {EKS_URL}

    @pytest.mark.asyncio
    async def test_disabled_agent_appears_in_incident_report_evidence(self, alert_context):
        registry = _build_registry(
            active_specialized=["eks"],
            disabled_specialized=["slack_scanner"],
        )
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(chat_platform=chat_platform, registry=registry)

        await orch.investigate(alert_context)

        _, _, report_text = _find_report_msg(chat_platform.messages)
        # 🚫 marker on the disabled agent's evidence block
        assert "📡 *Slack Scanner* 🚫" in report_text
        assert "is disabled in this deployment" in report_text
        # EKS was dispatched normally — no 🚫 on it
        assert "☸️ *EKS Cluster State* 🚫" not in report_text

    @pytest.mark.asyncio
    async def test_disabled_agent_not_in_started_notice(self, alert_context):
        registry = _build_registry(
            active_specialized=["eks", "cloudwatch_logs"],
            disabled_specialized=["slack_scanner"],
        )
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(chat_platform=chat_platform, registry=registry)

        await orch.investigate(alert_context)

        # The Started notice lists active agents only.
        started_msgs = [
            text for _, _, text in chat_platform.messages
            if "Investigation Started" in text
        ]
        assert started_msgs, "Expected a Started notice"
        started = started_msgs[0]
        assert "EKS Cluster State" in started
        assert "CloudWatch Logs" in started
        assert "Slack Scanner" not in started
