"""Shared webhook intake pipeline.

Owns the full flow: decode → verify → challenge → dedup → experiment →
invoke master agent → respond.  Platform-specific behaviour is delegated
to a WebhookAdapter.

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

from lambda_adapter.adapters import WebhookAdapter
from lambda_adapter.dedup import DeduplicationStore
from shared.a2a_protocol import build_a2a_request
from shared.experiment_store import ExperimentStore

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


def process_webhook(event: dict, adapter: WebhookAdapter) -> dict:
    """Run the full intake pipeline using the given platform adapter.

    Flow:
        1. Verify request signature
        2. Handle slash commands (if applicable)
        3. Handle platform challenge/ping
        4. Deduplicate via DynamoDB
        5. Invoke Master Agent(s) asynchronously
        6. Return HTTP 200
    """
    raw_body = _decode_body(event)
    headers = event.get("headers", {})

    # 1. Verify signature
    if not adapter.verify_signature(headers, raw_body):
        logger.warning("Rejected request: invalid signature")
        return _http_response(401, {"error": "invalid signature"})

    # 2. Slash command handling (different body format, separate flow)
    if adapter.is_command(headers, raw_body):
        return _process_command(raw_body, adapter)

    # 3. Parse JSON
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return _http_response(400, {"error": "invalid JSON body"})

    # 3. Handle platform challenge
    challenge = adapter.get_challenge_response(payload)
    if challenge is not None:
        return _http_response(200, challenge)

    # 4. Dedup
    dedup_table = _get_env("DEDUP_TABLE_NAME")
    alert_context = adapter.parse_alert_context(payload)

    store = DeduplicationStore(table_name=dedup_table)
    is_new = store.record_if_new(
        channel_id=alert_context.channel_id,
        message_id=alert_context.message_id,
        investigation_id=alert_context.investigation_id,
        platform=alert_context.platform,
    )
    if not is_new:
        logger.info(
            "Duplicate alert discarded: channel=%s message_id=%s",
            alert_context.channel_id,
            alert_context.message_id,
        )
        return _http_response(200)

    # 5. Check for active A/B experiment
    experiment = None
    experiments_table = os.environ.get("EXPERIMENTS_TABLE_NAME", "")
    if experiments_table:
        experiment = ExperimentStore(table_name=experiments_table).get_active_experiment()

    # 6. Invoke Master Agent(s)
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


def _process_command(raw_body: str, adapter: WebhookAdapter) -> dict:
    """Handle a slash command (e.g. /postmortem).

    Flow:
        1. Parse the command payload
        2. Validate: must be invoked in a thread
        3. Ack immediately
        4. Invoke Master Agent with a PIR task
        5. Return HTTP 200
    """
    command = adapter.parse_command(raw_body)

    if command.command not in ("/postmortem",):
        return _http_response(200, {"text": f"Unknown command: {command.command}"})

    if not command.thread_ts:
        adapter.ack_command(command, "⚠️ Please use /postmortem inside an incident thread.")
        return _http_response(200)

    adapter.ack_command(command, "📝 Generating Post-Incident Report...")

    # Invoke Master Agent with PIR task
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
