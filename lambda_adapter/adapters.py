"""Platform-specific webhook adapters for the unified intake pipeline.

Each adapter encapsulates the platform-specific operations: signature
verification, challenge detection, alert parsing, and slash-command
handling.  User-facing messages are emitted by the master agent directly,
not by the adapter.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol
from urllib.parse import parse_qs

from shared.models import AlertContext, CommandRequest
from shared.secrets import resolve_secret

logger = logging.getLogger(__name__)


class WebhookAdapter(Protocol):
    """Seam between the shared intake pipeline and a chat platform."""

    def verify_signature(self, headers: dict, raw_body: str) -> bool: ...

    def get_challenge_response(self, payload: dict) -> dict | None:
        """Return an HTTP response dict if this is a platform challenge, else None."""
        ...

    def parse_alert_context(self, payload: dict) -> AlertContext: ...

    def is_command(self, headers: dict, raw_body: str) -> bool:
        """Return True if the request is a slash command rather than an event."""
        ...

    def parse_command(self, raw_body: str) -> CommandRequest:
        """Parse a slash command payload into a CommandRequest."""
        ...

    def ack_command(self, command: CommandRequest, text: str) -> None:
        """Send an immediate acknowledgment for a slash command."""
        ...


class SlackWebhookAdapter:
    """WebhookAdapter for Slack Events API."""

    def __init__(
        self,
        signing_secret: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._signing_secret = signing_secret or resolve_secret("SLACK_SIGNING_SECRET")
        self._bot_token = bot_token or resolve_secret("SLACK_BOT_TOKEN")

    def verify_signature(self, headers: dict, raw_body: str) -> bool:
        from lambda_adapter.slack.signature import verify_slack_signature

        return verify_slack_signature(
            self._signing_secret,
            headers.get("x-slack-request-timestamp", ""),
            raw_body,
            headers.get("x-slack-signature", ""),
        )

    def get_challenge_response(self, payload: dict) -> dict | None:
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        return None

    def parse_alert_context(self, payload: dict) -> AlertContext:
        from lambda_adapter.slack.parser import parse_alert_context

        return parse_alert_context(payload)

    def is_command(self, headers: dict, raw_body: str) -> bool:
        content_type = headers.get("content-type", "")
        return "application/x-www-form-urlencoded" in content_type and "command=" in raw_body

    def parse_command(self, raw_body: str) -> CommandRequest:
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

    def ack_command(self, command: CommandRequest, text: str) -> None:
        import urllib.request

        if not command.response_url:
            return
        data = json.dumps({"response_type": "ephemeral", "text": text}).encode()
        req = urllib.request.Request(
            command.response_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req)


class DiscordWebhookAdapter:
    """WebhookAdapter for Discord Interactions API."""

    def __init__(
        self,
        public_key: str | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._public_key = public_key or resolve_secret("DISCORD_PUBLIC_KEY")
        self._bot_token = bot_token or resolve_secret("DISCORD_BOT_TOKEN")

    def verify_signature(self, headers: dict, raw_body: str) -> bool:
        from lambda_adapter.discord.signature import verify_discord_signature

        return verify_discord_signature(
            self._public_key,
            headers.get("x-signature-timestamp", ""),
            raw_body,
            headers.get("x-signature-ed25519", ""),
        )

    def get_challenge_response(self, payload: dict) -> dict | None:
        if payload.get("type") == 1:
            return {"type": 1}
        return None

    def parse_alert_context(self, payload: dict) -> AlertContext:
        from lambda_adapter.discord.parser import parse_alert_context

        return parse_alert_context(payload)

    def is_command(self, headers: dict, raw_body: str) -> bool:
        # Discord slash commands arrive as interaction type 2
        try:
            payload = json.loads(raw_body)
            return payload.get("type") == 2
        except (json.JSONDecodeError, TypeError):
            return False

    def parse_command(self, raw_body: str) -> CommandRequest:
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
            platform_metadata={"interaction_id": payload.get("id", ""), "interaction_token": payload.get("token", "")},
        )

    def ack_command(self, command: CommandRequest, text: str) -> None:
        import urllib.request

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


def detect_platform(headers: dict) -> str:
    """Sniff platform from request headers. Returns 'slack', 'discord', or raises."""
    if "x-slack-signature" in headers or "x-slack-request-timestamp" in headers:
        return "slack"
    if "x-signature-ed25519" in headers or "x-signature-timestamp" in headers:
        return "discord"
    raise ValueError("Unable to detect platform from request headers")


def create_webhook_adapter(platform: str) -> WebhookAdapter:
    """Factory — returns the adapter for the detected platform."""
    if platform == "slack":
        return SlackWebhookAdapter()
    if platform == "discord":
        return DiscordWebhookAdapter()
    raise ValueError(f"Unsupported platform: {platform!r}")
