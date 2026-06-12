"""Unit tests for the deterministic incident timeline (issue #34).

The timeline is assembled mechanically from real timestamps already on the
``AlertContext``, each ``Finding``, and each agent's completion metadata — it
is never LLM-synthesized, so a timestamp can never be fabricated. These tests
pin the ordering, the event kinds, and the chart linkage that drives the
interactive page's scrub-to-graph behaviour.
"""
from __future__ import annotations

from agents.master.report_formatter import ReportFormatter
from agents.master.synthesis import IncidentTimeline, TimelineEvent
from shared.models import (
    AgentFailure,
    AgentMetadata,
    AgentResult,
    AlertContext,
    ChartDescriptor,
    ChartSeries,
    Finding,
)


def _alert(ts: str = "2025-01-15 14:32:00 UTC") -> AlertContext:
    return AlertContext(
        investigation_id="inv-001",
        platform="slack",
        channel_id="C1",
        message_id="1705312320.0001",
        alert_text="High CPU on service-api\nsecond line ignored",
        alert_timestamp=ts,
        investigation_window=("2025-01-15 14:27:00 UTC", "2025-01-15 14:37:00 UTC"),
    )


def _finding(content="error spike", ts="2025-01-15T14:33:00Z", source="cw-logs",
             severity="critical", chart=None) -> Finding:
    return Finding(
        source=source, timestamp=ts, content=content, severity=severity, chart=chart,
    )


def _success(agent_name, findings, completed_at="2025-01-15T14:34:00Z",
             chart_series=None) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        status="success",
        findings=findings,
        summary="summary",
        duration_seconds=2.0,
        metadata=AgentMetadata(completed_at=completed_at),
        chart_series=chart_series or {},
    )


# --- model ----------------------------------------------------------------

def test_timeline_event_round_trips():
    e = TimelineEvent(
        timestamp="2025-01-15T14:33:00Z", source="cw-logs", kind="finding",
        label="error spike", severity="critical", chart_id="abc123",
    )
    assert e.to_json_dict() == {
        "timestamp": "2025-01-15T14:33:00Z", "source": "cw-logs",
        "kind": "finding", "label": "error spike", "severity": "critical",
        "chart_id": "abc123",
    }


def test_incident_timeline_round_trips():
    tl = IncidentTimeline(events=[TimelineEvent("t", "alert", "alert", "boom")])
    d = tl.to_json_dict()
    assert d == {"events": [{"timestamp": "t", "source": "alert", "kind": "alert",
                             "label": "boom", "severity": None, "chart_id": None}]}


# --- builder ---------------------------------------------------------------

def test_build_timeline_starts_with_alert_event():
    tl = ReportFormatter().build_timeline(_alert(), {})
    assert len(tl.events) == 1
    ev = tl.events[0]
    assert ev.kind == "alert"
    assert ev.source == "alert"
    # only the first line of the alert text is used as the label
    assert ev.label == "High CPU on service-api"


def test_build_timeline_emits_finding_and_action_events_in_time_order():
    results: dict[str, AgentResult | AgentFailure] = {
        "cloudwatch_logs": _success(
            "cloudwatch_logs",
            [_finding(content="spike", ts="2025-01-15T14:33:00Z")],
            completed_at="2025-01-15T14:34:30Z",
        ),
    }
    tl = ReportFormatter().build_timeline(_alert(), results)
    kinds = [e.kind for e in tl.events]
    assert kinds == ["alert", "finding", "action"]
    # strictly increasing timestamps after the sort
    times = [e.timestamp for e in tl.events]
    assert times == sorted_by_clock(times)


def test_build_timeline_sorts_across_mixed_timestamp_formats():
    # alert uses the human "YYYY-MM-DD HH:MM:SS UTC" form; findings use ISO-T.
    # A naive lexicographic sort would misorder them — the builder must not.
    results: dict[str, AgentResult | AgentFailure] = {
        "cloudwatch_logs": _success(
            "cloudwatch_logs",
            [_finding(ts="2025-01-15T14:30:00Z", content="before-alert")],
            completed_at="2025-01-15T14:40:00Z",
        ),
    }
    tl = ReportFormatter().build_timeline(_alert("2025-01-15 14:32:00 UTC"), results)
    labels = [e.label for e in tl.events]
    # the 14:30 finding sorts BEFORE the 14:32 alert despite the format gap
    assert labels.index("before-alert") < labels.index("High CPU on service-api")


def test_build_timeline_ignores_failed_and_non_success_agents():
    results: dict[str, AgentResult | AgentFailure] = {
        "eks": AgentFailure(agent_name="eks", error_message="boom",
                            timestamp="2025-01-15T14:33:00Z"),
        "prometheus": AgentResult(agent_name="prometheus", status="error",
                                  findings=[_finding()], summary=""),
    }
    tl = ReportFormatter().build_timeline(_alert(), results)
    assert [e.kind for e in tl.events] == ["alert"]


def test_build_timeline_carries_chart_id_only_when_series_present():
    desc = ChartDescriptor.create(
        source="cloudwatch_logs_insights", log_groups=["/aws/lambda/x"],
        query="fields @message", start_epoch=1, end_epoch=2,
    )
    charted = _finding(content="charted", chart=desc)
    plain = _finding(content="plain", ts="2025-01-15T14:33:30Z")
    results: dict[str, AgentResult | AgentFailure] = {
        "cloudwatch_logs": _success(
            "cloudwatch_logs", [charted, plain],
            chart_series={desc.chart_id: ChartSeries(points=[{"t": 1, "v": 2}])},
        ),
    }
    tl = ReportFormatter().build_timeline(_alert(), results)
    by_label = {e.label: e for e in tl.events}
    assert by_label["charted"].chart_id == desc.chart_id
    assert by_label["plain"].chart_id is None


def test_build_timeline_drops_chart_id_when_descriptor_has_no_series():
    # A descriptor with no harvested series can't be drawn — no linkage.
    desc = ChartDescriptor.create(
        source="cloudwatch_logs_insights", log_groups=["/aws/lambda/x"],
        query="fields @message", start_epoch=1, end_epoch=2,
    )
    results: dict[str, AgentResult | AgentFailure] = {
        "cloudwatch_logs": _success(
            "cloudwatch_logs", [_finding(content="charted", chart=desc)],
            chart_series={},
        ),
    }
    tl = ReportFormatter().build_timeline(_alert(), results)
    assert {e.label: e.chart_id for e in tl.events}["charted"] is None


# --- page model integration ------------------------------------------------

def test_build_page_model_populates_timeline():
    results: dict[str, AgentResult | AgentFailure] = {
        "cloudwatch_logs": _success(
            "cloudwatch_logs", [_finding(content="spike")],
        ),
    }
    model = ReportFormatter().build_page_model(_alert(), results)
    assert model.timeline is not None
    kinds = [e["kind"] for e in model.timeline]
    assert kinds[0] == "alert"
    assert "finding" in kinds


def sorted_by_clock(times: list[str]) -> list[str]:
    from agents.master.report_formatter import _timeline_sort_epoch

    return sorted(times, key=lambda t: (_timeline_sort_epoch(t) is None,
                                        _timeline_sort_epoch(t) or 0.0))
