#!/usr/bin/env python3
"""Send a synthetic, correctly-signed Slack ``app_mention`` event to the
deployed Lambda function URL and print the response.

Usage::

    SLACK_SIGNING_SECRET=... \
    ./scripts/synthetic_slack_webhook.py \
        --url https://<id>.lambda-url.us-east-1.on.aws/ \
        --channel <channel-id> \
        --team <team-id> \
        [--text "ALERT: high CPU on api-server"]

After it returns, tail the Lambda log group::

    aws logs tail /aws/lambda/sre-on-call-dev-lambda-adapter \
        --follow --profile <profile>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


def sign(signing_secret: str, timestamp: str, body: str) -> str:
    base = f"v0:{timestamp}:{body}".encode("utf-8")
    digest = hmac.new(
        signing_secret.encode("utf-8"), base, hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--url", required=True, help="Lambda function URL")
    parser.add_argument("--channel", required=True, help="Slack channel ID")
    parser.add_argument("--team", required=True, help="Slack workspace team ID")
    parser.add_argument(
        "--text",
        default="ALERT: synthetic high-CPU alert from the test webhook",
        help="Alert text body",
    )
    parser.add_argument(
        "--user", default="U0SYNTHETIC", help="Slack user id of the message author"
    )
    args = parser.parse_args()

    signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not signing_secret:
        print("SLACK_SIGNING_SECRET env var is required", file=sys.stderr)
        return 2

    now = int(time.time())
    payload = {
        "type": "event_callback",
        "team_id": args.team,
        "event_id": f"Ev{uuid.uuid4().hex[:10].upper()}",
        "event_time": now,
        "event": {
            "type": "app_mention",
            "user": args.user,
            "text": args.text,
            "ts": f"{now}.000100",
            "channel": args.channel,
            "event_ts": f"{now}.000100",
        },
    }
    body = json.dumps(payload, separators=(",", ":"))
    ts = str(now)
    signature = sign(signing_secret, ts, body)

    req = urllib.request.Request(
        args.url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": signature,
            "X-Slack-Request-Timestamp": ts,
            "User-Agent": "sre-on-call-synthetic-test/1",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"HTTP {resp.status}")
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}", file=sys.stderr)
        print(exc.read().decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
