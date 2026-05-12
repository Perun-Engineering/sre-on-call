"""Report formatter for assembling agent results into structured incident reports.

Builds platform-agnostic ReportSections from agent results, then delegates
to a ReportRenderer for platform-specific markup.  When no renderer is
provided, defaults to SlackReportRenderer for backward compatibility.
"""

from __future__ import annotations

from datetime import datetime

from shared.models import AgentMetadata, AgentResult, AgentFailure, AlertContext, Finding
from shared.report_renderer import (
    EnrichmentSections,
    EvidenceBlock,
    EvidenceStatus,
    InvestigationStartedSections,
    PIRSections,
    ReportRenderer,
    ReportSections,
    SlackReportRenderer,
)


# Display configuration for each agent: (emoji, display name)
AGENT_DISPLAY: dict[str, tuple[str, str]] = {
    "slack_scanner": ("📡", "Slack Scanner"),
    "discord_scanner": ("🎮", "Discord Scanner"),
    "cloudwatch_logs": ("📋", "CloudWatch Logs"),
    "eks": ("☸️", "EKS Cluster State"),
    "prometheus": ("📈", "Prometheus"),
}

# Preferred render order; unlisted agents render last alphabetically.
AGENT_ORDER: list[str] = [
    "slack_scanner",
    "discord_scanner",
    "cloudwatch_logs",
    "eks",
    "prometheus",
]

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
    if result.status == "error":
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
    """Assembles agent results into structured reports via a pluggable renderer."""

    def __init__(self, renderer: ReportRenderer | None = None) -> None:
        self._renderer: ReportRenderer = renderer or SlackReportRenderer()

    def format_incident_report(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str] | None = None,
    ) -> str:
        """Produce a formatted Incident Report string.

        ``pending_agents`` are agents that were dispatched but hadn't
        responded by the 60-second deadline. They render with a ``⏳``
        marker and trigger a late enrichment update if they respond
        before the hard cutoff. Agents not in ``agent_results`` and not
        in ``pending_agents`` are treated as not configured for this
        investigation and omitted entirely.
        """
        sections = self._build_report_sections(
            alert_context, agent_results, pending_agents or set(),
        )
        sections.variant_label = alert_context.variant_label
        return self._renderer.render_report(sections)

    def format_investigation_started(
        self,
        alert_context: AlertContext,
        dispatched_agents: list[str],
    ) -> str:
        """Format the "investigation kicked off" announcement message."""
        sections = InvestigationStartedSections(
            alert_text=alert_context.alert_text,
            investigation_id=alert_context.investigation_id,
            dispatched=[
                AGENT_DISPLAY.get(aid, ("📌", aid)) for aid in dispatched_agents
            ],
        )
        return self._renderer.render_investigation_started(sections)

    def format_enrichment_update(
        self,
        source_agent: str,
        new_findings: AgentResult | AgentFailure,
        initial_report_summary: str,
        variant_label: str | None = None,
    ) -> str:
        """Format a late-arriving result as an enrichment update.

        Handles both successful late results and late failures (an agent
        that crossed the 60s deadline and eventually errored).
        """
        emoji, display_name = AGENT_DISPLAY.get(source_agent, ("📌", source_agent))
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

        sections = EnrichmentSections(
            emoji=emoji,
            display_name=display_name,
            findings_lines=findings_lines,
            updated_assessment=updated_assessment,
            variant_label=variant_label,
            metadata_line=_format_metadata_line(new_findings.metadata),
            status=status,
        )
        return self._renderer.render_enrichment(sections)

    def format_pir(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> str:
        """Produce a formatted Post-Incident Report string."""
        sections = PIRSections(
            incident_summary=self._build_summary(alert_context, agent_results),
            timeline=self._build_pir_timeline(alert_context, agent_results),
            root_cause=self._build_root_cause(agent_results),
            impact=self._build_impact_assessment(alert_context, agent_results),
            action_items=self._build_recommended_actions(agent_results, set()),
            lessons_learned="(To be filled in by the team during the post-incident review.)",
        )
        return self._renderer.render_pir(sections)

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
        for agent_key in AGENT_ORDER:
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
    ) -> ReportSections:
        return ReportSections(
            severity=self._determine_severity(agent_results),
            affected_services=self._extract_affected_services(alert_context, agent_results),
            time_of_detection=alert_context.alert_timestamp,
            summary=self._build_summary(alert_context, agent_results),
            root_cause=self._build_root_cause(agent_results),
            evidence_blocks=self._build_evidence_blocks(agent_results, pending_agents),
            impact_assessment=self._build_impact_assessment(alert_context, agent_results),
            recommended_actions=self._build_recommended_actions(agent_results, pending_agents),
            links=self._build_links(agent_results),
            totals_line=_format_totals_line(agent_results),
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

    def _build_summary(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> str:
        summaries: list[str] = []
        for agent_key in AGENT_ORDER:
            result = agent_results.get(agent_key)
            if (
                isinstance(result, AgentResult)
                and result.status == "success"
                and result.summary
            ):
                summaries.append(self._renderer.normalize(result.summary))
        if summaries:
            return " ".join(summaries)
        return f"Alert detected: {alert_context.alert_text}. Insufficient agent data to provide a detailed summary."

    def _build_root_cause(
        self, agent_results: dict[str, AgentResult | AgentFailure]
    ) -> str:
        evidence_sources: list[str] = []
        for agent_key in AGENT_ORDER:
            result = agent_results.get(agent_key)
            if (
                isinstance(result, AgentResult)
                and result.status == "success"
                and result.summary
            ):
                _, display_name = AGENT_DISPLAY.get(agent_key, ("", agent_key))
                normalized = self._renderer.normalize(result.summary)
                evidence_sources.append(f"{display_name}: {normalized}")
        if evidence_sources:
            return "Based on available evidence:\n" + "\n".join(
                f"- {src}" for src in evidence_sources
            )
        return "Insufficient data to determine root cause. See agent availability in Evidence section."

    def _build_evidence_blocks(
        self,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str],
    ) -> list[EvidenceBlock]:
        """Build one evidence block per agent the orchestrator dispatched.

        Agents not in ``agent_results`` and not in ``pending_agents`` are
        skipped — emitting "data unavailable" for an agent the orchestrator
        chose not to invoke (e.g. ``ENABLED_AGENTS`` exclusion) would mislead.
        """
        configured = set(agent_results.keys()) | pending_agents
        ordered = [a for a in AGENT_ORDER if a in configured]
        ordered += sorted(configured - set(AGENT_ORDER))

        blocks: list[EvidenceBlock] = []
        for agent_key in ordered:
            emoji, display_name = AGENT_DISPLAY.get(agent_key, ("📌", agent_key))
            lines, status, metadata_line = self._render_evidence_lines(
                agent_key, agent_results.get(agent_key), pending_agents, display_name,
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
        display_name: str,
    ) -> tuple[list[str], EvidenceStatus, str | None]:
        if result is None and agent_key in pending_agents:
            return (
                [
                    f"⏳ {display_name} still investigating — results will arrive "
                    f"in a follow-up update"
                ],
                "pending",
                None,
            )
        if isinstance(result, AgentFailure):
            return (
                [f"⚠️ {display_name} data unavailable: {result.error_message}"],
                "error",
                _format_metadata_line(result.metadata),
            )
        if isinstance(result, AgentResult) and result.status == "error":
            error_detail = result.error_message or "unknown error"
            return (
                [f"⚠️ {display_name} data unavailable: {error_detail}"],
                "error",
                _format_metadata_line(result.metadata),
            )
        if isinstance(result, AgentResult) and result.status == "success":
            lines = (
                [f.content for f in result.findings]
                if result.findings
                else [f"No notable findings from {display_name}"]
            )
            return lines, "ok", _format_metadata_line(result.metadata)
        return [f"⚠️ {display_name} data unavailable"], "error", None

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
            _, display_name = AGENT_DISPLAY.get(agent_key, ("", agent_key))
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
