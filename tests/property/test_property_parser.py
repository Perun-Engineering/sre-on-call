"""Property-based tests for alert context extraction."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.platforms.slack import parse_alert_context
from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import AlertContext

# --- Strategies ---

# Slack channel IDs: uppercase letter followed by alphanumeric characters.
channel_ids = st.from_regex(r"C[A-Z0-9]{4,12}", fullmatch=True)

# Slack message timestamps: epoch seconds with microsecond fractional part.
# Use a realistic range (2020-01-01 to 2030-01-01) to avoid edge cases with
# extremely small or large floats.
_MIN_EPOCH = 1_577_836_800  # 2020-01-01 00:00:00 UTC
_MAX_EPOCH = 1_893_456_000  # 2030-01-01 00:00:00 UTC

message_timestamps = st.integers(min_value=_MIN_EPOCH, max_value=_MAX_EPOCH).flatmap(
    lambda epoch: st.integers(min_value=0, max_value=999_999).map(
        lambda micro: f"{epoch}.{micro:06d}"
    )
)

# Alert text: arbitrary non-empty strings.
alert_texts = st.text(min_size=1, max_size=300)

# Event timestamps: same format as message timestamps but can differ.
event_timestamps = st.integers(min_value=_MIN_EPOCH, max_value=_MAX_EPOCH).flatmap(
    lambda epoch: st.integers(min_value=0, max_value=999_999).map(
        lambda micro: f"{epoch}.{micro:06d}"
    )
)


def _build_slack_payload(
    channel_id: str,
    message_ts: str,
    text: str,
    event_ts: str,
) -> dict:
    """Build a Slack Events API event_callback payload from components."""
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": channel_id,
            "ts": message_ts,
            "text": text,
            "event_ts": event_ts,
        },
    }


@settings(max_examples=150)
@given(
    channel_id=channel_ids,
    message_ts=message_timestamps,
    text=alert_texts,
    event_ts=event_timestamps,
)
def test_alert_context_extraction_preserves_all_fields(
    channel_id: str,
    message_ts: str,
    text: str,
    event_ts: str,
) -> None:
    """
    For any valid Slack Events API payload containing a channel_id, message_ts,
    text, and event timestamp, ``parse_alert_context`` SHALL produce an
    ``AlertContext`` where channel_id, message_ts, alert_text, and
    alert_timestamp exactly match the corresponding values from the input
    payload. The investigation_window SHALL be ±5 minutes from the alert
    timestamp, and investigation_id SHALL be a valid UUID.
    """
    payload = _build_slack_payload(channel_id, message_ts, text, event_ts)
    ctx = parse_alert_context(payload)

    # Result is an AlertContext instance
    assert isinstance(ctx, AlertContext)

    # channel_id matches input
    assert ctx.channel_id == channel_id

    # message_id matches input
    assert ctx.message_id == message_ts

    # alert_text matches input
    assert ctx.alert_text == text

    # alert_timestamp is correctly derived from the event timestamp
    epoch_seconds = float(event_ts)
    expected_dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    assert ctx.alert_timestamp == expected_dt.isoformat()

    # investigation_window is ±5 minutes from the alert timestamp
    half_window = timedelta(minutes=INVESTIGATION_WINDOW_MINUTES / 2)
    expected_start = (expected_dt - half_window).isoformat()
    expected_end = (expected_dt + half_window).isoformat()
    assert ctx.investigation_window == (expected_start, expected_end)

    # investigation_id is a valid UUID (raises ValueError if not)
    uuid.UUID(ctx.investigation_id)
