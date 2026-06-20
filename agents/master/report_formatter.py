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
    clean_finding_content,
    enrichment_error,
    format_metadata_line,
    render_pir_timeline_markdown,
)
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
        facts: IncidentFacts,
        *,
        variant_label: str | None = None,
        analysis: AnalysisSection | None = None,
        interactive_page_url: str | None = None,
    ) -> ReportSections:
        """Project pre-derived :class:`IncidentFacts` into Incident Report sections.

        The five-state evidence (ok / error / pending / disabled / skipped) is
        already resolved on ``facts`` — the caller derives once via
        :meth:`derive_facts` with the appropriate pending / disabled / skipped
        inputs. ``variant_label`` (A/B), the #27 ``analysis`` block, and the #33
        ``interactive_page_url`` ride alongside the facts and are set on the
        returned sections.
        """
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
            root_cause_symptoms=facts.root_cause_fallback.symptoms,
            root_cause_ruled_out=facts.root_cause_fallback.ruled_out,
            root_cause_next_checks=facts.root_cause_fallback.next_checks,
        )
        sections.variant_label = variant_label
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
            cleaned = [
                content
                for f in new_findings.findings
                if (content := clean_finding_content(f.content)) is not None
            ]
            findings_lines = cleaned or [new_findings.summary]
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
        facts: IncidentFacts,
        *,
        analysis: dict | None = None,
    ) -> PIRSections:
        """Project pre-derived :class:`IncidentFacts` into PIR sections.

        The PIR is built from the archived results at ``/postmortem`` time, so
        the caller derives ``facts`` with empty pending / disabled / skipped
        sets — only the settled findings inform the post-incident report.

        ``analysis`` is the #27 root-cause dict archived on the trace manifest
        (Rec #5). When present its synthesized hypothesis (plus the #3 causal
        chain / ruled-out, if any) becomes the PIR Root Cause; when ``None``
        the PIR degrades to the deterministic honest fallback (Rec #1) carried
        on ``facts.root_cause``. Fail-open: an unexpectedly-shaped dict falls
        back to the deterministic text rather than raising.
        """
        return PIRSections(
            incident_summary=facts.summary,
            timeline=render_pir_timeline_markdown(facts.timeline),
            root_cause=self._pir_root_cause(facts, analysis),
            impact=facts.impact_assessment,
            action_items=facts.recommended_actions,
            lessons_learned="(To be filled in by the team during the post-incident review.)",
        )

    @staticmethod
    def _pir_root_cause(facts: IncidentFacts, analysis: dict | None) -> str:
        """Render the PIR Root Cause from the #27 analysis, or the fallback (#5).

        Mirrors the chat report's 🧠 Analysis content so the PIR carries the
        same conclusion the initial report posted. Degrades to the honest
        deterministic ``facts.root_cause`` when there is no analysis (or it
        lacks a hypothesis).
        """
        if not isinstance(analysis, dict):
            return facts.root_cause
        hypothesis = str(analysis.get("root_cause_hypothesis") or "").strip()
        if not hypothesis:
            return facts.root_cause
        lines = [hypothesis]
        correlation = str(analysis.get("correlation") or "").strip()
        if correlation:
            lines.append(f"Correlation: {correlation}")
        chain = [str(x) for x in (analysis.get("causal_chain") or []) if str(x).strip()]
        if chain:
            lines.append("Causal chain: " + " → ".join(chain))
        ruled_out = [str(x) for x in (analysis.get("ruled_out") or []) if str(x).strip()]
        if ruled_out:
            lines.append("Ruled out:")
            lines.extend(f"- {r}" for r in ruled_out)
        return "\n".join(lines)

    def build_page_model(
        self,
        facts: IncidentFacts,
        *,
        analysis: AnalysisSection | None = None,
    ) -> PageModel:
        """Project pre-derived :class:`IncidentFacts` into the #33 page model.

        Emits all five evidence states (full mirror of the chat report) — the
        page no longer drops error/pending/disabled/skipped agents (#66). The
        caller derives ``facts`` with the same pending / disabled / skipped
        inputs it used for the chat report, so the page mirrors it exactly.
        """
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
                    "causal_chain": list(analysis.causal_chain),
                    "competing_hypotheses": list(analysis.competing_hypotheses),
                    "ruled_out": list(analysis.ruled_out),
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

    # --- Facts derivation ---

    def derive_facts(
        self,
        alert_context: AlertContext,
        agent_results: dict[str, AgentResult | AgentFailure],
        *,
        pending: set[str] | None = None,
        disabled: set[str] | None = None,
        skipped: dict[str, str] | None = None,
    ) -> IncidentFacts:
        """Derive the canonical :class:`IncidentFacts` for one investigation.

        The single derivation seam: every outbound surface (chat report, PIR,
        interactive page, trace manifest timeline) is a projection of the facts
        this returns. Callers derive once over their current result set and
        thread the result into the ``build_*`` projections. ``pending`` /
        ``disabled`` / ``skipped`` default to empty (the PIR's case); the live
        report passes the real sets. The included :attr:`IncidentFacts.timeline`
        draws only from settled results, so it is invariant to those inputs.
        """
        return IncidentFacts.derive(
            self.registry, alert_context, agent_results,
            pending=pending or set(), disabled=disabled or set(), skipped=skipped or {},
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

