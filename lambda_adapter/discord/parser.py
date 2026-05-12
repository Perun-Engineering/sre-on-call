"""Parse Discord Interactions API payloads into AlertContext."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import AlertContext


def parse_alert_context(interaction_payload: dict) -> AlertContext:
    """Extract alert context from a Discord message or interaction payload.

    Supports MESSAGE_CREATE gateway events forwarded through a webhook
    adapter.  The payload is expected to contain at minimum ``channel_id``,
    ``id`` (message ID), ``content``, and ``timestamp``.

    Args:
        interaction_payload: The Discord event payload.

    Returns:
        An AlertContext populated from the event fields.

    Raises:
        KeyError: If required fields are missing from the payload.
    """
    channel_id = interaction_payload["channel_id"]
    message_id = str(interaction_payload["id"])
    alert_text = interaction_payload.get("content", "")

    # Discord timestamps are ISO 8601
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
