"""IncidentFacts — the canonical derived view of one investigation (#66)."""
from __future__ import annotations

from agents.master.incident_facts import EvidenceFact, IncidentFacts
from agents.master.synthesis import IncidentTimeline
from shared.report_renderer import EvidenceLine


def test_evidence_fact_carries_status_and_lines():
    fact = EvidenceFact(
        agent_id="eks",
        emoji="☸️",
        display_name="EKS",
        status="ok",
        lines=[EvidenceLine("pod crashloop", "https://x")],
        metadata_line="model=haiku",
        chart_id=None,
    )
    assert fact.status == "ok"
    assert fact.lines[0].text == "pod crashloop"


def test_incident_facts_is_a_dataclass_with_timeline():
    facts = IncidentFacts(
        investigation_id="inv-1",
        alert_text="Disk full",
        time_of_detection="2025-01-15T14:32:00Z",
        severity="🔴 Critical",
        affected_services="api",
        summary="s",
        summary_parts=["s"],
        root_cause="rc",
        root_cause_parts=[("EKS", "s")],
        evidence=[],
        timeline=IncidentTimeline(events=[]),
        chart_ids=[],
        impact_assessment="i",
        recommended_actions="a",
        links=[],
        totals_line=None,
    )
    assert facts.investigation_id == "inv-1"
    assert isinstance(facts.timeline, IncidentTimeline)
