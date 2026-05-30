"""Discord ChatPlatform — Discord-specific implementation of the
:class:`shared.platforms.ChatPlatform` seam.

Owns the full per-platform lifecycle: signature verification, alert/command
parsing, slash-command callback, and report delivery. The Discord Markdown
dialect itself lives in :mod:`shared.report_renderer` (used internally by
:class:`DiscordChatPlatform.deliver`).
"""

from __future__ import annotations

import json
import logging
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

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
from shared.report_renderer import (
    DiscordReportRenderer,
    EnrichmentSections,
    InvestigationStartedSections,
    PIRSections,
    ReportSections,
    SnapshotSections,
)
from shared.secrets import resolve_secret

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_discord_signature(
    public_key: str,
    timestamp: str,
    body: str,
    signature: str,
) -> bool:
    """Verify a Discord interaction request signature (Ed25519).

    The signed message is ``{timestamp}{body}``; *signature* is the
    hex-encoded 64-byte value from the ``X-Signature-Ed25519`` header.
    Returns ``True`` when the signature is valid; ``False`` otherwise.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(bytes.fromhex(signature), f"{timestamp}{body}".encode())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Alert payload parsing
# ---------------------------------------------------------------------------


def parse_alert_context(interaction_payload: dict) -> AlertContext:
    """Extract :class:`AlertContext` from a Discord message/interaction payload.

    Supports MESSAGE_CREATE gateway events forwarded through a webhook
    adapter. Required keys: ``channel_id``, ``id``, ``content``, ``timestamp``.
    """
    channel_id = interaction_payload["channel_id"]
    message_id = str(interaction_payload["id"])
    alert_text = interaction_payload.get("content", "")

    raw_ts = interaction_payload.get("timestamp", "")
    if raw_ts:
        alert_dt = datetime.fromisoformat(raw_ts)
    else:
        alert_dt = datetime.now(tz=timezone.utc)

    if alert_dt.tzinfo is None:
        alert_dt = alert_dt.replace(tzinfo=timezone.utc)

    alert_timestamp = alert_dt.isoformat()

    half_window = timedelta(minutes=INVESTIGATION_WINDOW_MINUTES / 2)
    window_start = (alert_dt - half_window).isoformat()
    window_end = (alert_dt + half_window).isoformat()

    guild_id = interaction_payload.get("guild_id", "")

    return AlertContext(
        investigation_id=str(uuid.uuid4()),
        platform="discord",
        channel_id=channel_id,
        message_id=message_id,
        alert_text=alert_text,
        alert_timestamp=alert_timestamp,
        investigation_window=(window_start, window_end),
        platform_metadata={
            "guild_id": guild_id,
            "message_id": message_id,
        },
    )


# ---------------------------------------------------------------------------
# ChatPlatform implementation
# ---------------------------------------------------------------------------


class DiscordChatPlatform:
    """ChatPlatform implementation for Discord."""

    name = "discord"

    def __init__(
        self,
        public_key: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._public_key = public_key or resolve_secret("DISCORD_PUBLIC_KEY")
        self._bot_token = bot_token or resolve_secret("DISCORD_BOT_TOKEN")
        self._renderer = DiscordReportRenderer()

    # --- ingest -----------------------------------------------------------

    def ingest(self, headers: dict, raw_body: str) -> WebhookEvent:
        if not verify_discord_signature(
            self._public_key,
            headers.get("x-signature-timestamp", ""),
            raw_body,
            headers.get("x-signature-ed25519", ""),
        ):
            return InvalidWebhook(status_code=401, reason="invalid signature")

        if self._is_command(raw_body):
            return CommandWebhook(command=self._parse_command(raw_body))

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            return InvalidWebhook(status_code=400, reason="invalid JSON body")

        if payload.get("type") == 1:  # PING
            return ChallengeWebhook(response={"type": 1})

        return AlertWebhook(context=parse_alert_context(payload))

    @staticmethod
    def _is_command(raw_body: str) -> bool:
        # Discord slash commands arrive as interaction type 2
        try:
            payload = json.loads(raw_body)
            return payload.get("type") == 2
        except (json.JSONDecodeError, TypeError):
            return False

    @staticmethod
    def _parse_command(raw_body: str) -> CommandRequest:
        payload = json.loads(raw_body)
        data = payload.get("data", {})
        options = {o["name"]: o.get("value", "") for o in data.get("options", [])}
        channel_id = payload.get("channel_id", "")
        return CommandRequest(
            platform="discord",
            command=f"/{data.get('name', '')}",
            text=options.get("text", ""),
            channel_id=channel_id,
            user_id=payload.get("member", {}).get("user", {}).get("id", ""),
            thread_ts=None,
            response_url="",
            platform_metadata={
                "interaction_id": payload.get("id", ""),
                "interaction_token": payload.get("token", ""),
            },
        )

    # --- ack --------------------------------------------------------------

    def ack(self, command: CommandRequest, text: str) -> None:
        interaction_id = command.platform_metadata.get("interaction_id", "")
        interaction_token = command.platform_metadata.get("interaction_token", "")
        if not interaction_id or not interaction_token:
            return
        url = f"https://discord.com/api/v10/interactions/{interaction_id}/{interaction_token}/callback"
        data = json.dumps({"type": 4, "data": {"content": text, "flags": 64}}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req)

    # --- deliver ----------------------------------------------------------

    async def deliver(
        self, target: DeliveryTarget, payload: DeliverPayload
    ) -> str:
        text = self._render(payload)
        await self._post_reply(target, text)
        return text

    def _render(self, payload: DeliverPayload) -> str:
        if isinstance(payload, ReportSections):
            return self._renderer.render_report(payload)
        if isinstance(payload, EnrichmentSections):
            return self._renderer.render_enrichment(payload)
        if isinstance(payload, InvestigationStartedSections):
            return self._renderer.render_investigation_started(payload)
        if isinstance(payload, PIRSections):
            return self._renderer.render_pir(payload)
        if isinstance(payload, SnapshotSections):
            return self._renderer.render_snapshot(payload)
        raise TypeError(
            f"Unsupported deliver payload: {type(payload).__name__}"
        )

    async def _post_reply(self, target: DeliveryTarget, text: str) -> None:
        import aiohttp

        url = f"https://discord.com/api/v10/channels/{target.channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self._bot_token}",
            "Content-Type": "application/json",
        }
        body: dict = {"content": text}

        # No thread anchor means "post at top-level" (e.g. /status snapshots).
        if target.thread_anchor:
            body["message_reference"] = {"message_id": target.thread_anchor}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers) as resp:
                if resp.status >= 400:
                    err = await resp.text()
                    raise RuntimeError(
                        f"Discord API error {resp.status}: {err}"
                    )
