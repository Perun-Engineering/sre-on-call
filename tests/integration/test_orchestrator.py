"""Unit tests for the Master Agent InvestigationOrchestrator."""

from __future__ import annotations

import asyncio

import boto3
import pytest
from moto import mock_aws

from agents.master.orchestrator import (
    AsyncHTTPClient,
    InvestigationOrchestrator,
    _serialize_alert_context,
)
from shared.a2a_protocol import build_a2a_request
from shared.agents import AgentRegistry
from shared.config import AgentConfig, Defaults, ProjectConfig
from shared.platforms import ChatPlatform
from shared.report_renderer import SlackReportRenderer
from agents.master.report_formatter import ReportFormatter
from shared.models import AgentMetadata, AgentResult, AlertContext, Finding
from shared.trace_store import (
    EVENT_A2A_REQUEST,
    EVENT_A2A_RESPONSE,
    EVENT_INVESTIGATION_TERMINATED,
    TraceStore,
)


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

    async def deliver(self, target, payload) -> str:
        text = self._renderer.render(payload)
        self.deliveries.append((target, payload, text))
        return text

    @property
    def messages(self) -> list[tuple[str, str, str]]:
        """Legacy compat: list of (channel_id, thread_anchor, rendered_text) tuples."""
        return [
            (target.channel_id, target.thread_anchor, text)
            for target, _, text in self.deliveries
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
    synthesizer=None,
    embedding_client=None,
    history_store=None,
    router=None,
    followup=None,
    page_signer=None,
    trace_store=None,
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
        synthesizer=synthesizer,
        embedding_client=embedding_client,
        history_store=history_store,
        router=router,
        followup=followup,
        page_signer=page_signer,
        trace_store=trace_store,
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


class TestInvokeAgentMapping:
    """invoke_agent maps a parsed A2AReply onto an AgentResult.

    Wire-format parsing (the three Strands response shapes, footer
    extraction, JSON-RPC error detection) is tested directly against
    ``A2AClient.send`` in :mod:`tests.test_a2a_client`. These tests cover
    the alert path's *mapping* knowledge through the public ``invoke_agent``
    seam: a JSON-RPC error becomes an ``error`` result, structured findings
    round-trip, and the ``AGENT_METADATA`` footer merges with the
    orchestrator's wall-clock timing.
    """

    async def test_jsonrpc_error_maps_to_error_result(self, alert_context):
        http_client = FakeHTTPClient(responses={
            EKS_URL: {
                "jsonrpc": "2.0",
                "id": "x",
                "error": {"code": -32000, "message": "Agent unavailable"},
            },
        })
        orch = _make_orchestrator(http_client=http_client)

        result = await orch.invoke_agent("eks", alert_context)

        assert result.agent_name == "eks"
        assert result.status == "error"
        assert result.error_message is not None
        assert "Agent unavailable" in result.error_message

    async def test_structured_findings_and_metadata_round_trip(self, alert_context):
        """Structured findings survive the A2A text seam and the footer's
        model id overlays the orchestrator's wall-clock metadata."""
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
        text = f"Inspected 1 item(s). Found 1 finding(s). {structured_footer}"
        http_client = FakeHTTPClient(responses={
            EKS_URL: {
                "jsonrpc": "2.0",
                "result": {
                    "kind": "task",
                    "status": {"state": "completed"},
                    "artifacts": [
                        {"name": "agent_response", "parts": [{"kind": "text", "text": text}]},
                    ],
                },
            },
        })
        orch = _make_orchestrator(http_client=http_client)

        result = await orch.invoke_agent("eks", alert_context)

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
        # Footer model id overlays the orchestrator's wall-clock window.
        assert result.metadata.model_id == "model-from-agent"
        assert result.metadata.started_at is not None
        assert result.duration_seconds > 0


# ---------------------------------------------------------------------------
# Tests: InvestigationOrchestrator
# ---------------------------------------------------------------------------


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
                "incident_history",
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

    def test_default_transport_is_per_endpoint_router(self, monkeypatch):
        # Transport is no longer picked globally (the old any(arn) rule):
        # the default http_client is a RoutingHTTPClient that routes each
        # endpoint independently (ARN -> AgentCore, URL -> aiohttp). The
        # routing itself is covered by tests/test_fanout.py.
        from shared.a2a_client import RoutingHTTPClient

        monkeypatch.setenv(
            "EKS_AGENT_RUNTIME_ARN",
            "arn:aws:bedrock-agentcore:us-east-1:0:runtime/x",
        )
        orch = InvestigationOrchestrator(
            chat_platform=FakeChatPlatform(),
            registry=_build_registry(active_specialized=["eks", "cloudwatch_logs"]),
        )
        assert isinstance(orch.http_client, RoutingHTTPClient)


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
        from shared.agent_telemetry import AGENT_METADATA

        meta = AgentMetadata(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=120,
            output_tokens=42,
            total_tokens=162,
            cost_usd=0.00033,
        )
        footer = AGENT_METADATA.encode(meta)

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



    async def test_prefers_datapart_metadata_over_text_footer(self, alert_context):
        """Issue #24: when both are present, the DataPart metadata wins."""
        from shared.agent_telemetry import AGENT_METADATA
        from shared.tool_result import AGENT_RESULT, format_result

        text_meta = AgentMetadata(input_tokens=1, output_tokens=1, total_tokens=2)
        data_meta = AgentMetadata(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=120,
            output_tokens=42,
            total_tokens=162,
            cost_usd=0.00033,
        )
        agent_result = AgentResult(
            agent_name="eks", status="success", findings=[], summary="Pod healthy.",
        )
        text_body = f"{format_result(agent_result)}\n\n{AGENT_METADATA.encode(text_meta)}"

        class StubClient:
            async def post_json(self, url, payload):
                return {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "kind": "task",
                        "artifacts": [
                            {
                                "name": "agent_response",
                                "parts": [{"kind": "text", "text": text_body}],
                            },
                            {
                                "name": "agent_data",
                                "parts": [
                                    {"kind": "data", "data": AGENT_RESULT.encode_data(agent_result)},
                                    {"kind": "data", "data": AGENT_METADATA.encode_data(data_meta)},
                                ],
                            },
                        ],
                    },
                }

        orch = InvestigationOrchestrator(
            http_client=StubClient(),
            chat_platform=FakeChatPlatform(),
            registry=_build_registry(active_specialized=["eks"]),
        )

        result = await orch.invoke_agent("eks", alert_context)

        assert result.metadata.model_id == data_meta.model_id
        assert result.metadata.input_tokens == 120  # DataPart, not the text footer's 1
        assert result.metadata.cost_usd == 0.00033
        assert "Pod healthy" in result.summary


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


# ---------------------------------------------------------------------------
# Trace archive — verify the orchestrator writes A2A round-trip events and
# the end-of-investigation manifest when a TraceStore is configured.
# ---------------------------------------------------------------------------


_TRACE_BUCKET = "test-orchestrator-traces"
_TRACE_TABLE = "test-orchestrator-traces"


def _make_trace_resources():
    """Create a moto-mocked S3 bucket + DDB table for trace archive."""
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=_TRACE_BUCKET)

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=_TRACE_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return s3, ddb


class TestTraceArchive:
    """The orchestrator emits trace events + a manifest when a TraceStore is wired in."""

    @pytest.mark.asyncio
    async def test_manifest_and_events_written_for_full_investigation(
        self, alert_context
    ):
        with mock_aws():
            s3, ddb = _make_trace_resources()
            trace_store = TraceStore(
                bucket=_TRACE_BUCKET,
                table_name=_TRACE_TABLE,
                s3_client=s3,
                dynamodb_resource=ddb,
            )

            registry = _build_registry()
            http_client = FakeHTTPClient()
            chat_platform = FakeChatPlatform()
            orch = InvestigationOrchestrator(
                http_client=http_client,
                chat_platform=chat_platform,
                report_formatter=ReportFormatter(registry),
                registry=registry,
                trace_store=trace_store,
            )
            orch.INITIAL_DEADLINE_SECONDS = 0.1
            orch.HARD_CUTOFF_SECONDS = 0.5

            await orch.investigate(alert_context)

            # Manifest should land under dt=YYYY-MM-DD/investigation_id=.../manifest.json
            objs = s3.list_objects_v2(Bucket=_TRACE_BUCKET, Prefix="dt=")
            keys = [o["Key"] for o in objs.get("Contents", [])]
            assert any(
                f"/investigation_id={alert_context.investigation_id}/manifest.json" in k
                for k in keys
            ), f"Manifest not written; got {keys}"

            # A2A request + response events for each of the 3 active agents.
            event_keys = [k for k in keys if "/events/" in k]
            assert sum(EVENT_A2A_REQUEST in k for k in event_keys) == 3
            assert sum(EVENT_A2A_RESPONSE in k for k in event_keys) == 3
            assert sum(EVENT_INVESTIGATION_TERMINATED in k for k in event_keys) == 1

            # DDB index entry written.
            item = ddb.Table(_TRACE_TABLE).get_item(
                Key={"pk": alert_context.investigation_id}
            ).get("Item")
            assert item is not None
            assert item["status"] == "completed"
            assert item["agent_count"] == 3
            assert item["error_count"] == 0
            assert item["channel_id"] == alert_context.channel_id

    @pytest.mark.asyncio
    async def test_manifest_status_partial_when_some_agents_fail(
        self, alert_context
    ):
        """Mixed success/error fan-out yields ``status: partial`` in the manifest."""
        with mock_aws():
            s3, ddb = _make_trace_resources()
            trace_store = TraceStore(
                bucket=_TRACE_BUCKET,
                table_name=_TRACE_TABLE,
                s3_client=s3,
                dynamodb_resource=ddb,
            )

            # eks errors; slack and cloudwatch succeed.
            http_client = FakeHTTPClient(responses={
                EKS_URL: {
                    "jsonrpc": "2.0",
                    "id": "x",
                    "error": {"code": -32000, "message": "EKS unavailable"},
                },
            })
            registry = _build_registry()
            orch = InvestigationOrchestrator(
                http_client=http_client,
                chat_platform=FakeChatPlatform(),
                report_formatter=ReportFormatter(registry),
                registry=registry,
                trace_store=trace_store,
            )
            orch.INITIAL_DEADLINE_SECONDS = 0.1
            orch.HARD_CUTOFF_SECONDS = 0.5

            await orch.investigate(alert_context)

            item = ddb.Table(_TRACE_TABLE).get_item(
                Key={"pk": alert_context.investigation_id}
            )["Item"]
            assert item["status"] == "partial"
            assert item["error_count"] == 1

    @pytest.mark.asyncio
    async def test_no_traces_written_when_store_unset(self, alert_context):
        """Default behaviour (no TraceStore + no env vars) writes no traces."""
        with mock_aws():
            # Bucket exists but neither env var nor explicit store — must skip.
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket=_TRACE_BUCKET)

            registry = _build_registry()
            orch = InvestigationOrchestrator(
                http_client=FakeHTTPClient(),
                chat_platform=FakeChatPlatform(),
                report_formatter=ReportFormatter(registry),
                registry=registry,
                # trace_store omitted; from_env() returns None (env vars
                # cleared by the autouse _clean_env fixture above).
            )
            orch.INITIAL_DEADLINE_SECONDS = 0.1
            orch.HARD_CUTOFF_SECONDS = 0.5

            await orch.investigate(alert_context)

            objs = s3.list_objects_v2(Bucket=_TRACE_BUCKET, Prefix="dt=")
            assert objs.get("KeyCount", 0) == 0


# ---------------------------------------------------------------------------
# Tests: LLM synthesis Analysis section (#27)
# ---------------------------------------------------------------------------


class _FakeSynthesizer:
    """Injectable synthesizer for orchestrator tests.

    Records each (alert, results) call and returns a configurable
    ``IncidentAnalysis`` (or ``None`` to simulate fail-open).
    """

    timeout_seconds = 0.0  # no budget reservation in fast tests

    def __init__(self, *, returns="default"):
        from agents.master.synthesis import IncidentAnalysis

        if returns == "default":
            returns = IncidentAnalysis(
                root_cause_hypothesis="service-api CPU saturation",
                correlation="CPU alert lines up with slow-query logs",
                confidence="medium",
                suggested_next_action="Scale out service-api",
            )
        self._returns = returns
        self.calls: list[dict] = []

    async def synthesize(self, alert_context, results):
        self.calls.append(dict(results))
        return self._returns


class TestOrchestratorSynthesis:
    @pytest.mark.asyncio
    async def test_initial_report_contains_analysis(self, alert_context):
        chat_platform = FakeChatPlatform()
        synth = _FakeSynthesizer()
        orch = _make_orchestrator(chat_platform=chat_platform, synthesizer=synth)

        await orch.investigate(alert_context)

        _, _, report_text = _find_report_msg(chat_platform.messages)
        assert "Analysis" in report_text
        assert "service-api CPU saturation" in report_text
        assert synth.calls, "synthesizer should be invoked for the initial report"

    @pytest.mark.asyncio
    async def test_fail_open_when_synthesis_returns_none(self, alert_context):
        chat_platform = FakeChatPlatform()
        synth = _FakeSynthesizer(returns=None)
        orch = _make_orchestrator(chat_platform=chat_platform, synthesizer=synth)

        await orch.investigate(alert_context)

        _, _, report_text = _find_report_msg(chat_platform.messages)
        assert "Incident Report" in report_text
        assert "🧠 Analysis" not in report_text

    @pytest.mark.asyncio
    async def test_no_synthesis_call_without_synthesizer(self, alert_context):
        chat_platform = FakeChatPlatform()
        orch = _make_orchestrator(chat_platform=chat_platform, synthesizer=None)

        await orch.investigate(alert_context)

        _, _, report_text = _find_report_msg(chat_platform.messages)
        assert "🧠 Analysis" not in report_text

    @pytest.mark.asyncio
    async def test_enrichment_resynthesizes_per_late_result(self, alert_context):
        # Agents respond after the initial deadline but before the hard cutoff,
        # so every result arrives as a late enrichment.
        http_client = FakeHTTPClient(delay=0.1)
        chat_platform = FakeChatPlatform()
        synth = _FakeSynthesizer()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat_platform,
            initial_deadline=0.02,
            hard_cutoff=1.0,
            synthesizer=synth,
        )

        await orch.investigate(alert_context)

        enrichment_texts = [
            text for _, _, text in chat_platform.messages if "Enrichment Update" in text
        ]
        assert enrichment_texts, "expected at least one enrichment update"
        assert any("Analysis" in t for t in enrichment_texts)
        # One synthesis for the initial report plus one per late result.
        assert len(synth.calls) >= 2


# ---------------------------------------------------------------------------
# Tests: Phase 8 — incident-history outcome write (issue #30)
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Stand-in EmbeddingClient: records inputs, returns a fixed vector."""

    def __init__(self, vector):
        self.vector = vector
        self.texts: list[str] = []

    def embed(self, text):
        self.texts.append(text)
        return self.vector


class _RecordingHistoryStore:
    """Stand-in IncidentHistoryStore: captures put_outcome calls."""

    def __init__(self):
        self.outcomes = []

    def put_outcome(self, outcome):
        self.outcomes.append(outcome)


class TestOrchestratorIncidentHistoryWrite:
    """Phase 8 embeds the alert and stores one outcome record, fail-open."""

    @pytest.mark.asyncio
    async def test_records_outcome_with_embedding_and_refs(self, alert_context):
        embedder = _FakeEmbedder([0.1, 0.2, 0.3])
        store = _RecordingHistoryStore()
        orch = _make_orchestrator(embedding_client=embedder, history_store=store)

        await orch.investigate(alert_context)

        assert len(store.outcomes) == 1
        outcome = store.outcomes[0]
        assert outcome.investigation_id == alert_context.investigation_id
        assert outcome.alert_text == alert_context.alert_text
        assert outcome.embedding == [0.1, 0.2, 0.3]
        assert outcome.platform == alert_context.platform
        assert outcome.channel_id == alert_context.channel_id
        # The alert text is what gets embedded.
        assert embedder.texts == [alert_context.alert_text]

    @pytest.mark.asyncio
    async def test_skips_write_when_embedding_unavailable(self, alert_context):
        # Embedding failure (None) => no searchable record, so nothing stored.
        store = _RecordingHistoryStore()
        orch = _make_orchestrator(
            embedding_client=_FakeEmbedder(None), history_store=store
        )

        await orch.investigate(alert_context)

        assert store.outcomes == []

    @pytest.mark.asyncio
    async def test_noop_when_history_not_configured(self, alert_context):
        # Default orchestrator (no client/store, env cleared) must not raise.
        orch = _make_orchestrator()
        await orch.investigate(alert_context)


# ---------------------------------------------------------------------------
# Tests: Phase 0.5 routing + Stage 2 follow-up (#28)
# ---------------------------------------------------------------------------

import json as _json

from agents.master.routing import AgentRouter, RoutingResult
from agents.master.followup import FollowupPlanner
from agents.master.synthesis import AnalysisSynthesizer
from shared.trace_store import EVENT_FOLLOWUP_DECISION, EVENT_ROUTING_DECISION


class _FakeRouter:
    """Injectable router returning a fixed RoutingResult (or None=fail-open)."""

    def __init__(self, result):
        self._result = result
        self.calls: list[list[str]] = []

    async def route(self, alert_context, candidates):
        self.calls.append([c.agent_id for c in candidates])
        return self._result


class _FakePlanner:
    """Injectable follow-up planner returning a fixed list of (id, hint)."""

    def __init__(self, plan):
        self._plan = plan
        self.calls: list[list[str]] = []

    async def plan(self, alert_context, results, candidates):
        self.calls.append([c.agent_id for c in candidates])
        return self._plan


class _RaisingStructuredAgent:
    """A model client that always fails — used to prove fail-open behavior."""

    async def structured_output_async(self, output_model, prompt):
        raise RuntimeError("bedrock down")


class _PerUrlDelayClient:
    """HTTP client that delays only a chosen URL, others respond instantly."""

    def __init__(self, slow_url: str, delay: float):
        self.slow_url = slow_url
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def post_json(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        if url == self.slow_url:
            await asyncio.sleep(self.delay)
        return _default_a2a_response(payload)


def _payload_text(payload: dict) -> str:
    return payload["params"]["message"]["parts"][0]["text"]


class TestRoutingDispatch:
    @pytest.mark.asyncio
    async def test_dispatches_only_selected_agents(self, alert_context):
        router = _FakeRouter(
            RoutingResult(
                selected={"eks": "", "cloudwatch_logs": ""},
                skipped={"slack_scanner": "no chatter relevance"},
                rationale="logs + k8s suffice",
            )
        )
        http_client = FakeHTTPClient()
        chat = FakeChatPlatform()
        orch = _make_orchestrator(http_client=http_client, chat_platform=chat, router=router)

        await orch.investigate(alert_context)

        urls = {url for url, _ in http_client.calls}
        assert urls == {EKS_URL, CLOUDWATCH_URL}
        assert SLACK_URL not in urls

    @pytest.mark.asyncio
    async def test_skipped_agent_renders_distinctly_not_as_failure(self, alert_context):
        router = _FakeRouter(
            RoutingResult(
                selected={"eks": "", "cloudwatch_logs": ""},
                skipped={"slack_scanner": "no chatter relevance"},
                rationale="r",
            )
        )
        chat = FakeChatPlatform()
        orch = _make_orchestrator(chat_platform=chat, router=router)

        await orch.investigate(alert_context)

        _, _, report = _find_report_msg(chat.messages)
        assert "➖" in report
        assert "not investigated" in report
        assert "no chatter relevance" in report
        assert "⚠️ Slack Scanner data unavailable" not in report

    @pytest.mark.asyncio
    async def test_per_agent_hint_injected_on_payload(self, alert_context):
        router = _FakeRouter(
            RoutingResult(
                selected={"eks": "describe payment pods in the window"},
                skipped={"slack_scanner": "x", "cloudwatch_logs": "y"},
                rationale="r",
            )
        )
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client, router=router)

        await orch.investigate(alert_context)

        eks_payloads = [p for u, p in http_client.calls if u == EKS_URL]
        assert eks_payloads, "eks should have been dispatched"
        text = _payload_text(eks_payloads[0])
        assert "describe payment pods in the window" in text
        assert _json.loads(text)["investigation_hints"] == "describe payment pods in the window"

    @pytest.mark.asyncio
    async def test_router_none_dispatches_all(self, alert_context):
        router = _FakeRouter(None)  # fail-open: dispatch every active agent
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client, router=router)

        await orch.investigate(alert_context)

        urls = {url for url, _ in http_client.calls}
        assert urls == {SLACK_URL, CLOUDWATCH_URL, EKS_URL}


class TestFailingModelClientDegradesToToday:
    """Acceptance: a failing model client degrades to *exactly* today's
    behavior — all active agents dispatched, no follow-up, report still posts."""

    @pytest.mark.asyncio
    async def test_all_fail_open_to_baseline(self, alert_context):
        router = AgentRouter(agent=_RaisingStructuredAgent())
        followup = FollowupPlanner(agent=_RaisingStructuredAgent())
        synth = AnalysisSynthesizer(agent=_RaisingStructuredAgent())
        http_client = FakeHTTPClient()
        chat = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat,
            router=router,
            followup=followup,
            synthesizer=synth,
        )

        await orch.investigate(alert_context)

        urls = [url for url, _ in http_client.calls]
        # Every active agent dispatched, exactly once — no follow-up round.
        assert sorted(urls) == sorted([SLACK_URL, CLOUDWATCH_URL, EKS_URL])
        # Report still posts, with no skipped block and no Analysis section.
        _, _, report = _find_report_msg(chat.messages)
        assert "Incident Report" in report
        assert "➖" not in report
        assert "🧠 Analysis" not in report


class TestFollowupRound:
    @pytest.mark.asyncio
    async def test_followup_dispatches_one_refined_round(self, alert_context):
        planner = _FakePlanner([("eks", "recheck payment pods now")])
        http_client = FakeHTTPClient()
        chat = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat,
            followup=planner,
            initial_deadline=0.1,
            hard_cutoff=3.0,
        )

        await orch.investigate(alert_context)

        assert planner.calls, "follow-up planner should be consulted"
        eks_payloads = [_payload_text(p) for u, p in http_client.calls if u == EKS_URL]
        # eks dispatched twice: initial sweep + refined follow-up.
        assert len(eks_payloads) == 2
        assert any("recheck payment pods now" in t for t in eks_payloads)

    @pytest.mark.asyncio
    async def test_followup_holds_hard_cutoff(self, alert_context):
        # Router skips eks initially; the follow-up re-dispatches it, but eks
        # hangs well past the cutoff. The investigation must still terminate at
        # the cutoff, not wait for the slow agent.
        router = _FakeRouter(
            RoutingResult(
                selected={"slack_scanner": "", "cloudwatch_logs": ""},
                skipped={"eks": "not k8s on first read"},
                rationale="r",
            )
        )
        planner = _FakePlanner([("eks", "confirm in k8s")])
        http_client = _PerUrlDelayClient(slow_url=EKS_URL, delay=5.0)
        chat = FakeChatPlatform()
        orch = _make_orchestrator(
            http_client=http_client,
            chat_platform=chat,
            router=router,
            followup=planner,
            initial_deadline=0.1,
            hard_cutoff=1.5,
        )

        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await orch.investigate(alert_context)
        elapsed = loop.time() - t0

        # Cutoff held: did NOT wait for the 5s follow-up agent.
        assert elapsed < 2.3, f"investigation overran the cutoff: {elapsed:.2f}s"
        # The follow-up genuinely dispatched eks (otherwise the test is vacuous).
        assert any(u == EKS_URL for u, _ in http_client.calls)
        assert _find_report_msg(chat.messages)

    @pytest.mark.asyncio
    async def test_no_followup_when_disabled(self, alert_context):
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client, followup=None)

        await orch.investigate(alert_context)

        # Exactly one dispatch per active agent — no extra round.
        urls = [url for url, _ in http_client.calls]
        assert sorted(urls) == sorted([SLACK_URL, CLOUDWATCH_URL, EKS_URL])


class TestBothDisabledIsTodaysBehavior:
    @pytest.mark.asyncio
    async def test_no_routing_no_followup(self, alert_context):
        http_client = FakeHTTPClient()
        chat = FakeChatPlatform()
        # router/followup default to None (env unset) — byte-for-byte today's.
        orch = _make_orchestrator(http_client=http_client, chat_platform=chat)

        await orch.investigate(alert_context)

        urls = [url for url, _ in http_client.calls]
        assert sorted(urls) == sorted([SLACK_URL, CLOUDWATCH_URL, EKS_URL])
        _, _, report = _find_report_msg(chat.messages)
        assert "➖" not in report


class TestRoutingTrace:
    @pytest.mark.asyncio
    async def test_routing_decision_in_trace_archive(self, alert_context):
        with mock_aws():
            s3, ddb = _make_trace_resources()
            trace_store = TraceStore(
                bucket=_TRACE_BUCKET,
                table_name=_TRACE_TABLE,
                s3_client=s3,
                dynamodb_resource=ddb,
            )
            registry = _build_registry()
            router = _FakeRouter(
                RoutingResult(
                    selected={"eks": "check pods"},
                    skipped={"slack_scanner": "sk", "cloudwatch_logs": "sk2"},
                    rationale="k8s only",
                )
            )
            orch = InvestigationOrchestrator(
                http_client=FakeHTTPClient(),
                chat_platform=FakeChatPlatform(),
                report_formatter=ReportFormatter(registry),
                registry=registry,
                trace_store=trace_store,
                router=router,
            )
            orch.INITIAL_DEADLINE_SECONDS = 0.1
            orch.HARD_CUTOFF_SECONDS = 0.5

            await orch.investigate(alert_context)

            objs = s3.list_objects_v2(Bucket=_TRACE_BUCKET, Prefix="dt=")
            keys = [o["Key"] for o in objs.get("Contents", [])]
            event_keys = [k for k in keys if "/events/" in k]
            assert sum(EVENT_ROUTING_DECISION in k for k in event_keys) == 1

            # The routing event payload carries selected hints + skip reasons.
            routing_key = next(k for k in event_keys if EVENT_ROUTING_DECISION in k)
            body = s3.get_object(Bucket=_TRACE_BUCKET, Key=routing_key)["Body"].read()
            payload = _json.loads(body)["payload"]
            assert payload["selected"] == {"eks": "check pods"}
            assert "slack_scanner" in payload["skipped"]

            # The manifest records the routing block too.
            manifest_key = next(k for k in keys if "/manifest.json" in k)
            manifest = _json.loads(
                s3.get_object(Bucket=_TRACE_BUCKET, Key=manifest_key)["Body"].read()
            )
            assert manifest["routing"]["selected"] == {"eks": "check pods"}


# ---------------------------------------------------------------------------
# Tests: Phase 7 — chart series snapshot (#32)
# ---------------------------------------------------------------------------


class TestSnapshotCharts:
    def _alert_context(self):
        from shared.models import AlertContext

        return AlertContext(
            investigation_id="inv-charts-1",
            platform="slack",
            channel_id="C1",
            message_id="m1",
            alert_text="boom",
            alert_timestamp="2026-01-01T00:00:00+00:00",
            investigation_window=("2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00"),
        )

    def test_writes_one_per_chart_id(self):
        from unittest.mock import MagicMock

        from shared.models import (
            AgentResult,
            ChartDescriptor,
            ChartSeries,
            Finding,
        )

        orch = _make_orchestrator()
        orch._trace_store = MagicMock()

        desc = ChartDescriptor.create(
            source="cloudwatch_logs_insights",
            log_groups=["lg"],
            query="fields @message",
            start_epoch=1,
            end_epoch=2,
        )
        finding = Finding(
            source="lg", timestamp="t", content="c", severity="info", chart=desc,
        )
        result = AgentResult(
            agent_name="cloudwatch_logs",
            status="success",
            findings=[finding],
            summary="s",
            chart_series={desc.chart_id: ChartSeries(points=[{"x": 1}])},
        )

        orch._snapshot_charts(self._alert_context(), {"cloudwatch_logs": result})

        assert orch._trace_store.put_chart_series.call_count == 1
        kwargs = orch._trace_store.put_chart_series.call_args.kwargs
        assert kwargs["chart_id"] == desc.chart_id
        assert kwargs["investigation_id"] == "inv-charts-1"
        assert kwargs["payload"]["points"] == [{"x": 1}]
        assert kwargs["payload"]["descriptor"]["query"] == "fields @message"

    def test_noop_without_trace_store(self):
        orch = _make_orchestrator()
        orch._trace_store = None
        # Must not raise.
        orch._snapshot_charts(self._alert_context(), {})

    def test_ignores_agent_failures_and_chartless_results(self):
        from unittest.mock import MagicMock

        from shared.models import AgentFailure, AgentResult, Finding

        orch = _make_orchestrator()
        orch._trace_store = MagicMock()

        plain = AgentResult(
            agent_name="eks",
            status="success",
            findings=[Finding(source="s", timestamp="t", content="c", severity="info")],
            summary="s",
        )
        failure = AgentFailure(
            agent_name="slack_scanner", error_message="timeout", timestamp="t",
        )

        orch._snapshot_charts(
            self._alert_context(), {"eks": plain, "slack_scanner": failure},
        )

        orch._trace_store.put_chart_series.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Phase 3 + 7 — page signer + page model write (#33)
# ---------------------------------------------------------------------------


class TestOrchestratorPageModel:
    """Master signs the page URL in Phase 3 and writes the page model in Phase 7."""

    @pytest.mark.asyncio
    async def test_signs_link_and_writes_page_model(self, alert_context):
        from unittest.mock import MagicMock

        page_signer = MagicMock()
        page_signer.sign.return_value = "https://d/pages/x.html?Signature=1"
        trace_store = MagicMock()

        orch = _make_orchestrator(
            page_signer=page_signer,
            trace_store=trace_store,
        )

        await orch.investigate(alert_context)

        page_signer.sign.assert_called_once()
        trace_store.put_page_model.assert_called_once()
        _, kwargs = trace_store.put_page_model.call_args
        assert kwargs["investigation_id"]
        assert "chart_ids" in kwargs["payload"]
