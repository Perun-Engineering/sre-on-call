"""Property-based tests for time utilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.time_utils import compute_investigation_window

# Strategy: generate timezone-aware ISO 8601 timestamps.
# We build a datetime from components and attach a random UTC offset.
_utc_offsets = st.sampled_from([timezone(timedelta(hours=h)) for h in range(-12, 15)])

_aware_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 2),
    max_value=datetime(2099, 12, 30),
    timezones=_utc_offsets,
)


@settings(max_examples=150)
@given(alert_dt=_aware_datetimes)
def test_investigation_window_symmetric_around_alert(
    alert_dt: datetime,
) -> None:
    """
    For any valid timezone-aware ISO 8601 timestamp T:
    1. start == T - 5 minutes
    2. end == T + 5 minutes
    3. Total window duration is exactly 10 minutes
    4. The window is symmetric: T - start == end - T
    """
    alert_timestamp = alert_dt.isoformat()

    start_iso, end_iso = compute_investigation_window(alert_timestamp)

    start_dt = datetime.fromisoformat(start_iso)
    end_dt = datetime.fromisoformat(end_iso)

    five_minutes = timedelta(minutes=5)
    ten_minutes = timedelta(minutes=10)

    # 1. start == alert_timestamp - 5 minutes
    assert start_dt == alert_dt - five_minutes, (
        f"start {start_dt} != alert {alert_dt} - 5min"
    )

    # 2. end == alert_timestamp + 5 minutes
    assert end_dt == alert_dt + five_minutes, f"end {end_dt} != alert {alert_dt} + 5min"

    # 3. Total window is exactly 10 minutes
    assert end_dt - start_dt == ten_minutes, (
        f"window duration {end_dt - start_dt} != 10min"
    )

    # 4. Symmetry: alert - start == end - alert
    assert alert_dt - start_dt == end_dt - alert_dt, (
        f"asymmetric: left={alert_dt - start_dt}, right={end_dt - alert_dt}"
    )
