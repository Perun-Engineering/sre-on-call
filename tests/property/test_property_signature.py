"""Property-based tests for Slack signature verification."""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.platforms.slack import verify_slack_signature


def _compute_signature(signing_secret: str, timestamp: str, body: str) -> str:
    """Compute a valid Slack HMAC-SHA256 signature."""
    sig_basestring = f"v0:{timestamp}:{body}"
    hex_digest = hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"v0={hex_digest}"


def _corrupt_signature(signature: str) -> str:
    """Flip a single hex character in the hash portion of the signature.

    The signature format is ``v0=<64 hex chars>``.  We pick a character in the
    hex portion and replace it with a different hex digit so the result is
    always a structurally valid but *incorrect* signature.
    """
    prefix = "v0="
    hex_part = signature[len(prefix) :]
    # Pick the first character and flip it
    original_char = hex_part[0]
    # Choose a different hex digit
    replacement = "0" if original_char != "0" else "1"
    corrupted_hex = replacement + hex_part[1:]
    return prefix + corrupted_hex


# Strategy: signing secrets are non-empty ASCII strings (Slack secrets are hex strings,
# but the HMAC computation works with any string).
signing_secrets = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "S")),
    min_size=1,
    max_size=64,
)

# Strategy: timestamps within the valid 5-minute window relative to "now".
# We freeze time in the test so we control what "now" means.
timestamps_within_window = st.integers(min_value=0, max_value=5 * 60)

# Strategy: arbitrary request body strings.
bodies = st.text(min_size=0, max_size=500)


@settings(max_examples=150)
@given(secret=signing_secrets, offset=timestamps_within_window, body=bodies)
def test_signature_round_trip_accepts_correct_and_rejects_corrupted(
    secret: str,
    offset: int,
    body: str,
) -> None:
    """
    For any (signing_secret, timestamp, body) tuple where the timestamp is
    within the valid 5-minute window:
    1. Computing the correct HMAC-SHA256 signature and calling
       ``verify_slack_signature`` SHALL return True.
    2. Corrupting the signature by flipping a character SHALL cause
       ``verify_slack_signature`` to return False.
    """
    # Fix "now" so the timestamp window is deterministic.
    frozen_now = 1_700_000_000
    ts = str(frozen_now - offset)

    correct_sig = _compute_signature(secret, ts, body)
    corrupted_sig = _corrupt_signature(correct_sig)

    with patch("shared.platforms.slack.time") as mock_time:
        mock_time.time.return_value = float(frozen_now)

        # Correct signature must be accepted
        assert verify_slack_signature(secret, ts, body, correct_sig) is True

        # Corrupted signature must be rejected
        assert verify_slack_signature(secret, ts, body, corrupted_sig) is False
