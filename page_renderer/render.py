"""Compose one self-contained interactive incident-page HTML document.

Pure function: ``render_page(page_model, charts, echarts_js) -> str``. No I/O.
All investigation-derived text is HTML-escaped — alert text, findings, and
queries carry attacker-influenceable content. The investigation data and the
ECharts library are inlined so the page is fully self-contained and works years
later with no network access.
"""
from __future__ import annotations

import html
import json


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _evidence_html(blocks: list[dict]) -> str:
    out: list[str] = []
    for b in blocks:
        lines = []
        for line in b.get("lines", []):
            text = _esc(line.get("text", ""))
            link = line.get("link")
            if link:
                lines.append(
                    f'<li><a href="{_esc(link)}" rel="noopener noreferrer" '
                    f'target="_blank">{text}</a></li>'
                )
            else:
                lines.append(f"<li>{text}</li>")
        chart_id = b.get("chart_id")
        chart_div = (
            f'<div class="chart" data-chart-id="{_esc(chart_id)}"></div>'
            if chart_id
            else ""
        )
        out.append(
            f'<section class="evidence">'
            f'<h3>{_esc(b.get("emoji",""))} {_esc(b.get("display_name",""))}</h3>'
            f"<ul>{''.join(lines)}</ul>{chart_div}</section>"
        )
    return "".join(out)


def _analysis_html(analysis: dict | None) -> str:
    if not analysis:
        return ""
    return (
        '<section class="analysis"><h2>🧠 Analysis</h2>'
        f'<p><strong>Root cause:</strong> {_esc(analysis.get("root_cause_hypothesis",""))}</p>'
        f'<p><strong>Correlation:</strong> {_esc(analysis.get("correlation",""))}</p>'
        f'<p><strong>Confidence:</strong> {_esc(analysis.get("confidence",""))}</p>'
        f'<p><strong>Next action:</strong> {_esc(analysis.get("suggested_next_action",""))}</p>'
        "</section>"
    )


def render_page(page_model: dict, charts: dict[str, dict], echarts_js: str) -> str:
    """Return the full HTML document for one investigation."""
    inv_id = _esc(page_model.get("investigation_id", ""))
    title = f"Incident {inv_id}"
    # Harden the inlined JSON against a </script> break-out from finding text.
    data_json = json.dumps({"model": page_model, "charts": charts}).replace("</", "<\\/")
    init_js = """
    (function () {
      var data = JSON.parse(document.getElementById('investigation-data').textContent);
      var charts = data.charts || {};
      document.querySelectorAll('.chart').forEach(function (el) {
        var id = el.getAttribute('data-chart-id');
        var series = (charts[id] && charts[id].points) || [];
        if (!window.echarts || !el.clientWidth) { return; }
        var chart = window.echarts.init(el);
        var xs = series.map(function (p, i) { return p.timestamp || p['@timestamp'] || i; });
        var ys = series.map(function (p) {
          var keys = Object.keys(p);
          var num = keys.map(function (k) { return p[k]; })
                        .find(function (v) { return typeof v === 'number'; });
          return num != null ? num : 0;
        });
        chart.setOption({
          tooltip: { trigger: 'axis' },
          xAxis: { type: 'category', data: xs },
          yAxis: { type: 'value' },
          series: [{ type: 'line', data: ys, areaStyle: {} }]
        });
      });
    })();
    """
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;"
        "padding:0 1rem;color:#1a1a1a}"
        ".chart{width:100%;height:320px;margin:1rem 0}"
        "section{border-top:1px solid #eee;padding-top:1rem;margin-top:1rem}"
        "code,pre{background:#f5f5f5;padding:.1rem .3rem;border-radius:3px}"
        "</style></head><body>"
        f"<h1>🚨 Incident Report — {inv_id}</h1>"
        f"<p><strong>Severity:</strong> {_esc(page_model.get('severity',''))} · "
        f"<strong>Services:</strong> {_esc(page_model.get('affected_services',''))} · "
        f"<strong>Detected:</strong> {_esc(page_model.get('time_of_detection',''))} · "
        f"<strong>Status:</strong> {_esc(page_model.get('status',''))}</p>"
        f"<section><h2>Alert</h2><p>{_esc(page_model.get('alert_text',''))}</p></section>"
        f"<section><h2>Summary</h2><p>{_esc(page_model.get('summary',''))}</p></section>"
        f"<section><h2>Root Cause Hypothesis</h2><p>{_esc(page_model.get('root_cause',''))}</p></section>"
        f"{_analysis_html(page_model.get('analysis'))}"
        f"<h2>Evidence</h2>{_evidence_html(page_model.get('evidence', []))}"
        f'<script type="application/json" id="investigation-data">{data_json}</script>'
        f"<script>{echarts_js}</script>"
        f"<script>{init_js}</script>"
        "</body></html>"
    )
