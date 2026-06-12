"""Discord Scanner Agent tools — scan channel history for correlated alerts."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from strands import tool

from shared.channel_scan import ALERT_KEYWORDS, execute_channel_scan
from shared.models import Finding, SnapshotReport, SnapshotSection
from shared.secrets import resolve_secret
from shared.tool_result import (
    build_agent_result,
    format_result,
    format_snapshot_result,
    severity_from_text,
)

logger = logging.getLogger(__name__)


class DiscordMessageSource:
    """ChannelMessageSource adapter for Discord."""

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token

    def fetch_messages(
        self, channel_id: str, start_iso: str, end_iso: str
    ) -> list[dict]:
        after = _timestamp_to_snowflake(start_iso)
        before = _timestamp_to_snowflake(end_iso)
        url = (
            f"https://discord.com/api/v10/channels/{channel_id}/messages"
            f"?limit=100&after={after}&before={before}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def is_alert(self, message: dict) -> bool:
        if message.get("author", {}).get("bot", False):
            return True
        if message.get("webhook_id"):
            return True
        text = (message.get("content") or "").lower()
        return any(kw in text for kw in ALERT_KEYWORDS)

    def extract_finding(self, channel_id: str, message: dict) -> Finding:
        text = message.get("content", "")
        return Finding(
            source=channel_id,
            timestamp=message.get("timestamp", ""),
            content=text,
            severity=severity_from_text(text),
            metadata={
                "channel_id": channel_id,
                "message_id": message.get("id", ""),
                "author_id": message.get("author", {}).get("id", ""),
                "is_bot": message.get("author", {}).get("bot", False),
            },
        )


def _timestamp_to_snowflake(iso_timestamp: str) -> str:
    """Convert an ISO 8601 timestamp to a Discord snowflake ID (approximate)."""
    dt = datetime.fromisoformat(iso_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    discord_epoch_ms = int(dt.timestamp() * 1000) - 1420070400000
    return str(discord_epoch_ms << 22)


@tool
def scan_discord_channels(
    alert_timestamp: str,
    channel_ids: list[str],
    window_minutes: int = 10,
) -> str:
    """Scan Discord channels for correlated alerts around an incident timestamp.

    Args:
        alert_timestamp: ISO 8601 timestamp of the triggering alert.
        channel_ids: Discord channel IDs to scan (max 10).
        window_minutes: Total investigation window size in minutes (default 10).

    Returns:
        A human-readable summary string for the LLM to consume.
    """
    source = DiscordMessageSource(resolve_secret("DISCORD_BOT_TOKEN"))
    result = execute_channel_scan(alert_timestamp, channel_ids, source)
    return format_result(build_agent_result("discord_scanner", result))


# ---------------------------------------------------------------------------
# /sre-snapshot snapshot — bot reachability + guild count
# ---------------------------------------------------------------------------


class DiscordRESTClient:
    """Minimal Discord REST helper for the snapshot probe.

    Two methods, each returning ``(status_code, body)``. Tests substitute
    a :class:`unittest.mock.MagicMock` to drive every branch (2xx, 4xx,
    network exception) without exercising real HTTP.
    """

    BASE_URL = "https://discord.com/api/v10"

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token

    def get_user_self(self) -> tuple[int, dict]:
        """``GET /users/@me`` — bot identity."""
        return self._get("/users/@me")

    def get_user_guilds(self) -> tuple[int, list]:
        """``GET /users/@me/guilds`` — list of guilds the bot belongs to."""
        return self._get("/users/@me/guilds")

    def _get(self, path: str) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.BASE_URL + path,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except Exception:
                body = {}
            return exc.code, body


@tool
def capture_snapshot(requested_at: str) -> str:
    """Capture a read-only snapshot of Discord bot reachability.

    Probes :meth:`DiscordRESTClient.get_user_self` (token validity, bot
    identity) and :meth:`DiscordRESTClient.get_user_guilds` (guild count).
    Returns a human-readable summary with an embedded
    :class:`SnapshotReport` footer.

    The tool never raises — any failure is folded into the snapshot.
    Auth failure flips the report to ``anomaly=True``; guild-listing
    failure surfaces as a section line only.

    Args:
        requested_at: ISO 8601 timestamp from the master, used as the
            ``captured_at`` field of the returned report.

    Returns:
        A short human-readable string ending with a
        ``<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>`` footer.
    """
    client = DiscordRESTClient(resolve_secret("DISCORD_BOT_TOKEN"))
    report = _execute_capture_snapshot(client, requested_at=requested_at)
    return format_snapshot_result(report)


def _execute_capture_snapshot(
    client: DiscordRESTClient,
    *,
    requested_at: str,
) -> SnapshotReport:
    """Pure snapshot builder — all I/O goes through *client*.

    Tests pass a :class:`unittest.mock.MagicMock` to drive every branch:
    happy path, 4xx auth, exception during auth, partial failure on
    guild listing.
    """
    sections: list[SnapshotSection] = []
    anomaly = False
    anomaly_summary: str | None = None

    # Probe 1: GET /users/@me — anomaly-triggering.
    try:
        status, body = client.get_user_self()
        if not _is_2xx(status):
            err = _discord_error_message(body, status)
            anomaly = True
            anomaly_summary = f"Discord /users/@me returned {status}: {err}"
            sections.append(
                SnapshotSection(
                    label="Authentication",
                    lines=[f"❌ /users/@me returned {status}: {err}"],
                )
            )
        elif not isinstance(body, dict):
            anomaly = True
            anomaly_summary = "Discord /users/@me returned a non-object body"
            sections.append(
                SnapshotSection(
                    label="Authentication",
                    lines=["❌ /users/@me returned a non-object body"],
                )
            )
        else:
            sections.append(
                SnapshotSection(
                    label="Authentication",
                    lines=[
                        f"bot id: {body.get('id', '(unknown)')}",
                        f"username: {body.get('username', '(unknown)')}#{body.get('discriminator', '0')}",
                    ],
                )
            )
    except Exception as exc:
        anomaly = True
        anomaly_summary = f"Discord /users/@me failed: {exc}"
        sections.append(
            SnapshotSection(
                label="Authentication",
                lines=[f"❌ /users/@me failed: {exc}"],
            )
        )

    # Probe 2: GET /users/@me/guilds — informational, does NOT trigger anomaly.
    try:
        status, body = client.get_user_guilds()
        if not _is_2xx(status):
            err = _discord_error_message(body, status)
            sections.append(
                SnapshotSection(
                    label="Guild access",
                    lines=[f"❌ /users/@me/guilds returned {status}: {err}"],
                )
            )
        elif not isinstance(body, list):
            sections.append(
                SnapshotSection(
                    label="Guild access",
                    lines=["❌ /users/@me/guilds returned a non-array body"],
                )
            )
        else:
            sections.append(
                SnapshotSection(
                    label="Guild access",
                    lines=[f"bot is a member of {len(body)} guild(s)"],
                )
            )
    except Exception as exc:
        sections.append(
            SnapshotSection(
                label="Guild access",
                lines=[f"❌ /users/@me/guilds failed: {exc}"],
            )
        )

    return SnapshotReport(
        agent_name="discord_scanner",
        captured_at=requested_at,
        sections=sections,
        anomaly=anomaly,
        anomaly_summary=anomaly_summary,
    )


def _is_2xx(status: int) -> bool:
    return 200 <= int(status) < 300


def _discord_error_message(body: object, status: int) -> str:
    """Pull a useful error string out of a Discord API error body."""
    if isinstance(body, dict):
        msg = body.get("message")
        if msg:
            return str(msg)
    return f"HTTP {status}"
