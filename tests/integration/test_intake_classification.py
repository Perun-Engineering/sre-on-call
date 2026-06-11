"""Intake classification gate — non-alert mentions must not fan out.

Exercises the full :func:`process_webhook` path (signature → dedup → gate)
with a recording dispatch, asserting the gate suppresses chatter while real
alerts still dispatch, the manual override forces investigation, and the
kill-switch disables the gate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import boto3
import pytest
from moto import mock_aws

from lambda_adapter.intake import process_webhook
from lambda_adapter.master_dispatch import RecordingMasterDispatch
from shared.platforms import detect_platform
from shared.platforms.slack import SlackChatPlatform

SIGNING_SECRET = "test_signing_secret_abc123"
DEDUP_TABLE = "test-dedup-table"


def _make_signature(secret: str, timestamp: str, body: str) -> str:
    sig_basestring = f"v0:{timestamp}:{body}"
    h = hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return f"v0={h}"


def _event(text: str, *, ts: str = "1700000000.000100") -> dict:
    payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C12345",
            "ts": ts,
            "text": text,
            "event_ts": ts,
        },
    }
    raw_body = json.dumps(payload)
    timestamp = str(int(time.time()))
    return {
        "headers": {
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": _make_signature(SIGNING_SECRET, timestamp, raw_body),
        },
        "body": raw_body,
        "isBase64Encoded": False,
    }


def _process(event: dict, dispatch: RecordingMasterDispatch) -> dict:
    return process_webhook(event, detect_platform(event["headers"]), dispatch)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
    monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "TESTAGENT123")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    # Default-on; individual tests flip it. Set explicitly so suite order can't leak.
    monkeypatch.delenv("ALERT_CLASSIFICATION_ENABLED", raising=False)
    monkeypatch.delenv("CLASSIFIER_LLM_ENABLED", raising=False)


def _create_dedup_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=DEDUP_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb


@mock_aws
class TestClassificationGate:
    def test_non_alert_mention_does_not_dispatch(self, monkeypatch):
        _create_dedup_table()
        dispatch = RecordingMasterDispatch()
        notices: list = []
        monkeypatch.setattr(
            SlackChatPlatform, "notice",
            lambda self, target, text: notices.append((target, text)),
        )

        result = _process(_event("thanks!"), dispatch)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["classified"] == "non_alert"
        assert dispatch.tasks == []  # acceptance criterion: no MasterDispatch
        assert len(notices) == 1
        assert "investigate" in notices[0][1]
        # Notice threads under the originating message.
        assert notices[0][0].thread_anchor == "1700000000.000100"

    def test_real_alert_still_dispatches(self, monkeypatch):
        _create_dedup_table()
        dispatch = RecordingMasterDispatch()
        notices: list = []
        monkeypatch.setattr(
            SlackChatPlatform, "notice",
            lambda self, target, text: notices.append(text),
        )

        result = _process(_event("🚨 api-server is DOWN"), dispatch)

        assert result["statusCode"] == 200
        assert len(dispatch.tasks) == 1
        assert notices == []  # alerts are not nudged

    def test_investigate_override_forces_dispatch(self, monkeypatch):
        _create_dedup_table()
        dispatch = RecordingMasterDispatch()
        monkeypatch.setattr(SlackChatPlatform, "notice", lambda *a, **k: None)

        # "please investigate" would otherwise be ambiguous chatter.
        _process(_event("hey can you investigate this?"), dispatch)

        assert len(dispatch.tasks) == 1

    def test_ambiguous_message_defaults_to_dispatch(self, monkeypatch):
        _create_dedup_table()
        dispatch = RecordingMasterDispatch()
        monkeypatch.setattr(SlackChatPlatform, "notice", lambda *a, **k: None)

        _process(
            _event("users in europe are seeing slow page loads since the deploy"),
            dispatch,
        )

        assert len(dispatch.tasks) == 1

    def test_kill_switch_disables_gate(self, monkeypatch):
        monkeypatch.setenv("ALERT_CLASSIFICATION_ENABLED", "false")
        _create_dedup_table()
        dispatch = RecordingMasterDispatch()
        notices: list = []
        monkeypatch.setattr(
            SlackChatPlatform, "notice",
            lambda self, target, text: notices.append(text),
        )

        _process(_event("thanks!"), dispatch)

        # With the gate off, even chatter investigates and no notice is posted.
        assert len(dispatch.tasks) == 1
        assert notices == []

    def test_notice_failure_is_fail_open(self, monkeypatch):
        _create_dedup_table()
        dispatch = RecordingMasterDispatch()

        def _boom(self, target, text):
            raise RuntimeError("slack down")

        monkeypatch.setattr(SlackChatPlatform, "notice", _boom)

        result = _process(_event("thanks!"), dispatch)

        assert result["statusCode"] == 200
        assert dispatch.tasks == []
