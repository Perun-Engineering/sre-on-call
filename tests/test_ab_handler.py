"""Tests for A/B experiment forking in the unified Lambda handler."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from lambda_adapter.handler import lambda_handler
from shared.experiment import (
    ExperimentConfig,
    PipelineVariant,
)
from shared.experiment_store import DEFAULT_TABLE_NAME as EXP_TABLE


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


def _patch_runtime():
    """Patch the intake's boto3.client so we don't hit AgentCore."""
    return patch("lambda_adapter.intake.boto3.client")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoExperiment:
    @mock_aws
    def test_single_invoke_when_no_experiment_table(self) -> None:
        _create_tables()
        event = _build_event(_slack_event_payload())

        with _patch_runtime() as mock_boto:
            mock_runtime = MagicMock()
            mock_boto.return_value = mock_runtime

            result = lambda_handler(event, None)

            assert result["statusCode"] == 200
            mock_runtime.invoke_agent_runtime.assert_called_once()
            assert (
                mock_runtime.invoke_agent_runtime.call_args[1]["agentRuntimeArn"]
                == "DEFAULT_AGENT"
            )


class TestWithExperiment:
    @mock_aws
    def test_two_invokes_when_experiment_active(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(_make_experiment())

        event = _build_event(_slack_event_payload())

        with _patch_runtime() as mock_boto:
            mock_runtime = MagicMock()
            mock_boto.return_value = mock_runtime

            result = lambda_handler(event, None)

            assert result["statusCode"] == 200
            assert mock_runtime.invoke_agent_runtime.call_count == 2
            agent_arns = [
                c[1]["agentRuntimeArn"]
                for c in mock_runtime.invoke_agent_runtime.call_args_list
            ]
            assert "AGENT_A" in agent_arns
            assert "AGENT_B" in agent_arns

    @mock_aws
    def test_variant_ids_in_input_text(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(_make_experiment())

        event = _build_event(_slack_event_payload())

        with _patch_runtime() as mock_boto:
            mock_runtime = MagicMock()
            mock_boto.return_value = mock_runtime

            lambda_handler(event, None)

            for call in mock_runtime.invoke_agent_runtime.call_args_list:
                envelope = json.loads(call[1]["payload"].decode("utf-8"))
                payload = json.loads(envelope["params"]["message"]["parts"][0]["text"])
                assert payload["experiment_id"] == "exp-001"
                assert payload["variant_id"] in ("a", "b")
                assert payload["variant_label"] in ("Claude Sonnet", "Nova Pro")

    @mock_aws
    def test_session_ids_differ_per_variant(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(_make_experiment())

        event = _build_event(_slack_event_payload())

        with _patch_runtime() as mock_boto:
            mock_runtime = MagicMock()
            mock_boto.return_value = mock_runtime

            lambda_handler(event, None)

            session_ids = [
                c[1]["runtimeSessionId"]
                for c in mock_runtime.invoke_agent_runtime.call_args_list
            ]
            assert len(set(session_ids)) == 2
            assert any(s.endswith("-a") for s in session_ids)
            assert any(s.endswith("-b") for s in session_ids)

    @mock_aws
    def test_no_fork_when_experiment_paused(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENTS_TABLE_NAME", EXP_TABLE)
        ddb = _create_tables(create_experiments=True)

        from shared.experiment_store import ExperimentStore
        exp = _make_experiment()
        exp.status = "paused"
        ExperimentStore(table_name=EXP_TABLE, dynamodb_resource=ddb).put_experiment(exp)

        event = _build_event(_slack_event_payload())

        with _patch_runtime() as mock_boto:
            mock_runtime = MagicMock()
            mock_boto.return_value = mock_runtime

            lambda_handler(event, None)

            mock_runtime.invoke_agent_runtime.assert_called_once()
            assert (
                mock_runtime.invoke_agent_runtime.call_args[1]["agentRuntimeArn"]
                == "DEFAULT_AGENT"
            )
