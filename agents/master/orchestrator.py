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
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

from shared.a2a_client import (
    A2AReply,
    AsyncHTTPClient,
)
from shared.agent_telemetry import AGENT_METADATA
from shared.agents import AgentRegistry, get_registry
from shared.constants import HARD_CUTOFF_SECONDS, INITIAL_DEADLINE_SECONDS
from shared.embeddings import EmbeddingClient
from shared.fanout import Fanout
from shared.incident_history_store import IncidentHistoryStore, IncidentOutcome
from shared.models import AgentFailure, AgentMetadata, AgentResult, AlertContext
from shared.time_utils import now_iso
from shared.platforms import ChatPlatform, DeliveryTarget, deliver_with_retry, for_platform
from shared.experiment import ExperimentResult
from shared.experiment_results_store import ExperimentResultsStore
from shared.tool_result import AGENT_RESULT
from shared.trace_store import (
    EVENT_A2A_REQUEST,
    EVENT_A2A_RESPONSE,
    EVENT_FOLLOWUP_DECISION,
    EVENT_INVESTIGATION_TERMINATED,
    EVENT_ROUTING_DECISION,
    SOURCE_MASTER,
    ResultSummary,
    TraceManifest,
    TraceStore,
)
from agents.master.followup import FollowupCandidate, FollowupPlanner
from agents.master.report_formatter import ReportFormatter
from agents.master.routing import AgentCandidate, AgentRouter, RoutingResult
from agents.master.synthesis import AnalysisSynthesizer, IncidentAnalysis
from shared.page_signer import CloudFrontUrlSigner
from shared.report_renderer import AnalysisSection

logger = logging.getLogger(__name__)

# Cap on the outcome record's free-text summary when no synthesis correlation
# is available to fall back on — keeps the history item compact.
_OUTCOME_SUMMARY_CHARS = 600

# Stage 2 follow-up only runs when at least this much of the hard-cutoff budget
# remains after the initial report — enough to plan and land a refined dispatch.
# The dispatched follow-up tasks are *also* bounded by the Phase 4 cutoff loop,
# so the 5-minute deadline holds regardless; this just avoids a pointless call.
_MIN_FOLLOWUP_BUDGET_SECONDS = 1.0


def _to_analysis_section(analysis: IncidentAnalysis | None) -> AnalysisSection | None:
    """Map the synthesizer's structured output onto the renderer's section.

    Keeps the pydantic ``IncidentAnalysis`` (the structured-output vehicle)
    out of the platform-agnostic rendering layer.
    """
    if analysis is None:
        return None
    return AnalysisSection(
        root_cause_hypothesis=analysis.root_cause_hypothesis,
        correlation=analysis.correlation,
        confidence=analysis.confidence,
        suggested_next_action=analysis.suggested_next_action,
    )


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

    # Prefer the structured DataPart metadata (issue #24); fall back to the
    # legacy text footer for agents still on the pre-#24 image. The text footer
    # is always stripped from the summary regardless of which source wins.
    clean_summary, text_footer_metadata = AGENT_METADATA.extract(reply.text)
    data_metadata = reply.data.get(AGENT_METADATA.kind)
    footer_metadata = (
        AGENT_METADATA.decode_data(data_metadata)
        if data_metadata is not None
        else text_footer_metadata
    )
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


def _sum_agent_telemetry(
    results: Mapping[str, AgentResult | AgentFailure],
) -> tuple[float | None, int | None]:
    """Sum per-agent cost and token usage for the experiment result (issue #26).

    Returns ``(total_cost_usd, total_tokens)``; either is ``None`` when no agent
    reported that figure, so the judge report shows a blank rather than a
    misleading zero.
    """
    total_cost = 0.0
    total_tokens = 0
    cost_seen = tokens_seen = False
    for r in results.values():
        meta = r.metadata
        if meta.cost_usd is not None:
            total_cost += meta.cost_usd
            cost_seen = True
        if meta.total_tokens is not None:
            total_tokens += meta.total_tokens
            tokens_seen = True
    return (total_cost if cost_seen else None, total_tokens if tokens_seen else None)


@dataclass(frozen=True)
class _RoutingPlan:
    """The orchestrator's resolved dispatch plan for one investigation.

    ``selected_ids`` is the ordered set of agents to fan out to; ``hints`` maps
    each onto its per-agent investigation hint; ``skipped`` maps deliberately
    skipped agents onto the router's reason (rendered ➖ in the report).
    ``manifest_record`` is the JSON-safe routing block for the trace manifest,
    or ``None`` when routing was disabled or fell open (dispatched all).
    """

    selected_ids: list[str]
    hints: dict[str, str]
    skipped: dict[str, str]
    manifest_record: dict | None


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
        synthesizer: AnalysisSynthesizer | None = None,
        embedding_client: EmbeddingClient | None = None,
        history_store: IncidentHistoryStore | None = None,
        router: AgentRouter | None = None,
        followup: FollowupPlanner | None = None,
        page_signer: CloudFrontUrlSigner | None = None,
    ) -> None:
        self._chat_platform: ChatPlatform | None = chat_platform
        self._registry: AgentRegistry = registry or get_registry()
        self.report_formatter = report_formatter or ReportFormatter(self._registry)

        # LLM synthesis is opt-in via env (SYNTHESIS_ENABLED); from_env()
        # returns None when disabled, so the report stays the deterministic
        # concatenation it has always been. Tests inject a fake or None.
        self._synthesizer = (
            synthesizer if synthesizer is not None else AnalysisSynthesizer.from_env()
        )

        # Phase 0.5 routing (issue #28) is opt-in via ALERT_ROUTING_ENABLED, and
        # the Stage 2 follow-up round via FOLLOWUP_ROUND_ENABLED. Both from_env()
        # return None when disabled, in which case the orchestrator dispatches
        # every active agent and runs no follow-up — byte-for-byte today's flow.
        # Both call sites are independently fail-open. Tests inject a fake or None.
        self._router = router if router is not None else AgentRouter.from_env()
        self._followup = followup if followup is not None else FollowupPlanner.from_env()

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

        # #33 — signs the stable interactive-page URL at report-post time.
        # from_env() returns None unless INCIDENT_PAGE_ENABLED + config present,
        # so the link is simply omitted when the feature is off. Fail-open.
        self._page_signer = (
            page_signer if page_signer is not None else CloudFrontUrlSigner.from_env()
        )

        # Incident-history write path (issue #30). Both are opt-in via env:
        # EmbeddingClient.from_env() is None unless INCIDENT_HISTORY_ENABLED;
        # IncidentHistoryStore.from_env() is None unless TRACES_TABLE_NAME. When
        # either is None the Phase 8 write is skipped — no embedding, no record.
        self._embedding_client = (
            embedding_client if embedding_client is not None else EmbeddingClient.from_env()
        )
        self._history_store = (
            history_store if history_store is not None else IncidentHistoryStore.from_env()
        )

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

        # --- Phase 0.5: routing — which agents to dispatch + per-agent hints --
        # Fail-open: routing disabled or any error → dispatch every active
        # agent (today's behavior). Skipped agents render distinctly in the
        # report. Routing runs before the announce so the kick-off notice
        # reflects the agents we're actually querying; its latency is spent out
        # of the 60s window and is captured by the elapsed math below.
        routing = await self._route(alert_context)
        dispatched_agents = routing.selected_ids
        skipped_agents = routing.skipped

        # --- Phase 0: announce which agents will be queried ------------------
        # Fire-and-forget so fan-out starts immediately; a slow chat post
        # would otherwise add an RTT to every investigation. The "started"
        # notice lists only agents we're actively dispatching to — disabled-
        # in-config and router-skipped agents only appear in the Incident
        # Report's Evidence section, not the kick-off announcement.
        if dispatched_agents:
            started_sections = self.report_formatter.build_started_sections(
                alert_context, dispatched_agents,
            )
            asyncio.create_task(
                self._post_started_notice(platform, target, started_sections),
                name=f"started-notice-{alert_context.investigation_id}",
            )

        # --- Phase 1: fan-out ------------------------------------------------
        # Each selected agent receives the alert with its router-supplied hint
        # injected onto the payload; the rest of the active roster is skipped.
        pending = self._fanout.dispatch(
            lambda agent_id: self._invoke_agent_safe(
                agent_id, self._with_hint(alert_context, routing.hints.get(agent_id))
            ),
            agent_ids=dispatched_agents,
        )

        # --- Phase 2: wait up to INITIAL_DEADLINE_SECONDS --------------------
        # When synthesis is active, reserve its time budget out of the initial
        # window so the LLM call still lands the report inside the 60s deadline.
        synthesis_budget = (
            self._synthesizer.timeout_seconds if self._synthesizer is not None else 0.0
        )
        elapsed = asyncio.get_event_loop().time() - start_time
        initial_timeout = max(
            0, self.INITIAL_DEADLINE_SECONDS - elapsed - synthesis_budget
        )
        settled, pending = await self._fanout.harvest(pending, initial_timeout)
        self._merge(settled, results)

        # --- Phase 3: synthesize and post initial report ---------------------
        # Agents still pending at the deadline are reported as in-progress;
        # their late results trigger an enrichment update in Phase 4.
        pending_ids = set(pending)

        analysis = await self._synthesize_analysis(alert_context, results)

        page_url = (
            self._page_signer.sign(alert_context.investigation_id)
            if self._page_signer is not None
            else None
        )
        report_sections = self.report_formatter.build_incident_sections(
            alert_context,
            results,
            pending_agents=pending_ids,
            disabled_agents=self.disabled_agents,
            analysis=analysis,
            skipped_agents=skipped_agents,
            interactive_page_url=page_url,
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

        # --- Stage 2: one bounded follow-up round ----------------------------
        # Fail-open and hard-capped: at most one extra dispatch to ≤N agents,
        # only when the remaining cutoff budget can absorb it. New tasks merge
        # into `pending` and land through the Phase 4 enrichment path below;
        # the 5-minute cutoff is held by that loop's deadline + cancel.
        pending, followup_dispatched = await self._maybe_followup(
            alert_context, results, pending, start_time
        )
        dispatched_agents = sorted(set(dispatched_agents) | followup_dispatched)

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
                # crossed the 60s mark is still surfaced. Re-synthesize over
                # everything gathered so far so the enrichment carries an
                # updated analysis (fail-open: None → no Analysis block).
                analysis = await self._synthesize_analysis(alert_context, results)
                try:
                    enrichment_sections = self.report_formatter.build_enrichment_sections(
                        source_agent=agent_id,
                        new_findings=result,
                        initial_report_summary=initial_report_summary,
                        variant_label=alert_context.variant_label,
                        analysis=analysis,
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
            routing=routing.manifest_record,
        )

        # Snapshot the series behind chart-carrying findings (#32). Same
        # fail-open contract as the manifest write above.
        self._snapshot_charts(alert_context=alert_context, results=results)

        # Write the #33 page model last in Phase 7 — after the manifest and the
        # chart series — so its S3 ObjectCreated event triggers the renderer only
        # once the charts it references already exist. Fail-open.
        self._write_page_model(
            alert_context=alert_context, results=results, analysis=analysis,
        )

        # --- Phase 8: record the incident outcome for similar-incident lookup ---
        # Embeds the alert + stores a compact record (issue #30) so a future,
        # similar alert can surface this one. Fail-open and skipped entirely
        # when history isn't configured. Runs after the report is posted, so
        # the embedding call never delays the user-facing report.
        self._record_incident_outcome(
            alert_context=alert_context,
            analysis=analysis,
            report_summary=initial_report_summary,
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

    async def _synthesize_analysis(
        self,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
    ) -> AnalysisSection | None:
        """Run the LLM synthesis over the results so far, or ``None``.

        Returns ``None`` when synthesis is disabled or the call fails — the
        synthesizer is itself fail-open, so this never raises.
        """
        if self._synthesizer is None:
            return None
        analysis = await self._synthesizer.synthesize(alert_context, results)
        return _to_analysis_section(analysis)

    # ------------------------------------------------------------------
    # Routing + follow-up (issue #28)
    # ------------------------------------------------------------------

    async def _route(self, alert_context: AlertContext) -> _RoutingPlan:
        """Resolve which active agents to dispatch and their per-agent hints.

        Fail-open: with routing disabled, or on any router failure / a decision
        that would skip every agent, returns a plan that dispatches every
        active agent with no hints — exactly today's behavior. The routing
        decision (when present) is written to the trace archive.
        """
        all_ids = list(self.agent_endpoints)
        if self._router is None:
            return _RoutingPlan(all_ids, {}, {}, None)

        candidates = [
            AgentCandidate(aid, self._candidate_description(aid)) for aid in all_ids
        ]
        result: RoutingResult | None = await self._router.route(alert_context, candidates)
        if result is None:
            return _RoutingPlan(all_ids, {}, {}, None)

        selected_ids = [aid for aid in all_ids if aid in result.selected]
        record = {
            "selected": result.selected,
            "skipped": result.skipped,
            "rationale": result.rationale,
        }
        self._put_trace_event(alert_context, EVENT_ROUTING_DECISION, record)
        return _RoutingPlan(selected_ids, result.selected, result.skipped, record)

    def _candidate_description(self, agent_id: str) -> str:
        """Short role blurb for the router/follow-up prompt, from the registry."""
        try:
            return self._registry.lookup(agent_id).display_name
        except KeyError:
            return agent_id

    @staticmethod
    def _with_hint(alert_context: AlertContext, hint: str | None) -> AlertContext:
        """Return the alert with a per-agent investigation hint injected, if any."""
        if not hint:
            return alert_context
        return replace(alert_context, investigation_hints=hint)

    async def _maybe_followup(
        self,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
        pending: dict[str, asyncio.Task[AgentResult]],
        start_time: float,
    ) -> tuple[dict[str, asyncio.Task[AgentResult]], set[str]]:
        """Run at most one bounded follow-up round, fail-open.

        Returns the (possibly extended) ``pending`` map and the set of agents
        the follow-up dispatched. Skips entirely when follow-up is disabled,
        when the remaining cutoff budget is too small, or when there are no
        eligible (not-currently-in-flight) candidates. The planning call is
        bounded by the remaining budget, and the dispatched tasks are harvested
        by the Phase 4 cutoff loop — so the 5-minute deadline always holds.
        """
        if self._followup is None:
            return pending, set()

        elapsed = asyncio.get_event_loop().time() - start_time
        remaining = self.HARD_CUTOFF_SECONDS - elapsed
        if remaining <= _MIN_FOLLOWUP_BUDGET_SECONDS:
            return pending, set()

        # Eligible candidates are active agents not currently in flight —
        # re-dispatching a still-pending agent would clobber its live task.
        candidate_ids = [aid for aid in self.agent_endpoints if aid not in pending]
        if not candidate_ids:
            return pending, set()
        candidates = [
            FollowupCandidate(aid, self._candidate_description(aid))
            for aid in candidate_ids
        ]

        try:
            plan = await asyncio.wait_for(
                self._followup.plan(alert_context, results, candidates),
                timeout=remaining,
            )
        except Exception:
            logger.warning(
                "Follow-up planning exceeded the remaining budget for "
                "investigation %s; skipping the round.",
                alert_context.investigation_id,
                exc_info=True,
            )
            return pending, set()

        if not plan:
            return pending, set()

        self._put_trace_event(
            alert_context,
            EVENT_FOLLOWUP_DECISION,
            {"dispatches": [{"agent_id": aid, "hint": hint} for aid, hint in plan]},
        )

        dispatched: set[str] = set()
        for agent_id, hint in plan:
            new_tasks = self._fanout.dispatch(
                lambda aid, _hint=hint: self._invoke_agent_safe(
                    aid, self._with_hint(alert_context, _hint)
                ),
                agent_ids=[agent_id],
            )
            pending.update(new_tasks)
            dispatched.add(agent_id)
        return pending, dispatched

    def _put_trace_event(
        self, alert_context: AlertContext, event_type: str, payload: dict
    ) -> None:
        """Write a routing/follow-up decision event to the trace archive (no-op
        when tracing is disabled). The trace store swallows its own errors."""
        if self._trace_store is None:
            return
        self._trace_store.put_event(
            investigation_id=alert_context.investigation_id,
            source=SOURCE_MASTER,
            event_type=event_type,
            payload=payload,
        )

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
        total_cost, total_tokens = _sum_agent_telemetry(results)
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
                total_cost_usd=total_cost,
                total_tokens=total_tokens,
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
        routing: dict | None = None,
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
            routing=routing,
            timeline=self.report_formatter.build_timeline(
                alert_context, results
            ).to_json_dict()["events"],
        )
        self._trace_store.put_manifest(manifest)

    def _snapshot_charts(
        self,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
    ) -> None:
        """Write the series behind every descriptor-carrying finding to S3.

        Approach A (#32): specialized agents ship the rows they already
        harvested on :attr:`AgentResult.chart_series`; the master persists each
        once under ``charts/<chart_id>.json`` so the interactive incident page
        (#33) can draw graphs from an immutable record. Runs in Phase 7, after
        the report is posted, so it never delays the user-facing report.

        Fail-open: ``self._trace_store`` may be ``None`` (tracing disabled),
        and every store write swallows its own errors — this never raises.
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
                    "investigation_id": alert_context.investigation_id,
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
                    investigation_id=alert_context.investigation_id,
                    chart_id=chart_id,
                    payload=payload,
                )

    def _write_page_model(
        self,
        *,
        alert_context: AlertContext,
        results: dict[str, AgentResult | AgentFailure],
        analysis: AnalysisSection | None,
    ) -> None:
        """Build + persist the #33 page model (render trigger). Fail-open."""
        if self._trace_store is None:
            return
        model = self.report_formatter.build_page_model(
            alert_context, results, analysis=analysis,
        )
        self._trace_store.put_page_model(
            investigation_id=alert_context.investigation_id,
            payload=model.to_json_dict(),
        )

    def _record_incident_outcome(
        self,
        *,
        alert_context: AlertContext,
        analysis: AnalysisSection | None,
        report_summary: str,
    ) -> None:
        """Embed the alert and store a compact outcome record (issue #30).

        Fail-open and a no-op unless both the embedding client and the history
        store are configured. The root cause comes from the synthesized
        Analysis section (#27); the summary prefers the analysis correlation,
        falling back to a truncated report. A vector that can't be produced
        means the record would never be searchable, so the write is skipped.
        """
        if self._embedding_client is None or self._history_store is None:
            return
        try:
            embedding = self._embedding_client.embed(alert_context.alert_text)
            if embedding is None:
                return
            root_cause = analysis.root_cause_hypothesis if analysis else None
            summary = (
                (analysis.correlation if analysis else "")
                or report_summary[:_OUTCOME_SUMMARY_CHARS]
            )
            self._history_store.put_outcome(
                IncidentOutcome(
                    investigation_id=alert_context.investigation_id,
                    alert_text=alert_context.alert_text,
                    summary=summary,
                    embedding=embedding,
                    platform=alert_context.platform,
                    channel_id=alert_context.channel_id,
                    message_id=alert_context.message_id,
                    alert_timestamp=alert_context.alert_timestamp,
                    root_cause=root_cause,
                )
            )
        except Exception:
            logger.exception(
                "Failed to record incident outcome for investigation %s",
                alert_context.investigation_id,
            )

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
