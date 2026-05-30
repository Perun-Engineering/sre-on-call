"""Master Agent orchestrator — parallel fan-out and deadline management.

Coordinates the active specialized agents (read from the
:class:`shared.agents.AgentRegistry`) via A2A JSON-RPC 2.0, enforces a
60-second initial-report deadline and a 5-minute hard cutoff, and posts
results back to the originating chat platform via a pluggable :class:`ChatPlatform`.

The set of agents the orchestrator dispatches to is now derived entirely
from ``config.yaml`` via the registry. There is no ``ENABLED_AGENTS``
allowlist — operators control fan-out by flipping ``enabled: true|false``
on each agent in ``config.yaml``. Disabled-in-config specialized agents
appear in the Incident Report's Evidence section as 🚫 disabled blocks.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 9.2
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, replace

from shared.a2a_client import (
    A2AClient,
    A2AReply,
    AgentCoreClient,
    AiohttpClient,
    AsyncHTTPClient,
)
from shared.agent_telemetry import AGENT_METADATA
from shared.agents import AgentRegistry, get_registry
from shared.constants import HARD_CUTOFF_SECONDS, INITIAL_DEADLINE_SECONDS
from shared.fanout import Fanout
from shared.models import AgentFailure, AgentMetadata, AgentResult, AlertContext
from shared.time_utils import now_iso
from shared.platforms import ChatPlatform, DeliveryTarget, deliver_with_retry, for_platform
from shared.experiment import ExperimentResult
from shared.experiment_results_store import ExperimentResultsStore
from shared.tool_result import AGENT_RESULT
from shared.trace_store import (
    EVENT_A2A_REQUEST,
    EVENT_A2A_RESPONSE,
    EVENT_INVESTIGATION_TERMINATED,
    SOURCE_MASTER,
    ResultSummary,
    TraceManifest,
    TraceStore,
)
from agents.master.report_formatter import ReportFormatter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A2A JSON-RPC 2.0 helpers
#
# The transport adapters (AsyncHTTPClient / AiohttpClient / AgentCoreClient)
# and the request/response round-trip now live in shared.a2a_client; they are
# re-exported above so existing ``from agents.master.orchestrator import ...``
# call sites keep working.
# ---------------------------------------------------------------------------


def _serialize_alert_context(alert_context: AlertContext) -> str:
    """Serialize an :class:`AlertContext` to a JSON string for A2A transport."""
    return json.dumps(asdict(alert_context))


def _result_from_reply(
    agent_name: str,
    reply: A2AReply[AgentResult],
    base_metadata: AgentMetadata | None = None,
) -> AgentResult:
    """Map a parsed :class:`A2AReply` onto an :class:`AgentResult`.

    Owns the alert path's domain knowledge — the wire parsing already
    happened in :meth:`shared.a2a_client.A2AClient.send`. A JSON-RPC error
    becomes an ``error`` result. The ``AGENT_METADATA`` footer (which
    ``send`` leaves in ``reply.text`` — it only strips the requested
    ``AGENT_RESULT`` footer) is peeled here and merged with the
    orchestrator's wall-clock window (``base_metadata``) and any
    footer-supplied metadata (model, tokens, cost).
    """
    base_metadata = base_metadata or AgentMetadata()

    if reply.error is not None:
        return AgentResult(
            agent_name=agent_name,
            status="error",
            findings=[],
            summary="",
            error_message=reply.error,
            metadata=base_metadata,
        )

    clean_summary, footer_metadata = AGENT_METADATA.extract(reply.text)
    structured = reply.payload
    merged = _merge_metadata(base_metadata, structured.metadata if structured else None)
    merged = _merge_metadata(merged, footer_metadata)

    if structured is not None:
        structured.metadata = merged
        if not structured.summary:
            structured.summary = clean_summary
        return structured

    return AgentResult(
        agent_name=agent_name,
        status="success",
        findings=[],
        summary=clean_summary,
        metadata=merged,
    )


def _merge_metadata(
    base: AgentMetadata, overlay: AgentMetadata | None
) -> AgentMetadata:
    """Overlay non-``None`` fields from the agent footer onto orchestrator timing."""
    if overlay is None:
        return base
    overrides = {k: v for k, v in asdict(overlay).items() if v is not None}
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# InvestigationOrchestrator
# ---------------------------------------------------------------------------


class InvestigationOrchestrator:
    """Orchestrates a parallel investigation across the active specialized agents.

    The set of dispatch targets is read from the :class:`AgentRegistry`'s
    ``active(kind="specialized")`` view. Agents that are deployed but
    disabled in ``config.yaml`` (``enabled: false``) are passed to the
    formatter as ``disabled_agents`` and rendered as 🚫 evidence blocks
    in the Incident Report.

    Lifecycle:
        1. Fan out to all active specialized agents via A2A JSON-RPC 2.0
           ``message/send``
        2. Wait up to 60 s for initial results
        3. Synthesize and post the Incident Report
        4. Continue collecting late-arriving results until the 5-min cutoff
        5. Post enrichment updates for each late result
        6. Terminate the investigation at the 5-min mark
    """

    INITIAL_DEADLINE_SECONDS: float = INITIAL_DEADLINE_SECONDS
    HARD_CUTOFF_SECONDS: float = HARD_CUTOFF_SECONDS

    def __init__(
        self,
        http_client: AsyncHTTPClient | None = None,
        chat_platform: ChatPlatform | None = None,
        report_formatter: ReportFormatter | None = None,
        registry: AgentRegistry | None = None,
        results_store: ExperimentResultsStore | None = None,
        trace_store: TraceStore | None = None,
    ) -> None:
        self._chat_platform: ChatPlatform | None = chat_platform
        self._registry: AgentRegistry = registry or get_registry()
        self.report_formatter = report_formatter or ReportFormatter(self._registry)

        active_specialized = self._registry.active(kind="specialized")
        self.disabled_agents: set[str] = {
            a.id for a in self._registry.disabled_in_config(kind="specialized")
        }

        # The Fanout owns endpoint resolution + per-endpoint transport routing
        # + the one A2AClient. agent_endpoints / http_client / _client are
        # pass-throughs so existing call sites and tests keep working.
        self._fanout = Fanout(http_client=http_client, registry=self._registry)
        self.agent_endpoints: dict[str, str] = self._fanout.agent_endpoints
        self.http_client = self._fanout.http_client
        self._client = self._fanout.client
        self._results_store = results_store
        # Trace archive is opt-in via env vars; from_env() returns None when
        # TRACES_BUCKET_NAME / TRACES_TABLE_NAME are unset (e.g. local dev,
        # tests). All trace_store calls below must check for None.
        self._trace_store = trace_store if trace_store is not None else TraceStore.from_env()

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    def _get_platform(self, platform_name: str) -> ChatPlatform:
        """Return the ChatPlatform, selecting by name when not injected."""
        if self._chat_platform is not None:
            return self._chat_platform
        return for_platform(platform_name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def investigate(self, alert_context: AlertContext) -> None:
        """Run the full investigation lifecycle."""
        start_time = asyncio.get_event_loop().time()
        started_at_iso = now_iso()
        results: dict[str, AgentResult | AgentFailure] = {}
        initial_report_summary = ""
        platform = self._get_platform(alert_context.platform)
        target = DeliveryTarget.for_alert(alert_context)

        # --- Phase 0: announce which agents will be queried ------------------
        # Fire-and-forget so fan-out starts immediately; a slow chat post
        # would otherwise add an RTT to every investigation. The "started"
        # notice lists only agents we're actively dispatching to — disabled-
        # in-config agents only appear in the Incident Report's Evidence
        # section, not the kick-off announcement.
        dispatched_agents = list(self.agent_endpoints.keys())
        if dispatched_agents:
            started_sections = self.report_formatter.build_started_sections(
                alert_context, dispatched_agents,
            )
            asyncio.create_task(
                self._post_started_notice(platform, target, started_sections),
                name=f"started-notice-{alert_context.investigation_id}",
            )

        # --- Phase 1: fan-out ------------------------------------------------
        pending = self._fanout.dispatch(
            lambda agent_id: self._invoke_agent_safe(agent_id, alert_context)
        )

        # --- Phase 2: wait up to INITIAL_DEADLINE_SECONDS --------------------
        elapsed = asyncio.get_event_loop().time() - start_time
        initial_timeout = max(0, self.INITIAL_DEADLINE_SECONDS - elapsed)
        settled, pending = await self._fanout.harvest(pending, initial_timeout)
        self._merge(settled, results)

        # --- Phase 3: synthesize and post initial report ---------------------
        # Agents still pending at the deadline are reported as in-progress;
        # their late results trigger an enrichment update in Phase 4.
        pending_ids = set(pending)

        report_sections = self.report_formatter.build_incident_sections(
            alert_context,
            results,
            pending_agents=pending_ids,
            disabled_agents=self.disabled_agents,
        )

        try:
            initial_report_summary = await deliver_with_retry(
                platform, target, report_sections,
            )
        except Exception:
            logger.exception(
                "Failed to post initial Incident Report for investigation %s "
                "(all retries exhausted).",
                alert_context.investigation_id,
            )

        # --- Phase 4: accept late results until HARD_CUTOFF_SECONDS ----------
        # Each harvest re-waits the *same* in-flight requests — no re-send.
        while pending:
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = self.HARD_CUTOFF_SECONDS - elapsed
            if remaining <= 0:
                break

            settled, pending = await self._fanout.harvest(pending, remaining)
            late_results = self._merge(settled, results)

            for agent_id, result in late_results.items():
                # Late results post regardless of status — a failure that
                # crossed the 60s mark is still surfaced.
                try:
                    enrichment_sections = self.report_formatter.build_enrichment_sections(
                        source_agent=agent_id,
                        new_findings=result,
                        initial_report_summary=initial_report_summary,
                        variant_label=alert_context.variant_label,
                    )
                    await deliver_with_retry(
                        platform, target, enrichment_sections,
                    )
                except Exception:
                    logger.exception(
                        "Failed to post enrichment update for %s in investigation %s "
                        "(all retries exhausted)",
                        agent_id,
                        alert_context.investigation_id,
                    )

        # --- Phase 5: terminate — cancel any still-pending tasks -------------
        await self._fanout.cancel(pending)

        logger.info(
            "Investigation %s terminated. Agents responded: %s",
            alert_context.investigation_id,
            list(results.keys()),
        )

        # --- Phase 6: store experiment result if running under A/B test ------
        if alert_context.experiment_id and alert_context.variant_id:
            self._store_experiment_result(
                alert_context, results, initial_report_summary, start_time,
            )

        # --- Phase 7: finalize the trace archive (manifest + DDB index) ------
        # Fail-open: an error here is logged inside the trace store and
        # never raises into the surrounding investigation.
        self._finalize_trace(
            alert_context=alert_context,
            results=results,
            dispatched_agents=dispatched_agents,
            pending_ids=pending_ids,
            started_at_iso=started_at_iso,
            start_time=start_time,
        )

    # ------------------------------------------------------------------
    # Agent invocation
    # ------------------------------------------------------------------

    async def invoke_agent(
        self, agent_id: str, alert_context: AlertContext
    ) -> AgentResult:
        """Send an A2A JSON-RPC ``message/send`` to a specialized agent."""
        endpoint = self.agent_endpoints[agent_id]

        started_at = now_iso()
        start = time.monotonic()
        reply = await self._client.send(
            endpoint,
            _serialize_alert_context(alert_context),
            footer=AGENT_RESULT,
            request_id=f"req-{agent_id}-{alert_context.investigation_id}",
        )
        duration = time.monotonic() - start

        base_metadata = AgentMetadata(started_at=started_at, completed_at=now_iso())
        result = _result_from_reply(agent_id, reply, base_metadata)
        result.duration_seconds = duration
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _post_started_notice(
        self,
        platform: ChatPlatform,
        target: DeliveryTarget,
        sections,
    ) -> None:
        try:
            await deliver_with_retry(platform, target, sections)
        except Exception:
            logger.exception(
                "Failed to post investigation-started notice for %s",
                target.channel_id,
            )

    async def _invoke_agent_safe(
        self, agent_id: str, alert_context: AlertContext
    ) -> AgentResult:
        """Invoke an agent, catching exceptions and returning an error result."""
        started_at = now_iso()
        endpoint = self.agent_endpoints.get(agent_id, "")

        # Trace: write the outbound A2A request envelope so postmortem
        # tooling can replay this agent in isolation.
        if self._trace_store is not None:
            self._trace_store.put_event(
                investigation_id=alert_context.investigation_id,
                source=SOURCE_MASTER,
                event_type=EVENT_A2A_REQUEST,
                payload={
                    "agent_id": agent_id,
                    "endpoint": endpoint,
                    "started_at": started_at,
                },
            )

        try:
            result = await self.invoke_agent(agent_id, alert_context)
            if self._trace_store is not None:
                self._trace_store.put_event(
                    investigation_id=alert_context.investigation_id,
                    source=SOURCE_MASTER,
                    event_type=EVENT_A2A_RESPONSE,
                    payload={
                        "agent_id": agent_id,
                        "duration_seconds": result.duration_seconds,
                        "status": result.status,
                        "findings_count": len(result.findings),
                        "summary": result.summary,
                    },
                )
            return result
        except Exception as exc:
            logger.exception("Agent %s invocation failed", agent_id)
            if self._trace_store is not None:
                self._trace_store.put_event(
                    investigation_id=alert_context.investigation_id,
                    source=SOURCE_MASTER,
                    event_type=EVENT_A2A_RESPONSE,
                    payload={
                        "agent_id": agent_id,
                        "status": "error",
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    },
                )
            return AgentResult(
                agent_name=agent_id,
                status="error",
                findings=[],
                summary="",
                error_message=str(exc),
                metadata=AgentMetadata(started_at=started_at, completed_at=now_iso()),
            )

    def _store_experiment_result(
        self,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
        report: str,
        start_time: float,
    ) -> None:
        """Persist experiment result for offline comparison."""
        store = self._results_store or ExperimentResultsStore()
        durations: dict[str, float] = {}
        for aid, r in results.items():
            if isinstance(r, AgentResult):
                durations[aid] = r.duration_seconds
        total = asyncio.get_event_loop().time() - start_time
        try:
            store.put_result(ExperimentResult(
                experiment_id=alert_context.experiment_id or "",
                investigation_id=alert_context.investigation_id,
                variant_id=alert_context.variant_id or "",
                report=report,
                agent_durations=durations,
                total_duration_seconds=total,
                timestamp=alert_context.alert_timestamp,
            ))
        except Exception:
            logger.exception(
                "Failed to store experiment result for %s variant %s",
                alert_context.investigation_id,
                alert_context.variant_id,
            )

    def _finalize_trace(
        self,
        *,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
        dispatched_agents: list[str],
        pending_ids: set[str],
        started_at_iso: str,
        start_time: float,
    ) -> None:
        """Emit the investigation_terminated event + write the manifest.

        Fail-open. ``self._trace_store`` may be ``None`` (tracing
        disabled); both writes inside :class:`TraceStore` are themselves
        wrapped in try/except, so this method should never raise.
        """
        if self._trace_store is None:
            return

        ended_at_iso = now_iso()
        total_duration = asyncio.get_event_loop().time() - start_time

        # Emit a terminating event so the events folder has a tombstone
        # even if the manifest write fails. ``pending_ids`` is the set of
        # agents that didn't return before the hard cutoff.
        self._trace_store.put_event(
            investigation_id=alert_context.investigation_id,
            source=SOURCE_MASTER,
            event_type=EVENT_INVESTIGATION_TERMINATED,
            payload={
                "pending_agents": sorted(pending_ids),
                "elapsed_seconds": total_duration,
            },
        )

        # Build the per-agent rollup. Anything in `results` that's not an
        # AgentResult is an AgentFailure (timeout / cancellation); count
        # it as an error in the manifest.
        results_summary: dict[str, ResultSummary] = {}
        error_count = 0
        for aid in dispatched_agents:
            r = results.get(aid)
            if isinstance(r, AgentResult):
                if r.status != "success":
                    error_count += 1
                results_summary[aid] = ResultSummary(
                    status=r.status,
                    findings_count=len(r.findings),
                    duration_seconds=r.duration_seconds,
                )
            elif isinstance(r, AgentFailure):
                error_count += 1
                results_summary[aid] = ResultSummary(
                    status="error",
                    findings_count=0,
                    duration_seconds=0.0,
                )
            else:
                # Pending — never returned before the hard cutoff.
                error_count += 1
                results_summary[aid] = ResultSummary(
                    status="timeout",
                    findings_count=0,
                    duration_seconds=0.0,
                )

        if error_count == 0:
            status = "completed"
        elif error_count == len(dispatched_agents):
            status = "failed"
        else:
            status = "partial"

        manifest = TraceManifest(
            investigation_id=alert_context.investigation_id,
            alert_context=asdict(alert_context),
            started_at=started_at_iso,
            ended_at=ended_at_iso,
            total_duration_seconds=total_duration,
            dispatched_agents=list(dispatched_agents),
            results_summary=results_summary,
            status=status,
            error_count=error_count,
        )
        self._trace_store.put_manifest(manifest)

    @staticmethod
    def _merge(
        settled: dict[str, "AgentResult | BaseException"],
        results: dict[str, AgentResult | AgentFailure],
    ) -> dict[str, AgentResult | AgentFailure]:
        """Fold harvested results into *results* and return the new entries.

        ``_invoke_agent_safe`` already maps errors to ``AgentResult(status=
        "error")``, so a value is normally an :class:`AgentResult`. A raised
        exception handed back by :meth:`Fanout.harvest` (e.g. a cancellation)
        is mapped to an :class:`AgentFailure`.
        """
        new_entries: dict[str, AgentResult | AgentFailure] = {}
        for agent_id, value in settled.items():
            if isinstance(value, BaseException):
                mapped: AgentResult | AgentFailure = AgentFailure(
                    agent_name=agent_id, error_message=str(value), timestamp="",
                )
            else:
                mapped = value
            results[agent_id] = mapped
            new_entries[agent_id] = mapped
        return new_entries
