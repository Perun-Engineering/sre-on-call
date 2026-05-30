"""Unit tests for shared.time_utils."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from shared.time_utils import compute_investigation_window


class TestComputeInvestigationWindow:
    """Tests for compute_investigation_window."""

    def test_basic_utc_timestamp(self) -> None:
        """Window is ±5 minutes around a UTC timestamp."""
        alert_ts = "2025-01-15T14:32:00+00:00"
        start, end = compute_investigation_window(alert_ts)

        assert start == "2025-01-15T14:27:00+00:00"
        assert end == "2025-01-15T14:37:00+00:00"

    def test_naive_timestamp_defaults_to_utc(self) -> None:
        """A naive (no timezone) timestamp is treated as UTC."""
        alert_ts = "2025-06-01T12:00:00"
        start, end = compute_investigation_window(alert_ts)

        assert start == "2025-06-01T11:55:00+00:00"
        assert end == "2025-06-01T12:05:00+00:00"

    def test_non_utc_timezone_preserved(self) -> None:
        """Timezone offset is preserved in the output."""
        alert_ts = "2025-03-10T08:00:00+05:30"
        start, end = compute_investigation_window(alert_ts)

        assert start == "2025-03-10T07:55:00+05:30"
        assert end == "2025-03-10T08:05:00+05:30"

    def test_window_spans_midnight(self) -> None:
        """Window correctly crosses a date boundary."""
        alert_ts = "2025-01-15T00:02:00+00:00"
        start, end = compute_investigation_window(alert_ts)

        assert start == "2025-01-14T23:57:00+00:00"
        assert end == "2025-01-15T00:07:00+00:00"

    def test_window_symmetry(self) -> None:
        """Start and end are equidistant from the alert timestamp."""
        alert_ts = "2025-07-04T18:30:00+00:00"
        start, end = compute_investigation_window(alert_ts)

        alert_dt = datetime.fromisoformat(alert_ts)
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)

        assert alert_dt - start_dt == end_dt - alert_dt
        assert alert_dt - start_dt == timedelta(minutes=5)

    def test_window_total_duration(self) -> None:
        """Total window duration is exactly 10 minutes."""
        alert_ts = "2025-01-15T14:32:00+00:00"
        start, end = compute_investigation_window(alert_ts)

        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)

        assert end_dt - start_dt == timedelta(minutes=10)

    def test_subsecond_precision_preserved(self) -> None:
        """Microsecond precision in the input is preserved."""
        alert_ts = "2025-01-15T14:32:00.123456+00:00"
        start, end = compute_investigation_window(alert_ts)

        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)

        assert start_dt.microsecond == 123456
        assert end_dt.microsecond == 123456

    def test_invalid_timestamp_raises_value_error(self) -> None:
        """Non-ISO 8601 input raises ValueError."""
        with pytest.raises(ValueError):
            compute_investigation_window("not-a-timestamp")

    def test_return_types_are_strings(self) -> None:
        """Both elements of the returned tuple are strings."""
        start, end = compute_investigation_window("2025-01-15T14:32:00+00:00")

        assert isinstance(start, str)
        assert isinstance(end, str)
