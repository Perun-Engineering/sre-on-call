"""Unit tests for lambda_adapter.parser — alert context extraction."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from shared.platforms.slack import parse_alert_context
from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import AlertContext


def _make_event_payload(
    channel: str = "C1234567890",
    ts: str = "1234567890.123456",
    text: str = "Alert: CPU usage above 90%",
    event_ts: str | None = None,
) -> dict:
    """Build a minimal Slack Events API event_callback payload."""
    event: dict = {
        "type": "message",
        "channel": channel,
        "ts": ts,
        "text": text,
    }
    if event_ts is not None:
        event["event_ts"] = event_ts
    else:
        event["event_ts"] = ts
    return {"type": "event_callback", "event": event}


class TestParseAlertContext:
    """Tests for parse_alert_context."""

    def test_extracts_channel_id(self) -> None:
        payload = _make_event_payload(channel="C9999")
        ctx = parse_alert_context(payload)
        assert ctx.channel_id == "C9999"

    def test_extracts_message_ts(self) -> None:
        payload = _make_event_payload(ts="1111111111.000001")
        ctx = parse_alert_context(payload)
        assert ctx.message_id == "1111111111.000001"

    def test_extracts_alert_text(self) -> None:
        payload = _make_event_payload(text="Disk full on host-42")
        ctx = parse_alert_context(payload)
        assert ctx.alert_text == "Disk full on host-42"

    def test_alert_timestamp_is_iso8601(self) -> None:
        ts = "1705312320.000000"  # 2024-01-15 12:32:00 UTC
        payload = _make_event_payload(ts=ts)
        ctx = parse_alert_context(payload)
        # Should be parseable as ISO 8601
        parsed = datetime.fromisoformat(ctx.alert_timestamp)
        assert parsed.tzinfo is not None  # timezone-aware

    def test_alert_timestamp_matches_event_ts(self) -> None:
        ts = "1705312320.000000"
        payload = _make_event_payload(ts=ts, event_ts=ts)
        ctx = parse_alert_context(payload)
        expected_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        assert ctx.alert_timestamp == expected_dt.isoformat()

    def test_investigation_window_is_symmetric(self) -> None:
        ts = "1705312320.000000"
        payload = _make_event_payload(ts=ts)
        ctx = parse_alert_context(payload)

        alert_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        half = timedelta(minutes=INVESTIGATION_WINDOW_MINUTES / 2)
        expected_start = (alert_dt - half).isoformat()
        expected_end = (alert_dt + half).isoformat()

        assert ctx.investigation_window == (expected_start, expected_end)

    def test_investigation_id_is_valid_uuid(self) -> None:
        payload = _make_event_payload()
        ctx = parse_alert_context(payload)
        # Should not raise
        uuid.UUID(ctx.investigation_id)

    def test_investigation_id_is_unique_per_call(self) -> None:
        payload = _make_event_payload()
        ctx1 = parse_alert_context(payload)
        ctx2 = parse_alert_context(payload)
        assert ctx1.investigation_id != ctx2.investigation_id

    def test_returns_alert_context_instance(self) -> None:
        payload = _make_event_payload()
        ctx = parse_alert_context(payload)
        assert isinstance(ctx, AlertContext)

    def test_uses_event_ts_over_ts_when_present(self) -> None:
        payload = _make_event_payload(ts="1000000000.000000", event_ts="1705312320.000000")
        ctx = parse_alert_context(payload)
        expected_dt = datetime.fromtimestamp(1705312320.0, tz=timezone.utc)
        assert ctx.alert_timestamp == expected_dt.isoformat()

    def test_falls_back_to_ts_when_event_ts_missing(self) -> None:
        payload = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "ts": "1705312320.000000",
                "text": "alert",
            },
        }
        ctx = parse_alert_context(payload)
        expected_dt = datetime.fromtimestamp(1705312320.0, tz=timezone.utc)
        assert ctx.alert_timestamp == expected_dt.isoformat()

    def test_missing_event_key_raises(self) -> None:
        with pytest.raises(KeyError):
            parse_alert_context({"type": "event_callback"})

    def test_missing_channel_raises(self) -> None:
        payload = {
            "type": "event_callback",
            "event": {"type": "message", "ts": "123.456", "text": "hi"},
        }
        with pytest.raises(KeyError):
            parse_alert_context(payload)
