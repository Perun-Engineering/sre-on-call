"""Unit tests for page_renderer.render — pure HTML composition."""
from __future__ import annotations

from page_renderer.render import render_page

ECHARTS = "/* echarts stub */ window.echarts = {};"


def _model() -> dict:
    return {
        "schema_version": 1, "investigation_id": "inv-1",
        "generated_at": "2026-06-12T00:00:00+00:00",
        "alert_text": "DB <script>alert(1)</script> down",
        "severity": "🔴 Critical", "affected_services": "rds",
        "time_of_detection": "t", "status": "completed",
        "summary": "s", "root_cause": "rc",
        "analysis": {"root_cause_hypothesis": "h", "correlation": "c",
                     "confidence": "high", "suggested_next_action": "n"},
        "evidence": [{
            "emoji": "📜", "display_name": "CloudWatch Logs", "status": "ok",
            "lines": [{"text": "error spike", "link": "https://console"}],
            "chart_id": "abc123",
        }],
        "chart_ids": ["abc123"],
    }


def test_render_inlines_echarts_and_investigation_json():
    html = render_page(_model(), {"abc123": {"points": [{"t": 1, "v": 2}]}}, ECHARTS)
    assert "<!DOCTYPE html>" in html
    assert "window.echarts" in html
    assert "abc123" in html


def test_render_escapes_injected_text():
    html = render_page(_model(), {}, ECHARTS)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_one_chart_container_per_chart_id():
    html = render_page(_model(), {"abc123": {"points": []}}, ECHARTS)
    assert html.count('data-chart-id="abc123"') == 1


def test_render_handles_missing_chart_file():
    html = render_page(_model(), {}, ECHARTS)
    assert "<!DOCTYPE html>" in html


def test_render_without_analysis():
    m = _model()
    m["analysis"] = None
    html = render_page(m, {}, ECHARTS)
    assert "🧠" not in html
