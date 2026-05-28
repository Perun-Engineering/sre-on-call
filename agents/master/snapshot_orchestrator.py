"""Master agent's status-snapshot orchestrator — the engine behind ``/status``.

Differs materially from :class:`agents.master.orchestrator.InvestigationOrchestrator`:

* **Single 30-second hard cutoff** — no late-enrichment phase. Operators
  staring at a chat channel waiting for ``/status`` results don't want to
  see two follow-up posts; one snapshot is the deliverable.
* **Synthesises the master's own block** — the master itself contributes a
  section to the snapshot (registry view + configured model), built
  synchronously without any A2A fan-out for itself.
* **Snapshot-shaped fan-out** — A2A request payload is
  ``{"task": "snapshot", "requested_at": <ISO>}``, not a serialised
  :class:`AlertContext`. Specialized agents return their snapshot
  via a ``<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>`` footer that the
  orchestrator extracts via :func:`shared.tool_result.extract_snapshot_report`.
* **Posts a :class:`SnapshotSections` payload** at top-level (not a thread
  reply) — ``/status`` is a deliberate operational broadcast, not an
  incident-thread reply.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from agents.master.orchestrator import AgentCoreClient, AiohttpClient, AsyncHTTPClient
from shared.a2a_protocol import build_a2a_request, extract_response_text
from shared.agents import Agent, AgentRegistry, get_registry
from shared.models import AlertContext, SnapshotReport, SnapshotSection
from shared.platforms import ChatPlatform, deliver_with_retry, for_platform
from shared.report_renderer import SnapshotBlock, SnapshotSections
from shared.time_utils import now_iso
from shared.tool_result import extract_snapshot_report

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ---------------------------------------------------------------------------
# Master section builder
# ---------------------------------------------------------------------------


class MasterSnapshotBuilder:
    """Builds the master agent's own block in the snapshot.

    For the slice-3 baseline this is registry-only — every catalogue entry
    rendered with its current state badge, plus a header line carrying
    the master's configured model and skills. Later slices can layer in
    AgentCore runtime status (``bedrock-agentcore:GetAgentRuntime``),
    DynamoDB table reachability, and active-experiment lookup once the
    IAM policies catch up in Terraform.
    """

    def __init__(self, registry: AgentRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def build(self) -> SnapshotBlock:
        master = self._registry.lookup("master")
        model_id = self._resolve_model_id()
        registry_lines = self._build_registry_lines()

        sections = [
            SnapshotSection(label="Agent registry", lines=registry_lines),
        ]
        return SnapshotBlock(
            emoji=master.emoji,
            display_name=master.display_name,
            header_line=self._header_line(master, model_id),
            sections=sections,
            status="ok",
        )

    @staticmethod
    def _resolve_model_id() -> str:
        return os.environ.get("MODEL_ID") or DEFAULT_MODEL_ID

    @staticmethod
    def _header_line(master: Agent, model_id: str) -> str:
        skills = ", ".join(master.skills or [])
        network = master.network_mode or "PUBLIC"
        skill_part = f" · skills={skills}" if skills else ""
        return f"model={model_id} · network={network}{skill_part}"

    def _build_registry_lines(self) -> list[str]:
        """List every agent in the catalogue (excluding master itself) with a state badge."""
        lines: list[str] = []
        for agent in self._registry.all():
            if agent.id == "master":
                continue
            if agent.is_active:
                badge = "🟢 active"
            elif agent.deployed:
                badge = "🟫 disabled in config.yaml"
            else:
                badge = "⚪ not deployed"
            lines.append(f"{agent.emoji} {agent.display_name}: {badge}")
        return lines


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class StatusSnapshotOrchestrator:
    """Orchestrates a ``/status`` snapshot across active specialized agents.

    Lifecycle:
        1. Synthesise a chat-platform routing context from the snapshot
           request (top-level post, no thread).
        2. Build the master's own block (synchronous).
        3. Fan out an A2A snapshot request to every active specialized
           agent.
        4. Wait once for up to ``HARD_CUTOFF_SECONDS``. Cancel anything
           still running afterwards.
        5. Build a per-agent :class:`SnapshotBlock` from each result.
        6. Append disabled-in-config blocks (🚫).
        7. Compose a deterministic top-line summary, build
           :class:`SnapshotSections`, and post via the chat platform.
    """

    HARD_CUTOFF_SECONDS: float = 30.0

    def __init__(
        self,
        http_client: AsyncHTTPClient | None = None,
        chat_platform: ChatPlatform | None = None,
        registry: AgentRegistry | None = None,
        master_builder: MasterSnapshotBuilder | None = None,
    ) -> None:
        self._chat_platform = chat_platform
        self._registry = registry or get_registry()
        self._master_builder = master_builder or MasterSnapshotBuilder(self._registry)

        active_specialized = self._registry.active(kind="specialized")
        self.agent_endpoints: dict[str, str] = {
            a.id: a.resolve_endpoint() for a in active_specialized
        }
        self.disabled_agents: list[Agent] = list(
            self._registry.disabled_in_config(kind="specialized")
        )

        if http_client is None:
            any_arn = any(
                ep.startswith("arn:") for ep in self.agent_endpoints.values()
            )
            http_client = AgentCoreClient() if any_arn else AiohttpClient()
        self.http_client: AsyncHTTPClient = http_client

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture(self, snapshot_request: dict[str, Any]) -> None:
        """Run the full ``/status`` snapshot lifecycle."""
        requested_at = snapshot_request.get("requested_at") or now_iso()
        platform = self._get_platform(snapshot_request)
        delivery_ctx = self._synthetic_context(snapshot_request, requested_at)

        # Phase 1: master's own block (synchronous)
        master_block = self._master_builder.build()

        # Phase 2: fan out
        task_map: dict[str, asyncio.Task[SnapshotReport | None]] = {}
        for agent_id in self.agent_endpoints:
            task = asyncio.create_task(
                self._invoke_snapshot_safe(agent_id, requested_at),
                name=f"snapshot-{agent_id}",
            )
            task_map[agent_id] = task

        # Phase 3: 30s hard cutoff, single shot
        if task_map:
            done, pending = await asyncio.wait(
                set(task_map.values()),
                timeout=self.HARD_CUTOFF_SECONDS,
            )
        else:
            done, pending = set(), set()

        # Phase 4: build per-agent blocks
        agent_blocks: list[SnapshotBlock] = []
        ordered_active = sorted(
            self._registry.active(kind="specialized"), key=lambda a: a.order
        )
        for agent in ordered_active:
            agent_id = agent.id
            task = task_map.get(agent_id)
            if task is None:
                continue
            if task in pending:
                task.cancel()
                agent_blocks.append(
                    self._error_block(agent, f"no response within {int(self.HARD_CUTOFF_SECONDS)}s")
                )
                continue
            try:
                report = task.result()
            except Exception as exc:
                logger.exception("snapshot fan-out failed for %s", agent_id)
                agent_blocks.append(self._error_block(agent, str(exc)))
                continue
            if report is None:
                agent_blocks.append(self._error_block(agent, "agent returned no snapshot footer"))
                continue
            agent_blocks.append(self._block_from_report(agent, report))

        # Drain cancelled tasks
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Phase 5: disabled-in-config blocks
        for agent in sorted(self.disabled_agents, key=lambda a: a.order):
            agent_blocks.append(self._disabled_block(agent))

        # Phase 6: compose + deliver
        all_blocks = [master_block] + agent_blocks
        sections = SnapshotSections(
            requested_at=requested_at,
            summary_line=self._build_summary_line(all_blocks),
            blocks=all_blocks,
        )

        try:
            await deliver_with_retry(platform, delivery_ctx, sections)
        except Exception:
            logger.exception(
                "Failed to post status snapshot for requested_at=%s", requested_at
            )

    # ------------------------------------------------------------------
    # Per-agent invocation
    # ------------------------------------------------------------------

    async def _invoke_snapshot_safe(
        self, agent_id: str, requested_at: str
    ) -> SnapshotReport | None:
        endpoint = self.agent_endpoints[agent_id]
        text_payload = json.dumps({"task": "snapshot", "requested_at": requested_at})
        request = build_a2a_request(
            text=text_payload,
            request_id=f"req-snapshot-{agent_id}-{requested_at}",
        )
        response = await self.http_client.post_json(endpoint, request)
        if "error" in response:
            raise RuntimeError(
                f"a2a error from {agent_id}: "
                f"{response['error'].get('message', 'unknown')}"
            )
        result_data = response.get("result", {})
        text = extract_response_text(result_data)
        _, report = extract_snapshot_report(text)
        return report

    # ------------------------------------------------------------------
    # Block builders
    # ------------------------------------------------------------------

    def _block_from_report(self, agent: Agent, report: SnapshotReport) -> SnapshotBlock:
        return SnapshotBlock(
            emoji=agent.emoji,
            display_name=agent.display_name,
            header_line=self._agent_header_line(agent, report),
            sections=list(report.sections),
            status="anomaly" if report.anomaly else "ok",
            anomaly_summary=report.anomaly_summary,
        )

    def _error_block(self, agent: Agent, message: str) -> SnapshotBlock:
        return SnapshotBlock(
            emoji=agent.emoji,
            display_name=agent.display_name,
            header_line=self._agent_header_line(agent, None),
            sections=[],
            status="error",
            error_message=message,
        )

    def _disabled_block(self, agent: Agent) -> SnapshotBlock:
        return SnapshotBlock(
            emoji=agent.emoji,
            display_name=agent.display_name,
            header_line=self._agent_header_line(agent, None),
            sections=[],
            status="disabled",
        )

    @staticmethod
    def _agent_header_line(agent: Agent, report: SnapshotReport | None) -> str:
        # Prefer the model id reported by the agent itself (if surfaced via
        # SnapshotReport.metadata) — falls back to the master's resolved env
        # default. Skills come straight from the registry (config.yaml is the
        # source of truth there).
        model_id = (
            (report.metadata.model_id if report and report.metadata else None)
            or os.environ.get("MODEL_ID")
            or DEFAULT_MODEL_ID
        )
        network = agent.network_mode or "PUBLIC"
        skills = ", ".join(agent.skills or [])
        skill_part = f" · skills={skills}" if skills else ""
        return f"model={model_id} · network={network}{skill_part}"

    # ------------------------------------------------------------------
    # Summary line
    # ------------------------------------------------------------------

    @staticmethod
    def _build_summary_line(blocks: list[SnapshotBlock]) -> str:
        ok = sum(1 for b in blocks if b.status == "ok")
        anomaly = sum(1 for b in blocks if b.status == "anomaly")
        error = sum(1 for b in blocks if b.status == "error")
        responded = ok + anomaly
        attempted = responded + error  # disabled excluded — never tried

        parts = [f"🩺 {responded}/{attempted} agents responded"]
        if anomaly > 0:
            anomaly_summaries = [
                b.anomaly_summary or b.display_name
                for b in blocks
                if b.status == "anomaly"
            ]
            parts.append(f"anomalies: {', '.join(anomaly_summaries)}")
        if error > 0:
            error_names = [b.display_name for b in blocks if b.status == "error"]
            parts.append(f"errors: {', '.join(error_names)}")
        return " · ".join(parts)

    # ------------------------------------------------------------------
    # Platform routing
    # ------------------------------------------------------------------

    def _get_platform(self, snapshot_request: dict[str, Any]) -> ChatPlatform:
        if self._chat_platform is not None:
            return self._chat_platform
        return for_platform(snapshot_request["platform"])

    @staticmethod
    def _synthetic_context(
        snapshot_request: dict[str, Any], requested_at: str
    ) -> AlertContext:
        """Build a routing-only :class:`AlertContext` for snapshot delivery.

        ``message_id=""`` and ``platform_metadata={}`` together signal "post
        at top-level, not as a thread reply." :class:`SlackChatPlatform`
        drops the empty ``thread_ts`` kwarg in that case.
        """
        return AlertContext(
            investigation_id=f"snapshot-{requested_at}",
            platform=snapshot_request["platform"],
            channel_id=snapshot_request["channel_id"],
            message_id="",
            alert_text="",
            alert_timestamp=requested_at,
            investigation_window=(requested_at, requested_at),
            platform_metadata={},
        )
