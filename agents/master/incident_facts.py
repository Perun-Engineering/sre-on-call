"""Canonical derived view of one investigation's results (issue #66).

``IncidentFacts`` is built once from ``AgentResult``s, the alert, and the
Agent registry — never LLM output — and re-derived on each enrichment arrival
since facts are a pure function of the current result set, not an accumulator.
It is then projected into every outbound surface: the chat Incident Report
(``ReportSections``), the interactive page (``PageModel``), the PIR
(``PIRSections``), and the trace-manifest timeline. The #27 Analysis rides
alongside a projection, not inside the facts.
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


def enrichment_error(result: AgentResult | AgentFailure) -> str | None:
    """Return an error message when ``result`` represents a failure, else ``None``."""
    if isinstance(result, AgentFailure):
        return result.error_message
    if isinstance(result, AgentResult) and result.status in ("error", "unhealthy"):
        return result.error_message or "unknown error"
    return None


def timeline_sort_epoch(raw: str | None) -> float | None:
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


def format_metadata_line(metadata: AgentMetadata | None) -> str | None:
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
    """The deterministically-derived view of one investigation, built once.

    Built from ``AgentResult``s, the alert, and the Agent registry — never LLM
    output. Re-derived on each enrichment arrival since facts are a pure
    function of the current result set, not an accumulator. Projected into
    ``ReportSections``, ``PageModel``, ``PIRSections``, and the
    trace-manifest timeline.
    """

    investigation_id: str
    alert_text: str
    time_of_detection: str
    severity: str
    affected_services: str
    summary: str
    summary_parts: list[str]  # per-agent raw summaries in render order
    root_cause: str
    root_cause_parts: list[tuple[str, str]]  # (display_name, raw_summary) tuples in render order
    evidence: list[EvidenceFact]
    timeline: IncidentTimeline
    chart_ids: list[str]
    impact_assessment: str
    recommended_actions: str
    links: list[tuple[str, str]]
    totals_line: str | None

    @classmethod
    def derive(
        cls,
        registry: AgentRegistry,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
        *,
        pending: set[str],
        disabled: set[str],
        skipped: dict[str, str],
    ) -> "IncidentFacts":
        """Derive the canonical facts for one investigation, exactly once."""
        summary_parts = cls._collect_summary_parts(registry, agent_results)
        root_cause_parts = cls._collect_root_cause_parts(registry, agent_results)
        evidence = cls._build_evidence(
            registry, agent_results, pending, disabled, skipped
        )
        chart_ids: list[str] = []
        for ev in evidence:
            if ev.chart_id and ev.chart_id not in chart_ids:
                chart_ids.append(ev.chart_id)
        return cls(
            investigation_id=alert_context.investigation_id,
            alert_text=alert_context.alert_text,
            time_of_detection=alert_context.alert_timestamp,
            severity=cls._determine_severity(agent_results),
            affected_services=cls._extract_affected_services(agent_results),
            summary=cls._joined_summary_or_fallback(alert_context, summary_parts),
            summary_parts=summary_parts,
            root_cause=cls._joined_root_cause_or_fallback(root_cause_parts),
            root_cause_parts=root_cause_parts,
            evidence=evidence,
            timeline=cls._build_timeline(registry, alert_context, agent_results),
            chart_ids=chart_ids,
            impact_assessment=cls._build_impact_assessment(agent_results),
            recommended_actions=cls._build_recommended_actions(
                registry, agent_results, pending
            ),
            links=cls._build_links(agent_results),
            totals_line=_format_totals_line(agent_results),
        )

    # --- registry helpers ---

    @staticmethod
    def _display(registry: AgentRegistry, agent_id: str) -> tuple[str, str]:
        try:
            a = registry.lookup(agent_id)
            return a.emoji, a.display_name
        except KeyError:
            return "📌", agent_id

    @staticmethod
    def _ordered_specialized_ids(registry: AgentRegistry) -> list[str]:
        return [a.id for a in registry.all(kind="specialized")]

    # --- private static helpers (ported verbatim from report_formatter.py) ---

    @staticmethod
    def _build_evidence(
        registry: AgentRegistry,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str],
        disabled_agents: set[str],
        skipped_agents: dict[str, str],
    ) -> list[EvidenceFact]:
        """Build one EvidenceFact per agent the orchestrator considered.

        Includes agents that returned a result, agents still pending, agents
        deployed-but-inactive, and agents the router deliberately skipped.
        Order follows the registry's specialized agent order; unknown ids fall
        to the end alphabetically.
        """
        configured = (
            set(agent_results.keys())
            | pending_agents
            | disabled_agents
            | set(skipped_agents)
        )
        ordered_known = [
            a for a in IncidentFacts._ordered_specialized_ids(registry) if a in configured
        ]
        ordered = ordered_known + sorted(configured - set(ordered_known))

        facts: list[EvidenceFact] = []
        for agent_key in ordered:
            emoji, display_name = IncidentFacts._display(registry, agent_key)
            result = agent_results.get(agent_key)
            lines, status, metadata_line = IncidentFacts._render_evidence_lines(
                agent_key,
                result,
                pending_agents,
                disabled_agents,
                skipped_agents,
                display_name,
            )
            chart_id: str | None = None
            if status == "ok" and isinstance(result, AgentResult):
                for f in result.findings:
                    if f.chart is not None and f.chart.chart_id in result.chart_series:
                        chart_id = f.chart.chart_id
                        break
            facts.append(
                EvidenceFact(
                    agent_id=agent_key,
                    emoji=emoji,
                    display_name=display_name,
                    status=status,
                    lines=lines,
                    metadata_line=metadata_line,
                    chart_id=chart_id,
                )
            )
        return facts

    @staticmethod
    def _render_evidence_lines(
        agent_key: str,
        result: AgentResult | AgentFailure | None,
        pending_agents: set[str],
        disabled_agents: set[str],
        skipped_agents: dict[str, str],
        display_name: str,
    ) -> tuple[list[EvidenceLine], EvidenceStatus, str | None]:
        if result is None and agent_key in skipped_agents:
            reason = skipped_agents[agent_key] or "router judged it not relevant to this alert"
            return (
                [
                    EvidenceLine(
                        f"➖ {display_name} not investigated — {reason}"
                    )
                ],
                "skipped",
                None,
            )
        if result is None and agent_key in disabled_agents:
            return (
                [
                    EvidenceLine(
                        f"🚫 {display_name} is disabled in this deployment "
                        f"— investigate manually if relevant"
                    )
                ],
                "disabled",
                None,
            )
        if result is None and agent_key in pending_agents:
            return (
                [
                    EvidenceLine(
                        f"⏳ {display_name} still investigating — results will arrive "
                        f"in a follow-up update"
                    )
                ],
                "pending",
                None,
            )
        if isinstance(result, AgentFailure):
            return (
                [EvidenceLine(f"⚠️ {display_name} data unavailable: {result.error_message}")],
                "error",
                format_metadata_line(result.metadata),
            )
        if isinstance(result, AgentResult) and result.status == "unhealthy":
            reason = result.error_message or "agent reported unhealthy"
            return (
                [
                    EvidenceLine(
                        f"🚫 {display_name} reported unhealthy: {reason} "
                        f"— investigate agent configuration"
                    )
                ],
                "disabled",
                format_metadata_line(result.metadata),
            )
        if isinstance(result, AgentResult) and result.status == "error":
            error_detail = result.error_message or "unknown error"
            return (
                [EvidenceLine(f"⚠️ {display_name} data unavailable: {error_detail}")],
                "error",
                format_metadata_line(result.metadata),
            )
        if isinstance(result, AgentResult) and result.status == "success":
            lines = (
                [EvidenceLine(f.content, f.link) for f in result.findings]
                if result.findings
                else [EvidenceLine(f"No notable findings from {display_name}")]
            )
            return lines, "ok", format_metadata_line(result.metadata)
        return [EvidenceLine(f"⚠️ {display_name} data unavailable")], "error", None

    @staticmethod
    def _build_timeline(
        registry: AgentRegistry,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> IncidentTimeline:
        """Assemble the ordered incident timeline (#34), deterministically.

        Events come purely from timestamps already on the evidence — the alert
        time, each successful agent's finding timestamps, and each agent's
        completion (the enrichment arrival). Nothing is LLM-synthesized, so a
        time is never invented. A finding event carries ``chart_id`` only when
        its descriptor's series was harvested, so the page can focus the linked
        graph window on click. Events are sorted by parsed wall-clock; any
        unparseable timestamp keeps stable insertion order rather than
        misordering the narrative.
        """
        events: list[TimelineEvent] = [
            TimelineEvent(
                timestamp=alert_context.alert_timestamp,
                source="alert",
                kind="alert",
                label=_first_line(alert_context.alert_text),
            )
        ]
        for agent_key in IncidentFacts._ordered_specialized_ids(registry):
            result = agent_results.get(agent_key)
            if not isinstance(result, AgentResult) or result.status != "success":
                continue
            _, display_name = IncidentFacts._display(registry, agent_key)
            for f in result.findings:
                chart_id = (
                    f.chart.chart_id
                    if f.chart is not None and f.chart.chart_id in result.chart_series
                    else None
                )
                events.append(
                    TimelineEvent(
                        timestamp=f.timestamp,
                        source=f.source or display_name,
                        kind="finding",
                        label=_first_line(f.content),
                        severity=f.severity,
                        chart_id=chart_id,
                    )
                )
            completed_at = result.metadata.completed_at if result.metadata else None
            if completed_at:
                events.append(
                    TimelineEvent(
                        timestamp=completed_at,
                        source=display_name,
                        kind="action",
                        label=f"{display_name} reported",
                    )
                )

        events.sort(
            key=lambda e: (
                timeline_sort_epoch(e.timestamp) is None,
                timeline_sort_epoch(e.timestamp) or 0.0,
            )
        )
        return IncidentTimeline(events=events)

    @staticmethod
    def _determine_severity(
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> str:
        highest = "low"
        severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        for result in agent_results.values():
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    sev = finding.severity.lower()
                    if severity_rank.get(sev, 0) > severity_rank.get(highest, 0):
                        highest = sev
        return SEVERITY_EMOJI.get(highest, SEVERITY_EMOJI["low"])

    @staticmethod
    def _extract_affected_services(
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> str:
        services: list[str] = []
        for result in agent_results.values():
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    if finding.source and finding.source not in services:
                        services.append(finding.source)
        return ", ".join(services) if services else "Unknown (insufficient data)"

    @staticmethod
    def _collect_summary_parts(
        registry: AgentRegistry,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> list[str]:
        """Per-agent raw summaries in render order. Renderer normalizes each."""
        parts: list[str] = []
        for agent_key in IncidentFacts._ordered_specialized_ids(registry):
            result = agent_results.get(agent_key)
            if (
                isinstance(result, AgentResult)
                and result.status == "success"
                and result.summary
            ):
                parts.append(result.summary)
        return parts

    @staticmethod
    def _joined_summary_or_fallback(
        alert_context: AlertContext,
        summary_parts: list[str],
    ) -> str:
        """Backward-compat fallback string for ``ReportSections.summary``.

        Renderers that consult ``summary_parts`` ignore this; older callers
        that read ``.summary`` still get a usable joined string. The fallback
        prose is also surfaced here when no agent reported a summary.
        """
        if summary_parts:
            return " ".join(summary_parts)
        return (
            f"Alert detected: {alert_context.alert_text}. "
            "Insufficient agent data to provide a detailed summary."
        )

    @staticmethod
    def _collect_root_cause_parts(
        registry: AgentRegistry,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> list[tuple[str, str]]:
        """Per-agent (display_name, raw_summary) tuples in render order."""
        parts: list[tuple[str, str]] = []
        for agent_key in IncidentFacts._ordered_specialized_ids(registry):
            result = agent_results.get(agent_key)
            if (
                isinstance(result, AgentResult)
                and result.status == "success"
                and result.summary
            ):
                _, display_name = IncidentFacts._display(registry, agent_key)
                parts.append((display_name, result.summary))
        return parts

    @staticmethod
    def _joined_root_cause_or_fallback(
        root_cause_parts: list[tuple[str, str]],
    ) -> str:
        """Backward-compat fallback string for ``ReportSections.root_cause``."""
        if root_cause_parts:
            return "Based on available evidence:\n" + "\n".join(
                f"- {display}: {raw}" for display, raw in root_cause_parts
            )
        return "Insufficient data to determine root cause. See agent availability in Evidence section."

    @staticmethod
    def _build_impact_assessment(
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> str:
        critical_findings: list[str] = []
        for result in agent_results.values():
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    if finding.severity.lower() in ("critical", "high"):
                        critical_findings.append(finding.content)
        if critical_findings:
            return (
                "High-impact findings detected:\n"
                + "\n".join(f"- {f}" for f in critical_findings)
            )
        return "Impact assessment requires further investigation based on available data."

    @staticmethod
    def _build_recommended_actions(
        registry: AgentRegistry,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str],
    ) -> str:
        actions: list[str] = []
        action_num = 1

        has_critical = False
        for result in agent_results.values():
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    if finding.severity.lower() == "critical":
                        has_critical = True
                        break

        if has_critical:
            actions.append(f"{action_num}. Immediately investigate critical findings listed in Evidence section")
            action_num += 1

        for agent_key, result in agent_results.items():
            if agent_key in pending_agents:
                continue
            _, display_name = IncidentFacts._display(registry, agent_key)
            if isinstance(result, AgentResult) and result.status == "unhealthy":
                actions.append(
                    f"{action_num}. Investigate {display_name} configuration "
                    f"— agent reported unhealthy"
                )
                action_num += 1
                continue
            if isinstance(result, AgentFailure) or (
                isinstance(result, AgentResult) and result.status == "error"
            ):
                actions.append(f"{action_num}. Manually check {display_name} — automated data collection failed")
                action_num += 1

        if not actions:
            actions.append("1. Review evidence sections and correlate findings")
            actions.append("2. Monitor affected services for further anomalies")

        return "\n".join(actions)

    @staticmethod
    def _build_links(
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        for result in agent_results.values():
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    url = finding.metadata.get("url")
                    if url:
                        links.append((url, finding.source))
        return links


def render_pir_timeline_markdown(timeline: IncidentTimeline) -> str:
    """Project an ``IncidentTimeline`` to the PIR's markdown timeline block (#66).

    One bullet per event in clock order: ``- <timestamp> — [<source>] <label>``.
    Replaces the former second-walk ``_build_pir_timeline``; the PIR timeline is
    now a projection of the same deterministic ``IncidentTimeline`` the page and
    trace manifest use. Falls back to a single placeholder line when the timeline
    carries no agent contribution (only the alert, or nothing).
    """
    lines = [f"- {e.timestamp} — [{e.source}] {e.label}" for e in timeline.events]
    # The empty-timeline ("or nothing") branch is only reachable from an
    # externally-constructed/deserialized IncidentTimeline — _build_timeline
    # always emits the alert event.
    if not any(e.kind != "alert" for e in timeline.events):
        lines.append("- (No additional timeline data available from agents)")
    return "\n".join(lines)
