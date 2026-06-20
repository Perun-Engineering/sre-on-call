"""Pre-dispatch LLM routing for the master orchestrator (issue #28).

Before fanning out, the master asks a Sonnet-class model which of the active
specialized agents are worth dispatching for *this* alert, and a focused
investigation hint for each. Agents the router skips are rendered distinctly
in the Incident Report — a deliberate "not investigated" state, never an
error.

Everything here is fail-open: any error, timeout, unparseable response, or a
decision that would skip *every* agent yields ``None``, and the orchestrator
dispatches every active agent exactly as it does today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from shared.model_call import StructuredModelCall
from shared.models import AlertContext

logger = logging.getLogger(__name__)

# Seconds the routing call may take before it is abandoned. Routing runs
# *before* the fan-out, so its latency is spent out of the 60-second initial
# window; keep it well under the synthesis budget.
DEFAULT_ROUTING_TIMEOUT_SECONDS: float = 8.0

_SYSTEM_PROMPT = (
    "You are the triage router for an automated incident-investigation system. "
    "Given an alert and the roster of available specialized investigation "
    "agents, decide which agents are worth dispatching for THIS alert and give "
    "each dispatched agent a short, focused investigation hint.\n"
    "Reason about the failure boundary before you route. When the alert names a "
    "service or component, trace the failure across the dependency chain — the "
    "alerting component itself, its upstream dependencies, its data stores and "
    "queues, and the underlying infrastructure layer. Dispatch the agents that "
    "can observe each relevant hop so the team can see WHERE the failure "
    "originates, not just where it surfaced.\n"
    "Rules:\n"
    "- Bias toward dispatching. Only skip an agent when the alert is clearly "
    "outside its domain — a wrong skip is a blind spot during a live incident.\n"
    "- A hint is one sentence naming the boundary that agent owns: where to look "
    "(suspected services/pods, candidate log groups, the dependency/store/infra "
    "layer it can observe), what would confirm the failure crossing that "
    "boundary (which layer breaks), and any time-window emphasis. Leave it empty "
    "if you have nothing specific to add.\n"
    "- Never invent agents. Only reference the agent ids you were given.\n"
    "- Give a one-line overall rationale for the routing decision."
)


@dataclass(frozen=True)
class AgentCandidate:
    """One dispatchable agent offered to the router: its id and a role blurb."""

    agent_id: str
    description: str


@dataclass(frozen=True)
class RoutingResult:
    """The validated routing outcome the orchestrator acts on.

    ``selected`` maps each agent to dispatch onto its (possibly empty) hint;
    ``skipped`` maps each deliberately-skipped agent onto the router's reason.
    Together they always partition the candidate set.
    """

    selected: dict[str, str]
    skipped: dict[str, str]
    rationale: str


class AgentSelection(BaseModel):
    """The router's per-agent decision (structured-output vehicle)."""

    agent_id: str = Field(description="An agent id from the supplied roster.")
    dispatch: bool = Field(
        description="True to dispatch this agent for the alert, false to skip it."
    )
    hint: str = Field(
        default="",
        description="One-sentence investigation hint for the dispatched agent.",
    )
    reason: str = Field(
        default="",
        description="Brief justification for dispatching or skipping the agent.",
    )


class RoutingDecision(BaseModel):
    """The router's full decision over the candidate roster."""

    selections: list[AgentSelection] = Field(
        description="One entry per agent you have a decision about."
    )
    rationale: str = Field(description="One-line overall rationale.")


def _summarize_candidate(c: AgentCandidate) -> str:
    return f"- {c.agent_id}: {c.description}"


def build_routing_prompt(
    alert_context: AlertContext, candidates: list[AgentCandidate]
) -> str:
    """Build the routing user prompt. Pure and deterministic for unit testing."""
    parts = [
        "## Alert",
        alert_context.alert_text,
        "",
        "## Available agents",
    ]
    parts.extend(_summarize_candidate(c) for c in candidates)
    return "\n".join(parts)


class AgentRouter:
    """Runs the pre-dispatch routing call, fail-open.

    Composes one :class:`StructuredModelCall` (injectable for tests). ``route``
    never raises — on any error, timeout, build failure, or a decision that
    would skip every agent it returns ``None`` and the caller dispatches all
    agents.
    """

    def __init__(self, *, model_call: StructuredModelCall | None = None) -> None:
        self._model_call = model_call

    @property
    def timeout_seconds(self) -> float:
        return (
            self._model_call.timeout_seconds
            if self._model_call is not None
            else DEFAULT_ROUTING_TIMEOUT_SECONDS
        )

    @classmethod
    def from_env(cls) -> AgentRouter | None:
        """Build a router from the environment, or ``None`` when disabled.

        Gated on ``ALERT_ROUTING_ENABLED``. The routing model resolves to
        ``ROUTING_MODEL_ID`` → ``MODEL_ID`` → config/default; the timeout is
        overridable via ``ROUTING_TIMEOUT_SECONDS``.
        """
        model_call = StructuredModelCall.from_env(
            system_prompt=_SYSTEM_PROMPT,
            gate_env="ALERT_ROUTING_ENABLED",
            model_env="ROUTING_MODEL_ID",
            timeout_env="ROUTING_TIMEOUT_SECONDS",
            default_timeout=DEFAULT_ROUTING_TIMEOUT_SECONDS,
        )
        if model_call is None:
            return None
        return cls(model_call=model_call)

    async def route(
        self, alert_context: AlertContext, candidates: list[AgentCandidate]
    ) -> RoutingResult | None:
        """Decide which candidates to dispatch, or ``None`` to dispatch all.

        Fail-open: returns ``None`` on any failure *and* when the decision
        would leave no agent dispatched (an alert always warrants at least one
        investigator). Unknown agent ids in the model's response are ignored;
        candidates the model never mentions default to *dispatch* (conservative
        — never silently skip).
        """
        if not candidates or self._model_call is None:
            return None
        prompt = build_routing_prompt(alert_context, candidates)
        decision = await self._model_call.call(RoutingDecision, prompt)
        if decision is None:
            return None
        return self._validate(decision, candidates)

    @staticmethod
    def _validate(
        decision: RoutingDecision, candidates: list[AgentCandidate]
    ) -> RoutingResult | None:
        """Project the model's decision onto the candidate set, fail-open.

        A candidate the model never mentioned defaults to dispatch. If the
        validated decision would skip every candidate, return ``None`` so the
        caller falls back to dispatching all.
        """
        candidate_ids = [c.agent_id for c in candidates]
        mentioned = {
            s.agent_id: s for s in decision.selections if s.agent_id in candidate_ids
        }
        selected: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for cid in candidate_ids:
            s = mentioned.get(cid)
            if s is None or s.dispatch:
                selected[cid] = (s.hint.strip() if s else "")
            else:
                skipped[cid] = s.reason.strip() or "router judged it not relevant to this alert"
        if not selected:
            return None
        return RoutingResult(
            selected=selected, skipped=skipped, rationale=decision.rationale.strip()
        )
