"""Post-harvest LLM synthesis of an Analysis section for the Incident Report.

The master fans out to specialized agents and concatenates their findings
deterministically. This module adds the single highest-value output an
investigation bot can produce: a root-cause hypothesis that correlates
across sources. It runs *after* harvest and *before* the deterministic
report is built; the LLM reasons over the evidence but never rewrites it.

Everything here is fail-open: any error, timeout, or unparseable response
yields ``None`` and the report posts exactly as it would without synthesis.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from shared.models import AgentFailure, AgentResult, AlertContext

logger = logging.getLogger(__name__)

# Seconds the synthesis call may take before it is abandoned. Sized to fit
# inside the 60-second initial deadline with margin for the report post.
DEFAULT_SYNTHESIS_TIMEOUT_SECONDS: float = 10.0

_SYSTEM_PROMPT = (
    "You are the lead incident responder synthesizing a root-cause analysis "
    "from evidence gathered by specialized investigation agents.\n"
    "Reason over the alert and every agent's findings, summaries, and "
    "failures. Correlate signals across sources to explain what most likely "
    "caused the incident.\n"
    "Rules:\n"
    "- Ground every claim in the supplied evidence. Never invent log lines, "
    "metrics, or events that were not provided.\n"
    "- If the evidence is thin or contradictory, say so and lower your "
    "confidence accordingly.\n"
    "- Be specific and operational: name the component, the failure mode, and "
    "the next concrete action a responder should take."
)


class IncidentAnalysis(BaseModel):
    """Structured root-cause synthesis rendered as the report's Analysis section."""

    root_cause_hypothesis: str = Field(
        description="The most likely root cause, grounded in the evidence."
    )
    correlation: str = Field(
        description="How signals across the different agents line up to support "
        "the hypothesis."
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="Confidence in the hypothesis given the available evidence."
    )
    suggested_next_action: str = Field(
        description="The single most useful next step for the responder."
    )


@dataclass
class TimelineEvent:
    """One event on the incident timeline.

    Deterministically derived from a real timestamp on the alert, a finding,
    or an agent's completion — never LLM-synthesized, so a time is never
    fabricated. ``chart_id`` is set only when the event ties to a chart whose
    series was actually harvested, which lets the interactive page focus the
    corresponding graph window when the event is clicked (#34).
    """

    timestamp: str  # ISO 8601 (or the alert's human "… UTC" form)
    source: str  # "alert", or the finding source / agent display name
    kind: str  # "alert" | "finding" | "action" | "resolution" (PIR, #55)
    label: str  # short human-readable description
    severity: str | None = None  # finding severity, when applicable
    chart_id: str | None = None  # set when the event links to a chart region

    def to_json_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "kind": self.kind,
            "label": self.label,
            "severity": self.severity,
            "chart_id": self.chart_id,
        }


@dataclass
class IncidentTimeline:
    """The ordered incident narrative: alert → findings → agent enrichments.

    Purely deterministic — assembled by :meth:`ReportFormatter.build_timeline`
    from timestamps already present on the evidence. Stored in the trace
    manifest and carried into the #33 page model for rendering. ``resolution``
    events are appended later by the PIR flow (#55).
    """

    events: list[TimelineEvent] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {"events": [e.to_json_dict() for e in self.events]}


def _summarize_result(agent_id: str, result: AgentResult | AgentFailure) -> str:
    """Render one agent's contribution as a compact evidence block for the prompt."""
    if isinstance(result, AgentFailure):
        return f"### {agent_id} (FAILED)\n{result.error_message}"

    lines = [f"### {agent_id} ({result.status})"]
    if result.summary:
        lines.append(f"Summary: {result.summary}")
    if result.findings:
        lines.append("Findings:")
        for f in result.findings:
            lines.append(f"- [{f.severity}] {f.source}: {f.content}")
    return "\n".join(lines)


def build_synthesis_prompt(
    alert_context: AlertContext,
    results: dict[str, AgentResult | AgentFailure],
) -> str:
    """Build the synthesis user prompt from the alert and all agent results.

    Pure and deterministic so it can be unit-tested without a model. Evidence
    is included verbatim — the synthesis call reasons over it, never edits it.
    """
    parts = [
        "## Alert",
        alert_context.alert_text,
        "",
        "## Agent Evidence",
    ]
    if results:
        parts.extend(_summarize_result(agent_id, r) for agent_id, r in results.items())
    else:
        parts.append("(no agent responded before the deadline)")
    return "\n".join(parts)


class _StructuredAgent(Protocol):
    """Minimal seam over a Strands ``Agent`` for structured output."""

    async def structured_output_async(
        self, output_model: type[IncidentAnalysis], prompt: str
    ) -> IncidentAnalysis: ...


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class AnalysisSynthesizer:
    """Runs the post-harvest synthesis call, fail-open.

    The agent is injectable for tests; in production it is lazily built from
    a tools-less Strands ``Agent`` bound to the synthesis model. ``synthesize``
    never raises — on any error, timeout, or model build failure it returns
    ``None`` and the caller posts the report without an Analysis section.
    """

    def __init__(
        self,
        *,
        agent: _StructuredAgent | None = None,
        model_id: str | None = None,
        timeout_seconds: float = DEFAULT_SYNTHESIS_TIMEOUT_SECONDS,
    ) -> None:
        self._agent = agent
        self._model_id = model_id
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> float:
        """Time budget for the synthesis call; the orchestrator reserves it
        out of the initial-report deadline."""
        return self._timeout_seconds

    @classmethod
    def from_env(cls) -> AnalysisSynthesizer | None:
        """Build a synthesizer from the environment, or ``None`` when disabled.

        Gated on ``SYNTHESIS_ENABLED`` so the feature is explicitly opt-in
        (and inert in tests). The synthesis model resolves to
        ``SYNTHESIS_MODEL_ID`` → ``MODEL_ID`` → config/default; the timeout is
        overridable via ``SYNTHESIS_TIMEOUT_SECONDS``.
        """
        if not _truthy(os.environ.get("SYNTHESIS_ENABLED")):
            return None
        timeout_raw = os.environ.get("SYNTHESIS_TIMEOUT_SECONDS")
        try:
            timeout = float(timeout_raw) if timeout_raw else DEFAULT_SYNTHESIS_TIMEOUT_SECONDS
        except ValueError:
            timeout = DEFAULT_SYNTHESIS_TIMEOUT_SECONDS
        return cls(
            model_id=os.environ.get("SYNTHESIS_MODEL_ID") or None,
            timeout_seconds=timeout,
        )

    def _get_agent(self) -> _StructuredAgent | None:
        """Return the structured-output agent, building it lazily on first use."""
        if self._agent is not None:
            return self._agent
        try:
            from strands import Agent

            from shared.a2a_factory import _resolve_model

            model = _resolve_model(model_id_override=self._model_id)
            self._agent = Agent(model=model, system_prompt=_SYSTEM_PROMPT)
        except Exception:
            logger.exception("Failed to build the synthesis agent; skipping Analysis.")
            return None
        return self._agent

    async def synthesize(
        self,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
    ) -> IncidentAnalysis | None:
        """Synthesize an Analysis from the alert and all agent results.

        Returns ``None`` on any failure so the report posts unchanged.
        """
        agent = self._get_agent()
        if agent is None:
            return None
        prompt = build_synthesis_prompt(alert_context, results)
        try:
            return await asyncio.wait_for(
                agent.structured_output_async(IncidentAnalysis, prompt),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.warning(
                "Synthesis failed for investigation %s; posting report without "
                "Analysis.",
                alert_context.investigation_id,
                exc_info=True,
            )
            return None
