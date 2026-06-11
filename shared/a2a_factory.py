"""Shared A2A server factory for all investigation agents.

Owns the full agent lifecycle: look up the agent's entry in ``config.yaml``
→ resolve attached SKILL.md bundles via :mod:`shared.skill_loader` →
open external MCP connections via :mod:`shared.mcp_loader` → compose
``system_prompt`` from card prose plus skill bodies → build :class:`strands.Agent`
→ build :class:`A2AServer` (skill catalog generated from resolved bundles)
→ start uvicorn.

Usage:
    python -m shared.a2a_factory agents/eks
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sys

import uvicorn

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill, Part, TextPart
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.multiagent.a2a import A2AServer
from strands.multiagent.a2a.executor import StrandsA2AExecutor

from shared import busy_state
from shared.agent_telemetry import AGENT_METADATA, compute_cost_usd
from shared.models import AgentMetadata
from shared.time_utils import now_iso
from shared.tool_result import AGENT_RESULT

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


class TelemetryCapturingA2AExecutor(StrandsA2AExecutor):
    """A2A executor that appends an :class:`AgentMetadata` footer to responses.

    Wraps the upstream Strands executor and intercepts the final ``result``
    event so we can read ``AgentResult.metrics.accumulated_usage`` and embed
    model + token + cost telemetry in the outgoing text. The orchestrator
    decodes the footer via :data:`shared.agent_telemetry.AGENT_METADATA`.

    Also serialises every invocation through an :class:`asyncio.Lock`.
    Strands' :class:`Agent` is single-threaded conversation state and raises
    ``ConcurrencyException`` on overlapping ``stream_async`` calls; AgentCore
    runs one container per session, so a per-instance lock is the correct
    granularity. AgentCore-edge retries during cold-starts then queue
    cleanly behind the in-flight invoke instead of crashing.
    """

    def __init__(self, agent, *, model_id: str | None = None, **kwargs):
        super().__init__(agent, **kwargs)
        self._model_id = model_id
        self._invocation_lock = asyncio.Lock()

    async def execute(  # type: ignore[override]
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        async with self._invocation_lock:
            await super().execute(context, event_queue)

    async def _handle_agent_result(self, result, updater):  # type: ignore[override]
        if result is not None:
            streamed = (
                self.enable_a2a_compliant_streaming
                and not getattr(self, "_is_first_chunk", True)
            )
            structured_footer = _latest_agent_result_footer(
                getattr(self.agent, "messages", None)
            )
            if structured_footer and structured_footer not in str(result):
                if streamed:
                    await updater.add_artifact(
                        [Part(root=TextPart(text="\n" + structured_footer))],
                        artifact_id=self._current_artifact_id,
                        name="agent_response",
                        append=True,
                    )
                else:
                    content = result.message.setdefault("content", [])
                    content.append({"text": "\n" + structured_footer})

            metadata = self._build_metadata(result)
            footer = AGENT_METADATA.encode(metadata)
            if streamed:
                # In A2A-compliant streaming, the parent has already flushed
                # message content as artifact chunks and only emits an empty
                # terminator. Mutating result.message.content post-stream
                # would silently drop the footer, so push it as its own
                # appended chunk before super() closes the artifact.
                await updater.add_artifact(
                    [Part(root=TextPart(text="\n" + footer))],
                    artifact_id=self._current_artifact_id,
                    name="agent_response",
                    append=True,
                )
            else:
                # Non-streaming or first-chunk path: super() will fall back
                # to ``str(result)`` for the artifact text. AgentResult.__str__
                # concatenates text blocks from message.content, so appending
                # one carries the footer through.
                content = result.message.setdefault("content", [])
                content.append({"text": "\n" + footer})
        await super()._handle_agent_result(result, updater)

    def _build_metadata(self, result) -> AgentMetadata:
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None)
        input_tokens = usage.get("inputTokens") if usage else None
        output_tokens = usage.get("outputTokens") if usage else None
        total_tokens = usage.get("totalTokens") if usage else None
        return AgentMetadata(
            model_id=self._model_id,
            completed_at=now_iso(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=compute_cost_usd(self._model_id, input_tokens, output_tokens),
        )


def _latest_agent_result_footer(messages) -> str | None:
    """Find the newest structured AgentResult footer in recorded tool output."""
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue
            tool_content = tool_result.get("content", [])
            if not isinstance(tool_content, list):
                continue
            for item in reversed(tool_content):
                if not isinstance(item, dict):
                    continue
                footer = AGENT_RESULT.find(item.get("text", ""))
                if footer:
                    return footer
    return None


def _resolve_model(default_model_id: str | None = None) -> BedrockModel:
    """Build a BedrockModel.

    Resolution order for the model ID:
    1. ``MODEL_ID`` env var (deploy-time override).
    2. ``default_model_id`` arg (typically ``ProjectConfig.defaults.model_id``).
    3. ``DEFAULT_MODEL_ID`` module constant (last-resort fallback).

    Defence-in-depth: when ``BEDROCK_GUARDRAIL_ID`` is set, the model is
    bound to that Bedrock Guardrail (prompt-attack / content filtering on
    every invocation). ``BEDROCK_GUARDRAIL_VERSION`` selects the version,
    defaulting to ``DRAFT``. Unset → no guardrail (unchanged behaviour).
    """
    model_id = os.environ.get("MODEL_ID") or default_model_id or DEFAULT_MODEL_ID

    guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID") or None
    if guardrail_id is None:
        return BedrockModel(model_id=model_id)

    return BedrockModel(
        model_id=model_id,
        guardrail_id=guardrail_id,
        guardrail_version=os.environ.get("BEDROCK_GUARDRAIL_VERSION") or "DRAFT",
    )


def _load_card(agent_dir: pathlib.Path) -> dict:
    """Load and return the agent_card.json from *agent_dir*."""
    card_path = agent_dir / "agent_card.json"
    with open(card_path) as fh:
        return json.load(fh)


def _ping_status() -> str:
    """AgentCore health status reported by ``/ping``.

    ``HealthyBusy`` while fire-and-forget background work (master
    investigations/snapshots, tracked via :mod:`shared.busy_state`) is in
    flight, so AgentCore won't reclaim the instance mid-run; ``Healthy``
    otherwise. Specialized agents respond synchronously and register no
    background work, so they always report ``Healthy``.
    """
    return "HealthyBusy" if busy_state.is_busy() else "Healthy"


def agent_main(agent_dir: str | pathlib.Path) -> None:
    """Full agent lifecycle entry point.

    1. Read agent name from the trailing path component of *agent_dir*.
    2. Look up the :class:`shared.agents.Agent` record in the registry
       (which has folded ``config.yaml`` in at load time). Refuse to start
       when the agent is unknown to the catalogue, not deployed in this
       account's ``config.yaml``, or marked ``enabled: false``.
    3. Resolve attached SKILL.md bundles + MCP connections.
    4. Compose system prompt = card.system_prompt + skill bodies.
    5. Build :class:`strands.Agent`.
    6. Start :class:`A2AServer` with skills surfaced from resolved bundles.
    """
    from shared.agents import get_registry
    from shared.config import load as load_config
    from shared import skill_loader, mcp_loader

    agent_path = pathlib.Path(agent_dir).resolve()
    agent_name = agent_path.name

    # Triggers config.yaml load + validation as a side effect; surfaces
    # config errors with a clear message at startup rather than during the
    # first registry query.
    project_config = load_config()

    registry = get_registry()
    try:
        agent_record = registry.lookup(agent_name)
    except KeyError as exc:
        raise RuntimeError(
            f"agent {agent_name!r} is not in the registry catalogue "
            f"(shared/agents.py)"
        ) from exc
    if not agent_record.deployed:
        raise RuntimeError(f"agent {agent_name!r} not in config.yaml")
    if not agent_record.is_active:
        raise RuntimeError(f"agent {agent_name!r} is disabled in config.yaml")

    card = _load_card(agent_path)

    # Resolve skills: gather Skill objects, import their @tool functions,
    # and append their bodies to the agent's base system_prompt.
    skills_resolved = [
        skill_loader.resolve(name, agent_name) for name in (agent_record.skills or [])
    ]
    skill_tools = [skill_loader.import_tool(s.tool_symbol) for s in skills_resolved]

    # MCP connections live for the uvicorn server's lifetime. uvicorn.run
    # blocks until shutdown, so the `with` cleanly tears down all MCP
    # transports (HTTP sessions, stdio subprocesses) on SIGTERM/SIGINT.
    with mcp_loader.open(agent_record.mcps or []) as mcp_handle:
        tools = skill_tools + list(mcp_handle.tools)

        system_prompt = _compose_system_prompt(card["system_prompt"], skills_resolved)

        model = _resolve_model(default_model_id=project_config.defaults.model_id)
        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            name=card["name"],
            description=card["description"],
        )

        host = os.environ.get("A2A_HOST", "0.0.0.0")
        port = int(os.environ.get("A2A_PORT", "9000"))

        server = A2AServer(
            agent=agent,
            host=host,
            port=port,
            skills=_skills_from_bundles(skills_resolved),
            enable_a2a_compliant_streaming=True,
        )
        server.request_handler.agent_executor = TelemetryCapturingA2AExecutor(
            agent,
            model_id=model.config.get("model_id"),
            enable_a2a_compliant_streaming=True,
        )

        app = server.to_fastapi_app()

        @app.get("/ping")
        async def _ping() -> dict[str, str]:
            return {"status": _ping_status()}

        logger.info("%s A2A server starting on %s:%d", card["name"], host, port)
        uvicorn.run(app, host=host, port=port)


def _compose_system_prompt(base: str, skills: list) -> str:
    """Append each resolved skill's body under a '# Available skills' heading."""
    if not skills:
        return base
    parts = [base.rstrip(), "", "# Available skills", ""]
    for skill in skills:
        parts.append(f"## {skill.name}")
        parts.append("")
        parts.append(skill.body)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _skills_from_bundles(skills: list) -> list[AgentSkill]:
    """Build AgentSkill objects from resolved skill bundles for A2A metadata."""
    return [
        AgentSkill(
            id=s.name,
            name=s.name,
            description=s.description,
            tags=[],
            examples=[],
        )
        for s in skills
    ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 2:
        print("Usage: python -m shared.a2a_factory <agent_dir>", file=sys.stderr)
        sys.exit(1)
    agent_main(sys.argv[1])
