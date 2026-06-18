"""Slack ChatPlatform — Slack-specific implementation of the
:class:`shared.platforms.ChatPlatform` seam.

Owns the full per-platform lifecycle: signature verification, alert/command
parsing, slash-command callback, and report delivery. The Slack mrkdwn
dialect itself lives in :mod:`shared.report_renderer` (used internally by
:class:`SlackChatPlatform.deliver`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import AlertContext, CommandRequest
from shared.platforms import (
    AlertWebhook,
    ChallengeWebhook,
    CommandWebhook,
    DeliverPayload,
    DeliveryTarget,
    InvalidWebhook,
    WebhookEvent,
)
from shared.report_renderer import SlackReportRenderer
from shared.secrets import resolve_secret

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

# Maximum age of a request timestamp before it is rejected (replay protection).
_MAX_TIMESTAMP_AGE_SECONDS = 5 * 60  # 5 minutes


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: str,
    signature: str,
) -> bool:
    """Verify a Slack request signature.

    Computes HMAC-SHA256 of ``v0:{timestamp}:{body}`` using *signing_secret*
    and compares the result against the provided *signature* using
    constant-time comparison.  Requests whose timestamp is older than
    5 minutes are rejected to guard against replay attacks.

    Returns ``True`` when the signature is valid **and** the timestamp is
    fresh; ``False`` otherwise.
    """
    try:
        request_ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    current_ts = int(time.time())
    if abs(current_ts - request_ts) > _MAX_TIMESTAMP_AGE_SECONDS:
        return False

    sig_basestring = f"v0:{timestamp}:{body}"
    computed_hash = hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    computed_signature = f"v0={computed_hash}"

    return hmac.compare_digest(computed_signature, signature)


# ---------------------------------------------------------------------------
# Alert payload parsing
# ---------------------------------------------------------------------------


def parse_alert_context(event_payload: dict) -> AlertContext:
    """Extract :class:`AlertContext` from a Slack Events API event_callback payload.

    Raises :class:`KeyError` when required fields are missing.
    """
    event = event_payload["event"]

    channel_id = event["channel"]
    message_ts = event["ts"]
    alert_text = event["text"]

    event_ts = event.get("event_ts", event["ts"])
    epoch_seconds = float(event_ts)
    alert_dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    alert_timestamp = alert_dt.isoformat()

    half_window = timedelta(minutes=INVESTIGATION_WINDOW_MINUTES / 2)
    window_start = (alert_dt - half_window).isoformat()
    window_end = (alert_dt + half_window).isoformat()

    return AlertContext(
        investigation_id=str(uuid.uuid4()),
        platform="slack",
        channel_id=channel_id,
        message_id=message_ts,
        alert_text=alert_text,
        alert_timestamp=alert_timestamp,
        investigation_window=(window_start, window_end),
        platform_metadata={"thread_ts": message_ts},
    )


# ---------------------------------------------------------------------------
# ChatPlatform implementation
# ---------------------------------------------------------------------------


class SlackChatPlatform:
    """ChatPlatform implementation for Slack."""

    name = "slack"

    def __init__(
        self,
        signing_secret: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._signing_secret = signing_secret or resolve_secret("SLACK_SIGNING_SECRET")
        self._bot_token = bot_token or resolve_secret("SLACK_BOT_TOKEN")
        self._renderer = SlackReportRenderer()

    # --- ingest -----------------------------------------------------------

    def ingest(self, headers: dict, raw_body: str) -> WebhookEvent:
        if not verify_slack_signature(
            self._signing_secret,
            headers.get("x-slack-request-timestamp", ""),
            raw_body,
            headers.get("x-slack-signature", ""),
        ):
            return InvalidWebhook(status_code=401, reason="invalid signature")

        if self._is_command(headers, raw_body):
            return CommandWebhook(command=self._parse_command(raw_body))

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            return InvalidWebhook(status_code=400, reason="invalid JSON body")

        if payload.get("type") == "url_verification":
            return ChallengeWebhook(response={"challenge": payload.get("challenge", "")})

        return AlertWebhook(context=parse_alert_context(payload))

    @staticmethod
    def _is_command(headers: dict, raw_body: str) -> bool:
        content_type = headers.get("content-type", "")
        return "application/x-www-form-urlencoded" in content_type and "command=" in raw_body

    @staticmethod
    def _parse_command(raw_body: str) -> CommandRequest:
        fields = parse_qs(raw_body, keep_blank_values=True)
        return CommandRequest(
            platform="slack",
            command=fields.get("command", [""])[0],
            text=fields.get("text", [""])[0],
            channel_id=fields.get("channel_id", [""])[0],
            user_id=fields.get("user_id", [""])[0],
            thread_ts=fields.get("thread_ts", [None])[0] or None,
            response_url=fields.get("response_url", [""])[0],
        )

    # --- ack --------------------------------------------------------------

    def ack(self, command: CommandRequest, text: str) -> None:
        """Post the slash-command "working on it" message to ``response_url``, fail-open.

        The command's HTTP 200 is the real acknowledgement; this ephemeral
        message is best-effort feedback. A failing or slow ``response_url`` must
        never raise (it would 502 the command) or block past the intake
        deadline, so the POST is bounded by a short timeout and all errors are
        swallowed — same fail-open contract as :meth:`notice`.
        """
        if not command.response_url:
            return
        data = json.dumps({"response_type": "ephemeral", "text": text}).encode()
        req = urllib.request.Request(
            command.response_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            logger.warning("Slack ack post to response_url failed; continuing.", exc_info=True)

    # --- notice -----------------------------------------------------------

    def notice(self, target: DeliveryTarget, text: str) -> None:
        """Post a plain-text reply via ``chat.postMessage``, fail-open.

        Synchronous (urllib) so it runs inside the Lambda intake handler.
        Threads under ``target.thread_anchor`` when present; Slack rejects an
        empty ``thread_ts`` so it is omitted for top-level posts.
        """
        try:
            payload: dict = {"channel": target.channel_id, "text": text}
            if target.thread_anchor:
                payload["thread_ts"] = target.thread_anchor
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self._bot_token}",
                },
                method="POST",
            )
            urllib.request.urlopen(req)
        except Exception:
            logger.warning("Slack notice post failed; continuing.", exc_info=True)

    # --- deliver ----------------------------------------------------------

    async def deliver(
        self, target: DeliveryTarget, payload: DeliverPayload
    ) -> str:
        text = self._renderer.render(payload)
        await self._post_reply(target, text)
        return text

    async def _post_reply(self, target: DeliveryTarget, text: str) -> None:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=self._bot_token)
        kwargs: dict = {
            "channel": target.channel_id,
            "text": text,
            # #33 — the incident report carries infra internals and a signed
            # page link; never unfurl it into preview cards.
            "unfurl_links": False,
            "unfurl_media": False,
        }
        # No thread anchor means "post at top-level" — used by /sre-snapshot
        # snapshots, which are operational broadcasts, not thread replies.
        # The Slack API rejects ``thread_ts=""`` so we omit it in that case.
        if target.thread_anchor:
            kwargs["thread_ts"] = target.thread_anchor
        await client.chat_postMessage(**kwargs)
