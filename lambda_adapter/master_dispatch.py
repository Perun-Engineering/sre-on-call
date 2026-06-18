"""MasterDispatch — the seam for firing one fire-and-forget task at the
Master Agent runtime from the Lambda intake pipeline.

A typed Protocol with one method per task kind. Each method owns its
``runtimeSessionId`` convention, its A2A envelope build, and the
``MASTER_AGENT_RUNTIME_ARN`` resolution — the knowledge that was previously
open-coded at four ``invoke_agent_runtime`` call sites in ``intake.py``.

Three adapters: :class:`AsyncMasterDispatch` (production default — defers the
blocking master invoke to a fire-and-forget Lambda self-invocation so the
webhook returns within Slack's 3-second slash-command deadline),
:class:`AgentCoreMasterDispatch` (synchronous
``bedrock-agentcore.invoke_agent_runtime``; the response is discarded — the
master posts its own "Investigation Started" notice; used by the async worker)
and :class:`RecordingMasterDispatch` (tests capture dispatched tasks as values).

Distinct from :class:`shared.a2a_client.A2AClient`, which does an async
round-trip to one specialized agent and reads a reply; MasterDispatch fires
one task at the master and reads nothing.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Protocol

from shared.a2a_protocol import build_a2a_request
from shared.models import AlertContext, CommandRequest

MASTER_ARN_ENV = "MASTER_AGENT_RUNTIME_ARN"

# Async self-invoke: the function name/ARN the webhook re-invokes to run the
# blocking master dispatch off Slack's deadline. Falls back to the Lambda's own
# ``AWS_LAMBDA_FUNCTION_NAME`` ($LATEST) when unset; Terraform sets it to the
# ``live`` alias ARN so the worker runs the alias's published code.
SELF_INVOKE_ENV = "SELF_INVOKE_TARGET"
FUNCTION_NAME_ENV = "AWS_LAMBDA_FUNCTION_NAME"

# Marker key on the async-invoke event payload — distinguishes a self-dispatch
# from an HTTP webhook event in ``lambda_adapter.handler.lambda_handler``.
DISPATCH_EVENT_KEY = "_sre_dispatch"


@dataclass
class DispatchedTask:
    """One recorded master invocation — what :class:`RecordingMasterDispatch` captures.

    Also the serialization unit for :class:`AsyncMasterDispatch`: ``to_event`` /
    ``from_event`` carry the task across the async Lambda self-invocation
    boundary as plain JSON.
    """

    kind: str  # "investigate" | "postmortem" | "status"
    alert_context: AlertContext | None = None
    command: CommandRequest | None = None
    requested_at: str | None = None
    master_arn: str | None = None

    def to_event(self) -> dict:
        """JSON-serializable projection for the async self-invoke payload."""
        return {
            "kind": self.kind,
            "alert_context": asdict(self.alert_context) if self.alert_context is not None else None,
            "command": asdict(self.command) if self.command is not None else None,
            "requested_at": self.requested_at,
            "master_arn": self.master_arn,
        }

    @classmethod
    def from_event(cls, data: dict) -> DispatchedTask:
        """Reconstruct a task from a ``to_event`` payload (round-trips dataclasses)."""
        ctx = data.get("alert_context")
        cmd = data.get("command")
        return cls(
            kind=data["kind"],
            alert_context=_alert_context_from_dict(ctx) if ctx is not None else None,
            command=CommandRequest(**cmd) if cmd is not None else None,
            requested_at=data.get("requested_at"),
            master_arn=data.get("master_arn"),
        )


def _alert_context_from_dict(data: dict) -> AlertContext:
    """Rebuild an :class:`AlertContext`, restoring the window tuple JSON drops.

    ``asdict`` turns ``investigation_window`` into a list and JSON has no tuple
    type; normalise it back so downstream code that pattern-matches the
    ``(start, end)`` shape stays happy (mirrors ``agents.master.tools``).
    """
    window = data.get("investigation_window")
    if isinstance(window, list) and len(window) == 2:
        data = {**data, "investigation_window": (window[0], window[1])}
    return AlertContext(**data)


class MasterDispatch(Protocol):
    """Seam for firing one fire-and-forget task at the Master Agent runtime."""

    def investigate(
        self, alert_context: AlertContext, *, master_arn: str | None = None
    ) -> None: ...  # pragma: no cover

    def postmortem(self, command: CommandRequest) -> None: ...  # pragma: no cover

    def status(self, command: CommandRequest, requested_at: str) -> None: ...  # pragma: no cover


class AgentCoreMasterDispatch:
    """Production :class:`MasterDispatch` over ``bedrock-agentcore``.

    The boto3 client is built lazily on first send so constructing the
    dispatch (e.g. the default in ``process_webhook``) is free and never
    touches AWS on paths that don't dispatch (signature rejection, challenge).
    """

    def __init__(self, *, client=None) -> None:
        self._client = client

    def _runtime(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-agentcore")
        return self._client

    def investigate(
        self, alert_context: AlertContext, *, master_arn: str | None = None
    ) -> None:
        suffix = f"-{alert_context.variant_id}" if alert_context.variant_id else ""
        self._send(
            master_arn or _require_arn(),
            session_id=f"{alert_context.investigation_id}{suffix}",
            request_id=f"req-master-{alert_context.investigation_id}{suffix}",
            text=json.dumps(asdict(alert_context)),
        )

    def postmortem(self, command: CommandRequest) -> None:
        self._send(
            _require_arn(),
            session_id=f"pir-{command.channel_id}-{command.thread_ts}",
            request_id=f"req-master-pir-{command.channel_id}-{command.thread_ts}",
            text=json.dumps({
                "task": "pir",
                "platform": command.platform,
                "channel_id": command.channel_id,
                "thread_ts": command.thread_ts,
                "user_id": command.user_id,
                "command_text": command.text,
            }),
        )

    def status(self, command: CommandRequest, requested_at: str) -> None:
        self._send(
            _require_arn(),
            session_id=f"snapshot-{command.channel_id}-{requested_at}",
            request_id=f"req-master-snapshot-{command.channel_id}-{requested_at}",
            text=json.dumps({
                "task": "snapshot",
                "platform": command.platform,
                "channel_id": command.channel_id,
                "user_id": command.user_id,
                "requested_at": requested_at,
            }),
        )

    def _send(self, arn: str, *, session_id: str, request_id: str, text: str) -> None:
        self._runtime().invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            payload=json.dumps(build_a2a_request(text, request_id)).encode("utf-8"),
            contentType="application/json",
        )


class AsyncMasterDispatch:
    """Production :class:`MasterDispatch` that defers the blocking master invoke.

    ``bedrock-agentcore.invoke_agent_runtime`` blocks for the master's full LLM
    turn (several seconds), which overruns Slack's 3-second slash-command
    deadline. This adapter instead serialises the :class:`DispatchedTask` and
    fires a fire-and-forget ``lambda:InvokeFunction`` (``InvocationType="Event"``)
    at this same function, letting the webhook return HTTP 200 immediately. The
    async invocation re-enters :func:`lambda_adapter.handler.lambda_handler`,
    which replays the task through :class:`AgentCoreMasterDispatch` via
    :func:`run_dispatched_task` — with no client-facing deadline.

    The boto3 client is built lazily, so constructing the default in
    ``process_webhook`` is free on paths that never dispatch.
    """

    def __init__(self, *, client=None, function_name: str | None = None) -> None:
        self._client = client
        self._function_name = function_name

    def _lambda(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("lambda")
        return self._client

    def _target(self) -> str:
        name = (
            self._function_name
            or os.environ.get(SELF_INVOKE_ENV)
            or os.environ.get(FUNCTION_NAME_ENV)
        )
        if not name:
            raise EnvironmentError(
                f"Cannot resolve self-invoke target: set {SELF_INVOKE_ENV} "
                f"or {FUNCTION_NAME_ENV}."
            )
        return name

    def _enqueue(self, task: DispatchedTask) -> None:
        self._lambda().invoke(
            FunctionName=self._target(),
            InvocationType="Event",
            Payload=json.dumps({DISPATCH_EVENT_KEY: task.to_event()}).encode("utf-8"),
        )

    def investigate(
        self, alert_context: AlertContext, *, master_arn: str | None = None
    ) -> None:
        self._enqueue(
            DispatchedTask("investigate", alert_context=alert_context, master_arn=master_arn)
        )

    def postmortem(self, command: CommandRequest) -> None:
        self._enqueue(DispatchedTask("postmortem", command=command))

    def status(self, command: CommandRequest, requested_at: str) -> None:
        self._enqueue(
            DispatchedTask("status", command=command, requested_at=requested_at)
        )


def run_dispatched_task(event: dict, *, dispatch: MasterDispatch | None = None) -> None:
    """Replay an async-dispatched task through the synchronous master invoke.

    ``event`` is the payload an :class:`AsyncMasterDispatch` self-invocation
    delivers: ``{DISPATCH_EVENT_KEY: <serialised DispatchedTask>}``. Runs in the
    async (Event) Lambda invocation, where the blocking
    ``invoke_agent_runtime`` is free of Slack's slash-command deadline.
    """
    task = DispatchedTask.from_event(event[DISPATCH_EVENT_KEY])
    dispatch = dispatch or AgentCoreMasterDispatch()
    if task.kind == "investigate":
        if task.alert_context is None:
            raise ValueError("investigate task missing alert_context")
        dispatch.investigate(task.alert_context, master_arn=task.master_arn)
    elif task.kind == "postmortem":
        if task.command is None:
            raise ValueError("postmortem task missing command")
        dispatch.postmortem(task.command)
    elif task.kind == "status":
        if task.command is None or task.requested_at is None:
            raise ValueError("status task missing command/requested_at")
        dispatch.status(task.command, task.requested_at)
    else:
        raise ValueError(f"Unknown dispatch kind: {task.kind!r}")


class RecordingMasterDispatch:
    """Test :class:`MasterDispatch` — captures dispatched tasks as values, no I/O."""

    def __init__(self) -> None:
        self.tasks: list[DispatchedTask] = []

    def investigate(
        self, alert_context: AlertContext, *, master_arn: str | None = None
    ) -> None:
        self.tasks.append(
            DispatchedTask("investigate", alert_context=alert_context, master_arn=master_arn)
        )

    def postmortem(self, command: CommandRequest) -> None:
        self.tasks.append(DispatchedTask("postmortem", command=command))

    def status(self, command: CommandRequest, requested_at: str) -> None:
        self.tasks.append(
            DispatchedTask("status", command=command, requested_at=requested_at)
        )


def _require_arn() -> str:
    arn = os.environ.get(MASTER_ARN_ENV, "")
    if not arn:
        raise EnvironmentError(f"Missing required environment variable: {MASTER_ARN_ENV}")
    return arn
