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

from agents.master.incident_facts import (
    EvidenceFact,
    IncidentFacts,
    enrichment_error,
    format_metadata_line,
    render_pir_timeline_markdown,
)
from agents.master.synthesis import IncidentTimeline
from shared.agents import AgentRegistry, get_registry
from shared.models import AgentResult, AgentFailure, AlertContext
from shared.page_model import (
    SCHEMA_VERSION as PAGE_SCHEMA_VERSION,
    PageEvidenceBlock,
    PageEvidenceLine,
    PageModel,
)
from shared.report_renderer import (
    AnalysisSection,
    EnrichmentSections,
    EvidenceBlock,
    InvestigationStartedSections,
    PIRSections,
    ReportSections,
)
from shared.time_utils import now_iso


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
        interactive_page_url: str | None = None,
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
        facts = self._facts(
            alert_context, agent_results,
            pending_agents or set(), disabled_agents or set(), skipped_agents or {},
        )
        sections = ReportSections(
            severity=facts.severity,
            affected_services=facts.affected_services,
            time_of_detection=facts.time_of_detection,
            summary=facts.summary,
            root_cause=facts.root_cause,
            evidence_blocks=[self._evidence_block(e) for e in facts.evidence],
            impact_assessment=facts.impact_assessment,
            recommended_actions=facts.recommended_actions,
            links=facts.links,
            totals_line=facts.totals_line,
            summary_parts=facts.summary_parts,
            root_cause_parts=facts.root_cause_parts,
        )
        sections.variant_label = alert_context.variant_label
        sections.analysis = analysis
        sections.interactive_page_url = interactive_page_url
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
        error_message = enrichment_error(new_findings)

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
            metadata_line=format_metadata_line(new_findings.metadata),
            status=status,
            analysis=analysis,
        )

    def build_pir_sections(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
    ) -> PIRSections:
        """Build the structured Post-Incident Report sections."""
        facts = self._facts(alert_context, agent_results, set(), set(), {})
        return PIRSections(
            incident_summary=facts.summary,
            timeline=render_pir_timeline_markdown(facts.timeline),
            root_cause=facts.root_cause,
            impact=facts.impact_assessment,
            action_items=facts.recommended_actions,
            lessons_learned="(To be filled in by the team during the post-incident review.)",
        )

    def build_page_model(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
        analysis: AnalysisSection | None = None,
        pending_agents: set[str] | None = None,
        disabled_agents: set[str] | None = None,
        skipped_agents: dict[str, str] | None = None,
    ) -> PageModel:
        """Build the #33 interactive-page model as a projection of IncidentFacts.

        Emits all five evidence states (full mirror of the chat report) — the
        page no longer drops error/pending/disabled/skipped agents (#66).
        """
        facts = self._facts(
            alert_context, agent_results,
            pending_agents or set(), disabled_agents or set(), skipped_agents or {},
        )
        evidence = [
            PageEvidenceBlock(
                emoji=e.emoji, display_name=e.display_name, status=e.status,
                lines=[PageEvidenceLine(text=ln.text, link=ln.link) for ln in e.lines],
                chart_id=e.chart_id,
            )
            for e in facts.evidence
        ]
        return PageModel(
            schema_version=PAGE_SCHEMA_VERSION,
            investigation_id=facts.investigation_id,
            generated_at=now_iso(),
            alert_text=facts.alert_text,
            severity=facts.severity,
            affected_services=facts.affected_services,
            time_of_detection=facts.time_of_detection,
            status="completed",
            summary=facts.summary,
            root_cause=facts.root_cause,
            analysis=(
                {
                    "root_cause_hypothesis": analysis.root_cause_hypothesis,
                    "correlation": analysis.correlation,
                    "confidence": analysis.confidence,
                    "suggested_next_action": analysis.suggested_next_action,
                }
                if analysis is not None else None
            ),
            evidence=evidence,
            chart_ids=facts.chart_ids,
            timeline=facts.timeline.to_json_dict()["events"],
        )

    def resolve_page_model(
        self,
        page: dict,
        *,
        resolved_at: str,
        narrative: str | None = None,
    ) -> dict:
        """Finalize an archived page model into its ``resolved`` form (#55).

        Returns a new dict — the input is never mutated. The status flips to
        ``"resolved"`` and a single ``resolution``-kind event is appended to the
        timeline, carrying ``resolved_at`` and the operator's ``/postmortem``
        narrative (the leading slash-command word is stripped; a bare or empty
        command falls back to "Incident resolved"). Everything else — the
        synthesized Analysis block, evidence, and chart references — is
        preserved verbatim, which is why the PIR path finalizes the existing
        page rather than rebuilding it. ``generated_at`` is refreshed to the
        resolution time so the page footer reflects when it was closed out.
        """
        label = (narrative or "").strip()
        if label.startswith("/"):
            parts = label.split(None, 1)
            label = parts[1].strip() if len(parts) > 1 else ""
        resolution_event = {
            "timestamp": resolved_at,
            "source": "postmortem",
            "kind": "resolution",
            "label": label or "Incident resolved",
            "severity": None,
            "chart_id": None,
        }
        timeline = list(page.get("timeline") or [])
        timeline.append(resolution_event)
        return {
            **page,
            "status": "resolved",
            "generated_at": resolved_at,
            "timeline": timeline,
        }

    def build_timeline(
        self,
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
        # Timeline draws only from successful results; pending/disabled/skipped
        # agents contribute no timeline events, so empty sets are correct here.
        return IncidentFacts.derive(
            self.registry, alert_context, agent_results,
            pending=set(), disabled=set(), skipped={},
        ).timeline

    # --- Facts helpers ---

    def _facts(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
        pending_agents: set[str],
        disabled_agents: set[str],
        skipped_agents: dict[str, str],
    ) -> IncidentFacts:
        return IncidentFacts.derive(
            self.registry, alert_context, agent_results,
            pending=pending_agents, disabled=disabled_agents, skipped=skipped_agents,
        )

    @staticmethod
    def _evidence_block(fact: EvidenceFact) -> EvidenceBlock:
        return EvidenceBlock(
            emoji=fact.emoji, display_name=fact.display_name,
            lines=fact.lines, metadata_line=fact.metadata_line, status=fact.status,
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

