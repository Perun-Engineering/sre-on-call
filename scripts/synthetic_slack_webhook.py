#!/usr/bin/env python3
"""Send a synthetic, correctly-signed Slack request to the deployed Lambda
function URL and print the response.

By default the script sends an ``app_mention`` event (JSON body) — the
same shape Slack would send when a user mentions the bot in a channel.

With ``--command`` it sends a slash-command POST instead — the same shape
Slack would send when a user runs ``/sre-snapshot`` (or ``/postmortem``) in a
channel. The body is form-encoded and the content type changes
accordingly; the signing scheme is identical.

Usage::

    # app_mention event (alert path)
    SLACK_SIGNING_SECRET=... \\
    ./scripts/synthetic_slack_webhook.py \\
        --url https://<id>.lambda-url.us-east-1.on.aws/ \\
        --channel <channel-id> \\
        --team <team-id> \\
        [--text "ALERT: high CPU on api-server"]

    # /sre-snapshot slash command (snapshot path)
    SLACK_SIGNING_SECRET=... \\
    ./scripts/synthetic_slack_webhook.py \\
        --url https://<id>.lambda-url.us-east-1.on.aws/ \\
        --channel <channel-id> \\
        --team <team-id> \\
        --command /sre-snapshot

    # /postmortem slash command (PIR path — requires a thread_ts)
    SLACK_SIGNING_SECRET=... \\
    ./scripts/synthetic_slack_webhook.py \\
        --url https://<id>.lambda-url.us-east-1.on.aws/ \\
        --channel <channel-id> \\
        --team <team-id> \\
        --command /postmortem \\
        --thread-ts 1700000000.000100

After it returns, tail the Lambda log group::

    aws logs tail /aws/lambda/sre-on-call-dev-lambda-adapter \\
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
import urllib.parse
import urllib.request
import uuid


def sign(signing_secret: str, timestamp: str, body: str) -> str:
    """Compute Slack's HMAC-SHA256 signature for ``v0:{ts}:{body}``."""
    base = f"v0:{timestamp}:{body}".encode("utf-8")
    digest = hmac.new(
        signing_secret.encode("utf-8"), base, hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def build_app_mention_body(
    *, team: str, channel: str, user: str, text: str, now: int,
) -> tuple[str, str]:
    """Build a Slack ``app_mention`` event payload.

    Returns ``(body, content_type)`` ready for signing and POSTing.
    """
    payload = {
        "type": "event_callback",
        "team_id": team,
        "event_id": f"Ev{uuid.uuid4().hex[:10].upper()}",
        "event_time": now,
        "event": {
            "type": "app_mention",
            "user": user,
            "text": text,
            "ts": f"{now}.000100",
            "channel": channel,
            "event_ts": f"{now}.000100",
        },
    }
    return json.dumps(payload, separators=(",", ":")), "application/json"


def build_slash_command_body(
    *,
    command: str,
    team: str,
    channel: str,
    user: str,
    text: str,
    thread_ts: str | None = None,
    response_url: str = "https://hooks.slack.com/commands/synthetic/synthetic/synthetic",
) -> tuple[str, str]:
    """Build a Slack slash-command POST body.

    Slack sends slash-command invocations as form-encoded POSTs, NOT JSON.
    Returns ``(body, content_type)`` ready for signing and POSTing.
    """
    fields = {
        "command": command,
        "team_id": team,
        "channel_id": channel,
        "user_id": user,
        "text": text,
        "response_url": response_url,
        "trigger_id": f"synthetic.{uuid.uuid4().hex}",
    }
    if thread_ts:
        fields["thread_ts"] = thread_ts
    body = urllib.parse.urlencode(fields)
    return body, "application/x-www-form-urlencoded"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--url", required=True, help="Lambda function URL")
    parser.add_argument("--channel", required=True, help="Slack channel ID")
    parser.add_argument("--team", required=True, help="Slack workspace team ID")
    parser.add_argument(
        "--text",
        default="ALERT: synthetic high-CPU alert from the test webhook",
        help="Alert text (event mode) or slash-command argument text (command mode)",
    )
    parser.add_argument(
        "--user",
        default="U0SYNTHETIC",
        help="Slack user id of the message author / command invoker",
    )
    parser.add_argument(
        "--command",
        default=None,
        help=(
            "Send a slash-command POST instead of an app_mention event. "
            "Pass the command name including the leading slash, e.g. "
            "'/sre-snapshot' or '/postmortem'."
        ),
    )
    parser.add_argument(
        "--thread-ts",
        default=None,
        help=(
            "Thread timestamp to include in slash-command body. Required by "
            "/postmortem; ignored by /sre-snapshot."
        ),
    )
    args = parser.parse_args()

    signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not signing_secret:
        print("SLACK_SIGNING_SECRET env var is required", file=sys.stderr)
        return 2

    now = int(time.time())

    if args.command:
        # Slash-command mode: form-encoded body, command-shaped fields.
        body, content_type = build_slash_command_body(
            command=args.command,
            team=args.team,
            channel=args.channel,
            user=args.user,
            text=args.text if args.text != parser.get_default("text") else "",
            thread_ts=args.thread_ts,
        )
    else:
        # Default: app_mention event (alert path).
        body, content_type = build_app_mention_body(
            team=args.team,
            channel=args.channel,
            user=args.user,
            text=args.text,
            now=now,
        )

    ts = str(now)
    signature = sign(signing_secret, ts, body)

    req = urllib.request.Request(
        args.url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": content_type,
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
