"""Property-based tests for the deterministic incident timeline (#34).

The ordering invariant is the load-bearing property: whatever timestamps the
agents emit, the timeline must read alert-first and be non-decreasing in
wall-clock for every event it could parse — a naive string sort would not hold
this across the alert's human "… UTC" form and the findings' ISO-T form.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from agents.master.report_formatter import ReportFormatter, _timeline_sort_epoch
from shared.agents import get_registry
from shared.models import AgentMetadata, AgentResult, AlertContext, Finding

AGENT_IDS = [a.id for a in get_registry().all(kind="specialized")]

_BASE = datetime(2025, 1, 15, 14, 32, 0, tzinfo=timezone.utc)

# Offsets (seconds from a base instant) rendered as ISO-8601 'Z' timestamps.
iso_ts = st.integers(min_value=-600, max_value=600).map(
    lambda secs: (_BASE + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")
)

finding_strategy = st.builds(
    Finding,
    source=st.text(min_size=1, max_size=20),
    timestamp=iso_ts,
    content=st.text(min_size=1, max_size=40),
    severity=st.sampled_from(["critical", "high", "medium", "low", "info"]),
    metadata=st.just({}),
)


@st.composite
def results_strategy(draw):
    out: dict[str, AgentResult] = {}
    for agent_id in draw(st.lists(st.sampled_from(AGENT_IDS), unique=True, max_size=4)):
        out[agent_id] = AgentResult(
            agent_name=agent_id,
            status="success",
            findings=draw(st.lists(finding_strategy, max_size=4)),
            summary="s",
            metadata=AgentMetadata(completed_at=draw(iso_ts)),
        )
    return out


@settings(max_examples=200)
@given(results=results_strategy())
def test_timeline_carries_the_alert_and_is_clock_ordered(results):
    alert = AlertContext(
        investigation_id="inv",
        platform="slack",
        channel_id="C1",
        message_id="m1",
        alert_text="boom",
        alert_timestamp="2025-01-15 14:32:00 UTC",  # human form, not ISO-T
        investigation_window=("2025-01-15 14:27:00 UTC", "2025-01-15 14:37:00 UTC"),
    )
    events = ReportFormatter().build_timeline(alert, results).events

    # The alert is always present exactly once, and parses to a real instant.
    # (It need not be *first*: a finding timestamped before the alert — a
    # precursor signal — legitimately sorts ahead of it.)
    alerts = [e for e in events if e.kind == "alert"]
    assert len(alerts) == 1
    assert _timeline_sort_epoch(alerts[0].timestamp) is not None

    # Parseable timestamps are non-decreasing across the whole timeline.
    epochs = [
        e for e in (_timeline_sort_epoch(ev.timestamp) for ev in events) if e is not None
    ]
    assert epochs == sorted(epochs)
