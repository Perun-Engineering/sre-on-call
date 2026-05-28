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
import os
import time
from dataclasses import asdict, replace
from typing import Any, Protocol

from shared.a2a_protocol import build_a2a_request, extract_response_text
from shared.agent_telemetry import extract_metadata
from shared.agents import Agent, AgentRegistry, get_registry
from shared.constants import HARD_CUTOFF_SECONDS, INITIAL_DEADLINE_SECONDS
from shared.models import AgentFailure, AgentMetadata, AgentResult, AlertContext, Finding
from shared.time_utils import now_iso
from shared.platforms import ChatPlatform, deliver_with_retry, for_platform
from shared.experiment import ExperimentResult
from shared.experiment_results_store import ExperimentResultsStore
from shared.tool_result import extract_agent_result
from agents.master.report_formatter import ReportFormatter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP client protocol (for dependency injection / mocking)
# ---------------------------------------------------------------------------


class AsyncHTTPClient(Protocol):
    """Minimal async HTTP client interface for A2A calls."""

    async def post_json(self, url: str, payload: dict) -> dict:
        """POST *payload* as JSON to *url* and return the parsed response."""
        ...  # pragma: no cover


class AiohttpClient:
    """Default :class:`AsyncHTTPClient` backed by ``aiohttp``."""

    async def post_json(self, url: str, payload: dict) -> dict:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=HARD_CUTOFF_SECONDS),
            ) as resp:
                return await resp.json()


class AgentCoreClient:
    """:class:`AsyncHTTPClient` that invokes Bedrock AgentCore runtimes.

    ``url`` is interpreted as an Agent Runtime ARN. Use this client when
    the orchestrator's per-agent ``*_AGENT_RUNTIME_ARN`` env vars are set
    in deployed environments. Local-dev paths can still use
    :class:`AiohttpClient`.
    """

    def __init__(self, *, client: Any = None, region_name: str | None = None):
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client(
                "bedrock-agentcore",
                region_name=region_name or os.environ.get("AWS_REGION", "us-east-1"),
            )

    async def post_json(self, url: str, payload: dict) -> dict:
        response = await asyncio.to_thread(
            self._client.invoke_agent_runtime,
            agentRuntimeArn=url,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        body = response["response"]
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return json.loads(body)


# ---------------------------------------------------------------------------
# A2A JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------


def _serialize_alert_context(alert_context: AlertContext) -> str:
    """Serialize an :class:`AlertContext` to a JSON string for A2A transport."""
    return json.dumps(asdict(alert_context))


def _parse_agent_result(
    agent_name: str,
    response: dict,
    base_metadata: AgentMetadata | None = None,
) -> AgentResult:
    """Parse an A2A JSON-RPC response into an :class:`AgentResult`.

    ``base_metadata`` carries the orchestrator-owned wall-clock window;
    fields populated by the agent's footer (model, tokens, cost) overlay it.
    """
    base_metadata = base_metadata or AgentMetadata()
    try:
        if "error" in response:
            error_msg = response["error"].get("message", "Unknown A2A error")
            return AgentResult(
                agent_name=agent_name,
                status="error",
                findings=[],
                summary="",
                error_message=error_msg,
                metadata=base_metadata,
            )

        result_data = response.get("result", {})
        raw_summary = extract_response_text(result_data)
        clean_summary, structured = extract_agent_result(raw_summary)
        clean_summary, footer_metadata = extract_metadata(clean_summary)
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
    except Exception as exc:
        return AgentResult(
            agent_name=agent_name,
            status="error",
            findings=[],
            summary="",
            error_message=f"Failed to parse agent response: {exc}",
            metadata=base_metadata,
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
    ) -> None:
        self._chat_platform: ChatPlatform | None = chat_platform
        self._registry: AgentRegistry = registry or get_registry()
        self.report_formatter = report_formatter or ReportFormatter(self._registry)

        active_specialized = self._registry.active(kind="specialized")
        self.agent_endpoints: dict[str, str] = {
            a.id: a.resolve_endpoint() for a in active_specialized
        }
        self.disabled_agents: set[str] = {
            a.id for a in self._registry.disabled_in_config(kind="specialized")
        }

        if http_client is None:
            any_arn = any(
                ep.startswith("arn:") for ep in self.agent_endpoints.values()
            )
            http_client = AgentCoreClient() if any_arn else AiohttpClient()
        self.http_client: AsyncHTTPClient = http_client
        self._results_store = results_store

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    def _get_platform(self, alert_context: AlertContext) -> ChatPlatform:
        """Return the ChatPlatform, selecting from alert_context.platform when not injected."""
        if self._chat_platform is not None:
            return self._chat_platform
        return for_platform(alert_context.platform)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def investigate(self, alert_context: AlertContext) -> None:
        """Run the full investigation lifecycle."""
        start_time = asyncio.get_event_loop().time()
        results: dict[str, AgentResult | AgentFailure] = {}
        initial_report_summary = ""
        platform = self._get_platform(alert_context)

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
                self._post_started_notice(platform, alert_context, started_sections),
                name=f"started-notice-{alert_context.investigation_id}",
            )

        # --- Phase 1: fan-out ------------------------------------------------
        task_map: dict[str, asyncio.Task[AgentResult]] = {}
        for agent_id in self.agent_endpoints:
            task = asyncio.create_task(
                self._invoke_agent_safe(agent_id, alert_context),
                name=f"invoke-{agent_id}",
            )
            task_map[agent_id] = task

        task_to_agent: dict[asyncio.Task[AgentResult], str] = {
            t: aid for aid, t in task_map.items()
        }

        pending: set[asyncio.Task[AgentResult]] = set(task_map.values())

        # --- Phase 2: wait up to INITIAL_DEADLINE_SECONDS --------------------
        elapsed = asyncio.get_event_loop().time() - start_time
        initial_timeout = max(0, self.INITIAL_DEADLINE_SECONDS - elapsed)

        if pending:
            done, pending = await asyncio.wait(
                pending, timeout=initial_timeout
            )
            self._collect_done(done, task_to_agent, results)

        # --- Phase 3: synthesize and post initial report ---------------------
        # Agents still pending at the deadline are reported as in-progress;
        # their late results trigger an enrichment update in Phase 4.
        pending_ids = {task_to_agent[t] for t in pending}

        report_sections = self.report_formatter.build_incident_sections(
            alert_context,
            results,
            pending_agents=pending_ids,
            disabled_agents=self.disabled_agents,
        )

        try:
            initial_report_summary = await deliver_with_retry(
                platform, alert_context, report_sections,
            )
        except Exception:
            logger.exception(
                "Failed to post initial Incident Report for investigation %s "
                "(all retries exhausted).",
                alert_context.investigation_id,
            )

        # --- Phase 4: accept late results until HARD_CUTOFF_SECONDS ----------
        while pending:
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = self.HARD_CUTOFF_SECONDS - elapsed
            if remaining <= 0:
                break

            done, pending = await asyncio.wait(
                pending, timeout=remaining
            )
            late_results = self._collect_done(done, task_to_agent, results)

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
                        platform, alert_context, enrichment_sections,
                    )
                except Exception:
                    logger.exception(
                        "Failed to post enrichment update for %s in investigation %s "
                        "(all retries exhausted)",
                        agent_id,
                        alert_context.investigation_id,
                    )

        # --- Phase 5: terminate — cancel any still-pending tasks -------------
        for task in pending:
            task.cancel()

        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

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

    # ------------------------------------------------------------------
    # Agent invocation
    # ------------------------------------------------------------------

    async def invoke_agent(
        self, agent_id: str, alert_context: AlertContext
    ) -> AgentResult:
        """Send an A2A JSON-RPC ``message/send`` to a specialized agent."""
        endpoint = self.agent_endpoints[agent_id]
        payload = build_a2a_request(
            text=_serialize_alert_context(alert_context),
            request_id=f"req-{agent_id}-{alert_context.investigation_id}",
        )

        started_at = now_iso()
        start = time.monotonic()
        response = await self.http_client.post_json(endpoint, payload)
        duration = time.monotonic() - start

        base_metadata = AgentMetadata(started_at=started_at, completed_at=now_iso())
        result = _parse_agent_result(agent_id, response, base_metadata)
        result.duration_seconds = duration
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _post_started_notice(
        self,
        platform: ChatPlatform,
        alert_context: AlertContext,
        sections,
    ) -> None:
        try:
            await deliver_with_retry(platform, alert_context, sections)
        except Exception:
            logger.exception(
                "Failed to post investigation-started notice for %s",
                alert_context.investigation_id,
            )

    async def _invoke_agent_safe(
        self, agent_id: str, alert_context: AlertContext
    ) -> AgentResult:
        """Invoke an agent, catching exceptions and returning an error result."""
        started_at = now_iso()
        try:
            return await self.invoke_agent(agent_id, alert_context)
        except Exception as exc:
            logger.exception("Agent %s invocation failed", agent_id)
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

    @staticmethod
    def _collect_done(
        done: set[asyncio.Task[AgentResult]],
        task_to_agent: dict[asyncio.Task[AgentResult], str],
        results: dict[str, AgentResult | AgentFailure],
    ) -> dict[str, AgentResult | AgentFailure]:
        """Harvest completed tasks into *results* and return the new entries."""
        new_entries: dict[str, AgentResult | AgentFailure] = {}
        for task in done:
            agent_id = task_to_agent.get(task)
            if agent_id is None:
                continue
            try:
                result = task.result()
                results[agent_id] = result
                new_entries[agent_id] = result
            except Exception as exc:
                failure = AgentFailure(
                    agent_name=agent_id,
                    error_message=str(exc),
                    timestamp="",
                )
                results[agent_id] = failure
                new_entries[agent_id] = failure
        return new_entries
