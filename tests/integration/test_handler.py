"""Unit tests for the unified lambda_adapter.handler (Slack platform)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from lambda_adapter.handler import lambda_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNING_SECRET = "test_signing_secret_abc123"
SLACK_BOT_TOKEN = "xoxb-test-token"
DEDUP_TABLE = "test-dedup-table"
AGENT_ENDPOINT = "TESTAGENT123"


def _make_signature(secret: str, timestamp: str, body: str) -> str:
    sig_basestring = f"v0:{timestamp}:{body}"
    h = hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return f"v0={h}"


def _build_event(
    body_dict: dict,
    *,
    signing_secret: str = SIGNING_SECRET,
    timestamp: str | None = None,
    base64_encode: bool = False,
) -> dict:
    if timestamp is None:
        timestamp = str(int(time.time()))
    raw_body = json.dumps(body_dict)
    signature = _make_signature(signing_secret, timestamp, raw_body)

    body_value = raw_body
    is_base64 = False
    if base64_encode:
        body_value = base64.b64encode(raw_body.encode()).decode()
        is_base64 = True

    return {
        "headers": {
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
        },
        "body": body_value,
        "isBase64Encoded": is_base64,
    }


def _slack_event_payload(
    channel: str = "C12345",
    ts: str = "1700000000.000100",
    text: str = "ALERT: CPU usage > 90%",
) -> dict:
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": channel,
            "ts": ts,
            "text": text,
            "event_ts": ts,
        },
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_BOT_TOKEN)
    monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
    monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", AGENT_ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


def _create_dedup_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=DEDUP_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUrlVerification:
    def test_returns_challenge(self):
        payload = {"type": "url_verification", "challenge": "abc123xyz"}
        event = _build_event(payload)
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["challenge"] == "abc123xyz"

    def test_empty_challenge(self):
        payload = {"type": "url_verification", "challenge": ""}
        event = _build_event(payload)
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["challenge"] == ""


class TestSignatureRejection:
    def test_invalid_signature_returns_401(self):
        payload = _slack_event_payload()
        event = _build_event(payload)
        event["headers"]["x-slack-signature"] = "v0=0000000000000000"
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_missing_signature_returns_401(self):
        payload = _slack_event_payload()
        event = _build_event(payload)
        event["headers"].pop("x-slack-signature")
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_stale_timestamp_returns_401(self):
        payload = _slack_event_payload()
        stale_ts = str(int(time.time()) - 600)
        event = _build_event(payload, timestamp=stale_ts)
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401


class TestDuplicateHandling:
    @mock_aws
    def test_duplicate_returns_200_silently(self):
        _create_dedup_table()
        payload = _slack_event_payload()
        event = _build_event(payload)

        with patch("lambda_adapter.intake.boto3.client") as mock_client:
            mock_runtime = MagicMock()
            mock_client.return_value = mock_runtime

            result1 = lambda_handler(event, None)
            assert result1["statusCode"] == 200

            event2 = _build_event(payload)
            result2 = lambda_handler(event2, None)
            assert result2["statusCode"] == 200
            assert json.loads(result2["body"]) == {}

            assert mock_runtime.invoke_agent_runtime.call_count == 1


class TestHappyPath:
    @mock_aws
    def test_full_flow(self):
        _create_dedup_table()
        payload = _slack_event_payload()
        event = _build_event(payload)

        with patch("lambda_adapter.intake.boto3.client") as mock_client:
            mock_runtime = MagicMock()
            mock_client.return_value = mock_runtime

            result = lambda_handler(event, None)

            assert result["statusCode"] == 200
            assert json.loads(result["body"]).get("ok") is True

            mock_runtime.invoke_agent_runtime.assert_called_once()
            assert (
                mock_runtime.invoke_agent_runtime.call_args[1]["agentRuntimeArn"]
                == AGENT_ENDPOINT
            )

    @mock_aws
    def test_base64_encoded_body(self):
        _create_dedup_table()
        payload = _slack_event_payload()
        event = _build_event(payload, base64_encode=True)

        with patch("lambda_adapter.intake.boto3.client") as mock_client:
            mock_client.return_value = MagicMock()
            result = lambda_handler(event, None)
            assert result["statusCode"] == 200
            assert json.loads(result["body"]).get("ok") is True


# ---------------------------------------------------------------------------
# Trace archive — verify the lambda_adapter writes the dedup_outcome and
# alert_received events when the trace env vars are configured.
# ---------------------------------------------------------------------------


TRACES_BUCKET = "test-traces-bucket"
TRACES_TABLE = "test-traces-table"


def _create_traces_resources():
    """Create a moto-mocked S3 bucket and DDB table for the trace archive."""
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=TRACES_BUCKET)

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TRACES_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return s3


def _patched_boto3_client(mock_runtime):
    """Build a side_effect that returns *mock_runtime* for the AgentCore
    runtime client and the real moto-wrapped client for everything else.

    The handler tests patch ``lambda_adapter.intake.boto3.client`` to fake
    out the AgentCore invocation, but doing it unconditionally also
    breaks anything else that constructs an S3 / DDB client during the
    request — including :class:`shared.trace_store.TraceStore`. This
    helper lets the patch fall through to moto for those services.
    """
    real = boto3.client

    def _side_effect(service_name, *args, **kwargs):
        if service_name == "bedrock-agentcore":
            return mock_runtime
        return real(service_name, *args, **kwargs)

    return _side_effect


class TestTraceArchive:
    """The Lambda adapter writes to the trace archive when env vars are set."""

    @mock_aws
    def test_alert_received_and_dedup_outcome_written_for_new_alert(
        self, monkeypatch
    ):
        monkeypatch.setenv("TRACES_BUCKET_NAME", TRACES_BUCKET)
        monkeypatch.setenv("TRACES_TABLE_NAME", TRACES_TABLE)

        _create_dedup_table()
        s3 = _create_traces_resources()

        payload = _slack_event_payload()
        event = _build_event(payload)

        mock_runtime = MagicMock()
        with patch(
            "lambda_adapter.intake.boto3.client",
            side_effect=_patched_boto3_client(mock_runtime),
        ):
            result = lambda_handler(event, None)
            assert result["statusCode"] == 200

        objs = s3.list_objects_v2(Bucket=TRACES_BUCKET, Prefix="dt=")
        keys = [o["Key"] for o in objs.get("Contents", [])]
        assert any("alert_received" in k for k in keys), (
            f"alert_received event not written; got {keys}"
        )
        assert any("dedup_outcome" in k for k in keys), (
            f"dedup_outcome event not written; got {keys}"
        )
        # Master agent invocation must still have happened.
        mock_runtime.invoke_agent_runtime.assert_called_once()

    @mock_aws
    def test_only_dedup_outcome_written_for_duplicate(self, monkeypatch):
        monkeypatch.setenv("TRACES_BUCKET_NAME", TRACES_BUCKET)
        monkeypatch.setenv("TRACES_TABLE_NAME", TRACES_TABLE)

        _create_dedup_table()
        s3 = _create_traces_resources()

        payload = _slack_event_payload()

        mock_runtime = MagicMock()
        with patch(
            "lambda_adapter.intake.boto3.client",
            side_effect=_patched_boto3_client(mock_runtime),
        ):
            lambda_handler(_build_event(payload), None)
            # Re-send the same payload — second call hits the dedup path.
            lambda_handler(_build_event(payload), None)

        objs = s3.list_objects_v2(Bucket=TRACES_BUCKET, Prefix="dt=")
        keys = [o["Key"] for o in objs.get("Contents", [])]
        # First call: alert_received + dedup_outcome (is_new=True).
        # Second call: dedup_outcome only (is_new=False).
        assert sum("alert_received" in k for k in keys) == 1
        assert sum("dedup_outcome" in k for k in keys) == 2

    @mock_aws
    def test_intake_succeeds_when_traces_unconfigured(self, monkeypatch):
        # Unset both — the intake must still write an HTTP 200 and
        # invoke the master agent.
        monkeypatch.delenv("TRACES_BUCKET_NAME", raising=False)
        monkeypatch.delenv("TRACES_TABLE_NAME", raising=False)

        _create_dedup_table()
        payload = _slack_event_payload()
        event = _build_event(payload)

        mock_runtime = MagicMock()
        with patch(
            "lambda_adapter.intake.boto3.client",
            side_effect=_patched_boto3_client(mock_runtime),
        ):
            result = lambda_handler(event, None)
            assert result["statusCode"] == 200
            mock_runtime.invoke_agent_runtime.assert_called_once()
