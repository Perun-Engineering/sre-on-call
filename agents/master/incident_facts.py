"""Canonical derived view of one investigation's results (issue #66).

``IncidentFacts`` is the single deterministic projection source for every
outbound surface — the chat Incident Report, the interactive page, the PIR,
and the trace-manifest timeline. It is derived **exactly once per report
build** (and re-derived on each enrichment arrival, since facts are a function
of the current result set, not an accumulator). Facts only: everything here is
computed from ``AgentResult``s, the alert, and the Agent registry — never LLM
output. The #27 Analysis rides alongside a projection, not inside the facts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from agents.master.synthesis import IncidentTimeline, TimelineEvent
from shared.agents import AgentRegistry
from shared.models import AgentFailure, AgentMetadata, AgentResult, AlertContext
from shared.report_renderer import EvidenceLine, EvidenceStatus


# Severity emoji mapping
SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🔵 Low",
}


def _enrichment_error(result: AgentResult | AgentFailure) -> str | None:
    """Return an error message when ``result`` represents a failure, else ``None``."""
    if isinstance(result, AgentFailure):
        return result.error_message
    if isinstance(result, AgentResult) and result.status in ("error", "unhealthy"):
        return result.error_message or "unknown error"
    return None


def _timeline_sort_epoch(raw: str | None) -> float | None:
    """Parse a timeline timestamp to a UTC epoch for ordering, tolerant of forms.

    Handles ISO 8601 (``…Z`` / ``+00:00`` / naive) and the alert's human
    ``"YYYY-MM-DD HH:MM:SS UTC"`` form. Returns ``None`` for anything
    unparseable so the caller can keep such events in stable insertion order
    rather than misordering them with a naive string compare.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.endswith(" UTC"):
        text = text[:-4].strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _first_line(text: str, limit: int = 160) -> str:
    """Return a single compact line for an event label (first line, trimmed)."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _format_analysis_time(started_at: str | None, completed_at: str | None) -> str | None:
    """Render the elapsed duration between two ISO 8601 timestamps as mm:ss."""
    if not started_at or not completed_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at)
    except ValueError:
        return None
    total_seconds = max(0, int((end - start).total_seconds()))
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def _format_totals_line(
    agent_results: dict[str, "AgentResult | AgentFailure"],
) -> str | None:
    """Aggregate token usage and cost across every agent that reported metadata.

    Returns ``None`` when no agent supplied either token counts or cost — keeps
    the report header uncluttered when telemetry isn't available (e.g. an
    older deployment that didn't surface usage in the footer).
    """
    total_input = 0
    total_output = 0
    total_cost = 0.0
    saw_tokens = False
    saw_cost = False
    for result in agent_results.values():
        meta = result.metadata
        if meta is None:
            continue
        if meta.input_tokens is not None:
            total_input += meta.input_tokens
            saw_tokens = True
        if meta.output_tokens is not None:
            total_output += meta.output_tokens
            saw_tokens = True
        if meta.cost_usd is not None:
            total_cost += meta.cost_usd
            saw_cost = True
    if not saw_tokens and not saw_cost:
        return None
    parts: list[str] = []
    if saw_tokens:
        parts.append(f"tokens={total_input:,}in/{total_output:,}out")
    if saw_cost:
        parts.append(f"cost=${total_cost:.4f}")
    return " · ".join(parts)


def _format_metadata_line(metadata: AgentMetadata | None) -> str | None:
    """Render an agent's per-invocation telemetry as a one-liner.

    Returns ``None`` when there's nothing meaningful to show — keeps the
    rendered report uncluttered for agents that didn't supply any metadata.
    """
    if metadata is None:
        return None
    parts: list[str] = []
    if metadata.model_id:
        parts.append(f"model={metadata.model_id}")
    analysis_time = _format_analysis_time(metadata.started_at, metadata.completed_at)
    if analysis_time:
        parts.append(f"analysis time={analysis_time}")
    if metadata.input_tokens is not None or metadata.output_tokens is not None:
        in_t = metadata.input_tokens if metadata.input_tokens is not None else "?"
        out_t = metadata.output_tokens if metadata.output_tokens is not None else "?"
        parts.append(f"tokens={in_t}in/{out_t}out")
    if metadata.cost_usd is not None:
        parts.append(f"cost=${metadata.cost_usd:.4f}")
    return " · ".join(parts) if parts else None


@dataclass
class EvidenceFact:
    """One agent's evidence, in one of the five ``EvidenceStatus`` states.

    ``lines`` are the rendered content (finding text + deep link for ``ok``;
    a single status sentence for non-ok states), identical to what the chat
    Evidence section shows. ``chart_id`` is set only on an ``ok`` block whose
    findings carry a harvested chart series.
    """

    agent_id: str
    emoji: str
    display_name: str
    status: EvidenceStatus
    lines: list[EvidenceLine] = field(default_factory=list)
    metadata_line: str | None = None
    chart_id: str | None = None


@dataclass
class IncidentFacts:
    """The deterministically-derived view of one investigation, built once."""

    investigation_id: str
    alert_text: str
    time_of_detection: str
    severity: str
    affected_services: str
    summary: str
    summary_parts: list[str]
    root_cause: str
    root_cause_parts: list[tuple[str, str]]
    evidence: list[EvidenceFact]
    timeline: IncidentTimeline
    chart_ids: list[str]
    impact_assessment: str
    recommended_actions: str
    links: list[tuple[str, str]]
    totals_line: str | None
