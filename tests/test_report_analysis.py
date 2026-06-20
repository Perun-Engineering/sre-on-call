"""Tests for the LLM-synthesized Analysis block in the Incident Report."""

from __future__ import annotations

import pytest

from shared.report_renderer import (
    AnalysisSection,
    DiscordReportRenderer,
    EnrichmentSections,
    EvidenceBlock,
    EvidenceLine,
    ReportSections,
    SlackReportRenderer,
)


def _analysis() -> AnalysisSection:
    return AnalysisSection(
        root_cause_hypothesis="Payment pods OOMKilled under load",
        correlation="5xx spike aligns with exit-137 container restarts",
        confidence="high",
        suggested_next_action="Raise the payment deployment memory limit",
    )


def _sections(analysis: AnalysisSection | None) -> ReportSections:
    return ReportSections(
        severity="🔴 Critical",
        affected_services="payment",
        time_of_detection="2026-06-11T10:00:00Z",
        summary="Deterministic per-agent summary.",
        root_cause="Deterministic root cause.",
        evidence_blocks=[
            EvidenceBlock(
                emoji="📊",
                display_name="CloudWatch",
                lines=[EvidenceLine(text="exit 137 OOMKilled")],
            )
        ],
        impact_assessment="Payments degraded.",
        recommended_actions="- Page the on-call",
        links=[],
        analysis=analysis,
    )


@pytest.mark.parametrize("renderer", [SlackReportRenderer(), DiscordReportRenderer()])
class TestAnalysisRendering:
    def test_analysis_block_present_with_all_fields(self, renderer):
        text = renderer.render_report(_sections(_analysis()))

        assert "Analysis" in text
        assert "Payment pods OOMKilled under load" in text
        assert "5xx spike aligns with exit-137 container restarts" in text
        assert "high" in text
        assert "Raise the payment deployment memory limit" in text

    def test_analysis_rendered_above_evidence(self, renderer):
        text = renderer.render_report(_sections(_analysis()))

        assert text.index("Analysis") < text.index("Evidence")
        # Evidence stays verbatim — the synthesis never rewrites it.
        assert "exit 137 OOMKilled" in text

    def test_no_analysis_block_when_absent(self, renderer):
        text = renderer.render_report(_sections(None))

        assert "Analysis" not in text
        assert "Evidence" in text


class TestFormatterPassThrough:
    def test_build_incident_sections_threads_analysis(self):
        from agents.master.report_formatter import ReportFormatter
        from shared.models import AlertContext

        alert = AlertContext(
            investigation_id="inv-1",
            platform="slack",
            channel_id="C1",
            message_id="m1",
            alert_text="alert",
            alert_timestamp="2026-06-11T10:00:00Z",
            investigation_window=("a", "b"),
        )
        fmt = ReportFormatter()
        sections = fmt.build_incident_sections(
            fmt.derive_facts(alert, {}), analysis=_analysis()
        )
        assert sections.analysis is _analysis() or sections.analysis == _analysis()

    def test_build_incident_sections_defaults_to_no_analysis(self):
        from agents.master.report_formatter import ReportFormatter
        from shared.models import AlertContext

        alert = AlertContext(
            investigation_id="inv-1",
            platform="slack",
            channel_id="C1",
            message_id="m1",
            alert_text="alert",
            alert_timestamp="2026-06-11T10:00:00Z",
            investigation_window=("a", "b"),
        )
        fmt = ReportFormatter()
        sections = fmt.build_incident_sections(fmt.derive_facts(alert, {}))
        assert sections.analysis is None

    def test_build_enrichment_sections_threads_analysis(self):
        from agents.master.report_formatter import ReportFormatter
        from shared.models import AgentResult

        result = AgentResult(
            agent_name="eks", status="success", findings=[], summary="back online"
        )
        sections = ReportFormatter().build_enrichment_sections(
            source_agent="eks",
            new_findings=result,
            initial_report_summary="",
            analysis=_analysis(),
        )
        assert sections.analysis == _analysis()


def test_enrichment_carries_updated_analysis():
    sections = EnrichmentSections(
        emoji="📊",
        display_name="EKS",
        findings_lines=["pod evicted"],
        updated_assessment="New data from EKS is now available.",
        analysis=_analysis(),
    )
    text = SlackReportRenderer().render_enrichment(sections)

    assert "Analysis" in text
    assert "Payment pods OOMKilled under load" in text


# ---------------------------------------------------------------------------
# Rec #3 — causal chain / competing hypotheses / ruled out in the chat report
# ---------------------------------------------------------------------------


def _rich_analysis() -> AnalysisSection:
    return AnalysisSection(
        root_cause_hypothesis="Payment pods OOMKilled under load",
        correlation="5xx spike aligns with exit-137 container restarts",
        confidence="high",
        suggested_next_action="Raise the payment deployment memory limit",
        causal_chain=[
            "traffic surge",
            "memory pressure",
            "OOMKilled",
            "5xx spike",
        ],
        competing_hypotheses=[
            "Upstream DB latency (ranked lower: no slow-query evidence)",
        ],
        ruled_out=[
            "Network partition (checked: no SG changes in window)",
        ],
    )


@pytest.mark.parametrize("renderer", [SlackReportRenderer(), DiscordReportRenderer()])
class TestCausalChainRendering:
    def test_causal_chain_rendered(self, renderer):
        text = renderer.render_report(_sections(_rich_analysis()))
        assert "traffic surge" in text
        assert "5xx spike" in text
        # Rendered as an ordered chain (arrow joins the links).
        assert "→" in text

    def test_competing_hypotheses_rendered(self, renderer):
        text = renderer.render_report(_sections(_rich_analysis()))
        assert "Upstream DB latency" in text

    def test_ruled_out_rendered(self, renderer):
        text = renderer.render_report(_sections(_rich_analysis()))
        assert "Network partition" in text

    def test_empty_lists_omit_subsections(self, renderer):
        # The base _analysis() carries empty extension lists — no sub-headers.
        text = renderer.render_report(_sections(_analysis()))
        assert "Causal Chain" not in text
        assert "Competing Hypotheses" not in text
        assert "Ruled Out" not in text
        # The original four fields still render.
        assert "Payment pods OOMKilled under load" in text


class TestOrchestratorAnalysisMapping:
    """orchestrator._to_analysis_section carries the new #3 fields through."""

    def test_maps_causal_chain_fields(self):
        from agents.master.orchestrator import _to_analysis_section
        from agents.master.synthesis import IncidentAnalysis

        section = _to_analysis_section(
            IncidentAnalysis(
                root_cause_hypothesis="rc",
                correlation="co",
                confidence="high",
                suggested_next_action="na",
                causal_chain=["a", "b", "c"],
                competing_hypotheses=["alt"],
                ruled_out=["disconfirmed"],
            )
        )
        assert section is not None
        assert section.causal_chain == ["a", "b", "c"]
        assert section.competing_hypotheses == ["alt"]
        assert section.ruled_out == ["disconfirmed"]

    def test_maps_none(self):
        from agents.master.orchestrator import _to_analysis_section

        assert _to_analysis_section(None) is None
