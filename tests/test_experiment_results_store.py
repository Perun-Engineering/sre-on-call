"""Unit tests for shared.experiment_results_store."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from shared.experiment import ExperimentResult
from shared.experiment_results_store import DEFAULT_TABLE_NAME, ExperimentResultsStore


def _make_result(variant_id: str = "a") -> ExperimentResult:
    return ExperimentResult(
        experiment_id="exp-001",
        investigation_id="inv-001",
        variant_id=variant_id,
        report="Test report",
        agent_durations={"eks": 2.5, "prometheus": 1.1},
        total_duration_seconds=5.0,
        timestamp="2026-04-30T10:00:00Z",
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


class TestExperimentResultsStore:
    def test_put_and_get_single_result(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a"))
        results = store.get_results("exp-001", "inv-001")
        assert len(results) == 1
        assert results[0].variant_id == "a"
        assert results[0].report == "Test report"

    def test_get_both_variants(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a"))
        store.put_result(_make_result("b"))
        results = store.get_results("exp-001", "inv-001")
        assert len(results) == 2
        variant_ids = {r.variant_id for r in results}
        assert variant_ids == {"a", "b"}

    def test_get_results_empty(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        results = store.get_results("exp-999", "inv-999")
        assert results == []

    def test_agent_durations_roundtrip(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a"))
        results = store.get_results("exp-001", "inv-001")
        assert results[0].agent_durations["eks"] == 2.5
        assert results[0].agent_durations["prometheus"] == 1.1

    def test_total_duration_roundtrip(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a"))
        results = store.get_results("exp-001", "inv-001")
        assert results[0].total_duration_seconds == 5.0

    def test_item_has_ttl(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a"))
        table = dynamodb_table.Table(DEFAULT_TABLE_NAME)
        item = table.get_item(Key={"pk": "exp-001#inv-001#a"})["Item"]
        assert "ttl" in item
        assert int(item["ttl"]) > 0
