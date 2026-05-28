"""Round-trip test for scripts/synthetic_slack_webhook.py — its signing
must match the production verifier in lambda_adapter/slack/signature.py.

If this test passes, a payload signed by the script will pass signature
verification when the Lambda processes it. Covers both the default
``app_mention`` path and the new slash-command (``/status`` /
``/postmortem``) path.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import time
from urllib.parse import parse_qs

from shared.platforms.slack import verify_slack_signature


def _load_script_module():
    path = pathlib.Path(__file__).resolve().parent.parent.parent / "scripts" / "synthetic_slack_webhook.py"
    spec = importlib.util.spec_from_file_location("synthetic_slack_webhook", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["synthetic_slack_webhook"] = module
    spec.loader.exec_module(module)
    return module


def test_signature_round_trips_through_production_verifier():
    script = _load_script_module()
    secret = "deadbeefdeadbeefdeadbeefdeadbeef"
    body = '{"type":"event_callback","team_id":"T1"}'
    # Production verifier rejects requests older than 5 minutes, so use now.
    timestamp = str(int(time.time()))

    signature = script.sign(secret, timestamp, body)

    assert signature.startswith("v0=")
    assert verify_slack_signature(
        body=body,
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
    ) is True


def test_tampered_body_fails_verification():
    script = _load_script_module()
    secret = "deadbeefdeadbeefdeadbeefdeadbeef"
    body = '{"original":"body"}'
    timestamp = str(int(time.time()))

    signature = script.sign(secret, timestamp, body)

    assert verify_slack_signature(
        body='{"tampered":"body"}',
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
    ) is False


# ---------------------------------------------------------------------------
# Slash-command body builder + signature round-trip (added in slice 7)
# ---------------------------------------------------------------------------


def test_slash_command_body_is_form_encoded():
    script = _load_script_module()
    body, content_type = script.build_slash_command_body(
        command="/status",
        team="T123",
        channel="C456",
        user="U789",
        text="",
    )
    # Slack delivers slash commands as application/x-www-form-urlencoded,
    # not JSON — the Lambda's intake routes on that content type.
    assert content_type == "application/x-www-form-urlencoded"
    fields = parse_qs(body, keep_blank_values=True)
    assert fields["command"] == ["/status"]
    assert fields["team_id"] == ["T123"]
    assert fields["channel_id"] == ["C456"]
    assert fields["user_id"] == ["U789"]


def test_slash_command_body_includes_thread_ts_when_provided():
    script = _load_script_module()
    body, _ = script.build_slash_command_body(
        command="/postmortem",
        team="T1", channel="C1", user="U1", text="",
        thread_ts="1700000000.000100",
    )
    fields = parse_qs(body, keep_blank_values=True)
    assert fields["thread_ts"] == ["1700000000.000100"]


def test_slash_command_body_omits_thread_ts_when_absent():
    script = _load_script_module()
    body, _ = script.build_slash_command_body(
        command="/status",
        team="T1", channel="C1", user="U1", text="",
        thread_ts=None,
    )
    fields = parse_qs(body, keep_blank_values=True)
    assert "thread_ts" not in fields


def test_slash_command_signature_round_trips_through_production_verifier():
    """The Lambda accepts the script's signed slash-command body — same
    HMAC scheme as the production verifier."""
    script = _load_script_module()
    secret = "deadbeefdeadbeefdeadbeefdeadbeef"
    body, _ = script.build_slash_command_body(
        command="/status",
        team="T1", channel="C1", user="U1", text="",
    )
    timestamp = str(int(time.time()))
    signature = script.sign(secret, timestamp, body)

    assert verify_slack_signature(
        body=body,
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
    ) is True


def test_app_mention_body_remains_json_with_application_json_content_type():
    """Default mode (no --command) must keep the existing app_mention shape
    so the alert path tests don't regress."""
    script = _load_script_module()
    body, content_type = script.build_app_mention_body(
        team="T1", channel="C1", user="U1", text="ALERT", now=1700000000,
    )
    assert content_type == "application/json"
    # Body is JSON, not form-encoded
    import json as _json
    payload = _json.loads(body)
    assert payload["type"] == "event_callback"
    assert payload["event"]["type"] == "app_mention"
    assert payload["team_id"] == "T1"
