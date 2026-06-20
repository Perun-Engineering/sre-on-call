"""Master agent's status-snapshot orchestrator — the engine behind ``/sre-snapshot``.

Differs materially from :class:`agents.master.orchestrator.InvestigationOrchestrator`:

* **Single 30-second hard cutoff** — no late-enrichment phase. Operators
  staring at a chat channel waiting for ``/sre-snapshot`` results don't want to
  see two follow-up posts; one snapshot is the deliverable.
* **Synthesises the master's own block** — the master itself contributes a
  section to the snapshot (registry view + configured model), built
  synchronously without any A2A fan-out for itself.
* **Snapshot-shaped fan-out** — A2A request payload is
  ``{"task": "snapshot", "requested_at": <ISO>}``, not a serialised
  :class:`AlertContext`. Specialized agents return their snapshot
  via a ``<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>`` footer that the
  orchestrator extracts via :data:`shared.tool_result.SNAPSHOT_RESULT`.
* **Posts a :class:`SnapshotSections` payload** at top-level (not a thread
  reply) — ``/sre-snapshot`` is a deliberate operational broadcast, not an
  incident-thread reply.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.a2a_client import AsyncHTTPClient
from shared.a2a_factory import _resolve_agent_model_id
from shared.agents import Agent, AgentRegistry, get_registry
from shared.config import ProjectConfig
from shared.fanout import Fanout
from shared.models import SnapshotReport, SnapshotSection
from shared.platforms import ChatPlatform, DeliveryTarget, deliver_with_retry, for_platform
from shared.report_renderer import SnapshotBlock, SnapshotSections
from shared.time_utils import now_iso
from shared.tool_result import SNAPSHOT_RESULT

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

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        *,
        project_config: ProjectConfig | None = None,
    ) -> None:
        self._registry = registry or get_registry()
        # Default to the registry's own config so the header model resolves
        # against the same config the catalogue was folded from (issue #81).
        self._project_config = project_config or self._registry.project_config

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

    def _resolve_model_id(self) -> str:
        # Reuse the dispatch precedence (per-agent config model_id > MODEL_ID env
        # > defaults.model_id > bundled default) so the header reflects the model
        # the master actually runs on, not the deploy-wide Haiku env (issue #81).
        return _resolve_agent_model_id(self._project_config, "master")

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
    """Orchestrates a ``/sre-snapshot`` snapshot across active specialized agents.

    Lifecycle:
        1. Build a top-level :class:`DeliveryTarget` from the snapshot
           request (``thread_anchor=None`` — a broadcast, not a reply).
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
        *,
        project_config: ProjectConfig | None = None,
    ) -> None:
        self._chat_platform = chat_platform
        self._registry = registry or get_registry()
        # Default to the registry's own config so header model resolution and
        # the catalogue agree on a single config (issue #81).
        self._project_config = project_config or self._registry.project_config
        self._master_builder = master_builder or MasterSnapshotBuilder(
            self._registry, project_config=self._project_config
        )

        self.disabled_agents: list[Agent] = list(
            self._registry.disabled_in_config(kind="specialized")
        )

        # The Fanout owns endpoint resolution + per-endpoint transport routing
        # + the one A2AClient. agent_endpoints / http_client / _client are
        # pass-throughs so existing call sites and tests keep working.
        self._fanout = Fanout(http_client=http_client, registry=self._registry)
        self.agent_endpoints: dict[str, str] = self._fanout.agent_endpoints
        self.http_client = self._fanout.http_client
        self._client = self._fanout.client

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture(self, snapshot_request: dict[str, Any]) -> None:
        """Run the full ``/sre-snapshot`` snapshot lifecycle."""
        requested_at = snapshot_request.get("requested_at") or now_iso()
        platform = self._get_platform(snapshot_request["platform"])
        target = DeliveryTarget(
            platform=snapshot_request["platform"],
            channel_id=snapshot_request["channel_id"],
            thread_anchor=None,  # /sre-snapshot is a top-level broadcast, not a reply
        )

        # Phase 1: master's own block (synchronous)
        master_block = self._master_builder.build()

        # Phase 2: fan out
        pending = self._fanout.dispatch(
            lambda agent_id: self._invoke_snapshot_safe(agent_id, requested_at)
        )

        # Phase 3: single harvest at the hard cutoff (no late-enrichment phase)
        settled, pending = await self._fanout.harvest(pending, self.HARD_CUTOFF_SECONDS)

        # Phase 4: build per-agent blocks in registry render order
        agent_blocks: list[SnapshotBlock] = []
        for agent in self._fanout.targets:
            if agent.id in pending:
                agent_blocks.append(
                    self._error_block(agent, f"no response within {int(self.HARD_CUTOFF_SECONDS)}s")
                )
                continue
            if agent.id not in settled:
                continue
            value = settled[agent.id]
            if isinstance(value, BaseException):
                logger.exception(
                    "snapshot fan-out failed for %s", agent.id, exc_info=value
                )
                agent_blocks.append(self._error_block(agent, str(value)))
                continue
            if value is None:
                agent_blocks.append(self._error_block(agent, "agent returned no snapshot footer"))
                continue
            agent_blocks.append(self._block_from_report(agent, value))

        # Cancel + drain anything still pending
        await self._fanout.cancel(pending)

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
            await deliver_with_retry(platform, target, sections)
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
        reply = await self._client.send(
            self.agent_endpoints[agent_id],
            json.dumps({"task": "snapshot", "requested_at": requested_at}),
            footer=SNAPSHOT_RESULT,
            request_id=f"req-snapshot-{agent_id}-{requested_at}",
        )
        if reply.error is not None:
            raise RuntimeError(f"a2a error from {agent_id}: {reply.error}")
        return reply.payload

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

    def _agent_header_line(self, agent: Agent, report: SnapshotReport | None) -> str:
        # Prefer the model id reported by the agent itself (the model it actually
        # dispatched on, surfaced via SnapshotReport.metadata) — otherwise fall
        # back to the agent's *resolved per-agent config model*, using the same
        # precedence as real dispatch (per-agent config model_id > MODEL_ID env >
        # defaults.model_id > bundled default), not the deploy-wide Haiku env
        # (issue #81). Skills come straight from the registry (config.yaml is the
        # source of truth there).
        model_id = (
            report.metadata.model_id if report and report.metadata else None
        ) or _resolve_agent_model_id(self._project_config, agent.id)
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

    def _get_platform(self, platform_name: str) -> ChatPlatform:
        if self._chat_platform is not None:
            return self._chat_platform
        return for_platform(platform_name)
