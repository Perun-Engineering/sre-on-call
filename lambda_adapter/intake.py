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
from dataclasses import asdict, replace

from lambda_adapter.classifier import classify_alert, llm_classifier_from_env
from lambda_adapter.dedup import DeduplicationStore
from lambda_adapter.master_dispatch import AsyncMasterDispatch, MasterDispatch
from shared.env import truthy
from shared.experiment_store import ExperimentStore
from shared.models import AlertContext, CommandRequest
from shared.platforms import (
    AlertWebhook,
    ChallengeWebhook,
    ChatPlatform,
    CommandWebhook,
    DeliveryTarget,
    IgnoredWebhook,
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

# Reply posted (in-thread) when a mention is classified as non-alert chatter.
_NON_ALERT_NOTICE = (
    "👋 I investigate infrastructure alerts. Mention me on an alert message "
    "(or include the word *investigate*) and I'll dig in."
)


def _classification_enabled() -> bool:
    """Whether the intake classification gate is active.

    On by default; set ``ALERT_CLASSIFICATION_ENABLED`` to a falsy value as an
    operational kill-switch to investigate every mention unconditionally.
    """
    return truthy(os.environ.get("ALERT_CLASSIFICATION_ENABLED", "true"))


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


def process_webhook(
    event: dict, platform: ChatPlatform, dispatch: MasterDispatch | None = None
) -> dict:
    """Run the full intake pipeline using the given :class:`ChatPlatform`.

    Flow:
        1. ``platform.ingest(headers, raw_body)`` — verify signature, classify
           the request as :class:`InvalidWebhook`, :class:`ChallengeWebhook`,
           :class:`AlertWebhook`, or :class:`CommandWebhook`.
        2. Dispatch on the tagged event variant.
        3. For alerts: deduplicate via DynamoDB, fire the Master Agent.
        4. For commands: ack synchronously, fire the Master Agent.
        5. Return HTTP 200.

    *dispatch* is the :class:`MasterDispatch` seam; production defaults to
    :class:`AsyncMasterDispatch` (lazy boto3) — it fires the master invoke via
    a fire-and-forget Lambda self-invocation so this webhook returns within
    Slack's 3-second deadline. Tests inject a recording adapter and assert on
    the dispatched tasks.
    """
    dispatch = dispatch or AsyncMasterDispatch()
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

    if isinstance(webhook_event, IgnoredWebhook):
        # A valid event that is not an investigation trigger (e.g. a non-trigger
        # reaction). Ack with 200 so the platform does not retry; no dispatch.
        return _http_response(webhook_event.status_code)

    if isinstance(webhook_event, CommandWebhook):
        return _process_command(webhook_event.command, platform, dispatch)

    if isinstance(webhook_event, AlertWebhook):
        return _process_alert(webhook_event.context, platform, dispatch)

    # Defensive: unknown variant — return 500 rather than silently passing.
    raise RuntimeError(f"Unhandled WebhookEvent variant: {type(webhook_event).__name__}")


def _process_alert(
    alert_context: AlertContext, platform: ChatPlatform, dispatch: MasterDispatch
) -> dict:
    """Classify, dedup, and fire the Master Agent (or A/B variants).

    Non-alert mentions (casual chatter) are gated out before the fan-out: they
    get a lightweight in-thread notice and no investigation is dispatched.
    """
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

    # Classification gate: suppress the investigation fan-out for mentions that
    # are not alerts. Runs only for new investigations (duplicates already
    # returned above) and is fail-open — an ambiguous message investigates.
    if _classification_enabled():
        classification = classify_alert(
            alert_context.alert_text, llm=llm_classifier_from_env()
        )
        if not classification.is_alert:
            logger.info(
                "Non-alert mention gated: id=%s channel=%s tier=%s reason=%s",
                alert_context.investigation_id,
                alert_context.channel_id,
                classification.tier,
                classification.reason,
            )
            _post_non_alert_notice(platform, alert_context)
            return _http_response(200, {"ok": True, "classified": "non_alert"})

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

    if experiment:
        for variant in (experiment.variant_a, experiment.variant_b):
            variant_ctx = replace(
                alert_context,
                experiment_id=experiment.experiment_id,
                variant_id=variant.variant_id,
                variant_label=variant.label,
            )
            dispatch.investigate(variant_ctx, master_arn=variant.master_endpoint)
    else:
        dispatch.investigate(alert_context)

    logger.info(
        "Investigation started: id=%s channel=%s message_id=%s",
        alert_context.investigation_id,
        alert_context.channel_id,
        alert_context.message_id,
    )
    return _http_response(200, {"ok": True})


def _post_non_alert_notice(
    platform: ChatPlatform, alert_context: AlertContext
) -> None:
    """Reply to a gated non-alert mention, fail-open.

    The HTTP 200 the webhook returns is independent of this courtesy reply, so
    any delivery error is swallowed rather than surfaced to the platform.
    """
    try:
        platform.notice(DeliveryTarget.for_alert(alert_context), _NON_ALERT_NOTICE)
    except Exception:
        logger.warning(
            "Failed to post non-alert notice for %s; continuing.",
            alert_context.investigation_id,
            exc_info=True,
        )


def _process_command(
    command: CommandRequest, platform: ChatPlatform, dispatch: MasterDispatch
) -> dict:
    """Handle a slash command (e.g. ``/postmortem``, ``/sre-snapshot``).

    Each command is allow-listed by name and routed to its own handler.
    Unknown commands fall through with an ephemeral "Unknown command" reply.
    """
    if command.command == "/postmortem":
        return _process_postmortem_command(command, platform, dispatch)
    if command.command == "/sre-snapshot":
        return _process_status_command(command, platform, dispatch)
    return _http_response(200, {"text": f"Unknown command: {command.command}"})


def _process_postmortem_command(
    command: CommandRequest, platform: ChatPlatform, dispatch: MasterDispatch
) -> dict:
    """Handle ``/postmortem`` — must be invoked inside an incident thread.

    Acks synchronously, then fires the Master Agent with a PIR task
    referencing the originating thread.
    """
    if not command.thread_ts:
        platform.ack(command, "⚠️ Please use /postmortem inside an incident thread.")
        return _http_response(200)

    platform.ack(command, "📝 Generating Post-Incident Report...")
    dispatch.postmortem(command)

    logger.info(
        "PIR generation started: channel=%s thread=%s user=%s",
        command.channel_id,
        command.thread_ts,
        command.user_id,
    )
    return _http_response(200)


def _process_status_command(
    command: CommandRequest, platform: ChatPlatform, dispatch: MasterDispatch
) -> dict:
    """Handle ``/sre-snapshot`` — operator-driven snapshot. No thread required.

    Acks synchronously, then fires the Master Agent with a snapshot task.
    The master fans out to active specialized agents and posts a
    :class:`shared.report_renderer.SnapshotSections` payload at top-level
    (not as a thread reply) when collection completes.
    """
    platform.ack(command, "🩺 Capturing status snapshot...")

    requested_at = now_iso()
    dispatch.status(command, requested_at)

    logger.info(
        "Status snapshot started: channel=%s user=%s requested_at=%s",
        command.channel_id,
        command.user_id,
        requested_at,
    )
    return _http_response(200)
