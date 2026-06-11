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
        sections = ReportFormatter().build_incident_sections(
            alert, {}, analysis=_analysis()
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
        sections = ReportFormatter().build_incident_sections(alert, {})
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
