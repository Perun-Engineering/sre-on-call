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

import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from shared.model_call import StructuredModelCall
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


class AnalysisSynthesizer:
    """Runs the post-harvest synthesis call, fail-open.

    Composes one :class:`StructuredModelCall` (injectable for tests).
    ``synthesize`` never raises — on any error, timeout, or model build failure
    it returns ``None`` and the caller posts the report without an Analysis
    section.
    """

    def __init__(self, *, model_call: StructuredModelCall | None = None) -> None:
        self._model_call = model_call

    @property
    def timeout_seconds(self) -> float:
        """Time budget for the synthesis call; the orchestrator reserves it
        out of the initial-report deadline."""
        return (
            self._model_call.timeout_seconds
            if self._model_call is not None
            else DEFAULT_SYNTHESIS_TIMEOUT_SECONDS
        )

    @classmethod
    def from_env(cls) -> AnalysisSynthesizer | None:
        """Build a synthesizer from the environment, or ``None`` when disabled.

        Gated on ``SYNTHESIS_ENABLED`` so the feature is explicitly opt-in
        (and inert in tests). The synthesis model resolves to
        ``SYNTHESIS_MODEL_ID`` → ``MODEL_ID`` → config/default; the timeout is
        overridable via ``SYNTHESIS_TIMEOUT_SECONDS``.
        """
        model_call = StructuredModelCall.from_env(
            system_prompt=_SYSTEM_PROMPT,
            gate_env="SYNTHESIS_ENABLED",
            model_env="SYNTHESIS_MODEL_ID",
            timeout_env="SYNTHESIS_TIMEOUT_SECONDS",
            default_timeout=DEFAULT_SYNTHESIS_TIMEOUT_SECONDS,
        )
        if model_call is None:
            return None
        return cls(model_call=model_call)

    async def synthesize(
        self,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
    ) -> IncidentAnalysis | None:
        """Synthesize an Analysis from the alert and all agent results.

        Returns ``None`` on any failure so the report posts unchanged.
        """
        if self._model_call is None:
            return None
        prompt = build_synthesis_prompt(alert_context, results)
        return await self._model_call.call(IncidentAnalysis, prompt)
