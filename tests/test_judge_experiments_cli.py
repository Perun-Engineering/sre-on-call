"""Tests for the offline judge CLI orchestration (issue #26)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from typing import Any

from shared.experiment import (
    AgentVariantConfig,
    ExperimentConfig,
    ExperimentResult,
    Judgement,
    PipelineVariant,
)
from shared.experiment_results_store import DEFAULT_TABLE_NAME, ExperimentResultsStore

# Load the script module by path (scripts/ is not an importable package).
_CLI_PATH = Path(__file__).resolve().parent.parent / "scripts" / "judge_experiments.py"
_spec = importlib.util.spec_from_file_location("judge_experiments", _CLI_PATH)
assert _spec and _spec.loader
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture()
def results_store():
    with mock_aws():
        dynamodb: Any = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=DEFAULT_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ExperimentResultsStore(dynamodb_resource=dynamodb)


def _seed_pair(store: ExperimentResultsStore, inv: str) -> None:
    for vid in ("a", "b"):
        store.put_result(ExperimentResult(
            experiment_id="exp-1",
            investigation_id=inv,
            variant_id=vid,
            report=f"{vid} report",
        ))


class _CountingJudge:
    """Fake judge that records calls and always declares B the winner."""

    def __init__(self) -> None:
        self.calls = 0

    async def judge_pair(self, a, b, *, variant_model_ids=(), timestamp="") -> Judgement:
        self.calls += 1
        return Judgement(
            experiment_id=a.experiment_id,
            investigation_id=a.investigation_id,
            overall_winner="b",
            dimension_winners={
                "coverage": "b", "severity": "b", "actionability": "b", "noise": "b"
            },
            judge_model_id="fake-judge",
            timestamp=timestamp,
        )


class TestRun:
    def test_judges_and_persists(self, results_store):
        _seed_pair(results_store, "inv-1")
        judge = _CountingJudge()
        out = asyncio.run(cli.run("exp-1", results_store=results_store, judge=judge))
        assert judge.calls == 1
        assert "winner=B" in out
        # Persisted for reuse.
        assert results_store.get_judgement("exp-1", "inv-1") is not None

    def test_skips_already_judged(self, results_store):
        _seed_pair(results_store, "inv-1")
        judge = _CountingJudge()
        asyncio.run(cli.run("exp-1", results_store=results_store, judge=judge))
        # Second run should reuse the stored judgement, not re-invoke the judge.
        asyncio.run(cli.run("exp-1", results_store=results_store, judge=judge))
        assert judge.calls == 1

    def test_rejudge_forces_reinvocation(self, results_store):
        _seed_pair(results_store, "inv-1")
        judge = _CountingJudge()
        asyncio.run(cli.run("exp-1", results_store=results_store, judge=judge))
        asyncio.run(cli.run("exp-1", results_store=results_store, judge=judge, rejudge=True))
        assert judge.calls == 2

    def test_json_output(self, results_store):
        _seed_pair(results_store, "inv-1")
        out = asyncio.run(
            cli.run("exp-1", results_store=results_store, judge=_CountingJudge(), as_json=True)
        )
        data = json.loads(out)
        assert data["experiment_id"] == "exp-1"
        assert data["rows"][0]["overall_winner"] == "b"

    def test_failing_judge_is_skipped(self, results_store):
        _seed_pair(results_store, "inv-1")

        class _Boom:
            async def judge_pair(self, a, b, **_kw):
                raise RuntimeError("bedrock down")

        out = asyncio.run(cli.run("exp-1", results_store=results_store, judge=_Boom()))
        assert "No judged investigations." in out


class TestVariantModelIds:
    def _config(self) -> ExperimentConfig:
        return ExperimentConfig(
            experiment_id="exp-1",
            name="t",
            status="active",
            variant_a=PipelineVariant(
                variant_id="a",
                label="A",
                master_endpoint="m-a",
                agents={"eks": AgentVariantConfig(endpoint="e", model_id="model-a")},
            ),
            variant_b=PipelineVariant(
                variant_id="b",
                label="B",
                master_endpoint="m-b",
                agents={"eks": AgentVariantConfig(endpoint="e", model_id="model-b")},
            ),
            created_at="",
            updated_at="",
        )

    def test_collects_model_ids(self):
        assert set(cli.variant_model_ids(self._config())) == {"model-a", "model-b"}

    def test_none_config_yields_empty(self):
        assert cli.variant_model_ids(None) == ()
