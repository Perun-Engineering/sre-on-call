"""Integration tests for sre-on-call.

Tests verify component interactions end-to-end:
- Lambda handler with real DynamoDB (moto) and mocked Slack/AgentCore
- A2A JSON-RPC 2.0 communication between Master_Agent and specialized agents
- Slack thread reply posting with correct channel_id and thread_ts
- 60-second deadline and 5-minute cutoff with short timeouts for fast tests

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 3.1, 3.2, 3.5, 3.6
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from agents.master.orchestrator import (
    AsyncHTTPClient,
    InvestigationOrchestrator,
    build_a2a_request,
    _parse_agent_result,
)
from shared.chat_poster import ChatPoster, chat_post_with_retry
from agents.master.report_formatter import ReportFormatter
from lambda_adapter.handler import lambda_handler
from shared.models import AgentFailure, AgentResult, AlertContext, Finding


# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

SIGNING_SECRET = "integration_test_secret_xyz"
SLACK_BOT_TOKEN = "xoxb-integration-test-token"
DEDUP_TABLE = "integration-dedup-table"
AGENT_ENDPOINT = "INTEGRATIONAGENT"


def _compute_signature(secret: str, timestamp: str, body: str) -> str:
    """Compute a valid Slack HMAC-SHA256 signature."""
    sig_basestring = f"v0:{timestamp}:{body}"
    h = hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return f"v0={h}"


def _build_lambda_event(
    body_dict: dict,
    *,
    signing_secret: str = SIGNING_SECRET,
    timestamp: str | None = None,
    base64_encode: bool = False,
) -> dict:
    """Build a Lambda function URL event with valid Slack headers."""
    if timestamp is None:
        timestamp = str(int(time.time()))
    raw_body = json.dumps(body_dict)
    signature = _compute_signature(signing_secret, timestamp, raw_body)

    body_value = raw_body
    is_base64 = False
    if base64_encode:
        body_value = base64.b64encode(raw_body.encode()).decode()
        is_base64 = True

    return {
        "headers": {
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
        },
        "body": body_value,
        "isBase64Encoded": is_base64,
    }


def _slack_event_payload(
    channel: str = "C_INTEG_001",
    ts: str = "1700000000.000200",
    text: str = "ALERT: Integration test — pod crash loop detected",
) -> dict:
    """Return a Slack Events API event_callback payload."""
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": channel,
            "ts": ts,
            "text": text,
            "event_ts": ts,
        },
    }


def _create_dedup_table() -> None:
    """Create the DynamoDB dedup table inside an active moto context."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=DEDUP_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Set required environment variables for every integration test."""
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
# Fake collaborators (reused across integration tests)
# ---------------------------------------------------------------------------


class FakeHTTPClient:
    """Controllable fake HTTP client for A2A calls."""

    def __init__(
        self,
        responses: dict[str, dict] | None = None,
        delay: float = 0.0,
    ):
        self.responses = responses or {}
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def post_json(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        if self.delay > 0:
            await asyncio.sleep(self.delay)
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


def _default_a2a_response(request: dict) -> dict:
    """Build a default successful A2A JSON-RPC 2.0 response."""
    return {
        "jsonrpc": "2.0",
        "id": request.get("id", "unknown"),
        "result": {
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": "Agent analysis complete."}],
                "messageId": "resp-integ-001",
            }
        },
    }


def _make_alert_context(**overrides) -> AlertContext:
    """Create an AlertContext with sensible defaults, overridable per-field."""
    defaults = dict(
        investigation_id="inv-integ-001",
        platform="slack",
        channel_id="C_INTEG_001",
        message_id="1700000000.000200",
        alert_text="ALERT: Integration test — pod crash loop detected",
        alert_timestamp="2025-01-15T14:32:00+00:00",
        investigation_window=(
            "2025-01-15T14:27:00+00:00",
            "2025-01-15T14:37:00+00:00",
        ),
        platform_metadata={"thread_ts": "1700000000.000200"},
    )
    defaults.update(overrides)
    # Keep platform_metadata in sync with message_id if overridden
    if "message_id" in overrides and "platform_metadata" not in overrides:
        defaults["platform_metadata"] = {"thread_ts": overrides["message_id"]}
    return AlertContext(**defaults)  # type: ignore[arg-type]


def _find_report_msg(messages: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    """Find the Incident Report among a poster's messages.

    The orchestrator now posts an "Investigation Started" announcement before
    fan-out, so the Incident Report is no longer guaranteed to be at index 0.
    """
    for msg in messages:
        if "Incident Report" in msg[2]:
            return msg
    raise AssertionError(f"No Incident Report found in {len(messages)} messages")


def _enrichment_msgs(messages: list[tuple[str, str, str]]) -> list[str]:
    return [
        text for _, _, text in messages
        if "Enrichment Update" in text or "Late Result" in text
    ]


def _make_orchestrator(
    http_client: AsyncHTTPClient | None = None,
    slack_poster: ChatPoster | None = None,
    initial_deadline: float = 0.1,
    hard_cutoff: float = 0.5,
) -> InvestigationOrchestrator:
    """Create an orchestrator with short timeouts for fast integration tests."""
    orch = InvestigationOrchestrator(
        http_client=http_client or FakeHTTPClient(),
        chat_poster=slack_poster or FakeSlackPoster(),
        report_formatter=ReportFormatter(),
        agent_endpoints={
            "slack_scanner": "http://localhost:9001",
            "cloudwatch_logs": "http://localhost:9003",
            "eks": "http://localhost:9004",
        },
    )
    orch.INITIAL_DEADLINE_SECONDS = initial_deadline
    orch.HARD_CUTOFF_SECONDS = hard_cutoff
    return orch


# ===========================================================================
# 1. Lambda end-to-end integration tests
# ===========================================================================


class TestLambdaEndToEnd:
    """End-to-end Lambda handler: signature → dedup → invoke → 200.

    Uses moto for real DynamoDB and mocks for AgentCore.
    Requirements: 1.1, 1.2, 1.3, 1.4, 1.5
    """

    @mock_aws
    def test_valid_event_writes_dedup_and_invokes_agent(self):
        """A valid Slack event writes a dedup record and invokes the Master Agent."""
        _create_dedup_table()
        payload = _slack_event_payload()
        event = _build_lambda_event(payload)

        with patch("lambda_adapter.intake.boto3.client") as mock_boto_client:
            mock_runtime = MagicMock()
            mock_boto_client.return_value = mock_runtime

            result = lambda_handler(event, None)

        # HTTP 200 returned
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body.get("ok") is True

        # DynamoDB dedup record written
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.Table(DEDUP_TABLE)
        resp = table.get_item(Key={"pk": "slack#C_INTEG_001#1700000000.000200"})
        item = resp["Item"]  # type: ignore[typeddict-item]
        assert item["status"] == "IN_PROGRESS"
        assert "investigation_id" in item

        # Master Agent invoked
        mock_runtime.invoke_agent_runtime.assert_called_once()
        agent_kw = mock_runtime.invoke_agent_runtime.call_args[1]
        assert agent_kw["agentRuntimeArn"] == AGENT_ENDPOINT

    @mock_aws
    def test_invalid_signature_returns_401_no_side_effects(self):
        """An invalid signature returns 401 with no DynamoDB write or agent call."""
        _create_dedup_table()
        payload = _slack_event_payload()
        event = _build_lambda_event(payload)
        event["headers"]["x-slack-signature"] = "v0=tampered"

        with patch("lambda_adapter.intake.boto3.client") as mock_boto_client:
            mock_runtime = MagicMock()
            mock_boto_client.return_value = mock_runtime

            result = lambda_handler(event, None)

        assert result["statusCode"] == 401

        # No DynamoDB record
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.Table(DEDUP_TABLE)
        resp = table.scan()
        assert resp["Count"] == 0

        # No agent invocation
        mock_runtime.invoke_agent_runtime.assert_not_called()

    @mock_aws
    def test_duplicate_event_skips_invocation(self):
        """A duplicate event returns 200 but does not invoke the master agent again."""
        _create_dedup_table()
        payload = _slack_event_payload()

        with patch("lambda_adapter.intake.boto3.client") as mock_boto_client:
            mock_runtime = MagicMock()
            mock_boto_client.return_value = mock_runtime

            # First call — new alert
            event1 = _build_lambda_event(payload)
            r1 = lambda_handler(event1, None)
            assert r1["statusCode"] == 200

            # Second call — duplicate
            event2 = _build_lambda_event(payload)
            r2 = lambda_handler(event2, None)
            assert r2["statusCode"] == 200
            assert json.loads(r2["body"]) == {}

        # Invoke happened exactly once
        assert mock_runtime.invoke_agent_runtime.call_count == 1

    @mock_aws
    def test_alert_context_passed_to_agent(self):
        """The alert context JSON sent to the Master Agent contains the right fields."""
        _create_dedup_table()
        payload = _slack_event_payload(
            channel="C_CTX_TEST",
            ts="1700000001.000300",
            text="ALERT: disk full on db-primary",
        )
        event = _build_lambda_event(payload)

        with patch("lambda_adapter.intake.boto3.client") as mock_boto_client:
            mock_runtime = MagicMock()
            mock_boto_client.return_value = mock_runtime

            lambda_handler(event, None)

        agent_kw = mock_runtime.invoke_agent_runtime.call_args[1]
        envelope = json.loads(agent_kw["payload"].decode("utf-8"))
        assert envelope["jsonrpc"] == "2.0"
        assert envelope["method"] == "message/send"
        text = envelope["params"]["message"]["parts"][0]["text"]
        ctx = json.loads(text)
        assert ctx["channel_id"] == "C_CTX_TEST"
        assert ctx["message_id"] == "1700000001.000300"
        assert ctx["alert_text"] == "ALERT: disk full on db-primary"
        assert "investigation_window" in ctx


# ===========================================================================
# 2. A2A communication integration tests
# ===========================================================================


class TestA2ACommunication:
    """Test A2A JSON-RPC 2.0 request/response between orchestrator and agents.

    Requirements: 3.1
    """

    @pytest.mark.asyncio
    async def test_orchestrator_sends_a2a_to_all_agents(self):
        """The orchestrator sends a JSON-RPC 2.0 message/send to every agent."""
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client)
        ctx = _make_alert_context()

        await orch.investigate(ctx)

        called_urls = {url for url, _ in http_client.calls}
        assert called_urls == {
            "http://localhost:9001",
            "http://localhost:9003",
            "http://localhost:9004",
        }

        # Every payload is valid JSON-RPC 2.0
        for _, payload in http_client.calls:
            assert payload["jsonrpc"] == "2.0"
            assert payload["method"] == "message/send"
            assert payload["params"]["message"]["role"] == "user"
            parts = payload["params"]["message"]["parts"]
            assert len(parts) == 1
            assert parts[0]["kind"] == "text"
            # The text part should be a JSON-serialized AlertContext
            inner = json.loads(parts[0]["text"])
            assert inner["investigation_id"] == ctx.investigation_id

    @pytest.mark.asyncio
    async def test_a2a_success_response_parsed_correctly(self):
        """A successful A2A response is parsed into an AgentResult with status success."""
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client)
        ctx = _make_alert_context()

        result = await orch.invoke_agent("cloudwatch_logs", ctx)

        assert isinstance(result, AgentResult)
        assert result.agent_name == "cloudwatch_logs"
        assert result.status == "success"
        assert result.duration_seconds > 0

    @pytest.mark.asyncio
    async def test_a2a_error_response_parsed_correctly(self):
        """A JSON-RPC error response is parsed into an AgentResult with status error."""
        error_resp = {
            "jsonrpc": "2.0",
            "id": "req-eks-inv-integ-001",
            "error": {"code": -32000, "message": "EKS cluster unreachable"},
        }
        http_client = FakeHTTPClient(
            responses={"http://localhost:9004": error_resp}
        )
        orch = _make_orchestrator(http_client=http_client)
        ctx = _make_alert_context()

        result = await orch.invoke_agent("eks", ctx)

        assert result.status == "error"
        assert result.error_message is not None
        assert "EKS cluster unreachable" in result.error_message

    @pytest.mark.asyncio
    async def test_a2a_request_ids_contain_agent_and_investigation(self):
        """Each A2A request id encodes the agent name and investigation id."""
        http_client = FakeHTTPClient()
        orch = _make_orchestrator(http_client=http_client)
        ctx = _make_alert_context(investigation_id="inv-id-check")

        await orch.investigate(ctx)

        for _, payload in http_client.calls:
            req_id = payload["id"]
            assert req_id.startswith("req-")
            assert "inv-id-check" in req_id

    @pytest.mark.asyncio
    async def test_a2a_network_failure_returns_error_result(self):
        """A network failure during A2A call returns an error AgentResult, not an exception."""

        class FailingHTTPClient:
            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                raise ConnectionError("Connection refused")

        http_client = FailingHTTPClient()
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client, slack_poster=slack_poster
        )
        ctx = _make_alert_context()

        # Should not raise — errors are captured
        await orch.investigate(ctx)

        # Report still posted (after the "Investigation Started" notice)
        _, _, report = _find_report_msg(slack_poster.messages)
        assert "Incident Report" in report


# ===========================================================================
# 3. Slack thread reply integration tests
# ===========================================================================


class TestSlackThreadReply:
    """Test that Slack thread replies use the correct channel_id and thread_ts.

    Requirements: 1.3, 3.2, 3.5
    """

    @pytest.mark.asyncio
    async def test_initial_report_uses_correct_thread_ts(self):
        """The initial Incident Report is posted to the right channel and thread."""
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(slack_poster=slack_poster)
        ctx = _make_alert_context(
            channel_id="C_THREAD_TEST",
            message_id="1700099999.000500",
        )

        await orch.investigate(ctx)

        channel, thread_ts, text = _find_report_msg(slack_poster.messages)
        assert channel == "C_THREAD_TEST"
        assert thread_ts == "1700099999.000500"
        assert "Incident Report" in text

    @pytest.mark.asyncio
    async def test_enrichment_update_uses_same_thread_ts(self):
        """Enrichment updates are posted to the same thread as the initial report."""

        class SlowOneAgent:
            """One agent responds after the initial deadline."""

            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                if url == "http://localhost:9004":  # EKS is slow
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
        ctx = _make_alert_context(
            channel_id="C_ENRICH",
            message_id="1700088888.000600",
        )

        await orch.investigate(ctx)

        # All messages (initial report + enrichment) share the same thread
        for channel, thread_ts, _ in slack_poster.messages:
            assert channel == "C_ENRICH"
            assert thread_ts == "1700088888.000600"

        # At least one enrichment update
        enrichment_msgs = [
            text for _, _, text in slack_poster.messages if "Enrichment Update" in text
        ]
        assert len(enrichment_msgs) >= 1

    @pytest.mark.asyncio
    async def test_slack_retry_on_transient_failure(self):
        """Chat posting retries on transient failure and eventually succeeds."""
        call_count = 0

        class RetryPoster:
            def __init__(self):
                self.messages = []

            async def post_reply(self, alert_context, text):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("Chat API transient error")
                thread_ts = alert_context.platform_metadata.get("thread_ts", alert_context.message_id)
                self.messages.append((alert_context.channel_id, thread_ts, text))

        poster = RetryPoster()
        ctx = _make_alert_context(
            channel_id="C_RETRY",
            message_id="1700000000.000700",
        )
        await chat_post_with_retry(
            poster,
            ctx,
            "Test report",
            base_delay=0.0,
        )

        assert call_count == 2
        assert len(poster.messages) == 1
        assert poster.messages[0] == ("C_RETRY", "1700000000.000700", "Test report")

# ===========================================================================
# 4. Deadline and cutoff integration tests
# ===========================================================================


class TestDeadlineAndCutoff:
    """Test 60-second deadline and 5-minute cutoff using short timeouts.

    Requirements: 3.2, 3.6
    """

    @pytest.mark.asyncio
    async def test_initial_report_posted_at_deadline(self):
        """The initial report is posted once the initial deadline elapses."""
        slack_poster = FakeSlackPoster()
        # All agents are slow — none respond before the deadline
        http_client = FakeHTTPClient(delay=0.3)
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )
        ctx = _make_alert_context()

        await orch.investigate(ctx)

        # Report posted even though no agents responded in time. Agents that
        # haven't responded by the deadline are reported as ⏳ pending, not
        # ⚠️ failed (a late response can still arrive before hard cutoff).
        _, _, report = _find_report_msg(slack_poster.messages)
        assert "Incident Report" in report
        assert "still investigating" in report.lower() or "⏳" in report

    @pytest.mark.asyncio
    async def test_hard_cutoff_terminates_within_expected_time(self):
        """Investigation terminates near the hard cutoff, not waiting for slow agents."""
        http_client = FakeHTTPClient(delay=10.0)  # Much longer than cutoff
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.2,
        )
        ctx = _make_alert_context()

        start = time.monotonic()
        await orch.investigate(ctx)
        elapsed = time.monotonic() - start

        # Should finish near the hard cutoff, not wait for 10s agents
        assert elapsed < 1.0

    @pytest.mark.asyncio
    async def test_fast_agents_in_initial_report_slow_agents_in_enrichment(self):
        """Fast agents appear in the initial report; slow agents trigger enrichment."""

        class MixedSpeedHTTPClient:
            def __init__(self):
                self.calls = []

            async def post_json(self, url: str, payload: dict) -> dict:
                self.calls.append((url, payload))
                # Slack scanner is fast
                # CloudWatch and EKS are slow (after initial deadline)
                if url in ("http://localhost:9003", "http://localhost:9004"):
                    await asyncio.sleep(0.15)
                return _default_a2a_response(payload)

        http_client = MixedSpeedHTTPClient()
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )
        ctx = _make_alert_context()

        await orch.investigate(ctx)

        # Initial report posted (after the started notice)
        _, _, initial_report = _find_report_msg(slack_poster.messages)
        assert "Incident Report" in initial_report

        # Enrichment updates for late agents
        assert len(_enrichment_msgs(slack_poster.messages)) >= 1

    @pytest.mark.asyncio
    async def test_agents_responding_after_cutoff_are_ignored(self):
        """Agents that respond after the hard cutoff do not trigger enrichment updates."""
        http_client = FakeHTTPClient(delay=10.0)  # Way past cutoff
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.15,
        )
        ctx = _make_alert_context()

        await orch.investigate(ctx)

        # Two messages expected: "Investigation Started" + Incident Report.
        # No enrichment updates because no agent responded before the cutoff.
        assert len(slack_poster.messages) == 2
        assert "Investigation Started" in slack_poster.messages[0][2]
        assert "Incident Report" in slack_poster.messages[1][2]
        assert _enrichment_msgs(slack_poster.messages) == []

    @pytest.mark.asyncio
    async def test_all_agents_fast_no_enrichment_updates(self):
        """When all agents respond before the deadline, no enrichment updates are posted."""
        http_client = FakeHTTPClient(delay=0.0)  # Instant responses
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.1,
            hard_cutoff=0.5,
        )
        ctx = _make_alert_context()

        await orch.investigate(ctx)

        # Two messages: "Investigation Started" + Incident Report. Every agent
        # responded by the deadline so no enrichment updates fire.
        assert len(slack_poster.messages) == 2
        assert "Investigation Started" in slack_poster.messages[0][2]
        assert "Incident Report" in slack_poster.messages[1][2]
        assert _enrichment_msgs(slack_poster.messages) == []

    @pytest.mark.asyncio
    async def test_pending_notices_for_unresponsive_agents_at_deadline(self):
        """Agents that haven't responded by the deadline are flagged as ⏳ pending.

        Hard-failure notices (⚠️) are reserved for agents that errored or were
        otherwise dispatched-but-broken; an agent that's simply slow can still
        post an enrichment update later.
        """
        http_client = FakeHTTPClient(delay=0.3)  # All agents slow
        slack_poster = FakeSlackPoster()
        orch = _make_orchestrator(
            http_client=http_client,
            slack_poster=slack_poster,
            initial_deadline=0.05,
            hard_cutoff=0.5,
        )
        ctx = _make_alert_context()

        await orch.investigate(ctx)

        _, _, report = _find_report_msg(slack_poster.messages)
        assert report.count("⏳") >= 3
