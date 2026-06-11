"""Report formatter for assembling agent results into structured incident reports.

Builds platform-agnostic ``ReportSections`` (and the related section dataclasses)
from agent results. Platform-specific markup is owned by
:class:`shared.platforms.ChatPlatform` implementations, which call the
``build_*_sections`` methods on :class:`ReportFormatter` and render the
returned sections in their native dialect.

Display info (emoji, name, render order) is sourced from the
:class:`shared.agents.AgentRegistry` — there is no longer a separate
``AGENT_DISPLAY`` / ``AGENT_ORDER`` table here.
"""

from __future__ import annotations

from datetime import datetime

from shared.agents import AgentRegistry, get_registry
from shared.models import AgentMetadata, AgentResult, AgentFailure, AlertContext
from shared.report_renderer import (
    AnalysisSection,
    EnrichmentSections,
    EvidenceBlock,
    EvidenceLine,
    EvidenceStatus,
    InvestigationStartedSections,
    PIRSections,
    ReportSections,
)


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


class ReportFormatter:
    """Builds platform-agnostic report sections from agent results.

    The formatter knows nothing about platform-specific markup; rendering
    is owned by :class:`shared.platforms.ChatPlatform` implementations.
    Display info (emoji, name, order) is sourced from the
    :class:`AgentRegistry` — pass one for tests; production paths default
    to the cached process-wide registry.
    """

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self._registry: AgentRegistry | None = registry

    @property
    def registry(self) -> AgentRegistry:
        if self._registry is None:
            self._registry = get_registry()
        return self._registry

    # --- Public builders ---

    def build_incident_sections(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str] | None = None,
        disabled_agents: set[str] | None = None,
        analysis: AnalysisSection | None = None,
        skipped_agents: dict[str, str] | None = None,
    ) -> ReportSections:
        """Build the structured Incident Report sections.

        ``pending_agents`` are agents that were dispatched but hadn't
        responded by the 60-second deadline. They render with a ⏳
        marker and trigger a late enrichment update if they respond
        before the hard cutoff.

        ``disabled_agents`` are agents the orchestrator deliberately did
        not dispatch because they are deployed-but-inactive in
        ``config.yaml`` (``enabled: false``). They render as 🚫 disabled
        evidence blocks for transparency. Their ids must be in the
        registry; otherwise ``KeyError`` propagates.

        ``skipped_agents`` (issue #28) maps an agent the master's router
        deliberately did *not* dispatch for *this* alert onto the router's
        reason. They render as a distinct ➖ "not investigated" block —
        never as a failure — so a deliberate skip reads differently from an
        agent that errored or timed out.

        Agents not in any of these sets are treated as not configured
        for this investigation and omitted entirely.
        """
        sections = self._build_report_sections(
            alert_context,
            agent_results,
            pending_agents or set(),
            disabled_agents or set(),
            skipped_agents or {},
        )
        sections.variant_label = alert_context.variant_label
        sections.analysis = analysis
        return sections

    def build_started_sections(
        self,
        alert_context: AlertContext,
        dispatched_agents: list[str],
    ) -> InvestigationStartedSections:
        """Build the "investigation kicked off" announcement sections.

        Lists only the agents the orchestrator is actively dispatching to —
        disabled-in-config agents do *not* appear here (they only appear in
        the Incident Report's Evidence section as 🚫 blocks). The started
        notice is aspirational; it should reflect what we're actually doing.
        """
        return InvestigationStartedSections(
            alert_text=alert_context.alert_text,
            investigation_id=alert_context.investigation_id,
            dispatched=[self._display(aid) for aid in dispatched_agents],
        )

    def build_enrichment_sections(
        self,
        source_agent: str,
        new_findings: AgentResult | AgentFailure,
        initial_report_summary: str,
        variant_label: str | None = None,
        analysis: AnalysisSection | None = None,
    ) -> EnrichmentSections:
        """Build sections for a late-arriving result (success or failure)."""
        emoji, display_name = self._display(source_agent)
        error_message = _enrichment_error(new_findings)

        if error_message is not None:
            findings_lines = [error_message]
            updated_assessment = (
                f"{display_name} reported back after the initial deadline "
                f"but failed: {error_message}"
            )
            status = "error"
        else:
            assert isinstance(new_findings, AgentResult)
            findings_lines = (
                [f.content for f in new_findings.findings]
                if new_findings.findings
                else [new_findings.summary]
            )
            updated_assessment = (
                f"New data from {display_name} is now available. "
                f"{new_findings.summary}"
            )
            status = "ok"

        return EnrichmentSections(
            emoji=emoji,
            display_name=display_name,
            findings_lines=findings_lines,
            updated_assessment=updated_assessment,
            variant_label=variant_label,
            metadata_line=_format_metadata_line(new_findings.metadata),
            status=status,
            analysis=analysis,
        )

    def build_pir_sections(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> PIRSections:
        """Build the structured Post-Incident Report sections."""
        summary_parts = self._collect_summary_parts(agent_results)
        root_cause_parts = self._collect_root_cause_parts(agent_results)
        return PIRSections(
            incident_summary=self._joined_summary_or_fallback(alert_context, summary_parts),
            timeline=self._build_pir_timeline(alert_context, agent_results),
            root_cause=self._joined_root_cause_or_fallback(root_cause_parts),
            impact=self._build_impact_assessment(alert_context, agent_results),
            action_items=self._build_recommended_actions(agent_results, set()),
            lessons_learned="(To be filled in by the team during the post-incident review.)",
        )

    # --- Registry helpers ---

    def _display(self, agent_id: str) -> tuple[str, str]:
        """Look up an agent's (emoji, display_name) from the registry.

        Falls back to (📌, agent_id) for ids the registry doesn't recognise —
        this should only happen if an agent record is removed mid-flight while
        an in-progress investigation still references it.
        """
        try:
            a = self.registry.lookup(agent_id)
            return a.emoji, a.display_name
        except KeyError:
            return "📌", agent_id

    def _ordered_specialized_ids(self) -> list[str]:
        """Specialized agent ids in canonical render order (per registry)."""
        return [a.id for a in self.registry.all(kind="specialized")]

    # --- Private helpers ---

    def _build_pir_timeline(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> str:
        """Build a chronological timeline from alert context and agent findings."""
        entries: list[str] = [
            f"- {alert_context.alert_timestamp} — Alert detected: {alert_context.alert_text}",
        ]
        for agent_key in self._ordered_specialized_ids():
            result = agent_results.get(agent_key)
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    entries.append(f"- {finding.timestamp} — [{finding.source}] {finding.content}")
        if len(entries) == 1:
            entries.append("- (No additional timeline data available from agents)")
        return "\n".join(entries)

    def _build_report_sections(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str],
        disabled_agents: set[str],
        skipped_agents: dict[str, str],
    ) -> ReportSections:
        summary_parts = self._collect_summary_parts(agent_results)
        root_cause_parts = self._collect_root_cause_parts(agent_results)
        return ReportSections(
            severity=self._determine_severity(agent_results),
            affected_services=self._extract_affected_services(alert_context, agent_results),
            time_of_detection=alert_context.alert_timestamp,
            summary=self._joined_summary_or_fallback(alert_context, summary_parts),
            root_cause=self._joined_root_cause_or_fallback(root_cause_parts),
            evidence_blocks=self._build_evidence_blocks(
                agent_results, pending_agents, disabled_agents, skipped_agents,
            ),
            impact_assessment=self._build_impact_assessment(alert_context, agent_results),
            recommended_actions=self._build_recommended_actions(agent_results, pending_agents),
            links=self._build_links(agent_results),
            totals_line=_format_totals_line(agent_results),
            summary_parts=summary_parts,
            root_cause_parts=root_cause_parts,
        )

    def _determine_severity(
        self, agent_results: dict[str, AgentResult | AgentFailure]
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

    def _extract_affected_services(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> str:
        services: list[str] = []
        for result in agent_results.values():
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    if finding.source and finding.source not in services:
                        services.append(finding.source)
        return ", ".join(services) if services else "Unknown (insufficient data)"

    def _collect_summary_parts(
        self, agent_results: dict[str, AgentResult | AgentFailure]
    ) -> list[str]:
        """Per-agent raw summaries in render order. Renderer normalizes each."""
        parts: list[str] = []
        for agent_key in self._ordered_specialized_ids():
            result = agent_results.get(agent_key)
            if (
                isinstance(result, AgentResult)
                and result.status == "success"
                and result.summary
            ):
                parts.append(result.summary)
        return parts

    def _joined_summary_or_fallback(
        self,
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

    def _collect_root_cause_parts(
        self, agent_results: dict[str, AgentResult | AgentFailure]
    ) -> list[tuple[str, str]]:
        """Per-agent (display_name, raw_summary) tuples in render order."""
        parts: list[tuple[str, str]] = []
        for agent_key in self._ordered_specialized_ids():
            result = agent_results.get(agent_key)
            if (
                isinstance(result, AgentResult)
                and result.status == "success"
                and result.summary
            ):
                _, display_name = self._display(agent_key)
                parts.append((display_name, result.summary))
        return parts

    def _joined_root_cause_or_fallback(
        self, root_cause_parts: list[tuple[str, str]]
    ) -> str:
        """Backward-compat fallback string for ``ReportSections.root_cause``."""
        if root_cause_parts:
            return "Based on available evidence:\n" + "\n".join(
                f"- {display}: {raw}" for display, raw in root_cause_parts
            )
        return "Insufficient data to determine root cause. See agent availability in Evidence section."

    def _build_evidence_blocks(
        self,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str],
        disabled_agents: set[str],
        skipped_agents: dict[str, str],
    ) -> list[EvidenceBlock]:
        """Build one evidence block per agent the orchestrator considered.

        Includes:
        - Agents that returned a result (success / error / unhealthy).
        - Agents still pending at the initial deadline (⏳).
        - Agents that are deployed-but-inactive in this deployment (🚫).
        - Agents the router deliberately skipped for this alert (➖).

        Agents not in any of these sets are skipped — emitting "data
        unavailable" for an agent the orchestrator chose not to invoke would
        mislead. Order follows the registry's specialized agent order;
        unknown ids fall to the end alphabetically.
        """
        configured = (
            set(agent_results.keys())
            | pending_agents
            | disabled_agents
            | set(skipped_agents)
        )
        ordered_known = [a for a in self._ordered_specialized_ids() if a in configured]
        ordered = ordered_known + sorted(configured - set(ordered_known))

        blocks: list[EvidenceBlock] = []
        for agent_key in ordered:
            emoji, display_name = self._display(agent_key)
            lines, status, metadata_line = self._render_evidence_lines(
                agent_key,
                agent_results.get(agent_key),
                pending_agents,
                disabled_agents,
                skipped_agents,
                display_name,
            )
            blocks.append(
                EvidenceBlock(
                    emoji=emoji,
                    display_name=display_name,
                    lines=lines,
                    metadata_line=metadata_line,
                    status=status,
                )
            )
        return blocks

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
                _format_metadata_line(result.metadata),
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
                _format_metadata_line(result.metadata),
            )
        if isinstance(result, AgentResult) and result.status == "error":
            error_detail = result.error_message or "unknown error"
            return (
                [EvidenceLine(f"⚠️ {display_name} data unavailable: {error_detail}")],
                "error",
                _format_metadata_line(result.metadata),
            )
        if isinstance(result, AgentResult) and result.status == "success":
            lines = (
                [EvidenceLine(f.content, f.link) for f in result.findings]
                if result.findings
                else [EvidenceLine(f"No notable findings from {display_name}")]
            )
            return lines, "ok", _format_metadata_line(result.metadata)
        return [EvidenceLine(f"⚠️ {display_name} data unavailable")], "error", None

    def _build_impact_assessment(
        self,
        alert_context: AlertContext,
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

    def _build_recommended_actions(
        self,
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
            _, display_name = self._display(agent_key)
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

    def _build_links(
        self, agent_results: dict[str, AgentResult | AgentFailure]
    ) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        for result in agent_results.values():
            if isinstance(result, AgentResult) and result.status == "success":
                for finding in result.findings:
                    url = finding.metadata.get("url")
                    if url:
                        links.append((url, finding.source))
        return links
