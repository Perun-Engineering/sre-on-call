"""Unit tests for the A2A round-trip seam (:mod:`shared.a2a_client`).

The seam owns the wire format: build the A2A envelope, POST it via an
injected transport, surface a JSON-RPC error as a value, extract the
canonical reply text across Strands' three response shapes, and parse the
one caller-supplied footer. It knows nothing about ``AgentResult`` /
``SnapshotReport`` *semantics* — the caller picks the footer and maps the
reply to its own domain type.
"""

from __future__ import annotations

import json

from shared.a2a_client import A2AClient
from shared.tool_result import AGENT_RESULT, SNAPSHOT_RESULT, format_result
from shared.models import AgentResult


class _FakeTransport:
    """Records calls and returns a canned (or per-endpoint) response."""

    def __init__(self, response: dict | None = None, by_endpoint: dict | None = None):
        self._response = response
        self._by_endpoint = by_endpoint or {}
        self.calls: list[tuple[str, dict]] = []

    async def post_json(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        if url in self._by_endpoint:
            return self._by_endpoint[url]
        return self._response or {}


def _wrapped(text: str, *, request_id: str = "req-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": text}],
                "messageId": "resp-1",
            }
        },
    }


async def test_send_parses_footer_and_strips_it_from_text():
    """Tracer: send() returns the footer-stripped text and the parsed payload."""
    agent_result = AgentResult(
        agent_name="eks", status="success", findings=[], summary="Cluster healthy.",
    )
    transport = _FakeTransport(_wrapped(format_result(agent_result)))
    client = A2AClient(transport)

    reply = await client.send(
        "http://localhost:9005", '{"alert":"x"}',
        footer=AGENT_RESULT, request_id="req-eks-1",
    )

    assert reply.error is None
    assert reply.payload is not None
    assert reply.payload.agent_name == "eks"
    assert reply.payload.status == "success"
    # The footer marker must not leak into the cleaned text.
    assert "AGENT_RESULT" not in reply.text
    assert "Cluster healthy." in reply.text


async def test_send_surfaces_jsonrpc_error_as_value():
    """A JSON-RPC error becomes reply.error — not an exception — with no payload."""
    transport = _FakeTransport({
        "jsonrpc": "2.0",
        "id": "req-eks-1",
        "error": {"code": -32000, "message": "EKS cluster unreachable"},
    })
    client = A2AClient(transport)

    reply = await client.send(
        "http://localhost:9005", "{}", footer=AGENT_RESULT, request_id="req-eks-1",
    )

    assert reply.error == "EKS cluster unreachable"
    assert reply.payload is None
    assert reply.text == ""


async def test_send_extracts_inline_parts_shape():
    """Strands can return text directly under result.parts (no message wrapper)."""
    transport = _FakeTransport({
        "jsonrpc": "2.0",
        "result": {"parts": [{"kind": "text", "text": "inline reply"}]},
    })
    client = A2AClient(transport)

    reply = await client.send("u", "{}", footer=AGENT_RESULT, request_id="r")

    assert reply.error is None
    assert reply.payload is None  # no footer present
    assert reply.text == "inline reply"


async def test_send_extracts_named_task_artifact():
    """Tool-driven agents reply as a Task; the canonical text is the agent_response artifact."""
    transport = _FakeTransport({
        "jsonrpc": "2.0",
        "result": {
            "kind": "task",
            "status": {"state": "completed"},
            "artifacts": [
                {"name": "trace", "parts": [{"kind": "text", "text": "trace data"}]},
                {"name": "agent_response", "parts": [{"kind": "text", "text": "the answer"}]},
            ],
            # Streaming chunks must NOT leak into the reply text.
            "history": [{"role": "agent", "parts": [{"kind": "text", "text": "thinking"}]}],
        },
    })
    client = A2AClient(transport)

    reply = await client.send("u", "{}", footer=AGENT_RESULT, request_id="r")

    assert reply.text == "the answer"
    assert "history" not in reply.text and "trace data" not in reply.text


async def test_send_strips_only_the_requested_footer():
    """send() peels its own footer; other footers (AGENT_METADATA) stay for the caller."""
    from shared.agent_telemetry import AGENT_METADATA
    from shared.models import AgentMetadata

    meta_footer = AGENT_METADATA.encode(AgentMetadata(input_tokens=100, output_tokens=50))
    agent_result = AgentResult(agent_name="eks", status="success", findings=[], summary="ok")
    body = f"{format_result(agent_result)}\n\n{meta_footer}"
    transport = _FakeTransport(_wrapped(body))
    client = A2AClient(transport)

    reply = await client.send("u", "{}", footer=AGENT_RESULT, request_id="r")

    assert reply.payload is not None and reply.payload.agent_name == "eks"
    # AGENT_RESULT stripped; AGENT_METADATA left intact for the alert orchestrator.
    assert "AGENT_RESULT" not in reply.text
    assert AGENT_METADATA.find(reply.text) is not None


async def test_send_drops_malformed_footer_but_keeps_text():
    """A malformed footer yields payload=None while the marker is still stripped."""
    body = "Some summary. <<<AGENT_RESULT {not valid json} AGENT_RESULT>>>"
    transport = _FakeTransport(_wrapped(body))
    client = A2AClient(transport)

    reply = await client.send("u", "{}", footer=AGENT_RESULT, request_id="r")

    assert reply.payload is None
    assert "AGENT_RESULT" not in reply.text
    assert "Some summary." in reply.text


async def test_send_parses_snapshot_footer():
    """The status path's SNAPSHOT_RESULT footer parses through the same seam."""
    from shared.models import SnapshotReport, SnapshotSection
    from shared.tool_result import format_snapshot_result

    report = SnapshotReport(
        agent_name="slack_scanner",
        captured_at="2026-05-29T00:00:00Z",
        sections=[SnapshotSection(label="Auth", lines=["ok"])],
        anomaly=False,
    )
    transport = _FakeTransport(_wrapped(format_snapshot_result(report)))
    client = A2AClient(transport)

    reply = await client.send("u", "{}", footer=SNAPSHOT_RESULT, request_id="r")

    assert reply.payload is not None
    assert reply.payload.agent_name == "slack_scanner"
    assert reply.payload.anomaly is False


async def test_send_propagates_transport_failure():
    """Network/transport errors are NOT swallowed — they raise out of send()."""
    import pytest

    class _Failing:
        async def post_json(self, url, payload):
            raise ConnectionError("connection refused")

    client = A2AClient(_Failing())

    with pytest.raises(ConnectionError):
        await client.send("u", "{}", footer=AGENT_RESULT, request_id="r")


async def test_send_builds_request_with_caller_id_and_text():
    """The seam builds an A2A message/send envelope carrying the caller's id + text."""
    transport = _FakeTransport(_wrapped("ok"))
    client = A2AClient(transport)

    await client.send("http://ep", '{"alert":"x"}', footer=AGENT_RESULT, request_id="req-42")

    assert len(transport.calls) == 1
    url, payload = transport.calls[0]
    assert url == "http://ep"
    assert payload["id"] == "req-42"
    assert payload["method"] == "message/send"
    assert payload["params"]["message"]["parts"][0]["text"] == '{"alert":"x"}'


# ---------------------------------------------------------------------------
# Transport adapters (moved here from test_orchestrator.py — these classes
# now live in shared.a2a_client).
# ---------------------------------------------------------------------------


class TestAgentCoreClient:
    """AgentCoreClient invokes Bedrock AgentCore runtime ARNs via boto3."""

    async def test_invokes_runtime_with_arn_and_encoded_payload(self):
        from unittest.mock import MagicMock

        from shared.a2a_client import AgentCoreClient

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
        assert json.loads(kwargs["payload"].decode("utf-8")) == {
            "jsonrpc": "2.0",
            "method": "ping",
        }
        assert result == {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}

    async def test_decodes_streaming_body_response(self):
        from unittest.mock import MagicMock

        from shared.a2a_client import AgentCoreClient

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

    async def test_decodes_str_response(self):
        from unittest.mock import MagicMock

        from shared.a2a_client import AgentCoreClient

        fake_boto = MagicMock()
        fake_boto.invoke_agent_runtime.return_value = {
            "response": '{"result": "as-string"}',
        }
        client = AgentCoreClient(client=fake_boto)

        result = await client.post_json("arn:aws:bedrock-agentcore:us-east-1:0:r/x", {})

        assert result == {"result": "as-string"}
