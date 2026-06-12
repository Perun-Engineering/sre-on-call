"""Unit tests for shared.page_model — the master→renderer page contract."""
from __future__ import annotations

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
