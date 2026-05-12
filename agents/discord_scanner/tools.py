"""Discord Scanner Agent tools — scan channel history for correlated alerts."""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from strands import tool

from shared.channel_scan import ALERT_KEYWORDS, execute_channel_scan
from shared.models import Finding
from shared.secrets import resolve_secret
from shared.tool_result import build_agent_result, format_result, severity_from_text

logger = logging.getLogger(__name__)


class DiscordMessageSource:
    """ChannelMessageSource adapter for Discord."""

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token

    def fetch_messages(
        self, channel_id: str, start_iso: str, end_iso: str
    ) -> list[dict]:
        after = _timestamp_to_snowflake(start_iso)
        before = _timestamp_to_snowflake(end_iso)
        url = (
            f"https://discord.com/api/v10/channels/{channel_id}/messages"
            f"?limit=100&after={after}&before={before}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def is_alert(self, message: dict) -> bool:
        if message.get("author", {}).get("bot", False):
            return True
        if message.get("webhook_id"):
            return True
        text = (message.get("content") or "").lower()
        return any(kw in text for kw in ALERT_KEYWORDS)

    def extract_finding(self, channel_id: str, message: dict) -> Finding:
        text = message.get("content", "")
        return Finding(
            source=channel_id,
            timestamp=message.get("timestamp", ""),
            content=text,
            severity=severity_from_text(text),
            metadata={
                "channel_id": channel_id,
                "message_id": message.get("id", ""),
                "author_id": message.get("author", {}).get("id", ""),
                "is_bot": message.get("author", {}).get("bot", False),
            },
        )


def _timestamp_to_snowflake(iso_timestamp: str) -> str:
    """Convert an ISO 8601 timestamp to a Discord snowflake ID (approximate)."""
    dt = datetime.fromisoformat(iso_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    discord_epoch_ms = int(dt.timestamp() * 1000) - 1420070400000
    return str(discord_epoch_ms << 22)


@tool
def scan_discord_channels(
    alert_timestamp: str,
    channel_ids: list[str],
    window_minutes: int = 10,
) -> str:
    """Scan Discord channels for correlated alerts around an incident timestamp.

    Args:
        alert_timestamp: ISO 8601 timestamp of the triggering alert.
        channel_ids: Discord channel IDs to scan (max 10).
        window_minutes: Total investigation window size in minutes (default 10).

    Returns:
        A human-readable summary string for the LLM to consume.
    """
    source = DiscordMessageSource(resolve_secret("DISCORD_BOT_TOKEN"))
    result = execute_channel_scan(alert_timestamp, channel_ids, source)
    return format_result(build_agent_result("discord_scanner", result))
