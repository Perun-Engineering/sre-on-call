"""MasterDispatch — the seam for firing one fire-and-forget task at the
Master Agent runtime from the Lambda intake pipeline.

A typed Protocol with one method per task kind. Each method owns its
``runtimeSessionId`` convention, its A2A envelope build, and the
``MASTER_AGENT_RUNTIME_ARN`` resolution — the knowledge that was previously
open-coded at four ``invoke_agent_runtime`` call sites in ``intake.py``.

Two adapters: :class:`AgentCoreMasterDispatch` (production, synchronous
``bedrock-agentcore.invoke_agent_runtime``; the response is discarded — the
master posts its own "Investigation Started" notice) and
:class:`RecordingMasterDispatch` (tests capture dispatched tasks as values).

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


@dataclass
class DispatchedTask:
    """One recorded master invocation — what :class:`RecordingMasterDispatch` captures."""

    kind: str  # "investigate" | "postmortem" | "status"
    alert_context: AlertContext | None = None
    command: CommandRequest | None = None
    requested_at: str | None = None
    master_arn: str | None = None


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
