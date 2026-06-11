"""Tests for the MasterDispatch seam.

`AgentCoreMasterDispatch` owns the task -> A2A-envelope -> session-id -> ARN
mapping that intake tests previously asserted against boto3 directly. These
tests pin that mapping against a fake ``bedrock-agentcore`` client; the intake
tests now assert on dispatched tasks via :class:`RecordingMasterDispatch`.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from lambda_adapter.master_dispatch import AgentCoreMasterDispatch
from shared.models import AlertContext, CommandRequest


def _alert(**overrides) -> AlertContext:
    defaults = dict(
        investigation_id="inv-1",
        platform="slack",
        channel_id="C1",
        message_id="1700000000.0001",
        alert_text="ALERT",
        alert_timestamp="2025-01-15T14:32:00+00:00",
        investigation_window=("2025-01-15T14:27:00+00:00", "2025-01-15T14:37:00+00:00"),
    )
    defaults.update(overrides)
    return AlertContext(**defaults)  # type: ignore[arg-type]


def _command(**overrides) -> CommandRequest:
    defaults = dict(
        platform="slack", command="/sre-snapshot", text="", channel_id="C1",
        user_id="U1", thread_ts=None, response_url="",
    )
    defaults.update(overrides)
    return CommandRequest(**defaults)  # type: ignore[arg-type]


def _text(call) -> dict:
    envelope = json.loads(call.kwargs["payload"].decode("utf-8"))
    return json.loads(envelope["params"]["message"]["parts"][0]["text"])


@pytest.fixture
def client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def dispatch(client: MagicMock) -> AgentCoreMasterDispatch:
    return AgentCoreMasterDispatch(client=client)


class TestInvestigate:
    def test_resolves_env_arn_and_session_is_investigation_id(self, client, dispatch, monkeypatch):
        monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "MASTER")
        dispatch.investigate(_alert(investigation_id="inv-xyz"))
        kw = client.invoke_agent_runtime.call_args.kwargs
        assert kw["agentRuntimeArn"] == "MASTER"
        assert kw["runtimeSessionId"] == "inv-xyz"
        assert kw["contentType"] == "application/json"

    def test_envelope_is_jsonrpc_carrying_serialized_alert_context(self, client, dispatch, monkeypatch):
        monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "MASTER")
        dispatch.investigate(_alert(channel_id="C-CTX", message_id="ts-1"))
        envelope = json.loads(client.invoke_agent_runtime.call_args.kwargs["payload"].decode("utf-8"))
        assert envelope["jsonrpc"] == "2.0"
        assert envelope["method"] == "message/send"
        ctx = _text(client.invoke_agent_runtime.call_args)
        assert ctx["channel_id"] == "C-CTX"
        assert ctx["message_id"] == "ts-1"

    def test_variant_arn_override_and_session_suffix(self, client, dispatch):
        dispatch.investigate(_alert(investigation_id="inv-9", variant_id="b"), master_arn="AGENT_B")
        kw = client.invoke_agent_runtime.call_args.kwargs
        assert kw["agentRuntimeArn"] == "AGENT_B"
        assert kw["runtimeSessionId"] == "inv-9-b"

    def test_missing_arn_raises(self, dispatch, monkeypatch):
        monkeypatch.delenv("MASTER_AGENT_RUNTIME_ARN", raising=False)
        with pytest.raises(EnvironmentError):
            dispatch.investigate(_alert())


class TestPostmortem:
    def test_pir_payload_and_session(self, client, dispatch, monkeypatch):
        monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "MASTER")
        dispatch.postmortem(
            _command(command="/postmortem", channel_id="C2", thread_ts="t-9", text="notes")
        )
        kw = client.invoke_agent_runtime.call_args.kwargs
        assert kw["runtimeSessionId"] == "pir-C2-t-9"
        payload = _text(client.invoke_agent_runtime.call_args)
        assert payload["task"] == "pir"
        assert payload["channel_id"] == "C2"
        assert payload["thread_ts"] == "t-9"
        assert payload["command_text"] == "notes"


class TestStatus:
    def test_snapshot_payload_and_session(self, client, dispatch, monkeypatch):
        monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "MASTER")
        dispatch.status(_command(channel_id="C3", user_id="U3"), "2026-01-01T00:00:00+00:00")
        kw = client.invoke_agent_runtime.call_args.kwargs
        assert kw["runtimeSessionId"] == "snapshot-C3-2026-01-01T00:00:00+00:00"
        payload = _text(client.invoke_agent_runtime.call_args)
        assert payload["task"] == "snapshot"
        assert payload["platform"] == "slack"
        assert payload["channel_id"] == "C3"
        assert payload["user_id"] == "U3"
        assert payload["requested_at"] == "2026-01-01T00:00:00+00:00"
        assert "thread_ts" not in payload


def test_boto3_client_is_lazy() -> None:
    """Constructing the adapter must not build a boto3 client (free default)."""
    assert AgentCoreMasterDispatch()._client is None
