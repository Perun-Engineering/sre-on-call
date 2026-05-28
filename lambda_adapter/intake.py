"""Shared webhook intake pipeline.

Owns the full flow: ingest → dedup → experiment → invoke master agent →
respond. Platform-specific behaviour is delegated to a
:class:`shared.platforms.ChatPlatform`.

The master agent posts its own "Investigation Started" message as soon
as it boots, so we do not post a chat-level ack here — Lambda's HTTP 200
is sufficient for the platform's webhook contract.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import asdict

import boto3

from lambda_adapter.dedup import DeduplicationStore
from shared.a2a_protocol import build_a2a_request
from shared.experiment_store import ExperimentStore
from shared.models import AlertContext, CommandRequest
from shared.platforms import (
    AlertWebhook,
    ChallengeWebhook,
    ChatPlatform,
    CommandWebhook,
    InvalidWebhook,
)
from shared.time_utils import now_iso
from shared.trace_store import (
    EVENT_ALERT_RECEIVED,
    EVENT_DEDUP_OUTCOME,
    SOURCE_LAMBDA,
    TraceStore,
)

logger = logging.getLogger(__name__)


def _wrap_a2a_message(text: str, request_id: str) -> bytes:
    """Encode an A2A JSON-RPC ``message/send`` envelope as bytes.

    AgentCore proxies the byte payload directly to the agent's A2A server,
    which expects JSON-RPC 2.0 — raw JSON is rejected with a 502.
    """
    return json.dumps(build_a2a_request(text, request_id)).encode("utf-8")


def _get_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return value


def _decode_body(event: dict) -> str:
    body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        body = base64.b64decode(body).decode("utf-8")
    return body


def _http_response(status_code: int, body: dict | None = None) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body or {}),
    }


def process_webhook(event: dict, platform: ChatPlatform) -> dict:
    """Run the full intake pipeline using the given :class:`ChatPlatform`.

    Flow:
        1. ``platform.ingest(headers, raw_body)`` — verify signature, classify
           the request as :class:`InvalidWebhook`, :class:`ChallengeWebhook`,
           :class:`AlertWebhook`, or :class:`CommandWebhook`.
        2. Dispatch on the tagged event variant.
        3. For alerts: deduplicate via DynamoDB, invoke the Master Agent.
        4. For commands: ack synchronously, invoke the Master Agent for PIR.
        5. Return HTTP 200.
    """
    raw_body = _decode_body(event)
    headers = event.get("headers", {})

    webhook_event = platform.ingest(headers, raw_body)

    if isinstance(webhook_event, InvalidWebhook):
        logger.warning(
            "Rejected request: %s (status=%d)",
            webhook_event.reason,
            webhook_event.status_code,
        )
        return _http_response(
            webhook_event.status_code, {"error": webhook_event.reason},
        )

    if isinstance(webhook_event, ChallengeWebhook):
        return _http_response(200, webhook_event.response)

    if isinstance(webhook_event, CommandWebhook):
        return _process_command(webhook_event.command, platform)

    if isinstance(webhook_event, AlertWebhook):
        return _process_alert(webhook_event.context)

    # Defensive: unknown variant — return 500 rather than silently passing.
    raise RuntimeError(f"Unhandled WebhookEvent variant: {type(webhook_event).__name__}")


def _process_alert(alert_context: AlertContext) -> dict:
    """Dedup the alert and invoke the Master Agent (or A/B variants)."""
    dedup_table = _get_env("DEDUP_TABLE_NAME")
    store = DeduplicationStore(table_name=dedup_table)
    is_new = store.record_if_new(
        channel_id=alert_context.channel_id,
        message_id=alert_context.message_id,
        investigation_id=alert_context.investigation_id,
        platform=alert_context.platform,
    )

    # Trace archive (fail-open). Records the dedup decision for every
    # alert and the full AlertContext for new investigations. Duplicates
    # leave only a `dedup_outcome` event so postmortem tooling can see
    # which messages were dropped.
    trace_store = TraceStore.from_env()
    if trace_store is not None:
        trace_store.put_event(
            investigation_id=alert_context.investigation_id,
            source=SOURCE_LAMBDA,
            event_type=EVENT_DEDUP_OUTCOME,
            payload={"is_new": is_new},
        )

    if not is_new:
        logger.info(
            "Duplicate alert discarded: channel=%s message_id=%s",
            alert_context.channel_id,
            alert_context.message_id,
        )
        return _http_response(200)

    if trace_store is not None:
        trace_store.put_event(
            investigation_id=alert_context.investigation_id,
            source=SOURCE_LAMBDA,
            event_type=EVENT_ALERT_RECEIVED,
            payload=asdict(alert_context),
        )

    # Check for active A/B experiment
    experiment = None
    experiments_table = os.environ.get("EXPERIMENTS_TABLE_NAME", "")
    if experiments_table:
        experiment = ExperimentStore(table_name=experiments_table).get_active_experiment()

    runtime_client = boto3.client("bedrock-agentcore")

    if experiment:
        for variant in (experiment.variant_a, experiment.variant_b):
            ctx = asdict(alert_context)
            ctx["experiment_id"] = experiment.experiment_id
            ctx["variant_id"] = variant.variant_id
            ctx["variant_label"] = variant.label
            runtime_client.invoke_agent_runtime(
                agentRuntimeArn=variant.master_endpoint,
                runtimeSessionId=f"{alert_context.investigation_id}-{variant.variant_id}",
                payload=_wrap_a2a_message(
                    json.dumps(ctx),
                    f"req-master-{alert_context.investigation_id}-{variant.variant_id}",
                ),
                contentType="application/json",
            )
    else:
        agent_runtime_arn = _get_env("MASTER_AGENT_RUNTIME_ARN")
        runtime_client.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId=alert_context.investigation_id,
            payload=_wrap_a2a_message(
                json.dumps(asdict(alert_context)),
                f"req-master-{alert_context.investigation_id}",
            ),
            contentType="application/json",
        )

    logger.info(
        "Investigation started: id=%s channel=%s message_id=%s",
        alert_context.investigation_id,
        alert_context.channel_id,
        alert_context.message_id,
    )
    return _http_response(200, {"ok": True})


def _process_command(command: CommandRequest, platform: ChatPlatform) -> dict:
    """Handle a slash command (e.g. ``/postmortem``, ``/status``).

    Each command is allow-listed by name and routed to its own handler.
    Unknown commands fall through with an ephemeral "Unknown command" reply.
    """
    if command.command == "/postmortem":
        return _process_postmortem_command(command, platform)
    if command.command == "/status":
        return _process_status_command(command, platform)
    return _http_response(200, {"text": f"Unknown command: {command.command}"})


def _process_postmortem_command(
    command: CommandRequest, platform: ChatPlatform
) -> dict:
    """Handle ``/postmortem`` — must be invoked inside an incident thread.

    Acks synchronously, then invokes the Master Agent with a PIR task
    payload referencing the originating thread.
    """
    if not command.thread_ts:
        platform.ack(command, "⚠️ Please use /postmortem inside an incident thread.")
        return _http_response(200)

    platform.ack(command, "📝 Generating Post-Incident Report...")

    agent_runtime_arn = _get_env("MASTER_AGENT_RUNTIME_ARN")
    runtime_client = boto3.client("bedrock-agentcore")
    pir_payload = json.dumps({
        "task": "pir",
        "platform": command.platform,
        "channel_id": command.channel_id,
        "thread_ts": command.thread_ts,
        "user_id": command.user_id,
        "command_text": command.text,
    })
    runtime_client.invoke_agent_runtime(
        agentRuntimeArn=agent_runtime_arn,
        runtimeSessionId=f"pir-{command.channel_id}-{command.thread_ts}",
        payload=_wrap_a2a_message(
            pir_payload,
            f"req-master-pir-{command.channel_id}-{command.thread_ts}",
        ),
        contentType="application/json",
    )

    logger.info(
        "PIR generation started: channel=%s thread=%s user=%s",
        command.channel_id,
        command.thread_ts,
        command.user_id,
    )
    return _http_response(200)


def _process_status_command(
    command: CommandRequest, platform: ChatPlatform
) -> dict:
    """Handle ``/status`` — operator-driven snapshot. No thread required.

    Acks synchronously, then invokes the Master Agent with a snapshot
    task payload. The master fans out to active specialized agents and
    posts a :class:`shared.report_renderer.SnapshotSections` payload at
    top-level (not as a thread reply) when collection completes.
    """
    platform.ack(command, "🩺 Capturing status snapshot...")

    requested_at = now_iso()

    agent_runtime_arn = _get_env("MASTER_AGENT_RUNTIME_ARN")
    runtime_client = boto3.client("bedrock-agentcore")
    snapshot_payload = json.dumps({
        "task": "snapshot",
        "platform": command.platform,
        "channel_id": command.channel_id,
        "user_id": command.user_id,
        "requested_at": requested_at,
    })
    runtime_client.invoke_agent_runtime(
        agentRuntimeArn=agent_runtime_arn,
        runtimeSessionId=f"snapshot-{command.channel_id}-{requested_at}",
        payload=_wrap_a2a_message(
            snapshot_payload,
            f"req-master-snapshot-{command.channel_id}-{requested_at}",
        ),
        contentType="application/json",
    )

    logger.info(
        "Status snapshot started: channel=%s user=%s requested_at=%s",
        command.channel_id,
        command.user_id,
        requested_at,
    )
    return _http_response(200)
