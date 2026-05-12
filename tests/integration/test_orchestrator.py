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
from shared.chat_poster import ChatPoster, chat_post_with_retry
from agents.master.report_formatter import ReportFormatter
from shared.models import AgentFailure, AgentMetadata, AgentResult, AlertContext, Finding


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


class FakeSlackPoster:
    """Fake chat poster that records posted messages."""

    def __init__(self):
        self.messages: list[tuple[str, str, str]] = []

    async def post_reply(
        self, alert_context, text: str
    ) -> None:
        thread_ts = alert_context.platform_metadata.get("thread_ts", alert_context.message_id)
        self.messages.append((alert_context.channel_id, thread_ts, text))


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
    slack_poster: ChatPoster | None = None,
    initial_deadline: float = 0.1,
    hard_cutoff: float = 0.5,
) -> InvestigationOrchestrator:
    """Create an orchestrator with short timeouts for testing."""
    orch = InvestigationOrchestrator(
        http_client=http_client or FakeHTTPClient(),
        chat_poster=slack_poster or FakeSlackPoster(),
        report_formatter=ReportFormatter(),
        agent_endpoints={
            "slack_scanner": "http://localhost:9001",
            "cloudwatch_logs": "http://localhost:9002",
            "eks": "http://localhost:9003",
        },
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
        assert called_urls == {
            "http://localhost:9001",
            "http://localhost:9002",
            "http://localhost:9003",
        }

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
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            slack_poster=slack_poster,
            initial_deadline=0.1,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        # The Incident Report follows the "Investigation Started" notice.
        channel, thread_ts, text = _find_report_msg(slack_poster.messages)
        assert channel == alert_context.channel_id
        assert thread_ts == alert_context.message_id
        assert "Incident Report" in text

    @pytest.mark.asyncio
    async def test_report_marks_slow_agents_pending(self, alert_context):
        """Agents that don't respond within the initial deadline render as ⏳ pending."""
        # Make all agents slow (longer than initial deadline)
        http_client = FakeHTTPClient(delay=0.3)
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        _, _, report_text = _find_report_msg(slack_poster.messages)
        assert "⏳" in report_text or "still investigating" in report_text.lower()

    @pytest.mark.asyncio
    async def test_hard_cutoff_terminates_investigation(self, alert_context):
        """Investigation terminates at the hard cutoff."""
        # Agents take longer than the hard cutoff
        http_client = FakeHTTPClient(delay=5.0)
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
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
                if url in ("http://localhost:9003", "http://localhost:9004"):
                    await asyncio.sleep(slow_delay)
                return _default_a2a_response(payload)

        http_client = MixedHTTPClient()
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        # Should have initial report + enrichment updates
        assert len(slack_poster.messages) >= 2  # At least report + 1 enrichment

    @pytest.mark.asyncio
    async def test_enrichment_update_format(self, alert_context):
        """Enrichment updates contain the expected format."""

        class SlowOneAgent:
            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                if url == "http://localhost:9003":  # EKS is slow
                    await asyncio.sleep(0.15)
                return _default_a2a_response(payload)

        http_client = SlowOneAgent()
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        # Find enrichment update messages (not the initial report)
        enrichment_msgs = [
            text for _, _, text in slack_poster.messages
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
                if url == "http://localhost:9002":  # cloudwatch_logs fails
                    raise ConnectionError("Connection refused")
                return _default_a2a_response(payload)

        http_client = FailingHTTPClient()
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
        )

        # Should not raise
        await orch.investigate(alert_context)

        # Report should still be posted
        assert len(slack_poster.messages) >= 1

    @pytest.mark.asyncio
    async def test_slack_post_failure_does_not_crash(self, alert_context):
        """Slack posting failures are logged but don't crash the orchestrator."""

        class FailingSlackPoster:
            def __init__(self):
                self.messages = []

            async def post_reply(self, alert_context, text):
                raise RuntimeError("Slack API error")

        orch = _make_orchestrator(
            slack_poster=FailingSlackPoster(),
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
        assert url == "http://localhost:9001"
        assert payload["method"] == "message/send"
        assert "req-slack_scanner-inv-test-001" == payload["id"]


class TestOrchestratorEndpointConfig:
    """Test agent endpoint configuration."""

    def test_default_endpoints(self):
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
        )
        # Should have all 4 agents (Prometheus deferred)
        assert len(orch.agent_endpoints) == 4
        assert "slack_scanner" in orch.agent_endpoints
        assert "discord_scanner" in orch.agent_endpoints
        assert "cloudwatch_logs" in orch.agent_endpoints
        assert "eks" in orch.agent_endpoints

    def test_custom_endpoints(self):
        custom = {
            "slack_scanner": "http://custom:8001",
            "cloudwatch_logs": "http://custom:8003",
            "eks": "http://custom:8004",
        }
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
            agent_endpoints=custom,
        )
        assert orch.agent_endpoints == custom

    @patch.dict(
        "os.environ",
        {"CLOUDWATCH_LOGS_AGENT_URL": "http://env-cw:9999"},
    )
    def test_endpoints_from_environment(self):
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
        )
        assert orch.agent_endpoints["cloudwatch_logs"] == "http://env-cw:9999"

    @patch.dict(
        "os.environ",
        {
            "EKS_AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:0:agent-runtime/eks",
            "EKS_AGENT_URL": "http://should-be-ignored:1234",
        },
    )
    def test_runtime_arn_env_var_overrides_url(self):
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
        )
        assert (
            orch.agent_endpoints["eks"]
            == "arn:aws:bedrock-agentcore:us-east-1:0:agent-runtime/eks"
        )

    @patch.dict(
        "os.environ",
        {
            "ENABLED_AGENTS": "eks",
            "EKS_AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:0:agent-runtime/eks",
            "SLACK_SCANNER_AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:0:agent-runtime/slack",
        },
        clear=False,
    )
    def test_enabled_agents_filters_to_allowlist(self):
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
        )
        assert list(orch.agent_endpoints.keys()) == ["eks"]

    @patch.dict(
        "os.environ",
        {
            "ENABLED_AGENTS": " eks , cloudwatch_logs ",
            "EKS_AGENT_RUNTIME_ARN": "arn:eks",
            "CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN": "arn:cw",
        },
        clear=False,
    )
    def test_enabled_agents_handles_whitespace_and_multiples(self):
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
        )
        assert sorted(orch.agent_endpoints.keys()) == ["cloudwatch_logs", "eks"]

    def test_enabled_agent_without_endpoint_is_skipped(self, monkeypatch):
        # An explicit allowlist must NOT silently fall back to the localhost
        # default — that would mean deployed runtimes fan out to localhost.
        for k in (
            "EKS_AGENT_RUNTIME_ARN",
            "EKS_AGENT_URL",
            "SLACK_SCANNER_AGENT_RUNTIME_ARN",
            "SLACK_SCANNER_AGENT_URL",
            "DISCORD_SCANNER_AGENT_RUNTIME_ARN",
            "DISCORD_SCANNER_AGENT_URL",
            "CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN",
            "CLOUDWATCH_LOGS_AGENT_URL",
        ):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("ENABLED_AGENTS", "eks")

        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
        )
        assert orch.agent_endpoints == {}

    def test_auto_selects_agentcore_client_for_arn_endpoints(self):
        orch = InvestigationOrchestrator(
            chat_poster=FakeSlackPoster(),
            agent_endpoints={"eks": "arn:aws:bedrock-agentcore:us-east-1:0:runtime/x"},
        )
        assert isinstance(orch.http_client, AgentCoreClient)

    def test_auto_selects_aiohttp_client_for_url_endpoints(self):
        orch = InvestigationOrchestrator(
            chat_poster=FakeSlackPoster(),
            agent_endpoints={"eks": "http://localhost:9005"},
        )
        from agents.master.orchestrator import AiohttpClient
        assert isinstance(orch.http_client, AiohttpClient)

    @patch.dict("os.environ", {"ENABLED_AGENTS": ""}, clear=False)
    def test_empty_enabled_agents_keeps_default_behaviour(self):
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=FakeSlackPoster(),
        )
        # No allowlist => all four agents present (with localhost fallbacks).
        assert sorted(orch.agent_endpoints.keys()) == [
            "cloudwatch_logs",
            "discord_scanner",
            "eks",
            "slack_scanner",
        ]


# ---------------------------------------------------------------------------
# Tests: Slack post retry logic
# ---------------------------------------------------------------------------


class TestChatPostWithRetry:
    """Test the chat_post_with_retry utility function."""

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self, alert_context):
        """No retries needed when the first call succeeds."""
        poster = FakeSlackPoster()
        await chat_post_with_retry(
            poster, alert_context, "hello", base_delay=0.0
        )
        assert len(poster.messages) == 1
        assert poster.messages[0] == (alert_context.channel_id, alert_context.message_id, "hello")

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self, alert_context):
        """Retries after transient failures and eventually succeeds."""
        call_count = 0

        class FailThenSucceedPoster:
            def __init__(self):
                self.messages = []

            async def post_reply(self, alert_context, text):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise RuntimeError("Chat API transient error")
                self.messages.append((alert_context.channel_id, alert_context.message_id, text))

        poster = FailThenSucceedPoster()
        await chat_post_with_retry(
            poster, alert_context, "hello", base_delay=0.0
        )
        # 2 failures + 1 success = 3 total calls
        assert call_count == 3
        assert len(poster.messages) == 1

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self, alert_context):
        """Raises the last exception when all retries are exhausted."""

        class AlwaysFailPoster:
            async def post_reply(self, alert_context, text):
                raise RuntimeError("Permanent failure")

        poster = AlwaysFailPoster()
        with pytest.raises(RuntimeError, match="Permanent failure"):
            await chat_post_with_retry(
                poster, alert_context, "hello",
                max_retries=3, base_delay=0.0,
            )

    @pytest.mark.asyncio
    async def test_total_attempts_is_max_retries_plus_one(self, alert_context):
        """With max_retries=3, there are 4 total attempts (1 initial + 3 retries)."""
        call_count = 0

        class CountingFailPoster:
            async def post_reply(self, alert_context, text):
                nonlocal call_count
                call_count += 1
                raise RuntimeError("fail")

        poster = CountingFailPoster()
        with pytest.raises(RuntimeError):
            await chat_post_with_retry(
                poster, alert_context, "hello",
                max_retries=3, base_delay=0.0,
            )
        assert call_count == 4  # 1 initial + 3 retries

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(self, alert_context):
        """Verify that delays follow exponential backoff pattern."""
        import time

        delays_observed: list[float] = []
        last_call_time: list[float] = [time.monotonic()]

        class TimingFailPoster:
            async def post_reply(self, alert_context, text):
                now = time.monotonic()
                delays_observed.append(now - last_call_time[0])
                last_call_time[0] = now
                raise RuntimeError("fail")

        poster = TimingFailPoster()
        # Use small base_delay to keep test fast but measurable
        base_delay = 0.05
        with pytest.raises(RuntimeError):
            await chat_post_with_retry(
                poster, alert_context, "hello",
                max_retries=3, base_delay=base_delay,
            )

        # delays_observed[0] is the initial call (no delay)
        # delays_observed[1] should be ~base_delay * 1 = 0.05s
        # delays_observed[2] should be ~base_delay * 2 = 0.10s
        # delays_observed[3] should be ~base_delay * 4 = 0.20s
        assert len(delays_observed) == 4
        # Verify delays are increasing (exponential backoff)
        assert delays_observed[1] < delays_observed[2] < delays_observed[3]

    @pytest.mark.asyncio
    async def test_succeeds_on_last_retry(self, alert_context):
        """Succeeds on the final retry attempt (attempt 4 of 4)."""
        call_count = 0

        class FailUntilLastPoster:
            def __init__(self):
                self.messages = []

            async def post_reply(self, alert_context, text):
                nonlocal call_count
                call_count += 1
                if call_count < 4:  # Fail first 3 attempts
                    raise RuntimeError("transient")
                self.messages.append((alert_context.channel_id, alert_context.message_id, text))

        poster = FailUntilLastPoster()
        await chat_post_with_retry(
            poster, alert_context, "report",
            max_retries=3, base_delay=0.0,
        )
        assert call_count == 4
        assert len(poster.messages) == 1


# ---------------------------------------------------------------------------
# Tests: investigation-started announcement, late-failure enrichment, metadata
# ---------------------------------------------------------------------------


class TestInvestigationStartedAnnouncement:
    """The orchestrator posts a "started" notice listing dispatched agents."""

    @pytest.mark.asyncio
    async def test_started_message_posted_first(self, alert_context):
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(slack_poster=slack_poster)

        await orch.investigate(alert_context)

        first = slack_poster.messages[0][2]
        assert "Investigation Started" in first
        # Names every agent the orchestrator dispatched
        assert "Slack Scanner" in first
        assert "CloudWatch Logs" in first
        assert "EKS Cluster State" in first

    @pytest.mark.asyncio
    async def test_no_started_message_when_no_agents_configured(self, alert_context):
        slack_poster = FakeSlackPoster()
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=slack_poster,
            report_formatter=ReportFormatter(),
            agent_endpoints={"placeholder": "x"},  # avoid env fallback
        )
        # Now drop to empty so the orchestrator sees no dispatched agents.
        orch.agent_endpoints = {}
        orch.INITIAL_DEADLINE_SECONDS = 0.05
        orch.HARD_CUTOFF_SECONDS = 0.1

        await orch.investigate(alert_context)

        # Only the Incident Report (which says no agents configured) — no
        # started notice when we'd be naming zero agents.
        for _, _, text in slack_poster.messages:
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
                if url == "http://localhost:9003":  # slow + fails
                    await asyncio.sleep(0.15)
                    raise ConnectionError("boom")
                return _default_a2a_response(payload)

        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=OneSlowFailingClient(),
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )

        await orch.investigate(alert_context)

        late_msgs = [
            text for _, _, text in slack_poster.messages
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
            chat_poster=FakeSlackPoster(),
            report_formatter=ReportFormatter(),
            agent_endpoints={"eks": "http://localhost:9999"},
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
