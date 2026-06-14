"""Investigation finalization — the post-report persistence seam.

After the master posts the Incident Report, a finished investigation still has
to be *recorded*: the trace manifest + terminating event, the raw results
archive (for ``/postmortem`` rebuild), the chart series behind descriptor
findings (#32), the interactive page model (#33), and the compact incident
outcome for similar-incident lookup (#30).

:class:`InvestigationOrchestrator` previously drove these five writes inline
across its Phase 7 + 8 tail, with the load-bearing ordering (charts *before*
the page model, so the page's S3 trigger fires only once its series exist)
enforced by a comment. :class:`InvestigationFinalizer` owns that order and the
fail-open contract: every step runs under :meth:`_guard`, so one failing write
never skips the rest, and the seam as a whole never raises into the
investigation.

The finalizer takes a **pre-derived** :class:`IncidentFacts` (the orchestrator
derives once over the final result set via
:meth:`ReportFormatter.derive_facts`), the raw ``AgentResult``s (charts and the
results archive need the un-projected results), the #27 ``analysis`` that rides
alongside the facts, and a :class:`FinalizationContext` carrying the non-facts
run metadata. Pending / disabled / skipped agents are already folded into the
facts' five-state evidence, so they are not re-passed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass

from agents.master.incident_facts import IncidentFacts
from agents.master.report_formatter import ReportFormatter
from shared.embeddings import EmbeddingClient
from shared.incident_history_store import IncidentHistoryStore, IncidentOutcome
from shared.models import AgentFailure, AgentResult, AlertContext
from shared.report_renderer import AnalysisSection
from shared.time_utils import now_iso
from shared.trace_store import (
    EVENT_INVESTIGATION_TERMINATED,
    SOURCE_MASTER,
    ResultSummary,
    TraceManifest,
    TraceStore,
    _dt_partition_from_iso,
)

logger = logging.getLogger(__name__)

# The compact outcome record (#30) falls back to a truncated report summary
# when the synthesis correlation is empty.
_OUTCOME_SUMMARY_CHARS = 600


@dataclass
class FinalizationContext:
    """The non-facts run metadata the finalizer needs to persist a record.

    ``facts`` already carries the derived view (severity, five-state evidence,
    timeline). This carries what facts deliberately does not: the raw
    :class:`AlertContext` (the manifest archives it verbatim; the outcome needs
    its routing identity), the dispatched roster + still-pending ids (manifest
    rollup + terminating event), the run timing, the router decision, and the
    posted report summary (the outcome's fallback text).
    """

    alert_context: AlertContext
    dispatched_agents: list[str]
    pending_ids: set[str]
    started_at_iso: str
    total_duration_seconds: float
    routing: dict | None = None
    report_summary: str = ""


class InvestigationFinalizer:
    """Persists the complete investigation record after the report is posted.

    Composed from a :class:`ReportFormatter` (the page-model projection) plus
    the optional trace store, history store, and embedding client. Each step is
    a no-op when its backing store is unconfigured, so the finalizer is safe to
    construct and call unconditionally (local dev / tests inject ``None``).
    """

    def __init__(
        self,
        report_formatter: ReportFormatter,
        *,
        trace_store: TraceStore | None = None,
        history_store: IncidentHistoryStore | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self._formatter = report_formatter
        self._trace_store = trace_store
        self._history_store = history_store
        self._embedding_client = embedding_client

    def finalize(
        self,
        facts: IncidentFacts,
        *,
        results: dict[str, AgentResult | AgentFailure],
        analysis: AnalysisSection | None,
        trace_meta: FinalizationContext,
    ) -> None:
        """Run every record-write in order; never raises.

        The order is load-bearing: the chart series are written *before* the
        page model so the page's S3 ObjectCreated event triggers the renderer
        only once the ``charts/<id>.json`` it references already exist. Each
        step is independently fail-open via :meth:`_guard`.
        """
        self._guard(self._write_trace, facts, results, trace_meta)
        self._guard(self._persist_results, results, trace_meta)
        self._guard(self._snapshot_charts, facts, results)
        self._guard(self._write_page_model, facts, analysis)
        self._guard(self._record_outcome, facts, analysis, trace_meta)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _write_trace(
        self,
        facts: IncidentFacts,
        results: dict[str, AgentResult | AgentFailure],
        meta: FinalizationContext,
    ) -> None:
        """Emit the terminating event + write the trace manifest."""
        if self._trace_store is None:
            return

        # Tombstone event first, so the events folder records termination even
        # if the manifest write fails.
        self._trace_store.put_event(
            investigation_id=facts.investigation_id,
            source=SOURCE_MASTER,
            event_type=EVENT_INVESTIGATION_TERMINATED,
            payload={
                "pending_agents": sorted(meta.pending_ids),
                "elapsed_seconds": meta.total_duration_seconds,
            },
        )

        results_summary, error_count = self._rollup(meta.dispatched_agents, results)
        if error_count == 0:
            status = "completed"
        elif error_count == len(meta.dispatched_agents):
            status = "failed"
        else:
            status = "partial"

        manifest = TraceManifest(
            investigation_id=facts.investigation_id,
            alert_context=asdict(meta.alert_context),
            started_at=meta.started_at_iso,
            ended_at=now_iso(),
            total_duration_seconds=meta.total_duration_seconds,
            dispatched_agents=list(meta.dispatched_agents),
            results_summary=results_summary,
            status=status,
            error_count=error_count,
            routing=meta.routing,
            timeline=facts.timeline.to_json_dict()["events"],
        )
        self._trace_store.put_manifest(manifest)

    @staticmethod
    def _rollup(
        dispatched_agents: list[str],
        results: dict[str, AgentResult | AgentFailure],
    ) -> tuple[dict[str, ResultSummary], int]:
        """Project the result set into the manifest's per-agent rollup.

        Anything in ``results`` that isn't a successful :class:`AgentResult`
        counts toward ``error_count``; an agent absent from ``results`` never
        returned before the hard cutoff and is recorded as ``timeout``.
        """
        summary: dict[str, ResultSummary] = {}
        error_count = 0
        for aid in dispatched_agents:
            r = results.get(aid)
            if isinstance(r, AgentResult):
                if r.status != "success":
                    error_count += 1
                summary[aid] = ResultSummary(
                    status=r.status,
                    findings_count=len(r.findings),
                    duration_seconds=r.duration_seconds,
                )
            elif isinstance(r, AgentFailure):
                error_count += 1
                summary[aid] = ResultSummary(
                    status="error", findings_count=0, duration_seconds=0.0,
                )
            else:
                error_count += 1
                summary[aid] = ResultSummary(
                    status="timeout", findings_count=0, duration_seconds=0.0,
                )
        return summary, error_count

    def _persist_results(
        self,
        results: dict[str, AgentResult | AgentFailure],
        meta: FinalizationContext,
    ) -> None:
        """Archive the full results map so ``/postmortem`` can rebuild the PIR."""
        if self._trace_store is None:
            return
        self._trace_store.put_results(
            investigation_id=meta.alert_context.investigation_id,
            results=results,
            dt=_dt_partition_from_iso(meta.started_at_iso),
        )

    def _snapshot_charts(
        self,
        facts: IncidentFacts,
        results: dict[str, AgentResult | AgentFailure],
    ) -> None:
        """Write the series behind every descriptor-carrying finding to S3 (#32).

        Each ``chart_id`` is written once across all findings and agents. Runs
        *before* :meth:`_write_page_model` so the page can draw from an
        immutable record.
        """
        if self._trace_store is None:
            return
        seen: set[str] = set()
        for result in results.values():
            if not isinstance(result, AgentResult):
                continue
            descriptors = {
                f.chart.chart_id: f.chart
                for f in result.findings
                if f.chart is not None
            }
            for chart_id, series in result.chart_series.items():
                if chart_id in seen:
                    continue
                seen.add(chart_id)
                desc = descriptors.get(chart_id)
                payload = {
                    "schema_version": 1,
                    "chart_id": chart_id,
                    "investigation_id": facts.investigation_id,
                    "source": desc.source if desc else "",
                    "descriptor": {
                        "log_groups": desc.log_groups,
                        "query": desc.query,
                        "start_epoch": desc.start_epoch,
                        "end_epoch": desc.end_epoch,
                    } if desc else {},
                    "series_kind": series.series_kind,
                    "truncated": series.truncated,
                    "points": series.points,
                    "captured_at": now_iso(),
                }
                self._trace_store.put_chart_series(
                    investigation_id=facts.investigation_id,
                    chart_id=chart_id,
                    payload=payload,
                )

    def _write_page_model(
        self,
        facts: IncidentFacts,
        analysis: AnalysisSection | None,
    ) -> None:
        """Build + persist the #33 page model (the renderer's S3 trigger)."""
        if self._trace_store is None:
            return
        model = self._formatter.build_page_model(facts, analysis=analysis)
        self._trace_store.put_page_model(
            investigation_id=facts.investigation_id,
            payload=model.to_json_dict(),
        )

    def _record_outcome(
        self,
        facts: IncidentFacts,
        analysis: AnalysisSection | None,
        meta: FinalizationContext,
    ) -> None:
        """Embed the alert + store a compact outcome record (#30).

        No-op unless both the embedding client and the history store are
        configured. A vector that can't be produced means the record would
        never be searchable, so the write is skipped.
        """
        if self._embedding_client is None or self._history_store is None:
            return
        embedding = self._embedding_client.embed(facts.alert_text)
        if embedding is None:
            return
        root_cause = analysis.root_cause_hypothesis if analysis else None
        summary = (
            (analysis.correlation if analysis else "")
            or meta.report_summary[:_OUTCOME_SUMMARY_CHARS]
        )
        ctx = meta.alert_context
        self._history_store.put_outcome(
            IncidentOutcome(
                investigation_id=facts.investigation_id,
                alert_text=facts.alert_text,
                summary=summary,
                embedding=embedding,
                platform=ctx.platform,
                channel_id=ctx.channel_id,
                message_id=ctx.message_id,
                alert_timestamp=ctx.alert_timestamp,
                root_cause=root_cause,
            )
        )

    # ------------------------------------------------------------------
    # Fail-open guard
    # ------------------------------------------------------------------

    def _guard(self, step: Callable[..., None], *args: object) -> None:
        """Run one finalization step, logging and swallowing any error.

        The seam's contract is "runs every step, never raises" — independent of
        whether the underlying stores swallow their own errors. A non-store
        exception (e.g. a projection build raising) therefore can never abort a
        later step.
        """
        try:
            step(*args)
        except Exception:
            logger.exception("Finalization step %s failed", getattr(step, "__name__", step))
