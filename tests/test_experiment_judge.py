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


class _FakeJudge:
    """Stand-in Strands agent driven by a prompt -> verdict function."""

    def __init__(
        self,
        verdict_fn: Callable[[str], JudgeVerdict],
        *,
        delay: float = 0.0,
    ) -> None:
        self._fn = verdict_fn
        self._delay = delay
        self.prompts: list[str] = []

    async def structured_output_async(
        self, output_model: type[JudgeVerdict], prompt: str
    ) -> JudgeVerdict:
        self.prompts.append(prompt)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._fn(prompt)


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
        judge = ExperimentJudge(agent=_FakeJudge(lambda _p: _verdict("first")))

        judgement = await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))

        assert judgement.overall_winner == "tie"
        assert all(w == "tie" for w in judgement.dimension_winners.values())

    @pytest.mark.asyncio
    async def test_consistent_preference_yields_a_winner(self):
        # Judge consistently prefers the variant-A report regardless of position.
        def prefer_a(prompt: str) -> JudgeVerdict:
            a_is_first = prompt.index("ALPHA") < prompt.index("BETA")
            return _verdict("first" if a_is_first else "second")

        judge = ExperimentJudge(agent=_FakeJudge(prefer_a))
        judgement = await judge.judge_pair(_result("a", "ALPHA"), _result("b", "BETA"))

        assert judgement.overall_winner == "a"
        assert judgement.dimension_winners["coverage"] == "a"

    @pytest.mark.asyncio
    async def test_consistent_tie_stays_tie(self):
        judge = ExperimentJudge(agent=_FakeJudge(lambda _p: _verdict("tie")))
        judgement = await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))
        assert judgement.overall_winner == "tie"

    @pytest.mark.asyncio
    async def test_runs_both_orderings(self):
        fake = _FakeJudge(lambda _p: _verdict("tie"))
        judge = ExperimentJudge(agent=fake)
        await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))
        assert len(fake.prompts) == 2

    @pytest.mark.asyncio
    async def test_carries_pair_identity(self):
        judge = ExperimentJudge(agent=_FakeJudge(lambda _p: _verdict("tie")))
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
        assert ExperimentJudge().model_id == DEFAULT_JUDGE_MODEL_ID

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("JUDGE_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        assert ExperimentJudge().model_id == "us.anthropic.claude-sonnet-4-6"

    def test_explicit_arg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("JUDGE_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        assert ExperimentJudge(model_id="explicit-id").model_id == "explicit-id"

    def test_from_env_reads_timeout(self, monkeypatch):
        monkeypatch.setenv("JUDGE_TIMEOUT_SECONDS", "12.5")
        assert ExperimentJudge.from_env()._timeout_seconds == 12.5


class TestBiasWarning:
    @pytest.mark.asyncio
    async def test_warns_when_judge_equals_variant_model(self, monkeypatch, caplog):
        monkeypatch.setenv("JUDGE_MODEL_ID", "shared-model")
        judge = ExperimentJudge(agent=_FakeJudge(lambda _p: _verdict("tie")))
        with caplog.at_level(logging.WARNING):
            await judge.judge_pair(
                _result("a", "AA"),
                _result("b", "BB"),
                variant_model_ids=("shared-model", "other-model"),
            )
        assert any("matches a variant" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_warning_when_distinct(self, monkeypatch, caplog):
        monkeypatch.setenv("JUDGE_MODEL_ID", "judge-model")
        judge = ExperimentJudge(agent=_FakeJudge(lambda _p: _verdict("tie")))
        with caplog.at_level(logging.WARNING):
            await judge.judge_pair(
                _result("a", "AA"),
                _result("b", "BB"),
                variant_model_ids=("a-model", "b-model"),
            )
        assert not any("matches a variant" in r.message for r in caplog.records)


class TestTimeout:
    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        judge = ExperimentJudge(
            agent=_FakeJudge(lambda _p: _verdict("tie"), delay=0.2),
            timeout_seconds=0.01,
        )
        with pytest.raises(asyncio.TimeoutError):
            await judge.judge_pair(_result("a", "AA"), _result("b", "BB"))
