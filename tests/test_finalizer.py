"""Tests for :class:`agents.master.finalizer.InvestigationFinalizer`.

Behavior is exercised through the public ``finalize()`` interface with a fake
report formatter / fake stores — the finalizer's contract is "persist the
record in the right order, fail-open, no-op when a store is absent."
"""

from __future__ import annotations

from agents.master.finalizer import FinalizationContext, InvestigationFinalizer
from agents.master.report_formatter import ReportFormatter
from shared.models import (
    AgentFailure,
    AgentMetadata,
    AgentResult,
    AlertContext,
    ChartDescriptor,
    ChartSeries,
    Finding,
)
from shared.report_renderer import AnalysisSection


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTraceStore:
    """Records every write in call order so ordering can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def put_event(self, **kwargs) -> None:
        self.calls.append(("put_event", kwargs))

    def put_manifest(self, manifest) -> None:
        self.calls.append(("put_manifest", {"manifest": manifest}))

    def put_results(self, **kwargs) -> None:
        self.calls.append(("put_results", kwargs))

    def put_chart_series(self, **kwargs) -> None:
        self.calls.append(("put_chart_series", kwargs))

    def put_page_model(self, **kwargs) -> None:
        self.calls.append(("put_page_model", kwargs))

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeEmbeddingClient:
    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = list(vector) if vector is not None else None

    def embed(self, text: str) -> list[float] | None:
        return self._vector


class FakeHistoryStore:
    def __init__(self) -> None:
        self.outcomes: list = []

    def put_outcome(self, outcome) -> None:
        self.outcomes.append(outcome)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alert(investigation_id: str = "inv-fin-1") -> AlertContext:
    return AlertContext(
        investigation_id=investigation_id,
        platform="slack",
        channel_id="C123",
        message_id="170000.0001",
        alert_text="High CPU on service-api",
        alert_timestamp="2025-01-15T14:32:00Z",
        investigation_window=("2025-01-15T14:27:00Z", "2025-01-15T14:37:00Z"),
    )


def _success(agent_id: str, *, findings=None, chart_series=None) -> AgentResult:
    return AgentResult(
        agent_name=agent_id,
        status="success",
        findings=findings or [],
        summary=f"{agent_id} ok",
        chart_series=chart_series or {},
        metadata=AgentMetadata(),
    )


def _context(
    alert: AlertContext,
    *,
    dispatched: list[str],
    pending: set[str] | None = None,
    report_summary: str = "report text",
) -> FinalizationContext:
    return FinalizationContext(
        alert_context=alert,
        dispatched_agents=dispatched,
        pending_ids=pending or set(),
        started_at_iso="2025-01-15T14:32:00Z",
        total_duration_seconds=4.5,
        routing=None,
        report_summary=report_summary,
    )


def _finalizer(**kwargs) -> InvestigationFinalizer:
    return InvestigationFinalizer(ReportFormatter(), **kwargs)


def _facts(alert: AlertContext, results: dict):
    return ReportFormatter().derive_facts(alert, results)


# ---------------------------------------------------------------------------
# Tracer + manifest
# ---------------------------------------------------------------------------


def test_finalize_writes_terminating_event_then_manifest():
    alert = _alert()
    results = {"eks": _success("eks")}
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    kinds = store.kinds()
    assert kinds.index("put_event") < kinds.index("put_manifest")
    manifest = next(kw["manifest"] for name, kw in store.calls if name == "put_manifest")
    assert manifest.investigation_id == "inv-fin-1"
    assert manifest.status == "completed"
    assert manifest.error_count == 0


def test_manifest_carries_analysis_when_present():
    # Rec #5 — the #27 analysis is archived on the manifest so the PIR can use it.
    alert = _alert()
    results = {"eks": _success("eks")}
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)
    analysis = AnalysisSection(
        root_cause_hypothesis="bad deploy", correlation="errors after rollout",
        confidence="high", suggested_next_action="rollback",
        causal_chain=["deploy v2", "errors"], ruled_out=["network"],
    )

    fin.finalize(
        _facts(alert, results), results=results, analysis=analysis,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    manifest = next(kw["manifest"] for name, kw in store.calls if name == "put_manifest")
    assert manifest.analysis is not None
    assert manifest.analysis["root_cause_hypothesis"] == "bad deploy"
    assert manifest.analysis["causal_chain"] == ["deploy v2", "errors"]
    assert manifest.analysis["ruled_out"] == ["network"]


def test_manifest_analysis_none_when_synthesis_off():
    alert = _alert()
    results = {"eks": _success("eks")}
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    manifest = next(kw["manifest"] for name, kw in store.calls if name == "put_manifest")
    assert manifest.analysis is None


def test_manifest_status_partial_when_some_agents_fail():
    alert = _alert()
    results = {
        "eks": _success("eks"),
        "cloudwatch_logs": AgentFailure(
            agent_name="cloudwatch_logs", error_message="timeout", timestamp="t",
        ),
    }
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks", "cloudwatch_logs"]),
    )

    manifest = next(kw["manifest"] for name, kw in store.calls if name == "put_manifest")
    assert manifest.status == "partial"
    assert manifest.error_count == 1


def test_manifest_status_failed_when_all_agents_fail():
    alert = _alert()
    results = {
        "eks": AgentResult(agent_name="eks", status="error", findings=[], summary=""),
    }
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    manifest = next(kw["manifest"] for name, kw in store.calls if name == "put_manifest")
    assert manifest.status == "failed"


def test_dispatched_agent_absent_from_results_is_timeout():
    alert = _alert()
    results: dict = {}
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"], pending={"eks"}),
    )

    manifest = next(kw["manifest"] for name, kw in store.calls if name == "put_manifest")
    assert manifest.results_summary["eks"].status == "timeout"
    event = next(kw for name, kw in store.calls if name == "put_event")
    assert event["payload"]["pending_agents"] == ["eks"]


# ---------------------------------------------------------------------------
# Results archive + charts + page model ordering
# ---------------------------------------------------------------------------


def test_finalize_persists_raw_results():
    alert = _alert()
    results = {"eks": _success("eks")}
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    put_results = next(kw for name, kw in store.calls if name == "put_results")
    assert put_results["results"] is results


def test_charts_are_snapshotted_before_the_page_model():
    alert = _alert()
    desc = ChartDescriptor.create(
        source="cloudwatch_logs_insights", log_groups=["lg"],
        query="fields @message", start_epoch=1, end_epoch=2,
    )
    finding = Finding(source="lg", timestamp="t", content="c", severity="info", chart=desc)
    results = {
        "cloudwatch_logs": _success(
            "cloudwatch_logs", findings=[finding],
            chart_series={desc.chart_id: ChartSeries(points=[{"x": 1}])},
        )
    }
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["cloudwatch_logs"]),
    )

    kinds = store.kinds()
    assert kinds.index("put_chart_series") < kinds.index("put_page_model")
    chart = next(kw for name, kw in store.calls if name == "put_chart_series")
    assert chart["chart_id"] == desc.chart_id
    assert chart["payload"]["points"] == [{"x": 1}]


def test_finalize_writes_page_model():
    alert = _alert()
    results = {"eks": _success("eks")}
    store = FakeTraceStore()
    fin = _finalizer(trace_store=store)

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    page = next(kw for name, kw in store.calls if name == "put_page_model")
    assert page["investigation_id"] == "inv-fin-1"
    assert page["payload"]["investigation_id"] == "inv-fin-1"


# ---------------------------------------------------------------------------
# Incident outcome (#30)
# ---------------------------------------------------------------------------


def test_records_outcome_with_embedding_and_root_cause():
    alert = _alert()
    results = {"eks": _success("eks")}
    history = FakeHistoryStore()
    analysis = AnalysisSection(
        root_cause_hypothesis="bad deploy", correlation="errors after rollout",
        confidence="high", suggested_next_action="rollback",
    )
    fin = _finalizer(
        trace_store=FakeTraceStore(),
        history_store=history,
        embedding_client=FakeEmbeddingClient([0.5, 0.6]),
    )

    fin.finalize(
        _facts(alert, results), results=results, analysis=analysis,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    assert len(history.outcomes) == 1
    outcome = history.outcomes[0]
    assert outcome.investigation_id == "inv-fin-1"
    assert outcome.embedding == [0.5, 0.6]
    assert outcome.root_cause == "bad deploy"
    assert outcome.summary == "errors after rollout"
    assert outcome.channel_id == "C123"


def test_outcome_skipped_when_embedding_unavailable():
    alert = _alert()
    results = {"eks": _success("eks")}
    history = FakeHistoryStore()
    fin = _finalizer(
        trace_store=FakeTraceStore(),
        history_store=history,
        embedding_client=FakeEmbeddingClient(None),
    )

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )

    assert history.outcomes == []


def test_outcome_summary_falls_back_to_report_when_no_analysis():
    alert = _alert()
    results = {"eks": _success("eks")}
    history = FakeHistoryStore()
    fin = _finalizer(
        history_store=history,
        embedding_client=FakeEmbeddingClient([0.1]),
    )

    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"], report_summary="posted summary"),
    )

    assert history.outcomes[0].summary == "posted summary"
    assert history.outcomes[0].root_cause is None


# ---------------------------------------------------------------------------
# No-op when stores absent
# ---------------------------------------------------------------------------


def test_noop_when_no_trace_store():
    alert = _alert()
    results = {"eks": _success("eks")}
    # No trace store, no history/embedding — must not raise and write nothing.
    fin = _finalizer()
    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )


def test_outcome_noop_when_history_store_absent_but_embedding_present():
    alert = _alert()
    results = {"eks": _success("eks")}
    fin = _finalizer(
        trace_store=FakeTraceStore(),
        embedding_client=FakeEmbeddingClient([0.1]),
        # history_store omitted
    )
    # Must not raise.
    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["eks"]),
    )


# ---------------------------------------------------------------------------
# Fail-open: one step raising never aborts later steps
# ---------------------------------------------------------------------------


class ExplodingChartStore(FakeTraceStore):
    def put_chart_series(self, **kwargs) -> None:  # type: ignore[override]
        raise RuntimeError("S3 down")


def test_failing_step_does_not_abort_later_steps():
    alert = _alert()
    desc = ChartDescriptor.create(
        source="cloudwatch_logs_insights", log_groups=["lg"],
        query="q", start_epoch=1, end_epoch=2,
    )
    finding = Finding(source="lg", timestamp="t", content="c", severity="info", chart=desc)
    results = {
        "cloudwatch_logs": _success(
            "cloudwatch_logs", findings=[finding],
            chart_series={desc.chart_id: ChartSeries(points=[{"x": 1}])},
        )
    }
    store = ExplodingChartStore()
    history = FakeHistoryStore()
    fin = _finalizer(
        trace_store=store,
        history_store=history,
        embedding_client=FakeEmbeddingClient([0.1]),
    )

    # The chart write raises; the page model + outcome must still run.
    fin.finalize(
        _facts(alert, results), results=results, analysis=None,
        trace_meta=_context(alert, dispatched=["cloudwatch_logs"]),
    )

    assert "put_page_model" in store.kinds()
    assert len(history.outcomes) == 1
