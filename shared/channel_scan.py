"""Shared channel-scanning algorithm for all chat-platform scanner agents.

Owns the scanning loop: select channels → compute window → fetch →
filter → build Findings.  Platform-specific behaviour is delegated to
a ChannelMessageSource adapter.
"""

from __future__ import annotations

import logging
from typing import Protocol

from shared.constants import MAX_CHANNELS
from shared.models import Finding
from shared.time_utils import compute_investigation_window
from shared.tool_result import ToolResult

logger = logging.getLogger(__name__)

ALERT_KEYWORDS: frozenset[str] = frozenset({
    "alert",
    "error",
    "warning",
    "critical",
    "incident",
    "failure",
})


class ChannelMessageSource(Protocol):
    """Seam between the scanning algorithm and a chat platform."""

    def fetch_messages(
        self, channel_id: str, start_iso: str, end_iso: str
    ) -> list[dict]:
        """Return raw messages from *channel_id* within the time window."""
        ...  # pragma: no cover

    def is_alert(self, message: dict) -> bool:
        """Return True if *message* is a bot alert or matches alert keywords."""
        ...  # pragma: no cover

    def extract_finding(self, channel_id: str, message: dict) -> Finding:
        """Convert a raw platform message into a Finding."""
        ...  # pragma: no cover


def execute_channel_scan(
    alert_timestamp: str,
    channel_ids: list[str],
    source: ChannelMessageSource,
) -> ToolResult:
    """Run the shared scanning algorithm using *source* for platform I/O."""
    result = ToolResult()

    channels = channel_ids[:MAX_CHANNELS]

    try:
        start_iso, end_iso = compute_investigation_window(alert_timestamp)
    except ValueError as exc:
        result.errors.append(f"Invalid alert_timestamp: {exc}")
        return result

    for channel_id in channels:
        try:
            messages = source.fetch_messages(channel_id, start_iso, end_iso)
            result.scanned_items.append(channel_id)

            for msg in messages:
                if source.is_alert(msg):
                    result.findings.append(
                        source.extract_finding(channel_id, msg)
                    )
        except Exception as exc:
            error_msg = f"API error for channel {channel_id}: {exc}"
            logger.warning(error_msg)
            result.errors.append(error_msg)

    return result
