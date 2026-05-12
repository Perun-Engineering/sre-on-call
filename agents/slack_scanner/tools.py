"""Slack Scanner Agent tools — scan channel history for correlated alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from strands import tool

from shared.channel_scan import ALERT_KEYWORDS, execute_channel_scan
from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import Finding
from shared.secrets import resolve_secret
from shared.tool_result import build_agent_result, format_result, severity_from_text

logger = logging.getLogger(__name__)


class SlackMessageSource:
    """ChannelMessageSource adapter for Slack."""

    def __init__(self, client: WebClient) -> None:
        self._client = client

    def fetch_messages(
        self, channel_id: str, start_iso: str, end_iso: str
    ) -> list[dict]:
        start_epoch = str(datetime.fromisoformat(start_iso).timestamp())
        end_epoch = str(datetime.fromisoformat(end_iso).timestamp())
        response = self._client.conversations_history(
            channel=channel_id,
            oldest=start_epoch,
            latest=end_epoch,
            inclusive=True,
            limit=200,
        )
        return response.get("messages", [])

    def is_alert(self, message: dict) -> bool:
        if message.get("bot_id"):
            return True
        if message.get("subtype") == "bot_message":
            return True
        text = (message.get("text") or "").lower()
        return any(kw in text for kw in ALERT_KEYWORDS)

    def extract_finding(self, channel_id: str, message: dict) -> Finding:
        text = message.get("text", "")
        return Finding(
            source=channel_id,
            timestamp=_slack_ts_to_iso(message.get("ts", "")),
            content=text,
            severity=severity_from_text(text),
            metadata={
                "channel_id": channel_id,
                "message_ts": message.get("ts", ""),
                "bot_id": message.get("bot_id", ""),
                "subtype": message.get("subtype", ""),
            },
        )


def _slack_ts_to_iso(ts: str) -> str:
    """Convert a Slack message timestamp (epoch.micro) to ISO 8601."""
    try:
        epoch = float(ts)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return ts


@tool
def scan_slack_channels(
    alert_timestamp: str,
    bot_channel_ids: list[str],
    window_minutes: int = INVESTIGATION_WINDOW_MINUTES,
) -> str:
    """Scan Slack channels for correlated alerts around an incident timestamp.

    Scans up to 10 channels the bot is a member of. Retrieves messages within
    the investigation window (±5 minutes of *alert_timestamp*). Filters for
    alerts, notifications, and error messages from integration bots. Returns a
    correlation summary with findings and source channels.

    Args:
        alert_timestamp: ISO 8601 timestamp of the triggering alert.
        bot_channel_ids: Channel IDs the bot is a member of (max 10 scanned).
        window_minutes: Total investigation window size in minutes (default 10).

    Returns:
        A JSON-serialisable string summarising the scan results.
    """
    client = WebClient(token=resolve_secret("SLACK_BOT_TOKEN"))
    source = SlackMessageSource(client)
    result = execute_channel_scan(alert_timestamp, bot_channel_ids, source)
    return format_result(build_agent_result("slack_scanner", result))
