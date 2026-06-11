"""Property tests: render_page never raises on adversarial / degenerate input."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from page_renderer.render import render_page

ECHARTS = "window.echarts={};"

_text = st.text(min_size=0, max_size=200)
_line = st.fixed_dictionaries({"text": _text, "link": st.none() | _text})
_block = st.fixed_dictionaries({
    "emoji": _text, "display_name": _text, "status": _text,
    "lines": st.lists(_line, max_size=5),
    "chart_id": st.none() | st.text(min_size=1, max_size=16),
})
_model = st.fixed_dictionaries({
    "investigation_id": _text, "alert_text": _text, "severity": _text,
    "affected_services": _text, "time_of_detection": _text, "status": _text,
    "summary": _text, "root_cause": _text, "analysis": st.none(),
    "evidence": st.lists(_block, max_size=5),
    "chart_ids": st.lists(st.text(min_size=1, max_size=16), max_size=5),
})


@settings(max_examples=200, deadline=None)
@given(model=_model)
def test_render_never_raises_and_escapes(model: dict) -> None:
    """render_page must not raise on any valid-shaped model and must escape script tags."""
    html = render_page(model, {}, ECHARTS)
    assert "<!DOCTYPE html>" in html
    assert "<script>alert" not in html


@settings(max_examples=200, deadline=None)
@given(
    model=_model,
    points=st.lists(st.dictionaries(st.text(max_size=8), st.integers()), max_size=20),
)
def test_render_handles_arbitrary_series(model: dict, points: list[dict]) -> None:
    """render_page must embed chart data without raising on arbitrary point shapes."""
    chart_id = "c1"
    model["chart_ids"] = [chart_id]
    model["evidence"] = [{
        "emoji": "x", "display_name": "d", "status": "ok",
        "lines": [], "chart_id": chart_id,
    }]
    html = render_page(model, {chart_id: {"points": points, "truncated": True}}, ECHARTS)
    assert 'data-chart-id="c1"' in html
