"""Stage 2 bounded follow-up planner for the master orchestrator (issue #28).

After the initial harvest and report, the master asks a Sonnet-class model a
single question: given what came back, is one more targeted dispatch worth it?
The answer is at most ``max_agents`` refined dispatches, which land through the
existing late-result enrichment path. The round is hard-capped so the 5-minute
cutoff always holds — the orchestrator only invokes the planner when the
remaining budget can absorb it, and the planner truncates to ``max_agents``.

Everything here is fail-open: any error, timeout, unparseable response, or a
"no follow-up" decision yields an empty plan and the investigation proceeds
exactly as it does today.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from shared.models import AgentFailure, AgentResult, AlertContext

logger = logging.getLogger(__name__)

# Seconds the follow-up planning call may take before it is abandoned.
DEFAULT_FOLLOWUP_TIMEOUT_SECONDS: float = 6.0

# Hard cap on agents in the single follow-up round.
DEFAULT_MAX_FOLLOWUP_AGENTS: int = 2

_SYSTEM_PROMPT = (
    "You are the lead incident responder deciding whether ONE additional round "
    "of targeted investigation is worthwhile, after an initial parallel sweep.\n"
    "You are given the alert, the results gathered so far, and the agents still "
    "eligible for a second, refined dispatch.\n"
    "Rules:\n"
    "- Only request a follow-up when a specific, answerable question remains "
    "that a refined dispatch would resolve. If the evidence already tells the "
    "story, set should_followup to false.\n"
    "- Each dispatch needs a sharper hint than the first round: the exact "
    "resource, log group, or time window to drill into.\n"
    "- Never invent agents. Only reference the agent ids you were given.\n"
    "- Be frugal — this is the last round before the report is finalized."
)


@dataclass(frozen=True)
class FollowupCandidate:
    """An agent eligible for a second, refined dispatch."""

    agent_id: str
    description: str


class FollowupDispatch(BaseModel):
    """One refined re-dispatch the planner proposes."""

    agent_id: str = Field(description="An agent id from the supplied roster.")
    hint: str = Field(
        default="",
        description="A sharper, one-sentence hint for the refined dispatch.",
    )
    reason: str = Field(
        default="", description="Why this refined dispatch is worth the budget."
    )


class FollowupDecision(BaseModel):
    """The planner's decision about a single additional round."""

    should_followup: bool = Field(
        description="True only if a refined dispatch would add real value."
    )
    dispatches: list[FollowupDispatch] = Field(
        description="The refined dispatches to run; empty when should_followup is false."
    )
    rationale: str = Field(description="One-line overall rationale.")


def _summarize_result(agent_id: str, result: AgentResult | AgentFailure) -> str:
    if isinstance(result, AgentFailure):
        return f"- {agent_id}: FAILED — {result.error_message}"
    detail = result.summary or f"status={result.status}, {len(result.findings)} findings"
    return f"- {agent_id} ({result.status}): {detail}"


def build_followup_prompt(
    alert_context: AlertContext,
    results: dict[str, AgentResult | AgentFailure],
    candidates: list[FollowupCandidate],
) -> str:
    """Build the follow-up user prompt. Pure and deterministic for unit testing."""
    parts = [
        "## Alert",
        alert_context.alert_text,
        "",
        "## Results so far",
    ]
    if results:
        parts.extend(_summarize_result(aid, r) for aid, r in results.items())
    else:
        parts.append("(no agent responded before the deadline)")
    parts.append("")
    parts.append("## Agents eligible for a refined follow-up dispatch")
    parts.extend(f"- {c.agent_id}: {c.description}" for c in candidates)
    return "\n".join(parts)


class _StructuredAgent(Protocol):
    async def structured_output_async(
        self, output_model: type[FollowupDecision], prompt: str
    ) -> FollowupDecision: ...


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class FollowupPlanner:
    """Runs the Stage 2 follow-up decision, fail-open and hard-capped.

    The agent is injectable for tests; in production it is lazily built from a
    tools-less Strands ``Agent`` bound to the follow-up model. ``plan`` never
    raises — on any failure or a "no follow-up" decision it returns ``[]``.
    """

    def __init__(
        self,
        *,
        agent: _StructuredAgent | None = None,
        model_id: str | None = None,
        timeout_seconds: float = DEFAULT_FOLLOWUP_TIMEOUT_SECONDS,
        max_agents: int = DEFAULT_MAX_FOLLOWUP_AGENTS,
    ) -> None:
        self._agent = agent
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._max_agents = max_agents

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def max_agents(self) -> int:
        return self._max_agents

    @classmethod
    def from_env(cls) -> FollowupPlanner | None:
        """Build a planner from the environment, or ``None`` when disabled.

        Gated on ``FOLLOWUP_ROUND_ENABLED`` (off by default). The model
        resolves to ``FOLLOWUP_MODEL_ID`` → ``MODEL_ID`` → config/default; the
        timeout and cap are overridable via ``FOLLOWUP_TIMEOUT_SECONDS`` /
        ``FOLLOWUP_MAX_AGENTS``.
        """
        if not _truthy(os.environ.get("FOLLOWUP_ROUND_ENABLED")):
            return None
        timeout_raw = os.environ.get("FOLLOWUP_TIMEOUT_SECONDS")
        try:
            timeout = (
                float(timeout_raw) if timeout_raw else DEFAULT_FOLLOWUP_TIMEOUT_SECONDS
            )
        except ValueError:
            timeout = DEFAULT_FOLLOWUP_TIMEOUT_SECONDS
        max_raw = os.environ.get("FOLLOWUP_MAX_AGENTS")
        try:
            max_agents = int(max_raw) if max_raw else DEFAULT_MAX_FOLLOWUP_AGENTS
        except ValueError:
            max_agents = DEFAULT_MAX_FOLLOWUP_AGENTS
        return cls(
            model_id=os.environ.get("FOLLOWUP_MODEL_ID") or None,
            timeout_seconds=timeout,
            max_agents=max_agents,
        )

    def _get_agent(self) -> _StructuredAgent | None:
        if self._agent is not None:
            return self._agent
        try:
            from strands import Agent

            from shared.a2a_factory import _resolve_model

            model = _resolve_model(model_id_override=self._model_id)
            self._agent = Agent(model=model, system_prompt=_SYSTEM_PROMPT)
        except Exception:
            logger.exception("Failed to build the follow-up agent; skipping the round.")
            return None
        return self._agent

    async def plan(
        self,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
        candidates: list[FollowupCandidate],
    ) -> list[tuple[str, str]]:
        """Return ``(agent_id, hint)`` refined dispatches, capped at ``max_agents``.

        Fail-open: returns ``[]`` on any failure, when there are no eligible
        candidates, or when the planner decides no follow-up is warranted.
        Unknown agent ids are dropped.
        """
        if not candidates:
            return []
        agent = self._get_agent()
        if agent is None:
            return []
        prompt = build_followup_prompt(alert_context, results, candidates)
        try:
            decision = await asyncio.wait_for(
                agent.structured_output_async(FollowupDecision, prompt),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.warning(
                "Follow-up planning failed for investigation %s; skipping the round.",
                alert_context.investigation_id,
                exc_info=True,
            )
            return []
        if not decision.should_followup:
            return []
        candidate_ids = {c.agent_id for c in candidates}
        plan: list[tuple[str, str]] = []
        seen: set[str] = set()
        for d in decision.dispatches:
            if d.agent_id in candidate_ids and d.agent_id not in seen:
                plan.append((d.agent_id, d.hint.strip()))
                seen.add(d.agent_id)
            if len(plan) >= self._max_agents:
                break
        return plan
