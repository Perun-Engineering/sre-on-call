"""Unit tests for the emoji-reaction and in-thread @mention investigation
triggers (issue #83).

Covers:
  * ``SlackMessageReader`` — fail-open Slack Web API reads for the reacted
    message (conversations.history) and the thread parent (conversations.replies).
  * ``SlackChatPlatform.ingest`` routing for ``reaction_added`` and
    in-thread ``app_mention`` events, plus the trigger-emoji config.
  * ``process_webhook`` handling of the new ``IgnoredWebhook`` variant.

All Slack HTTP is stubbed — no live calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from shared.platforms import AlertWebhook, IgnoredWebhook
from shared.platforms.slack import SlackChatPlatform, SlackMessageReader

SIGNING_SECRET = "test_signing_secret_abc123"
BOT_USER_ID = "UBOT"


def _sig(timestamp: str, body: str) -> str:
    h = hmac.new(
        SIGNING_SECRET.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"v0={h}"


def _signed(payload: dict) -> tuple[dict, str]:
    body = json.dumps(payload)
    ts = str(int(time.time()))
    headers = {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": _sig(ts, body),
        "content-type": "application/json",
    }
    return headers, body


def _platform(**kwargs) -> SlackChatPlatform:
    defaults = dict(
        signing_secret=SIGNING_SECRET,
        bot_token="xoxb-test",
        trigger_emoji="sre-on-call",
        bot_user_id=BOT_USER_ID,
    )
    defaults.update(kwargs)
    return SlackChatPlatform(**defaults)


class _FakeResp:
    """Minimal context-manager stand-in for an ``http.client.HTTPResponse``."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# SlackMessageReader
# ---------------------------------------------------------------------------


class TestSlackMessageReader:
    def test_read_reacted_message_returns_text(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        resp = _FakeResp({"ok": True, "messages": [{"text": "Disk full on host-42"}]})
        with patch("urllib.request.urlopen", return_value=resp) as urlopen:
            text = reader.read_reacted_message("C1", "1700000000.000100")
        assert text == "Disk full on host-42"
        req = urlopen.call_args[0][0]
        assert req.full_url.startswith("https://slack.com/api/conversations.history")
        assert "channel=C1" in req.full_url
        assert "latest=1700000000.000100" in req.full_url
        assert "oldest=1700000000.000100" in req.full_url
        assert "inclusive=true" in req.full_url
        assert req.headers["Authorization"] == "Bearer tok-1"

    def test_read_reacted_message_empty_returns_none(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        with patch("urllib.request.urlopen", return_value=_FakeResp({"ok": True, "messages": []})):
            assert reader.read_reacted_message("C1", "123.456") is None

    def test_read_reacted_message_reads_attachment_alert(self) -> None:
        """Alertmanager/Grafana alerts have empty ``text`` — the body is in
        ``attachments``. The reader must surface the attachment title + text."""
        reader = SlackMessageReader(bot_token="tok-1")
        msg = {
            "text": "",
            "subtype": "bot_message",
            "attachments": [
                {
                    "color": "a30200",
                    "title": "[FIRING:1] TraefikEntrypointLatencyHigh",
                    "text": "Cluster: eks-uat p95 latency >2s",
                    "fallback": "[FIRING:1] TraefikEntrypointLatencyHigh | http://am",
                }
            ],
        }
        with patch("urllib.request.urlopen", return_value=_FakeResp({"ok": True, "messages": [msg]})):
            text = reader.read_reacted_message("C1", "123.456")
        assert text == "[FIRING:1] TraefikEntrypointLatencyHigh\nCluster: eks-uat p95 latency >2s"

    def test_read_reacted_message_attachment_fallback_only(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        msg = {"text": "", "attachments": [{"fallback": "DiskFull on host-42"}]}
        with patch("urllib.request.urlopen", return_value=_FakeResp({"ok": True, "messages": [msg]})):
            assert reader.read_reacted_message("C1", "123.456") == "DiskFull on host-42"

    def test_read_reacted_message_reads_block_kit(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        msg = {
            "text": "",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "PagerDuty: DB down"}},
                {"type": "section", "fields": [{"type": "mrkdwn", "text": "*sev:* P1"}]},
            ],
        }
        with patch("urllib.request.urlopen", return_value=_FakeResp({"ok": True, "messages": [msg]})):
            assert reader.read_reacted_message("C1", "123.456") == "PagerDuty: DB down\n*sev:* P1"

    def test_read_reacted_message_no_readable_content_returns_none(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        msg = {"text": "", "attachments": [{"color": "a30200"}]}
        with patch("urllib.request.urlopen", return_value=_FakeResp({"ok": True, "messages": [msg]})):
            assert reader.read_reacted_message("C1", "123.456") is None

    def test_read_reacted_message_not_ok_returns_none(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        resp = _FakeResp({"ok": False, "error": "channel_not_found"})
        with patch("urllib.request.urlopen", return_value=resp):
            assert reader.read_reacted_message("C1", "123.456") is None

    def test_read_reacted_message_http_error_fail_open(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
            assert reader.read_reacted_message("C1", "123.456") is None

    def test_read_thread_parent_returns_first_message(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        resp = _FakeResp(
            {"ok": True, "messages": [{"text": "PARENT alert"}, {"text": "a reply"}]}
        )
        with patch("urllib.request.urlopen", return_value=resp) as urlopen:
            text = reader.read_thread_parent("C1", "1700000000.000100")
        assert text == "PARENT alert"
        req = urlopen.call_args[0][0]
        assert req.full_url.startswith("https://slack.com/api/conversations.replies")
        assert "ts=1700000000.000100" in req.full_url

    def test_read_thread_parent_fail_open(self) -> None:
        reader = SlackMessageReader(bot_token="tok-1")
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            assert reader.read_thread_parent("C1", "123.456") is None


# ---------------------------------------------------------------------------
# Reaction-trigger routing
# ---------------------------------------------------------------------------


def _reaction_event(reaction: str = "sre-on-call", user: str = "U1", item_type: str = "message") -> dict:
    return {
        "type": "event_callback",
        "event": {
            "type": "reaction_added",
            "user": user,
            "reaction": reaction,
            "item": {"type": item_type, "channel": "C1", "ts": "1700000000.000100"},
            "event_ts": "1700000001.000200",
        },
    }


class TestReactionTrigger:
    def test_trigger_emoji_builds_alert_from_reacted_message(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        platform._reader.read_reacted_message.return_value = "ALERT: pods crashlooping"
        headers, body = _signed(_reaction_event())

        event = platform.ingest(headers, body)

        assert isinstance(event, AlertWebhook)
        ctx = event.context
        assert ctx.channel_id == "C1"
        assert ctx.message_id == "1700000000.000100"
        assert ctx.alert_text == "ALERT: pods crashlooping"
        platform._reader.read_reacted_message.assert_called_once_with("C1", "1700000000.000100")

    def test_window_centers_on_reacted_message_ts(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        platform._reader.read_reacted_message.return_value = "alert"
        headers, body = _signed(_reaction_event())

        event = platform.ingest(headers, body)
        assert isinstance(event, AlertWebhook)
        expected = datetime.fromtimestamp(1700000000.000100, tz=timezone.utc).isoformat()
        assert event.context.alert_timestamp == expected

    def test_non_trigger_emoji_is_ignored(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        headers, body = _signed(_reaction_event(reaction="thumbsup"))

        event = platform.ingest(headers, body)

        assert isinstance(event, IgnoredWebhook)
        assert event.status_code == 200
        platform._reader.read_reacted_message.assert_not_called()

    def test_bot_self_reaction_is_ignored(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        headers, body = _signed(_reaction_event(user=BOT_USER_ID))

        event = platform.ingest(headers, body)

        assert isinstance(event, IgnoredWebhook)
        platform._reader.read_reacted_message.assert_not_called()

    def test_non_message_item_is_ignored(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        headers, body = _signed(_reaction_event(item_type="file"))

        event = platform.ingest(headers, body)
        assert isinstance(event, IgnoredWebhook)

    def test_unreadable_reacted_message_is_ignored(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        platform._reader.read_reacted_message.return_value = None
        headers, body = _signed(_reaction_event())

        event = platform.ingest(headers, body)
        assert isinstance(event, IgnoredWebhook)


# ---------------------------------------------------------------------------
# Trigger-emoji configuration
# ---------------------------------------------------------------------------


class TestTriggerEmojiConfig:
    def test_default_is_sre_on_call(self, monkeypatch) -> None:
        monkeypatch.delenv("SLACK_TRIGGER_EMOJI", raising=False)
        platform = SlackChatPlatform(signing_secret=SIGNING_SECRET, bot_token="xoxb-test")
        assert platform._trigger_emoji == "sre-on-call"

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("SLACK_TRIGGER_EMOJI", "rotating_light")
        platform = SlackChatPlatform(signing_secret=SIGNING_SECRET, bot_token="xoxb-test")
        assert platform._trigger_emoji == "rotating_light"

    def test_strips_surrounding_colons(self, monkeypatch) -> None:
        monkeypatch.setenv("SLACK_TRIGGER_EMOJI", ":fire:")
        platform = SlackChatPlatform(signing_secret=SIGNING_SECRET, bot_token="xoxb-test")
        assert platform._trigger_emoji == "fire"


# ---------------------------------------------------------------------------
# Thread-mention routing
# ---------------------------------------------------------------------------


def _mention_event(text: str, ts: str, thread_ts: str | None) -> dict:
    event: dict = {
        "type": "app_mention",
        "text": text,
        "channel": "C1",
        "ts": ts,
        "event_ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event": event}


class TestThreadMentionTrigger:
    def test_uses_parent_as_alert_and_reply_as_operator_note(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        platform._reader.read_thread_parent.return_value = "ALERT: 5xx spike on api"
        payload = _mention_event(
            text="<@UBOT> can you look at this",
            ts="1700000050.000300",
            thread_ts="1700000000.000100",
        )
        headers, body = _signed(payload)

        event = platform.ingest(headers, body)

        assert isinstance(event, AlertWebhook)
        ctx = event.context
        # parent ts anchors the investigation (threading + dedup)
        assert ctx.message_id == "1700000000.000100"
        assert ctx.alert_text.startswith("ALERT: 5xx spike on api")
        assert "Operator note" in ctx.alert_text
        assert "can you look at this" in ctx.alert_text
        # the <@BOT> token is stripped from the note
        assert "<@UBOT>" not in ctx.alert_text
        platform._reader.read_thread_parent.assert_called_once_with("C1", "1700000000.000100")

    def test_window_centers_on_parent_ts(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        platform._reader.read_thread_parent.return_value = "parent"
        headers, body = _signed(
            _mention_event(text="<@UBOT> hi", ts="1700000050.000300", thread_ts="1700000000.000100")
        )
        event = platform.ingest(headers, body)
        assert isinstance(event, AlertWebhook)
        expected = datetime.fromtimestamp(1700000000.000100, tz=timezone.utc).isoformat()
        assert event.context.alert_timestamp == expected

    def test_no_operator_note_when_reply_is_only_the_mention(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        platform._reader.read_thread_parent.return_value = "ALERT parent"
        headers, body = _signed(
            _mention_event(text="<@UBOT>", ts="1700000050.000300", thread_ts="1700000000.000100")
        )
        event = platform.ingest(headers, body)
        assert isinstance(event, AlertWebhook)
        assert event.context.alert_text == "ALERT parent"
        assert "Operator note" not in event.context.alert_text

    def test_unreadable_parent_is_ignored(self) -> None:
        platform = _platform()
        platform._reader = MagicMock()
        platform._reader.read_thread_parent.return_value = None
        headers, body = _signed(
            _mention_event(text="<@UBOT> hi", ts="1700000050.000300", thread_ts="1700000000.000100")
        )
        assert isinstance(platform.ingest(headers, body), IgnoredWebhook)

    def test_top_level_mention_uses_payload_text(self) -> None:
        """A mention that is not a thread reply keeps today's behaviour: the
        message's own text is the alert, no Slack read."""
        platform = _platform()
        platform._reader = MagicMock()
        headers, body = _signed(
            _mention_event(text="<@UBOT> ALERT cpu high", ts="1700000000.000100", thread_ts=None)
        )
        event = platform.ingest(headers, body)
        assert isinstance(event, AlertWebhook)
        assert event.context.alert_text == "<@UBOT> ALERT cpu high"
        assert event.context.message_id == "1700000000.000100"
        platform._reader.read_thread_parent.assert_not_called()

    def test_mention_at_thread_root_is_top_level(self) -> None:
        """When thread_ts == ts the mention IS the thread root, not a reply —
        treat it as a top-level mention (use its own text)."""
        platform = _platform()
        platform._reader = MagicMock()
        headers, body = _signed(
            _mention_event(text="<@UBOT> ALERT", ts="1700000000.000100", thread_ts="1700000000.000100")
        )
        event = platform.ingest(headers, body)
        assert isinstance(event, AlertWebhook)
        platform._reader.read_thread_parent.assert_not_called()


# ---------------------------------------------------------------------------
# process_webhook integration with IgnoredWebhook
# ---------------------------------------------------------------------------


class TestProcessWebhookIgnored:
    def test_ignored_webhook_returns_200_without_dispatch(self) -> None:
        from lambda_adapter.intake import process_webhook

        platform = MagicMock()
        platform.ingest.return_value = IgnoredWebhook()
        dispatch = MagicMock()

        resp = process_webhook({"headers": {}, "body": "{}"}, platform, dispatch)

        assert resp["statusCode"] == 200
        dispatch.investigate.assert_not_called()
