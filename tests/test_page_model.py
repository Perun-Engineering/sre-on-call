"""Unit tests for shared.page_model — the master→renderer page contract."""
from __future__ import annotations

from shared.models import AgentFailure
from shared.page_model import PageEvidenceBlock, PageEvidenceLine, PageModel


def _model() -> PageModel:
    return PageModel(
        schema_version=1,
        investigation_id="inv-1",
        generated_at="2026-06-12T00:00:00+00:00",
        alert_text="DB down",
        severity="🔴 Critical",
        affected_services="rds",
        time_of_detection="2026-06-12T00:00:00Z",
        status="completed",
        summary="Primary unreachable.",
        root_cause="Failover stuck.",
        analysis={"root_cause_hypothesis": "x", "correlation": "y",
                  "confidence": "high", "suggested_next_action": "z"},
        evidence=[
            PageEvidenceBlock(
                emoji="📜", display_name="CloudWatch Logs", status="ok",
                lines=[PageEvidenceLine("error spike", link="https://x")],
                chart_id="abc123",
            )
        ],
        chart_ids=["abc123"],
    )


def test_to_json_dict_round_trips_nested_dataclasses():
    d = _model().to_json_dict()
    assert d["investigation_id"] == "inv-1"
    assert d["chart_ids"] == ["abc123"]
    block = d["evidence"][0]
    assert block["chart_id"] == "abc123"
    assert block["lines"][0] == {"text": "error spike", "link": "https://x"}
    assert d["analysis"]["confidence"] == "high"


def test_to_json_dict_handles_none_analysis_and_no_chart():
    m = _model()
    m.analysis = None
    m.evidence[0].chart_id = None
    m.evidence[0].lines = [PageEvidenceLine("no link", link=None)]
    d = m.to_json_dict()
    assert d["analysis"] is None
    assert d["evidence"][0]["chart_id"] is None
    assert d["evidence"][0]["lines"][0]["link"] is None


def test_build_page_model_mirrors_all_five_evidence_states():
    from agents.master.report_formatter import ReportFormatter
    from shared.agents import get_registry
    from shared.models import AlertContext, AgentResult, Finding

    reg = get_registry()
    specialized = [a.id for a in reg.all(kind="specialized")]
    assert len(specialized) >= 5, "test needs 5 distinct specialized agents"
    ok_id, err_id, pend_id, dis_id, skip_id = specialized[:5]
    ctx = AlertContext(
        investigation_id="inv-page-1", platform="slack", channel_id="C1",
        message_id="1700000000.0001", alert_text="Disk full",
        alert_timestamp="2025-01-15T14:32:00Z",
        investigation_window=("2025-01-15T14:27:00Z", "2025-01-15T14:37:00Z"),
    )
    results: dict[str, AgentResult | AgentFailure] = {
        ok_id: AgentResult(agent_name=ok_id, status="success",
                           findings=[Finding(source="s", timestamp="2025-01-15T14:31:00Z",
                                             content="found", severity="critical")],
                           summary="ok"),
        err_id: AgentResult(agent_name=err_id, status="error", findings=[],
                            summary="", error_message="boom"),
    }
    model = ReportFormatter().build_page_model(
        ctx, results, analysis=None,
        pending_agents={pend_id}, disabled_agents={dis_id},
        skipped_agents={skip_id: "not relevant"},
    )
    statuses = {b.status for b in model.evidence}
    assert {"ok", "error", "pending", "disabled", "skipped"} <= statuses
