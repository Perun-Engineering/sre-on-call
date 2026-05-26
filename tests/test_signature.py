"""Unit tests for Slack signature verification."""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import patch

from shared.platforms.slack import verify_slack_signature


def _compute_signature(signing_secret: str, timestamp: str, body: str) -> str:
    """Helper: compute a valid Slack signature."""
    sig_basestring = f"v0:{timestamp}:{body}"
    hex_digest = hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v0={hex_digest}"


class TestVerifySlackSignature:
    """Tests for verify_slack_signature."""

    def test_valid_signature_accepted(self) -> None:
        secret = "my_secret"
        ts = str(int(time.time()))
        body = '{"type":"event_callback"}'
        sig = _compute_signature(secret, ts, body)

        assert verify_slack_signature(secret, ts, body, sig) is True

    def test_wrong_signature_rejected(self) -> None:
        secret = "my_secret"
        ts = str(int(time.time()))
        body = '{"type":"event_callback"}'
        bad_sig = "v0=0000000000000000000000000000000000000000000000000000000000000000"

        assert verify_slack_signature(secret, ts, body, bad_sig) is False

    def test_wrong_secret_rejected(self) -> None:
        secret = "my_secret"
        ts = str(int(time.time()))
        body = '{"type":"event_callback"}'
        sig = _compute_signature(secret, ts, body)

        assert verify_slack_signature("wrong_secret", ts, body, sig) is False

    def test_tampered_body_rejected(self) -> None:
        secret = "my_secret"
        ts = str(int(time.time()))
        body = '{"type":"event_callback"}'
        sig = _compute_signature(secret, ts, body)

        assert verify_slack_signature(secret, ts, body + "x", sig) is False

    def test_stale_timestamp_rejected(self) -> None:
        secret = "my_secret"
        stale_ts = str(int(time.time()) - 6 * 60)  # 6 minutes ago
        body = '{"type":"event_callback"}'
        sig = _compute_signature(secret, stale_ts, body)

        assert verify_slack_signature(secret, stale_ts, body, sig) is False

    def test_future_timestamp_beyond_window_rejected(self) -> None:
        secret = "my_secret"
        future_ts = str(int(time.time()) + 6 * 60)  # 6 minutes in the future
        body = '{"type":"event_callback"}'
        sig = _compute_signature(secret, future_ts, body)

        assert verify_slack_signature(secret, future_ts, body, sig) is False

    def test_non_numeric_timestamp_rejected(self) -> None:
        secret = "my_secret"
        body = '{"type":"event_callback"}'
        sig = _compute_signature(secret, "not-a-number", body)

        assert verify_slack_signature(secret, "not-a-number", body, sig) is False

    def test_empty_body(self) -> None:
        secret = "my_secret"
        ts = str(int(time.time()))
        body = ""
        sig = _compute_signature(secret, ts, body)

        assert verify_slack_signature(secret, ts, body, sig) is True

    def test_timestamp_exactly_at_boundary_accepted(self) -> None:
        """A timestamp exactly 5 minutes old should still be accepted."""
        secret = "my_secret"
        now = int(time.time())
        boundary_ts = str(now - 5 * 60)
        body = "test"
        sig = _compute_signature(secret, boundary_ts, body)

        with patch("shared.platforms.slack.time") as mock_time:
            mock_time.time.return_value = float(now)
            assert verify_slack_signature(secret, boundary_ts, body, sig) is True

    def test_timestamp_one_second_past_boundary_rejected(self) -> None:
        """A timestamp 5 minutes + 1 second old should be rejected."""
        secret = "my_secret"
        now = int(time.time())
        past_ts = str(now - 5 * 60 - 1)
        body = "test"
        sig = _compute_signature(secret, past_ts, body)

        with patch("shared.platforms.slack.time") as mock_time:
            mock_time.time.return_value = float(now)
            assert verify_slack_signature(secret, past_ts, body, sig) is False
