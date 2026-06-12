"""Unit tests for shared.experiment_results_store."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws
from typing import Any

from shared.experiment import ExperimentResult, Judgement
from shared.experiment_results_store import DEFAULT_TABLE_NAME, ExperimentResultsStore


def _make_result(
    variant_id: str = "a",
    investigation_id: str = "inv-001",
    total_cost_usd: float | None = None,
    total_tokens: int | None = None,
) -> ExperimentResult:
    return ExperimentResult(
        experiment_id="exp-001",
        investigation_id=investigation_id,
        variant_id=variant_id,
        report="Test report",
        agent_durations={"eks": 2.5, "prometheus": 1.1},
        total_duration_seconds=5.0,
        timestamp="2026-04-30T10:00:00Z",
        total_cost_usd=total_cost_usd,
        total_tokens=total_tokens,
    )


@pytest.fixture()
def dynamodb_table():
    with mock_aws():
        dynamodb: Any = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=DEFAULT_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb


class TestTableNameResolution:
    """The results-table name resolves: explicit arg > env var > default.

    A/B scorecard runs (#29) deploy a second master arm under a different
    ``environment`` whose project-scoped results table is shared. The control
    master points at the shared table via ``EXPERIMENT_RESULTS_TABLE_NAME``.
    """

    @pytest.fixture()
    def shared_table(self):
        with mock_aws():
            dynamodb: Any = boto3.resource("dynamodb", region_name="us-east-1")
            dynamodb.create_table(
                TableName="shared-results",
                KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            yield dynamodb

    def test_env_var_selects_table_when_no_arg(self, shared_table, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENT_RESULTS_TABLE_NAME", "shared-results")
        store = ExperimentResultsStore(dynamodb_resource=shared_table)
        store.put_result(_make_result("a"))
        assert store.get_results("exp-001", "inv-001")[0].variant_id == "a"

    def test_explicit_arg_overrides_env_var(self, shared_table, monkeypatch) -> None:
        monkeypatch.setenv("EXPERIMENT_RESULTS_TABLE_NAME", "ignored-name")
        store = ExperimentResultsStore(
            table_name="shared-results", dynamodb_resource=shared_table
        )
        store.put_result(_make_result("b"))
        assert store.get_results("exp-001", "inv-001")[0].variant_id == "b"


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

    def test_cost_and_tokens_roundtrip(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a", total_cost_usd=0.0123, total_tokens=4567))
        result = store.get_results("exp-001", "inv-001")[0]
        assert result.total_cost_usd == 0.0123
        assert result.total_tokens == 4567

    def test_missing_cost_and_tokens_default_to_none(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a"))
        result = store.get_results("exp-001", "inv-001")[0]
        assert result.total_cost_usd is None
        assert result.total_tokens is None

    def test_legacy_row_without_telemetry_still_loads(self, dynamodb_table) -> None:
        # A row written before the telemetry fields existed (no attributes).
        table = dynamodb_table.Table(DEFAULT_TABLE_NAME)
        table.put_item(Item={
            "pk": "exp-001#inv-001#a",
            "experiment_id": "exp-001",
            "investigation_id": "inv-001",
            "variant_id": "a",
            "report": "old report",
        })
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        result = store.get_results("exp-001", "inv-001")[0]
        assert result.report == "old report"
        assert result.total_cost_usd is None
        assert result.total_tokens is None


class TestIterPairs:
    def test_yields_only_complete_pairs(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        # inv-001 has both variants; inv-002 only has a -> skipped.
        store.put_result(_make_result("a", investigation_id="inv-001"))
        store.put_result(_make_result("b", investigation_id="inv-001"))
        store.put_result(_make_result("a", investigation_id="inv-002"))

        pairs = list(store.iter_pairs("exp-001"))
        assert len(pairs) == 1
        a, b = pairs[0]
        assert a.investigation_id == "inv-001"
        assert (a.variant_id, b.variant_id) == ("a", "b")

    def test_ignores_judgement_rows(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a", investigation_id="inv-001"))
        store.put_result(_make_result("b", investigation_id="inv-001"))
        store.put_judgement(_make_judgement())
        pairs = list(store.iter_pairs("exp-001"))
        assert len(pairs) == 1


def _make_judgement(investigation_id: str = "inv-001") -> Judgement:
    return Judgement(
        experiment_id="exp-001",
        investigation_id=investigation_id,
        overall_winner="b",
        dimension_winners={
            "coverage": "b",
            "severity": "tie",
            "actionability": "a",
            "noise": "b",
        },
        judge_model_id="us.anthropic.claude-opus-4-5",
        rationale="B has deeper evidence coverage.",
        timestamp="2026-05-01T00:00:00Z",
    )


class TestJudgementStore:
    def test_put_and_get_judgement(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_judgement(_make_judgement())
        got = store.get_judgement("exp-001", "inv-001")
        assert got is not None
        assert got.overall_winner == "b"
        assert got.dimension_winners["severity"] == "tie"
        assert got.judge_model_id == "us.anthropic.claude-opus-4-5"

    def test_get_missing_judgement_returns_none(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        assert store.get_judgement("exp-001", "nope") is None

    def test_rejudge_overwrites_idempotently(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_judgement(_make_judgement())
        revised = _make_judgement()
        revised.overall_winner = "a"
        store.put_judgement(revised)
        got = store.get_judgement("exp-001", "inv-001")
        assert got is not None and got.overall_winner == "a"

    def test_judgement_does_not_collide_with_results(self, dynamodb_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=dynamodb_table)
        store.put_result(_make_result("a"))
        store.put_result(_make_result("b"))
        store.put_judgement(_make_judgement())
        # Both result rows and the judgement coexist under the same prefix.
        assert len(store.get_results("exp-001", "inv-001")) == 2
        assert store.get_judgement("exp-001", "inv-001") is not None
