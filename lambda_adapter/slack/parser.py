"""Parse Slack Events API payloads into AlertContext."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import AlertContext


def parse_alert_context(event_payload: dict) -> AlertContext:
    """Extract alert context from a Slack Events API event_callback payload.

    Args:
        event_payload: The full Slack Events API payload with type "event_callback".

    Returns:
        An AlertContext populated from the event fields.

    Raises:
        KeyError: If required fields are missing from the payload.
    """
    event = event_payload["event"]

    channel_id = event["channel"]
    message_ts = event["ts"]
    alert_text = event["text"]

    # Convert Slack epoch timestamp to ISO 8601
    event_ts = event.get("event_ts", event["ts"])
    epoch_seconds = float(event_ts)
    alert_dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    alert_timestamp = alert_dt.isoformat()

    # Compute investigation window: ±half of INVESTIGATION_WINDOW_MINUTES
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
