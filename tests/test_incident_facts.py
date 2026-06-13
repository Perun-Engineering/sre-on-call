"""IncidentFacts — the canonical derived view of one investigation (#66)."""
from __future__ import annotations

from agents.master.incident_facts import EvidenceFact, IncidentFacts, render_pir_timeline_markdown
from agents.master.synthesis import IncidentTimeline
from shared.agents import get_registry
from shared.models import AgentFailure, AgentMetadata, AgentResult, AlertContext, Finding
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


def _ctx(
    investigation_id: str = "inv-1",
    platform: str = "slack",
    channel_id: str = "C1",
    message_id: str = "1700000000.0001",
    alert_text: str = "Disk full on api-server",
    alert_timestamp: str = "2025-01-15T14:32:00Z",
    investigation_window: tuple[str, str] = (
        "2025-01-15T14:27:00Z",
        "2025-01-15T14:37:00Z",
    ),
) -> AlertContext:
    return AlertContext(
        investigation_id=investigation_id,
        platform=platform,
        channel_id=channel_id,
        message_id=message_id,
        alert_text=alert_text,
        alert_timestamp=alert_timestamp,
        investigation_window=investigation_window,
    )


def test_derive_emits_all_five_evidence_states_in_registry_order():
    reg = get_registry()
    specialized = [a.id for a in reg.all(kind="specialized")]
    assert len(specialized) >= 5, "test needs 5 distinct specialized agents"
    ok_id, err_id, pend_id, dis_id = specialized[:4]
    skip_id = specialized[4]

    results: dict[str, AgentResult | AgentFailure] = {
        ok_id: AgentResult(
            agent_name=ok_id, status="success",
            findings=[Finding(source="src", timestamp="2025-01-15T14:31:00Z",
                              content="found it", severity="critical")],
            summary="ok summary",
        ),
        err_id: AgentResult(
            agent_name=err_id, status="error", findings=[], summary="",
            error_message="boom",
        ),
    }
    facts = IncidentFacts.derive(
        reg, _ctx(), results,
        pending={pend_id}, disabled={dis_id}, skipped={skip_id: "not relevant"},
    )
    by_id = {e.agent_id: e for e in facts.evidence}
    assert by_id[ok_id].status == "ok"
    assert by_id[err_id].status == "error"
    assert by_id[pend_id].status == "pending"
    assert by_id[dis_id].status == "disabled"
    assert by_id[skip_id].status == "skipped"
    assert by_id[ok_id].lines[0].text == "found it"
    assert "not relevant" in by_id[skip_id].lines[0].text


def test_derive_timeline_is_clock_sorted_with_alert_and_findings():
    reg = get_registry()
    ok_id = next(a.id for a in reg.all(kind="specialized"))
    results: dict[str, AgentResult | AgentFailure] = {
        ok_id: AgentResult(
            agent_name=ok_id, status="success",
            findings=[Finding(source="src", timestamp="2025-01-15T14:31:00Z",
                              content="precursor", severity="warning")],
            summary="s",
        ),
    }
    facts = IncidentFacts.derive(reg, _ctx(), results, pending=set(), disabled=set(), skipped={})
    kinds = [e.kind for e in facts.timeline.events]
    assert "alert" in kinds and "finding" in kinds
    ts = [e.timestamp for e in facts.timeline.events]
    assert ts.index("2025-01-15T14:31:00Z") < ts.index("2025-01-15T14:32:00Z")


def test_pir_timeline_markdown_lists_alert_and_findings_in_clock_order():
    reg = get_registry()
    ok_id = next(a.id for a in reg.all(kind="specialized"))
    results: dict[str, AgentResult | AgentFailure] = {
        ok_id: AgentResult(agent_name=ok_id, status="success",
                           findings=[Finding(source="#alerts",
                                             timestamp="2025-01-15T14:31:00Z",
                                             content="Disk warning", severity="warning")],
                           summary="s"),
    }
    facts = IncidentFacts.derive(reg, _ctx(alert_text="Disk full"),
                                 results, pending=set(), disabled=set(), skipped={})
    md = render_pir_timeline_markdown(facts.timeline)
    assert "Disk warning" in md
    assert "2025-01-15T14:31:00Z" in md
    assert md.lstrip().startswith("-")
    assert "No additional timeline data" not in md
    # finding (14:31) precedes the alert (14:32) — clock order, not insertion order
    assert md.index("2025-01-15T14:31:00Z") < md.index("2025-01-15T14:32:00Z")


def test_pir_timeline_markdown_empty_has_fallback_line():
    md = render_pir_timeline_markdown(IncidentTimeline(events=[]))
    assert "No additional timeline data" in md
