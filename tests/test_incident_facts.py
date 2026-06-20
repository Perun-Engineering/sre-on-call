"""IncidentFacts — the canonical derived view of one investigation (#66)."""
from __future__ import annotations

from agents.master.incident_facts import (
    EvidenceFact,
    IncidentFacts,
    RootCauseFallback,
    clean_finding_content,
    render_pir_timeline_markdown,
)
from agents.master.synthesis import IncidentTimeline
from shared.agents import get_registry
from shared.models import AgentFailure, AgentResult, AlertContext, Finding
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
        root_cause_fallback=RootCauseFallback(),
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


def test_clean_finding_content_drops_empty_and_whitespace():
    assert clean_finding_content("") is None
    assert clean_finding_content(None) is None
    assert clean_finding_content("   \n\t ") is None
    assert clean_finding_content("  real text  ") == "real text"


def test_clean_finding_content_truncates_overlong():
    blob = "x" * 5000
    out = clean_finding_content(blob)
    assert out is not None
    assert out.startswith("x" * 1000)
    assert "chars truncated" in out
    assert len(out) < len(blob)


def test_derive_drops_empty_findings_and_truncates_overlong():
    reg = get_registry()
    ok_id = next(a.id for a in reg.all(kind="specialized"))
    huge = "LOGLINE " * 500  # well over the per-finding cap
    results: dict[str, AgentResult | AgentFailure] = {
        ok_id: AgentResult(
            agent_name=ok_id, status="success",
            findings=[
                Finding(source="s", timestamp="2025-01-15T14:31:00Z", content="", severity="info"),
                Finding(source="s", timestamp="2025-01-15T14:31:01Z", content="   ", severity="info"),
                Finding(source="s", timestamp="2025-01-15T14:31:02Z", content="kept", severity="info"),
                Finding(source="s", timestamp="2025-01-15T14:31:03Z", content=huge, severity="warning"),
            ],
            summary="s",
        ),
    }
    facts = IncidentFacts.derive(reg, _ctx(), results, pending=set(), disabled=set(), skipped={})
    lines = {e.agent_id: e for e in facts.evidence}[ok_id].lines
    texts = [ln.text for ln in lines]
    assert texts[0] == "kept"                       # both empty findings dropped
    assert len(texts) == 2                          # only "kept" + the truncated blob
    assert "chars truncated" in texts[1]            # huge finding elided, not flooded


def test_derive_all_empty_findings_falls_back_to_no_notable():
    reg = get_registry()
    ok_id = next(a.id for a in reg.all(kind="specialized"))
    results: dict[str, AgentResult | AgentFailure] = {
        ok_id: AgentResult(
            agent_name=ok_id, status="success",
            findings=[Finding(source="s", timestamp="2025-01-15T14:31:00Z", content="", severity="info")],
            summary="s",
        ),
    }
    facts = IncidentFacts.derive(reg, _ctx(), results, pending=set(), disabled=set(), skipped={})
    line = {e.agent_id: e for e in facts.evidence}[ok_id].lines[0].text
    assert "No notable findings" in line


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


# ---------------------------------------------------------------------------
# Rec #1 — honest "ruled out + next checks" root-cause fallback (synthesis OFF)
# ---------------------------------------------------------------------------


def _ok_with_finding(agent_id: str) -> AgentResult:
    return AgentResult(
        agent_name=agent_id, status="success",
        findings=[Finding(source="src", timestamp="2025-01-15T14:31:00Z",
                          content="symptom seen", severity="high")],
        summary="found a symptom",
    )


def _ok_no_findings(agent_id: str) -> AgentResult:
    return AgentResult(
        agent_name=agent_id, status="success", findings=[], summary="all clear",
    )


def test_root_cause_fallback_drops_fake_hypothesis_wording():
    reg = get_registry()
    specialized = [a.id for a in reg.all(kind="specialized")]
    sym_id, clean_id, err_id = specialized[:3]
    results: dict[str, AgentResult | AgentFailure] = {
        sym_id: _ok_with_finding(sym_id),
        clean_id: _ok_no_findings(clean_id),
        err_id: AgentResult(agent_name=err_id, status="error", findings=[],
                            summary="", error_message="timeout"),
    }
    facts = IncidentFacts.derive(reg, _ctx(), results,
                                 pending=set(), disabled=set(), skipped={})
    rc = facts.root_cause
    # No fake hypothesis dressing.
    assert "Based on available evidence" not in rc
    assert "Root Cause Hypothesis" not in rc
    # Honest header.
    assert "No single root cause established" in rc


def test_root_cause_fallback_lists_symptoms_ruled_out_and_next_checks():
    reg = get_registry()
    specialized = [a.id for a in reg.all(kind="specialized")]
    sym_id, clean_id, err_id = specialized[:3]
    skip_id = specialized[3]
    _, sym_name = IncidentFacts._display(reg, sym_id)
    _, clean_name = IncidentFacts._display(reg, clean_id)
    _, err_name = IncidentFacts._display(reg, err_id)
    _, skip_name = IncidentFacts._display(reg, skip_id)

    results: dict[str, AgentResult | AgentFailure] = {
        sym_id: _ok_with_finding(sym_id),
        clean_id: _ok_no_findings(clean_id),
        err_id: AgentResult(agent_name=err_id, status="error", findings=[],
                            summary="", error_message="timeout"),
    }
    facts = IncidentFacts.derive(reg, _ctx(), results,
                                 pending=set(), disabled=set(),
                                 skipped={skip_id: "not relevant"})
    rc = facts.root_cause
    assert "Symptoms observed" in rc
    assert "Ruled out" in rc
    assert "Next checks" in rc
    # Symptom agent appears under symptoms.
    assert sym_name in rc
    # Clean agent phrased as scope-limited, never "healthy".
    assert "no notable findings in its queried scope" in rc
    assert clean_name in rc
    assert "healthy" not in rc.lower()
    # Failed + skipped agents are next checks.
    assert err_name in rc
    assert skip_name in rc


def test_root_cause_fallback_no_results_is_honest():
    reg = get_registry()
    facts = IncidentFacts.derive(reg, _ctx(), {},
                                 pending=set(), disabled=set(), skipped={})
    rc = facts.root_cause
    assert "No single root cause established" in rc
    assert "healthy" not in rc.lower()
