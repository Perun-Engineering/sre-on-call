"""Time utilities for sre-on-call."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.constants import INVESTIGATION_WINDOW_MINUTES


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def compute_investigation_window(alert_timestamp: str) -> tuple[str, str]:
    """Compute the investigation window centered on the alert timestamp.

    The window spans ±(INVESTIGATION_WINDOW_MINUTES / 2) minutes around the
    given alert timestamp, producing a symmetric 10-minute window by default.

    Args:
        alert_timestamp: ISO 8601 formatted timestamp of the alert.

    Returns:
        A tuple of ``(start_iso, end_iso)`` where *start* is the alert time
        minus half the window and *end* is the alert time plus half the window.
        Both values are ISO 8601 strings.

    Raises:
        ValueError: If *alert_timestamp* cannot be parsed as ISO 8601.
    """
    alert_dt = datetime.fromisoformat(alert_timestamp)

    # Ensure the datetime is timezone-aware (default to UTC when no tz info)
    if alert_dt.tzinfo is None:
        alert_dt = alert_dt.replace(tzinfo=timezone.utc)

    half_window = timedelta(minutes=INVESTIGATION_WINDOW_MINUTES / 2)
    start_dt = alert_dt - half_window
    end_dt = alert_dt + half_window

    return start_dt.isoformat(), end_dt.isoformat()
