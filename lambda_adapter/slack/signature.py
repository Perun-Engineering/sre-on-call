"""Slack request signature verification.

Implements HMAC-SHA256 signature verification for Slack Events API webhooks,
including replay attack protection via timestamp validation.
"""

from __future__ import annotations

import hashlib
import hmac
import time


# Maximum age of a request timestamp before it is rejected (replay protection).
_MAX_TIMESTAMP_AGE_SECONDS = 5 * 60  # 5 minutes


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: str,
    signature: str,
) -> bool:
    """Verify a Slack request signature.

    Computes HMAC-SHA256 of ``v0:{timestamp}:{body}`` using *signing_secret*
    and compares the result against the provided *signature* using
    constant-time comparison.  Requests whose timestamp is older than
    5 minutes are rejected to guard against replay attacks.

    Args:
        signing_secret: The Slack app signing secret.
        timestamp: Value of the ``X-Slack-Request-Timestamp`` header.
        body: The raw request body string.
        signature: Value of the ``X-Slack-Signature`` header (e.g. ``v0=abc...``).

    Returns:
        ``True`` when the signature is valid **and** the timestamp is fresh;
        ``False`` otherwise.
    """
    # --- Replay protection ---------------------------------------------------
    try:
        request_ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    current_ts = int(time.time())
    if abs(current_ts - request_ts) > _MAX_TIMESTAMP_AGE_SECONDS:
        return False

    # --- HMAC-SHA256 verification --------------------------------------------
    sig_basestring = f"v0:{timestamp}:{body}"
    computed_hash = hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    computed_signature = f"v0={computed_hash}"

    return hmac.compare_digest(computed_signature, signature)
