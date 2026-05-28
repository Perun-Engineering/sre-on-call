"""Slack Scanner Agent tools — scan channel history for correlated alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from strands import tool

from shared.channel_scan import ALERT_KEYWORDS, execute_channel_scan
from shared.constants import INVESTIGATION_WINDOW_MINUTES
from shared.models import Finding, SnapshotReport, SnapshotSection
from shared.secrets import resolve_secret
from shared.tool_result import (
    build_agent_result,
    format_result,
    format_snapshot_result,
    severity_from_text,
)

logger = logging.getLogger(__name__)


class SlackMessageSource:
    """ChannelMessageSource adapter for Slack."""

    def __init__(self, client: WebClient) -> None:
        self._client = client

    def fetch_messages(
        self, channel_id: str, start_iso: str, end_iso: str
    ) -> list[dict]:
        start_epoch = str(datetime.fromisoformat(start_iso).timestamp())
        end_epoch = str(datetime.fromisoformat(end_iso).timestamp())
        response = self._client.conversations_history(
            channel=channel_id,
            oldest=start_epoch,
            latest=end_epoch,
            inclusive=True,
            limit=200,
        )
        return response.get("messages", [])

    def is_alert(self, message: dict) -> bool:
        if message.get("bot_id"):
            return True
        if message.get("subtype") == "bot_message":
            return True
        text = (message.get("text") or "").lower()
        return any(kw in text for kw in ALERT_KEYWORDS)

    def extract_finding(self, channel_id: str, message: dict) -> Finding:
        text = message.get("text", "")
        return Finding(
            source=channel_id,
            timestamp=_slack_ts_to_iso(message.get("ts", "")),
            content=text,
            severity=severity_from_text(text),
            metadata={
                "channel_id": channel_id,
                "message_ts": message.get("ts", ""),
                "bot_id": message.get("bot_id", ""),
                "subtype": message.get("subtype", ""),
            },
        )


def _slack_ts_to_iso(ts: str) -> str:
    """Convert a Slack message timestamp (epoch.micro) to ISO 8601."""
    try:
        epoch = float(ts)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return ts


@tool
def scan_slack_channels(
    alert_timestamp: str,
    bot_channel_ids: list[str],
    window_minutes: int = INVESTIGATION_WINDOW_MINUTES,
) -> str:
    """Scan Slack channels for correlated alerts around an incident timestamp.

    Scans up to 10 channels the bot is a member of. Retrieves messages within
    the investigation window (±5 minutes of *alert_timestamp*). Filters for
    alerts, notifications, and error messages from integration bots. Returns a
    correlation summary with findings and source channels.

    Args:
        alert_timestamp: ISO 8601 timestamp of the triggering alert.
        bot_channel_ids: Channel IDs the bot is a member of (max 10 scanned).
        window_minutes: Total investigation window size in minutes (default 10).

    Returns:
        A JSON-serialisable string summarising the scan results.
    """
    client = WebClient(token=resolve_secret("SLACK_BOT_TOKEN"))
    source = SlackMessageSource(client)
    result = execute_channel_scan(alert_timestamp, bot_channel_ids, source)
    return format_result(build_agent_result("slack_scanner", result))


@tool
def capture_snapshot(requested_at: str) -> str:
    """Capture a read-only snapshot of Slack workspace reachability.

    Probes Slack ``auth.test`` (token validity, bot identity, workspace info)
    and ``users.conversations`` (channel-membership count). Returns a
    human-readable summary with an embedded :class:`SnapshotReport` footer
    that the master orchestrator extracts via
    :func:`shared.tool_result.extract_snapshot_report`.

    The tool never raises — any failure of either probe is folded into the
    snapshot. ``auth.test`` failure flips the report to ``anomaly=True``;
    ``users.conversations`` failure is surfaced as a section line only.

    Args:
        requested_at: ISO 8601 timestamp from the master, used as the
            ``captured_at`` field of the returned report.

    Returns:
        A short human-readable string ending with a
        ``<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>`` footer.
    """
    client = WebClient(token=resolve_secret("SLACK_BOT_TOKEN"))
    report = _execute_capture_snapshot(client, requested_at=requested_at)
    return format_snapshot_result(report)


def _execute_capture_snapshot(
    client: WebClient,
    *,
    requested_at: str,
) -> SnapshotReport:
    """Build a :class:`SnapshotReport` from two Slack API probes.

    Pure: takes its WebClient as a parameter so tests can pass a mock and
    drive every branch — happy path, non-ok auth.test, ``SlackApiError`` on
    auth, generic exception on auth, and partial failure of the
    ``users.conversations`` probe.
    """
    sections: list[SnapshotSection] = []
    anomaly = False
    anomaly_summary: str | None = None

    # Probe 1: auth.test — anomaly-triggering.
    try:
        auth_response = client.auth_test()
        if not auth_response.get("ok", False):
            err = auth_response.get("error", "unknown")
            anomaly = True
            anomaly_summary = f"Slack auth.test returned non-ok: {err}"
            sections.append(
                SnapshotSection(
                    label="Authentication",
                    lines=[f"❌ auth.test returned non-ok: {err}"],
                )
            )
        else:
            team = auth_response.get("team", "unknown")
            team_id = auth_response.get("team_id", "unknown")
            bot_user = auth_response.get("user", "unknown")
            bot_id = auth_response.get("user_id", "unknown")
            url = auth_response.get("url", "")
            auth_lines = [
                f"workspace: {team} ({team_id})",
                f"bot user: {bot_user} ({bot_id})",
            ]
            if url:
                auth_lines.append(f"workspace URL: {url}")
            sections.append(SnapshotSection(label="Authentication", lines=auth_lines))
    except SlackApiError as exc:
        err = _slack_error_message(exc)
        anomaly = True
        anomaly_summary = f"Slack auth.test failed: {err}"
        sections.append(
            SnapshotSection(
                label="Authentication",
                lines=[f"❌ auth.test failed: {err}"],
            )
        )
    except Exception as exc:  # honour the no-raise contract
        anomaly = True
        anomaly_summary = f"Slack auth.test failed: {exc}"
        sections.append(
            SnapshotSection(
                label="Authentication",
                lines=[f"❌ auth.test failed: {exc}"],
            )
        )

    # Probe 2: users.conversations — informational, does NOT trigger anomaly.
    try:
        conv_response = client.users_conversations(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=1000,
        )
        channels = conv_response.get("channels") or []
        sections.append(
            SnapshotSection(
                label="Channel access",
                lines=[f"bot is a member of {len(channels)} channel(s)"],
            )
        )
    except SlackApiError as exc:
        err = _slack_error_message(exc)
        sections.append(
            SnapshotSection(
                label="Channel access",
                lines=[f"❌ users.conversations failed: {err}"],
            )
        )
    except Exception as exc:
        sections.append(
            SnapshotSection(
                label="Channel access",
                lines=[f"❌ users.conversations failed: {exc}"],
            )
        )

    return SnapshotReport(
        agent_name="slack_scanner",
        captured_at=requested_at,
        sections=sections,
        anomaly=anomaly,
        anomaly_summary=anomaly_summary,
    )


def _slack_error_message(exc: SlackApiError) -> str:
    """Pull the ``error`` field out of a SlackApiError's response, or fall back to str()."""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        return response.get("error") or str(exc)
    except (AttributeError, TypeError):
        return str(exc)
