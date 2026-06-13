"""Tests for the offline A/B LLM judge (issue #26)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest

from shared.experiment import ExperimentResult
from shared.experiment_judge import (
    DEFAULT_JUDGE_MODEL_ID,
    ExperimentJudge,
    JudgeVerdict,
    build_judge_prompt,
)
from tests.fakes import FakeModelCall


def _result(variant_id: str, report: str, investigation_id: str = "inv-1") -> ExperimentResult:
    return ExperimentResult(
        experiment_id="exp-1",
        investigation_id=investigation_id,
        variant_id=variant_id,
        report=report,
    )


def _verdict(pick: str) -> JudgeVerdict:
    """A verdict that returns the same pick for every dimension."""
    return JudgeVerdict(
        coverage=pick,  # type: ignore[arg-type]
        severity=pick,  # type: ignore[arg-type]
        actionability=pick,  # type: ignore[arg-type]
        noise=pick,  # type: ignore[arg-type]
        overall=pick,  # type: ignore[arg-type]
        rationale=f"always {pick}",
    )


def _judge(
    verdict_fn: Callable[[str], JudgeVerdict],
    *,
    model_id: str | None = DEFAULT_JUDGE_MODEL_ID,
) -> ExperimentJudge:
    return ExperimentJudge(
        model_call=FakeModelCall(response_fn=verdict_fn, model_id=model_id)
    )


class TestBuildJudgePrompt:
    def test_includes_both_reports_labelled(self):
        prompt = build_judge_prompt(_result("a", "alpha findings"), _result("b", "beta findings"))
        assert "REPORT 1" in prompt and "alpha findings" in prompt
        assert "REPORT 2" in prompt and "beta findings" in prompt

    def test_empty_report_rendered_placeholder(self):
        prompt = build_judge_prompt(_result("a", ""), _result("b", "beta"))
        assert "(empty report)" in prompt


class TestDualOrder:
    @pytest.mark.asyncio
    async def test_position_bias_resolves_to_tie(self):
        # Judge always prefers whatever is shown as REPORT 1 (pure position bias).
        judge = _judge(lambda _p: _verdict("first"))

        judgement = await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))

        assert judgement.overall_winner == "tie"
        assert all(w == "tie" for w in judgement.dimension_winners.values())

    @pytest.mark.asyncio
    async def test_consistent_preference_yields_a_winner(self):
        # Judge consistently prefers the variant-A report regardless of position.
        def prefer_a(prompt: str) -> JudgeVerdict:
            a_is_first = prompt.index("ALPHA") < prompt.index("BETA")
            return _verdict("first" if a_is_first else "second")

        judge = _judge(prefer_a)
        judgement = await judge.judge_pair(_result("a", "ALPHA"), _result("b", "BETA"))

        assert judgement.overall_winner == "a"
        assert judgement.dimension_winners["coverage"] == "a"

    @pytest.mark.asyncio
    async def test_consistent_tie_stays_tie(self):
        judge = _judge(lambda _p: _verdict("tie"))
        judgement = await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))
        assert judgement.overall_winner == "tie"

    @pytest.mark.asyncio
    async def test_runs_both_orderings(self):
        mc = FakeModelCall(response_fn=lambda _p: _verdict("tie"))
        judge = ExperimentJudge(model_call=mc)
        await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))
        assert len(mc.prompts) == 2

    @pytest.mark.asyncio
    async def test_carries_pair_identity(self):
        judge = _judge(lambda _p: _verdict("tie"))
        judgement = await judge.judge_pair(
            _result("a", "AA", investigation_id="inv-42"),
            _result("b", "BB", investigation_id="inv-42"),
            timestamp="2026-05-01T00:00:00Z",
        )
        assert judgement.experiment_id == "exp-1"
        assert judgement.investigation_id == "inv-42"
        assert judgement.timestamp == "2026-05-01T00:00:00Z"
        assert judgement.judge_model_id == DEFAULT_JUDGE_MODEL_ID


class TestModelResolution:
    def test_default_is_opus_class(self, monkeypatch):
        monkeypatch.delenv("JUDGE_MODEL_ID", raising=False)
        assert ExperimentJudge.from_env().model_id == DEFAULT_JUDGE_MODEL_ID

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("JUDGE_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        assert ExperimentJudge.from_env().model_id == "us.anthropic.claude-sonnet-4-6"

    def test_injected_model_id_is_reported(self):
        # The judge surfaces whatever model its composed call resolves to.
        judge = ExperimentJudge(model_call=FakeModelCall(model_id="explicit-id"))
        assert judge.model_id == "explicit-id"

    def test_from_env_reads_timeout(self, monkeypatch):
        monkeypatch.setenv("JUDGE_TIMEOUT_SECONDS", "12.5")
        assert ExperimentJudge.from_env().timeout_seconds == 12.5


class TestBiasWarning:
    @pytest.mark.asyncio
    async def test_warns_when_judge_equals_variant_model(self, caplog):
        judge = _judge(lambda _p: _verdict("tie"), model_id="shared-model")
        with caplog.at_level(logging.WARNING):
            await judge.judge_pair(
                _result("a", "AA"),
                _result("b", "BB"),
                variant_model_ids=("shared-model", "other-model"),
            )
        assert any("matches a variant" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_warning_when_distinct(self, caplog):
        judge = _judge(lambda _p: _verdict("tie"), model_id="judge-model")
        with caplog.at_level(logging.WARNING):
            await judge.judge_pair(
                _result("a", "AA"),
                _result("b", "BB"),
                variant_model_ids=("a-model", "b-model"),
            )
        assert not any("matches a variant" in r.message for r in caplog.records)


class TestTimeout:
    @pytest.mark.asyncio
    async def test_propagates_seam_failure(self):
        # The seam raises timeouts/errors from call_or_raise; the judge, which
        # owns its own failure policy, propagates them to the CLI.
        judge = ExperimentJudge(
            model_call=FakeModelCall(
                response_fn=lambda _p: _verdict("tie"), raises=asyncio.TimeoutError()
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))
