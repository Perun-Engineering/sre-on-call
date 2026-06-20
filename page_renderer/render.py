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
    parts = [
        '<section class="analysis"><h2>🧠 Analysis</h2>',
        f'<p><strong>Root cause:</strong> {_esc(analysis.get("root_cause_hypothesis",""))}</p>',
        f'<p><strong>Correlation:</strong> {_esc(analysis.get("correlation",""))}</p>',
        f'<p><strong>Confidence:</strong> {_esc(analysis.get("confidence",""))}</p>',
        f'<p><strong>Next action:</strong> {_esc(analysis.get("suggested_next_action",""))}</p>',
    ]
    # Optional causal-reasoning sub-sections (#3). Omitted when empty.
    chain = analysis.get("causal_chain") or []
    if chain:
        links = " → ".join(_esc(link) for link in chain)
        parts.append(f"<p><strong>Causal chain:</strong> {links}</p>")
    competing = analysis.get("competing_hypotheses") or []
    if competing:
        items = "".join(f"<li>{_esc(h)}</li>" for h in competing)
        parts.append(f"<p><strong>Competing hypotheses:</strong></p><ul>{items}</ul>")
    ruled_out = analysis.get("ruled_out") or []
    if ruled_out:
        items = "".join(f"<li>{_esc(r)}</li>" for r in ruled_out)
        parts.append(f"<p><strong>Ruled out:</strong></p><ul>{items}</ul>")
    parts.append("</section>")
    return "".join(parts)


_TIMELINE_KIND_EMOJI = {
    "alert": "🚨", "finding": "🔎", "action": "✅", "resolution": "🏁",
}


def _status_html(status: str) -> str:
    """Render the page status, highlighting ``resolved`` in green (#55)."""
    if status == "resolved":
        return f'<span class="status-resolved">{_esc(status)}</span>'
    return _esc(status)


def _timeline_html(timeline: list[dict] | None) -> str:
    """Render the incident timeline (#34) as an accessible ordered list.

    The interactive scrubbable strip is drawn over this by ECharts at runtime;
    the list is the no-JS fallback and the source of truth for the events. An
    event tied to a chart carries ``data-chart-id`` so its list item, like its
    plotted point, focuses the linked graph window on click.
    """
    if not timeline:
        return ""
    items: list[str] = []
    for e in timeline:
        kind = e.get("kind", "")
        emoji = _TIMELINE_KIND_EMOJI.get(kind, "•")
        severity = e.get("severity")
        sev_html = f' <em>[{_esc(severity)}]</em>' if severity else ""
        chart_id = e.get("chart_id")
        chart_attr = f' data-chart-id="{_esc(chart_id)}"' if chart_id else ""
        items.append(
            f'<li class="tl-event tl-{_esc(kind)}"{chart_attr}>'
            f'<span class="tl-time">{_esc(e.get("timestamp", ""))}</span> '
            f'{emoji} <strong>{_esc(e.get("source", ""))}</strong>: '
            f'{_esc(e.get("label", ""))}{sev_html}</li>'
        )
    return (
        '<section class="timeline"><h2>🕑 Timeline</h2>'
        '<div class="timeline-chart" id="incident-timeline"></div>'
        f'<ol class="timeline-list">{"".join(items)}</ol></section>'
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
      var model = data.model || {};
      var charts = data.charts || {};
      function parseTs(s) {
        var t = Date.parse(String(s == null ? '' : s).replace(' UTC', 'Z').replace(' ', 'T'));
        return isNaN(t) ? null : t;
      }
      // Registry of evidence-chart instances, keyed by chart_id, so a timeline
      // event can focus the graph window it points at.
      var registry = {};
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
        registry[id] = { chart: chart, el: el, xs: xs, xsParsed: xs.map(parseTs) };
      });

      // Focus the linked graph: scroll it into view and drop a marker at the
      // chart category nearest the event's timestamp.
      function focusChart(chartId, ts) {
        var entry = registry[chartId];
        if (!entry) { return; }
        if (entry.el.scrollIntoView) {
          entry.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        var target = parseTs(ts), idx = 0, best = Infinity;
        entry.xsParsed.forEach(function (xt, i) {
          if (xt != null && target != null) {
            var d = Math.abs(xt - target);
            if (d < best) { best = d; idx = i; }
          }
        });
        entry.chart.setOption({ series: [{ markLine: {
          symbol: 'none', label: { formatter: '' },
          lineStyle: { color: '#d73a4a' },
          data: [{ xAxis: entry.xs[idx] }]
        } }] });
      }

      // Clicking a timeline list item also focuses its chart (no-JS-graph
      // fallback path; works even if ECharts didn't lay out the strip).
      document.querySelectorAll('.timeline-list li[data-chart-id]').forEach(function (li) {
        li.style.cursor = 'pointer';
        li.addEventListener('click', function () {
          focusChart(li.getAttribute('data-chart-id'),
                     (li.querySelector('.tl-time') || {}).textContent);
        });
      });

      // The scrubbable timeline strip: events on lanes by kind over a time
      // axis, with a slider (the scrubber). Clicking a point focuses its graph.
      var tlEl = document.getElementById('incident-timeline');
      var events = model.timeline || [];
      if (window.echarts && tlEl && tlEl.clientWidth && events.length) {
        var LANES = ['finding', 'action', 'resolution', 'alert'];
        var COLOR = { alert: '#d73a4a', finding: '#fb8c00', action: '#1976d2',
                      resolution: '#2da44e' };
        var allTimed = events.every(function (e) { return parseTs(e.timestamp) != null; });
        var points = events.map(function (e, i) {
          var lane = LANES.indexOf(e.kind); if (lane < 0) { lane = 0; }
          var t = parseTs(e.timestamp);
          return {
            value: [allTimed ? t : i, lane], evt: e,
            itemStyle: { color: COLOR[e.kind] || '#888' }
          };
        });
        var tlChart = window.echarts.init(tlEl);
        tlChart.setOption({
          tooltip: { formatter: function (p) {
            var e = p.data.evt;
            return '<strong>' + e.kind + '</strong><br/>' + e.source + '<br/>' + e.label;
          } },
          grid: { left: 80, right: 20, top: 12, bottom: 64 },
          xAxis: { type: allTimed ? 'time' : 'value', name: 'time' },
          yAxis: { type: 'category', data: LANES },
          dataZoom: [{ type: 'slider', xAxisIndex: 0 }, { type: 'inside', xAxisIndex: 0 }],
          series: [{ type: 'scatter', symbolSize: 16, data: points }]
        });
        tlChart.on('click', function (p) {
          var e = p.data && p.data.evt;
          if (e && e.chart_id) { focusChart(e.chart_id, e.timestamp); }
        });
      }
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
        ".timeline-chart{width:100%;height:220px;margin:.5rem 0}"
        ".timeline-list{list-style:none;padding-left:0}"
        ".timeline-list .tl-event{padding:.25rem 0;border-left:3px solid #eee;"
        "padding-left:.6rem;margin:.15rem 0}"
        ".timeline-list .tl-alert{border-left-color:#d73a4a}"
        ".timeline-list .tl-finding{border-left-color:#fb8c00}"
        ".timeline-list .tl-action{border-left-color:#1976d2}"
        ".timeline-list .tl-resolution{border-left-color:#2da44e}"
        ".status-resolved{color:#2da44e;font-weight:600}"
        ".timeline-list .tl-time{color:#666;font-variant-numeric:tabular-nums;"
        "font-size:.85em;margin-right:.4rem}"
        "section{border-top:1px solid #eee;padding-top:1rem;margin-top:1rem}"
        "code,pre{background:#f5f5f5;padding:.1rem .3rem;border-radius:3px}"
        "</style></head><body>"
        f"<h1>🚨 Incident Report — {inv_id}</h1>"
        f"<p><strong>Severity:</strong> {_esc(page_model.get('severity',''))} · "
        f"<strong>Services:</strong> {_esc(page_model.get('affected_services',''))} · "
        f"<strong>Detected:</strong> {_esc(page_model.get('time_of_detection',''))} · "
        f"<strong>Status:</strong> {_status_html(page_model.get('status',''))}</p>"
        f"<section><h2>Alert</h2><p>{_esc(page_model.get('alert_text',''))}</p></section>"
        f"<section><h2>Summary</h2><p>{_esc(page_model.get('summary',''))}</p></section>"
        f"<section><h2>Root Cause Hypothesis</h2><p>{_esc(page_model.get('root_cause',''))}</p></section>"
        f"{_analysis_html(page_model.get('analysis'))}"
        f"{_timeline_html(page_model.get('timeline'))}"
        f"<h2>Evidence</h2>{_evidence_html(page_model.get('evidence', []))}"
        f'<script type="application/json" id="investigation-data">{data_json}</script>'
        f"<script>{echarts_js}</script>"
        f"<script>{init_js}</script>"
        "</body></html>"
    )
