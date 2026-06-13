"""Offline LLM judge that scores A/B experiment variant reports (issue #26).

The A/B harness stores each variant's investigation report
(:mod:`shared.experiment_results_store`) but nothing decides which variant won.
This module adds a pairwise judge: a stronger (Opus-class) model reads the two
reports for the same alert and scores them on a fixed rubric — evidence
coverage, severity correctness, actionability, and noise.

Position bias is the main hazard in pairwise LLM judging, so every pair is
judged **twice** with the variants swapped. A dimension counts as a real win
only when the judge picks the same variant in both orderings; if its pick
flips with presentation order, that dimension is recorded as a ``tie``. Both
calls run at temperature 0 (configured on the resolved model) so the only
intended source of variation is the swap itself.

The judge runs strictly out-of-band (a CLI / scheduled pass over the trace
archive), never on the investigation hot path.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from shared.experiment import JUDGEMENT_DIMENSIONS, ExperimentResult, Judgement
from shared.model_call import StructuredModelCall

logger = logging.getLogger(__name__)

# Opus-class default; override per-run with ``JUDGE_MODEL_ID``. Short Bedrock
# inference-profile form (the date-stamped variants are not assigned — see the
# #47/#48 model-id correction). Operators must confirm this profile is enabled
# in the target account, or point ``JUDGE_MODEL_ID`` at one that is.
DEFAULT_JUDGE_MODEL_ID = "us.anthropic.claude-opus-4-5"

DEFAULT_JUDGE_TIMEOUT_SECONDS: float = 60.0

_Pick = Literal["first", "second", "tie"]

_SYSTEM_PROMPT = (
    "You are an impartial incident-response evaluator comparing two AI-generated "
    "incident investigation reports for the SAME alert. The reports are labelled "
    "REPORT 1 and REPORT 2.\n"
    "Score which report is better on each rubric dimension:\n"
    "- coverage: breadth and depth of relevant evidence gathered.\n"
    "- severity: correctness of the severity / impact assessment.\n"
    "- actionability: how directly it points a responder to the next useful action.\n"
    "- noise: freedom from irrelevant, speculative, or redundant content "
    "(less noise is better).\n"
    "For every dimension answer 'first' if REPORT 1 is better, 'second' if "
    "REPORT 2 is better, or 'tie' if they are genuinely equivalent. Also give an "
    "'overall' verdict the same way. Judge only on the evidence shown; do not "
    "reward length for its own sake. Be consistent and decisive."
)


class JudgeVerdict(BaseModel):
    """One ordering's structured verdict. Picks are relative to presentation order."""

    coverage: _Pick = Field(description="Which report has better evidence coverage.")
    severity: _Pick = Field(description="Which report assesses severity more correctly.")
    actionability: _Pick = Field(description="Which report is more actionable.")
    noise: _Pick = Field(description="Which report has less noise (less is better).")
    overall: _Pick = Field(description="Overall better report.")
    rationale: str = Field(description="One or two sentences justifying the verdict.")


def build_judge_prompt(first: ExperimentResult, second: ExperimentResult) -> str:
    """Render the head-to-head prompt for one ordering. Pure and deterministic."""
    return "\n".join([
        "## REPORT 1",
        first.report or "(empty report)",
        "",
        "## REPORT 2",
        second.report or "(empty report)",
    ])


def _default_model_call() -> StructuredModelCall:
    """The judge's structured-output call: Opus-class default, ``temperature=0``,
    no gate flag (the judge is always available; it is gated by being offline)."""
    model_call = StructuredModelCall.from_env(
        system_prompt=_SYSTEM_PROMPT,
        model_env="JUDGE_MODEL_ID",
        timeout_env="JUDGE_TIMEOUT_SECONDS",
        default_timeout=DEFAULT_JUDGE_TIMEOUT_SECONDS,
        default_model_id=DEFAULT_JUDGE_MODEL_ID,
        temperature=0.0,
    )
    assert model_call is not None  # no gate_env → always built
    return model_call


def _map_pick(pick: str, first_variant: str) -> str:
    """Translate an order-relative pick into a variant id (``a``/``b``/``tie``)."""
    if pick == "tie":
        return "tie"
    second_variant = "b" if first_variant == "a" else "a"
    return first_variant if pick == "first" else second_variant


def _combine(winner_order_ab: str, winner_order_ba: str) -> str:
    """Agree → that winner; disagree (position bias) → tie."""
    return winner_order_ab if winner_order_ab == winner_order_ba else "tie"


class ExperimentJudge:
    """Runs the dual-order pairwise judge for one variant pair.

    Composes one :class:`StructuredModelCall` (injectable for tests), pinned to
    its own (Opus-class by default, ``JUDGE_MODEL_ID`` override) model at
    ``temperature=0`` independent of the deploy-time ``MODEL_ID`` the scanners
    run on. ``judge_pair`` is offline-only and uses ``call_or_raise`` — the
    judge owns its failure policy (the CLI fails per-pair). ``judge_pair`` is
    offline-only.
    """

    def __init__(self, *, model_call: StructuredModelCall | None = None) -> None:
        self._model_call = model_call if model_call is not None else _default_model_call()

    @classmethod
    def from_env(cls) -> ExperimentJudge:
        """Build a judge from the environment (``JUDGE_MODEL_ID``/``JUDGE_TIMEOUT_SECONDS``)."""
        return cls(model_call=_default_model_call())

    @property
    def model_id(self) -> str:
        """The judge model id actually in effect (explicit → env → Opus default)."""
        return self._model_call.model_id or DEFAULT_JUDGE_MODEL_ID

    @property
    def timeout_seconds(self) -> float:
        return self._model_call.timeout_seconds

    async def _verdict(self, first: ExperimentResult, second: ExperimentResult) -> JudgeVerdict:
        prompt = build_judge_prompt(first, second)
        return await self._model_call.call_or_raise(JudgeVerdict, prompt)

    async def judge_pair(
        self,
        variant_a: ExperimentResult,
        variant_b: ExperimentResult,
        *,
        variant_model_ids: tuple[str | None, ...] = (),
        timestamp: str = "",
    ) -> Judgement:
        """Judge a variant pair in both orderings and return the combined verdict.

        ``variant_model_ids`` is used only to warn when the judge shares a model
        with a variant (a self-judging bias); it never blocks the judgement.
        """
        if self.model_id in {m for m in variant_model_ids if m}:
            logger.warning(
                "Judge model %s matches a variant's model; pairwise judgement "
                "may be biased toward that variant.",
                self.model_id,
            )

        verdict_ab = await self._verdict(variant_a, variant_b)  # REPORT 1 = a
        verdict_ba = await self._verdict(variant_b, variant_a)  # REPORT 1 = b

        dimension_winners = {
            dim: _combine(
                _map_pick(getattr(verdict_ab, dim), "a"),
                _map_pick(getattr(verdict_ba, dim), "b"),
            )
            for dim in JUDGEMENT_DIMENSIONS
        }
        overall_winner = _combine(
            _map_pick(verdict_ab.overall, "a"),
            _map_pick(verdict_ba.overall, "b"),
        )

        return Judgement(
            experiment_id=variant_a.experiment_id,
            investigation_id=variant_a.investigation_id,
            overall_winner=overall_winner,
            dimension_winners=dimension_winners,
            judge_model_id=self.model_id,
            rationale=verdict_ab.rationale,
            timestamp=timestamp,
        )
