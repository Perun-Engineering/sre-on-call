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

from lambda_adapter.master_dispatch import (
    DISPATCH_EVENT_KEY,
    AgentCoreMasterDispatch,
    AsyncMasterDispatch,
    DispatchedTask,
    run_dispatched_task,
)
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


# ---------------------------------------------------------------------------
# DispatchedTask serialization — the async self-invoke payload round-trips.
# ---------------------------------------------------------------------------


class TestDispatchedTaskRoundTrip:
    def test_investigate_round_trips_alert_and_window_tuple(self) -> None:
        task = DispatchedTask(
            "investigate", alert_context=_alert(investigation_id="inv-rt"), master_arn="ARN_B"
        )
        restored = DispatchedTask.from_event(json.loads(json.dumps(task.to_event())))
        assert restored.kind == "investigate"
        assert restored.master_arn == "ARN_B"
        assert restored.command is None
        assert restored.alert_context is not None
        assert restored.alert_context.investigation_id == "inv-rt"
        # JSON has no tuples; from_event must restore the (start, end) tuple shape.
        assert restored.alert_context.investigation_window == (
            "2025-01-15T14:27:00+00:00",
            "2025-01-15T14:37:00+00:00",
        )

    def test_status_round_trips_command_and_requested_at(self) -> None:
        task = DispatchedTask(
            "status", command=_command(channel_id="C9"), requested_at="2026-01-01T00:00:00+00:00"
        )
        restored = DispatchedTask.from_event(json.loads(json.dumps(task.to_event())))
        assert restored.kind == "status"
        assert restored.alert_context is None
        assert restored.command is not None
        assert restored.command.channel_id == "C9"
        assert restored.requested_at == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# AsyncMasterDispatch — defers the blocking master invoke to a fire-and-forget
# Lambda self-invocation so the webhook returns within Slack's 3s deadline.
# ---------------------------------------------------------------------------


def _enqueued_task(lambda_client: MagicMock) -> DispatchedTask:
    kw = lambda_client.invoke.call_args.kwargs
    assert kw["InvocationType"] == "Event"
    payload = json.loads(kw["Payload"].decode("utf-8"))
    return DispatchedTask.from_event(payload[DISPATCH_EVENT_KEY])


class TestAsyncMasterDispatch:
    @pytest.fixture
    def lambda_client(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def async_dispatch(self, lambda_client: MagicMock) -> AsyncMasterDispatch:
        return AsyncMasterDispatch(client=lambda_client, function_name="self-fn")

    def test_investigate_enqueues_event_invoke(self, lambda_client, async_dispatch) -> None:
        async_dispatch.investigate(_alert(investigation_id="inv-a"), master_arn="ARN_A")
        kw = lambda_client.invoke.call_args.kwargs
        assert kw["FunctionName"] == "self-fn"
        assert kw["InvocationType"] == "Event"
        task = _enqueued_task(lambda_client)
        assert task.kind == "investigate"
        assert task.master_arn == "ARN_A"
        assert task.alert_context is not None
        assert task.alert_context.investigation_id == "inv-a"

    def test_status_enqueues_event_invoke(self, lambda_client, async_dispatch) -> None:
        async_dispatch.status(_command(channel_id="C3"), "2026-01-01T00:00:00+00:00")
        task = _enqueued_task(lambda_client)
        assert task.kind == "status"
        assert task.requested_at == "2026-01-01T00:00:00+00:00"
        assert task.command is not None and task.command.channel_id == "C3"

    def test_postmortem_enqueues_event_invoke(self, lambda_client, async_dispatch) -> None:
        async_dispatch.postmortem(_command(command="/postmortem", thread_ts="t-1"))
        task = _enqueued_task(lambda_client)
        assert task.kind == "postmortem"
        assert task.command is not None and task.command.thread_ts == "t-1"

    def test_target_resolves_from_env_when_unset(self, lambda_client, monkeypatch) -> None:
        monkeypatch.delenv("SELF_INVOKE_TARGET", raising=False)
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn-from-env")
        AsyncMasterDispatch(client=lambda_client).status(_command(), "2026-01-01T00:00:00+00:00")
        assert lambda_client.invoke.call_args.kwargs["FunctionName"] == "fn-from-env"

    def test_self_invoke_target_env_takes_precedence(self, lambda_client, monkeypatch) -> None:
        monkeypatch.setenv("SELF_INVOKE_TARGET", "fn-alias:live")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn-from-env")
        AsyncMasterDispatch(client=lambda_client).status(_command(), "2026-01-01T00:00:00+00:00")
        assert lambda_client.invoke.call_args.kwargs["FunctionName"] == "fn-alias:live"

    def test_missing_target_raises(self, lambda_client, monkeypatch) -> None:
        monkeypatch.delenv("SELF_INVOKE_TARGET", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        with pytest.raises(EnvironmentError):
            AsyncMasterDispatch(client=lambda_client).status(_command(), "2026-01-01T00:00:00+00:00")

    def test_boto3_client_is_lazy(self) -> None:
        assert AsyncMasterDispatch()._client is None


# ---------------------------------------------------------------------------
# run_dispatched_task — the async (Event) worker replays a serialized task
# through the synchronous AgentCoreMasterDispatch (off the Slack deadline).
# ---------------------------------------------------------------------------


class TestRunDispatchedTask:
    def test_replays_investigate(self) -> None:
        inner = MagicMock()
        task = DispatchedTask("investigate", alert_context=_alert(investigation_id="inv-w"), master_arn="ARN_W")
        run_dispatched_task({DISPATCH_EVENT_KEY: task.to_event()}, dispatch=inner)
        inner.investigate.assert_called_once()
        ctx = inner.investigate.call_args.args[0]
        assert ctx.investigation_id == "inv-w"
        assert inner.investigate.call_args.kwargs["master_arn"] == "ARN_W"

    def test_replays_status(self) -> None:
        inner = MagicMock()
        task = DispatchedTask("status", command=_command(channel_id="C5"), requested_at="2026-02-02T00:00:00+00:00")
        run_dispatched_task({DISPATCH_EVENT_KEY: task.to_event()}, dispatch=inner)
        inner.status.assert_called_once()
        assert inner.status.call_args.args[0].channel_id == "C5"
        assert inner.status.call_args.args[1] == "2026-02-02T00:00:00+00:00"

    def test_replays_postmortem(self) -> None:
        inner = MagicMock()
        task = DispatchedTask("postmortem", command=_command(command="/postmortem", thread_ts="t-9"))
        run_dispatched_task({DISPATCH_EVENT_KEY: task.to_event()}, dispatch=inner)
        inner.postmortem.assert_called_once()
        assert inner.postmortem.call_args.args[0].thread_ts == "t-9"

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError):
            run_dispatched_task({DISPATCH_EVENT_KEY: {"kind": "bogus"}}, dispatch=MagicMock())
