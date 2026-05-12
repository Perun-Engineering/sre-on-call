"""Unit tests for shared.experiment_store — DynamoDB experiment config store."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from shared.experiment import (
    AgentVariantConfig,
    ExperimentConfig,
    PipelineVariant,
)
from shared.experiment_store import DEFAULT_TABLE_NAME, ExperimentStore


def _make_experiment(experiment_id: str = "exp-001", status: str = "active") -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id=experiment_id,
        name="claude-vs-nova",
        status=status,
        variant_a=PipelineVariant(
            variant_id="a",
            label="Claude Sonnet",
            master_endpoint="AGENT_A",
            agents={"eks": AgentVariantConfig(endpoint="http://eks-a:9000", model_id="claude-3")},
        ),
        variant_b=PipelineVariant(
            variant_id="b",
            label="Nova Pro",
            master_endpoint="AGENT_B",
            agents={"eks": AgentVariantConfig(endpoint="http://eks-b:9000")},
        ),
        created_at="2026-04-30T10:00:00Z",
        updated_at="2026-04-30T10:00:00Z",
    )


@pytest.fixture()
def dynamodb_table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=DEFAULT_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb


class TestExperimentStore:
    def test_put_and_get_experiment(self, dynamodb_table) -> None:
        store = ExperimentStore(dynamodb_resource=dynamodb_table)
        config = _make_experiment()
        store.put_experiment(config)
        result = store.get_experiment("exp-001")
        assert result is not None
        assert result.experiment_id == "exp-001"
        assert result.name == "claude-vs-nova"
        assert result.variant_a.label == "Claude Sonnet"
        assert result.variant_b.master_endpoint == "AGENT_B"

    def test_get_experiment_not_found(self, dynamodb_table) -> None:
        store = ExperimentStore(dynamodb_resource=dynamodb_table)
        assert store.get_experiment("nonexistent") is None

    def test_get_active_experiment_returns_active(self, dynamodb_table) -> None:
        store = ExperimentStore(dynamodb_resource=dynamodb_table)
        store.put_experiment(_make_experiment("exp-001", status="active"))
        store.put_experiment(_make_experiment("exp-002", status="paused"))
        result = store.get_active_experiment()
        assert result is not None
        assert result.experiment_id == "exp-001"
        assert result.status == "active"

    def test_get_active_experiment_returns_none_when_all_paused(self, dynamodb_table) -> None:
        store = ExperimentStore(dynamodb_resource=dynamodb_table)
        store.put_experiment(_make_experiment("exp-001", status="paused"))
        assert store.get_active_experiment() is None

    def test_get_active_experiment_returns_none_when_empty(self, dynamodb_table) -> None:
        store = ExperimentStore(dynamodb_resource=dynamodb_table)
        assert store.get_active_experiment() is None

    def test_agent_variant_config_roundtrip(self, dynamodb_table) -> None:
        store = ExperimentStore(dynamodb_resource=dynamodb_table)
        config = _make_experiment()
        store.put_experiment(config)
        result = store.get_experiment("exp-001")
        assert result is not None
        eks_a = result.variant_a.agents["eks"]
        assert eks_a.endpoint == "http://eks-a:9000"
        assert eks_a.model_id == "claude-3"
        eks_b = result.variant_b.agents["eks"]
        assert eks_b.endpoint == "http://eks-b:9000"
        assert eks_b.model_id is None

    def test_overwrite_experiment(self, dynamodb_table) -> None:
        store = ExperimentStore(dynamodb_resource=dynamodb_table)
        store.put_experiment(_make_experiment("exp-001", status="active"))
        updated = _make_experiment("exp-001", status="completed")
        store.put_experiment(updated)
        result = store.get_experiment("exp-001")
        assert result is not None
        assert result.status == "completed"
