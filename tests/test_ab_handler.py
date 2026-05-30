"""Tests for A/B experiment forking in the unified Lambda handler."""

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
from shared.experiment import (
    ExperimentConfig,
    PipelineVariant,
)
from shared.experiment_store import DEFAULT_TABLE_NAME as EXP_TABLE


def _process(event: dict) -> tuple[dict, RecordingMasterDispatch]:
    dispatch = RecordingMasterDispatch()
    result = process_webhook(event, detect_platform(event["headers"]), dispatch)
    return result, dispatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIGNING_SECRET = "test_signing_secret_abc123"
DEDUP_TABLE = "test-dedup-table"


def _make_signature(secret: str, timestamp: str, body: str) -> str:
    sig_basestring = f"v0:{timestamp}:{body}"
    h = hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return f"v0={h}"


def _build_event(body_dict: dict) -> dict:
    timestamp = str(int(time.time()))
    raw_body = json.dumps(body_dict)
    signature = _make_signature(SIGNING_SECRET, timestamp, raw_body)
    return {
        "headers": {
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
        },
        "body": raw_body,
        "isBase64Encoded": False,
    }


def _slack_event_payload(ts: str = "1700000000.000100") -> dict:
    return {
        "type": "event_callback",
        "event": {"type": "message", "channel": "C12345", "ts": ts, "text": "ALERT: CPU > 90%", "event_ts": ts},
    }


def _make_experiment() -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id="exp-001",
        name="claude-vs-nova",
        status="active",
        variant_a=PipelineVariant(variant_id="a", label="Claude Sonnet", master_endpoint="AGENT_A"),
        variant_b=PipelineVariant(variant_id="b", label="Nova Pro", master_endpoint="AGENT_B"),
        created_at="2026-04-30T10:00:00Z",
        updated_at="2026-04-30T10:00:00Z",
    )


def _create_tables(create_experiments: bool = False):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=DEDUP_TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    if create_experiments:
        ddb.create_table(
            TableName=EXP_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    return ddb


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
    monkeypatch.setenv("MASTER_AGENT_RUNTIME_ARN", "DEFAULT_AGENT")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoExperiment:
    @mock_aws
    def test_single_invoke_when_no_experiment_table(self) -> None:
        _create_tables()
        result, dispatch = _process(_build_event(_slack_event_payload()))

        assert result["statusCode"] == 200
        assert len(dispatch.tasks) == 1
        task = dispatch.tasks[0]
        assert task.kind == "investigate"
        # Non-experiment path uses the default ARN, resolved by the adapter.
        assert task.master_arn is None


class TestWithExperiment:
    @mock_aws
    def test_two_invokes_when_experiment_active(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(_make_experiment())

        result, dispatch = _process(_build_event(_slack_event_payload()))

        assert result["statusCode"] == 200
        assert len(dispatch.tasks) == 2
        arns = {t.master_arn for t in dispatch.tasks}
        assert arns == {"AGENT_A", "AGENT_B"}

    @mock_aws
    def test_variant_ids_in_alert_context(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(_make_experiment())

        _, dispatch = _process(_build_event(_slack_event_payload()))

        for task in dispatch.tasks:
            ctx = task.alert_context
            assert ctx.experiment_id == "exp-001"
            assert ctx.variant_id in ("a", "b")
            assert ctx.variant_label in ("Claude Sonnet", "Nova Pro")

    @mock_aws
    def test_variants_differ_per_task(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(_make_experiment())

        _, dispatch = _process(_build_event(_slack_event_payload()))

        # Two distinct variants — the per-variant session-id suffix is
        # verified in test_master_dispatch.
        assert {t.alert_context.variant_id for t in dispatch.tasks} == {"a", "b"}

    @mock_aws
    def test_no_fork_when_experiment_paused(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        exp = _make_experiment()
        exp.status = "paused"
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(exp)

        _, dispatch = _process(_build_event(_slack_event_payload()))

        assert len(dispatch.tasks) == 1
        assert dispatch.tasks[0].master_arn is None
