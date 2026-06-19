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
import os
import re
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode

from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import AlertContext, CommandRequest
from shared.platforms import (
    AlertWebhook,
    ChallengeWebhook,
    CommandWebhook,
    DeliverPayload,
    DeliveryTarget,
    IgnoredWebhook,
    InvalidWebhook,
    WebhookEvent,
)
from shared.report_renderer import SlackReportRenderer
from shared.secrets import resolve_secret

logger = logging.getLogger(__name__)

# Default emoji whose reaction triggers an investigation. Overridable via the
# ``SLACK_TRIGGER_EMOJI`` env var (or the constructor). Stored without the
# surrounding colons, matching the bare name Slack puts in ``reaction_added``.
_DEFAULT_TRIGGER_EMOJI = "sre-on-call"

# Matches a Slack user-mention token, e.g. ``<@U0B9QDFB1D2>`` — stripped from an
# operator's in-thread reply before it is carried as a steering note.
_MENTION_TOKEN = re.compile(r"<@[A-Z0-9]+>")


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


def _build_alert_context(
    *,
    channel_id: str,
    message_id: str,
    alert_text: str,
    window_ts: str,
) -> AlertContext:
    """Assemble an :class:`AlertContext`, centring the ±window on *window_ts*.

    ``message_id`` is both the dedup key and the thread anchor the report
    replies under; ``window_ts`` is the Slack ``ts`` whose epoch the
    investigation window is centred on (the alert's own time).
    """
    alert_dt = datetime.fromtimestamp(float(window_ts), tz=timezone.utc)
    half_window = timedelta(minutes=INVESTIGATION_WINDOW_MINUTES / 2)

    return AlertContext(
        investigation_id=str(uuid.uuid4()),
        platform="slack",
        channel_id=channel_id,
        message_id=message_id,
        alert_text=alert_text,
        alert_timestamp=alert_dt.isoformat(),
        investigation_window=(
            (alert_dt - half_window).isoformat(),
            (alert_dt + half_window).isoformat(),
        ),
        platform_metadata={"thread_ts": message_id},
    )


def parse_alert_context(event_payload: dict) -> AlertContext:
    """Extract :class:`AlertContext` from a Slack Events API event_callback payload.

    Raises :class:`KeyError` when required fields are missing.
    """
    event = event_payload["event"]

    message_ts = event["ts"]
    return _build_alert_context(
        channel_id=event["channel"],
        message_id=message_ts,
        alert_text=event["text"],
        window_ts=event.get("event_ts", message_ts),
    )


def _strip_bot_mention(text: str) -> str:
    """Remove ``<@U…>`` mention tokens and collapse the surrounding whitespace.

    Used to turn an operator's in-thread ``@sre-on-call look at this`` reply
    into the bare steering note ``look at this``.
    """
    return " ".join(_MENTION_TOKEN.sub(" ", text).split())


# ---------------------------------------------------------------------------
# Message reads (reaction / thread-mention triggers)
# ---------------------------------------------------------------------------


def _blocks_text(blocks: object) -> str:
    """Concatenate human-readable text out of Slack block-kit blocks.

    Pulls a ``section``/``header`` block's ``text.text``, a section's
    ``fields[].text``, and a ``context`` block's ``elements[].text``. Deeper
    ``rich_text`` nesting is skipped — those messages already carry a populated
    top-level ``text``.
    """
    if not isinstance(blocks, list):
        return ""
    out: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text_obj = block.get("text")
        if isinstance(text_obj, dict):
            piece = (text_obj.get("text") or "").strip()
            if piece:
                out.append(piece)
        for collection in (block.get("fields"), block.get("elements")):
            if not isinstance(collection, list):
                continue
            for item in collection:
                if isinstance(item, dict):
                    piece = (item.get("text") or "").strip()
                    if isinstance(piece, str) and piece:
                        out.append(piece)
    return "\n".join(out)


def _extract_message_text(message: dict) -> str | None:
    """Best-effort plain text of a Slack message.

    Alert integrations (Alertmanager, Grafana) post the alert body inside
    ``attachments`` or block-kit ``blocks`` with an *empty* top-level ``text`` —
    so reading ``text`` alone drops the very messages this bot exists to
    investigate. Fall back through attachments (title/text, else fallback, else
    nested blocks) and then top-level blocks, concatenating the readable
    fragments. Returns ``None`` when nothing readable is found.
    """
    parts: list[str] = []

    top = (message.get("text") or "").strip()
    if top:
        parts.append(top)

    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for att in attachments:
            if not isinstance(att, dict):
                continue
            att_text = "\n".join(
                piece
                for piece in ((att.get("title") or "").strip(), (att.get("text") or "").strip())
                if piece
            )
            if not att_text:
                att_text = (att.get("fallback") or "").strip()
            if not att_text:
                att_text = _blocks_text(att.get("blocks"))
            if att_text:
                parts.append(att_text)

    blocks_text = _blocks_text(message.get("blocks"))
    if blocks_text:
        parts.append(blocks_text)

    combined = "\n".join(parts).strip()
    return combined or None


class SlackMessageReader:
    """Fail-open Slack Web API reads for the message behind a trigger event.

    A ``reaction_added`` payload carries only the reacted message's
    ``channel``/``ts``, and an in-thread ``app_mention`` carries the operator's
    reply, not the alert. Both triggers must fetch the underlying message text.

    Synchronous (``urllib``) so it runs inside the Lambda intake handler under
    Slack's 3-second deadline, bounded by a short timeout. Every failure path —
    HTTP error, ``ok=false``, or an empty result — returns ``None`` so the
    caller can drop the event rather than raise.
    """

    _API_BASE = "https://slack.com/api"

    def __init__(self, bot_token: str, *, timeout: float = 1.5) -> None:
        self._bot_token = bot_token
        self._timeout = timeout

    def read_reacted_message(self, channel: str, ts: str) -> str | None:
        """Return the text of the single message at *ts* in *channel*."""
        messages = self._get(
            "conversations.history",
            {
                "channel": channel,
                "latest": ts,
                "oldest": ts,
                "inclusive": "true",
                "limit": "1",
            },
        )
        return self._first_text(messages)

    def read_thread_parent(self, channel: str, thread_ts: str) -> str | None:
        """Return the text of the parent message of the thread *thread_ts*."""
        messages = self._get(
            "conversations.replies",
            {"channel": channel, "ts": thread_ts, "limit": "1"},
        )
        return self._first_text(messages)

    @staticmethod
    def _first_text(messages: list | None) -> str | None:
        if not messages:
            return None
        return _extract_message_text(messages[0])

    def _get(self, method: str, params: dict) -> list | None:
        """GET a Slack Web API method, returning ``messages`` or ``None``."""
        url = f"{self._API_BASE}/{method}?{urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._bot_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read())
        except Exception:
            logger.warning("Slack %s read failed; treating as unreadable.", method, exc_info=True)
            return None
        if not body.get("ok"):
            logger.warning("Slack %s returned ok=false: %s", method, body.get("error"))
            return None
        return body.get("messages", [])


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
        trigger_emoji: str | None = None,
        bot_user_id: str | None = None,
    ) -> None:
        self._signing_secret = signing_secret or resolve_secret("SLACK_SIGNING_SECRET")
        self._bot_token = bot_token or resolve_secret("SLACK_BOT_TOKEN")
        self._renderer = SlackReportRenderer()
        emoji = trigger_emoji or os.environ.get("SLACK_TRIGGER_EMOJI") or _DEFAULT_TRIGGER_EMOJI
        # Slack reports reactions by their bare name; tolerate a :wrapped: value.
        self._trigger_emoji = emoji.strip().strip(":")
        # When set, the bot's own reactions are ignored (loop guard).
        self._bot_user_id = bot_user_id or os.environ.get("SLACK_BOT_USER_ID") or None
        self._reader = SlackMessageReader(self._bot_token)

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

        event = payload.get("event", {})
        event_type = event.get("type")
        logger.info(
            "Slack event_callback: type=%s subtype=%s thread_ts=%s ts=%s",
            event_type,
            event.get("subtype"),
            event.get("thread_ts"),
            event.get("ts"),
        )

        if event_type == "reaction_added":
            return self._ingest_reaction(event)

        # A mention that is a reply *inside* a thread (thread_ts present and not
        # the message's own ts) targets the parent — the alert — not the reply.
        if event_type == "app_mention":
            thread_ts = event.get("thread_ts")
            if thread_ts and thread_ts != event.get("ts"):
                return self._ingest_thread_mention(event)

        return AlertWebhook(context=parse_alert_context(payload))

    # --- new-trigger routing ----------------------------------------------

    def _ingest_reaction(self, event: dict) -> WebhookEvent:
        """Route a ``reaction_added`` event.

        Investigates only when the configured trigger emoji is added to a
        message by someone other than the bot; every other reaction is dropped.
        """
        reaction = event.get("reaction")
        if reaction != self._trigger_emoji:
            return IgnoredWebhook(
                reason=f"reaction ':{reaction}:' != trigger ':{self._trigger_emoji}:'"
            )
        if self._bot_user_id and event.get("user") == self._bot_user_id:
            return IgnoredWebhook(reason="bot's own reaction")

        item = event.get("item", {})
        if item.get("type") != "message":
            return IgnoredWebhook(reason=f"reacted item type={item.get('type')!r}, not 'message'")
        channel, ts = item.get("channel"), item.get("ts")
        if not channel or not ts:
            return IgnoredWebhook(reason="reaction event missing item channel/ts")

        text = self._reader.read_reacted_message(channel, ts)
        if not text:
            return IgnoredWebhook(
                reason=f"reacted message unreadable/empty (channel={channel} ts={ts})"
            )

        return AlertWebhook(
            context=_build_alert_context(
                channel_id=channel, message_id=ts, alert_text=text, window_ts=ts,
            )
        )

    def _ingest_thread_mention(self, event: dict) -> WebhookEvent:
        """Route an ``app_mention`` posted as a reply within a thread.

        The thread parent is the alert; the operator's reply (minus the
        ``@mention``) rides along as a steering note appended to the alert text.
        """
        channel, thread_ts = event.get("channel"), event.get("thread_ts")
        if not channel or not thread_ts:
            return IgnoredWebhook(reason="thread mention missing channel/thread_ts")

        parent_text = self._reader.read_thread_parent(channel, thread_ts)
        if not parent_text:
            return IgnoredWebhook(
                reason=f"thread parent unreadable/empty (channel={channel} thread_ts={thread_ts})"
            )

        note = _strip_bot_mention(event.get("text", ""))
        alert_text = (
            f"{parent_text}\n\n---\nOperator note (via @mention): {note}"
            if note
            else parent_text
        )

        return AlertWebhook(
            context=_build_alert_context(
                channel_id=channel,
                message_id=thread_ts,
                alert_text=alert_text,
                window_ts=thread_ts,
            )
        )

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
