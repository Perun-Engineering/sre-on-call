"""Agent registry — single source of truth for what agents exist and their
deployment state.

The static **catalogue** (`CATALOGUE`) lists every agent that exists in code:
the master orchestrator plus every specialized agent (Slack Scanner, Discord
Scanner, CloudWatch Logs, EKS, Prometheus). Each catalogue record carries
identity (id, display name, emoji, render order), kind (`orchestrator` |
`specialized`), and wiring (env-var keys for runtime ARN and local URL,
default local URL).

The **deployment manifest** is the `agents:` block in `config.yaml`. Its keys
must be a subset of the catalogue ids; presence here means "build & deploy in
this account." An `enabled: false` flag marks an agent as deployed-but-inactive
(built and pushed to ECR, but the orchestrator skips it on fan-out and the
formatter renders it as a 🚫 disabled evidence block).

The :class:`AgentRegistry` folds the catalogue against a
:class:`shared.config.ProjectConfig` at construction. Consumers ask one
question and get one answer:

* ``registry.all()`` — catalogue (used by config-yaml validation).
* ``registry.deployed()`` — agents listed in config.yaml (used by terraform / build).
* ``registry.active()`` — deployed AND ``enabled=True`` (used by orchestrator fan-out).
* ``registry.disabled_in_config()`` — deployed AND ``enabled=False`` (used by
  the formatter to render 🚫 disabled evidence blocks).
* ``registry.lookup(id)`` — by id; raises ``KeyError`` on unknown.

Each query takes an optional ``kind`` filter for ``orchestrator`` | ``specialized``.

Replaces the constants ``KNOWN_AGENTS``, ``DEFAULT_AGENT_ENDPOINTS``,
``_ENV_KEYS``, ``_RUNTIME_ARN_ENV_KEYS``, ``AGENT_DISPLAY``, ``AGENT_ORDER``,
and the ``ENABLED_AGENTS`` env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

AgentKind = Literal["orchestrator", "specialized"]


@dataclass
class Agent:
    """One record per agent in the project.

    The first eight fields are *catalogue facts* — defined in :data:`CATALOGUE`
    and immutable per agent. The remaining fields are *deployment facts* —
    populated from the ``agents:`` block in ``config.yaml`` at registry
    construction. They are ``None`` when the agent isn't listed in this
    deployment's ``config.yaml`` (e.g. Prometheus today).
    """

    id: str
    display_name: str
    emoji: str
    order: int  # render rank in the Incident Report's evidence section
    kind: AgentKind
    runtime_arn_env: str  # e.g. "EKS_AGENT_RUNTIME_ARN"
    url_env: str  # e.g. "EKS_AGENT_URL"
    default_local_url: str  # e.g. "http://localhost:9005"

    # Deployment fields (None when not in config.yaml).
    enabled: bool | None = None
    skills: list[str] | None = None
    mcps: list | None = None  # list[MCPConfig] | None
    network_mode: str | None = None  # NetworkMode = "PUBLIC" | "VPC" | None

    @property
    def deployed(self) -> bool:
        """True when this agent is listed in ``config.yaml``."""
        return self.enabled is not None

    @property
    def is_active(self) -> bool:
        """True when deployed AND ``enabled=True``. Use ``Agent.deployed`` to
        distinguish "not in config.yaml" from "in config.yaml with enabled=False"."""
        return self.enabled is True

    def resolve_endpoint(self) -> str:
        """Return the AgentCore runtime ARN if set, else the URL env, else the
        default local URL.

        Used by the orchestrator to decide where to dispatch an A2A request.
        Mirrors the order ``AgentCoreClient`` (ARN) > ``AiohttpClient`` (URL) >
        ``localhost`` default that the legacy ``_load_agent_endpoints`` used.
        """
        arn = os.environ.get(self.runtime_arn_env, "").strip()
        if arn:
            return arn
        url = os.environ.get(self.url_env, "").strip()
        if url:
            return url
        return self.default_local_url


# ---------------------------------------------------------------------------
# Static catalogue
# ---------------------------------------------------------------------------
#
# Order in this list is the canonical render order in the Incident Report's
# evidence section (specialized agents only — the master is the orchestrator
# and never appears as evidence).
#
# Adding an agent: add a record here, add a directory under ``agents/``, list
# it in ``config.yaml`` to deploy it. Removing an agent: delete the record
# (and the agents/ directory and config.yaml entry).

CATALOGUE: tuple[Agent, ...] = (
    Agent(
        id="master",
        display_name="Master Agent",
        emoji="🎯",
        order=0,
        kind="orchestrator",
        runtime_arn_env="MASTER_AGENT_RUNTIME_ARN",
        url_env="MASTER_AGENT_URL",
        default_local_url="http://localhost:9000",
    ),
    Agent(
        id="slack_scanner",
        display_name="Slack Scanner",
        emoji="📡",
        order=1,
        kind="specialized",
        runtime_arn_env="SLACK_SCANNER_AGENT_RUNTIME_ARN",
        url_env="SLACK_SCANNER_AGENT_URL",
        default_local_url="http://localhost:9001",
    ),
    Agent(
        id="discord_scanner",
        display_name="Discord Scanner",
        emoji="🎮",
        order=2,
        kind="specialized",
        runtime_arn_env="DISCORD_SCANNER_AGENT_RUNTIME_ARN",
        url_env="DISCORD_SCANNER_AGENT_URL",
        default_local_url="http://localhost:9002",
    ),
    Agent(
        id="cloudwatch_logs",
        display_name="CloudWatch Logs",
        emoji="📋",
        order=3,
        kind="specialized",
        runtime_arn_env="CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN",
        url_env="CLOUDWATCH_LOGS_AGENT_URL",
        default_local_url="http://localhost:9004",
    ),
    Agent(
        id="eks",
        display_name="EKS Cluster State",
        emoji="☸️",
        order=4,
        kind="specialized",
        runtime_arn_env="EKS_AGENT_RUNTIME_ARN",
        url_env="EKS_AGENT_URL",
        default_local_url="http://localhost:9005",
    ),
    Agent(
        id="prometheus",
        display_name="Prometheus",
        emoji="📈",
        order=5,
        kind="specialized",
        runtime_arn_env="PROMETHEUS_AGENT_RUNTIME_ARN",
        url_env="PROMETHEUS_AGENT_URL",
        default_local_url="http://localhost:9003",
    ),
    Agent(
        id="incident_history",
        display_name="Incident History",
        emoji="📚",
        order=6,
        kind="specialized",
        runtime_arn_env="INCIDENT_HISTORY_AGENT_RUNTIME_ARN",
        url_env="INCIDENT_HISTORY_AGENT_URL",
        default_local_url="http://localhost:9006",
    ),
)


def catalogue_ids() -> frozenset[str]:
    """Return the set of all known agent ids — used by config.yaml validation."""
    return frozenset(a.id for a in CATALOGUE)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Resolves the static catalogue against a :class:`ProjectConfig`'s deployment manifest.

    Construct once per process via :func:`get_registry` (cached), or directly
    in tests with a custom :class:`ProjectConfig`.
    """

    def __init__(self, project_config) -> None:
        # Imported here to avoid a circular import: shared.config imports from
        # shared.agents (for catalogue_ids), and the registry imports config
        # only as a type hint at runtime.
        from shared.config import AgentConfig, ProjectConfig  # noqa: F401

        # Retained so consumers that need the full ProjectConfig (e.g. resolving
        # a per-agent model id with the dispatch precedence) read it from the
        # same config the catalogue was folded against — no second load that
        # could drift (issue #81).
        self._project_config = project_config
        self._records: list[Agent] = [
            self._fold(static, project_config.agents.get(static.id))
            for static in CATALOGUE
        ]

    @property
    def project_config(self):
        """The :class:`ProjectConfig` this registry was folded against."""
        return self._project_config

    @staticmethod
    def _fold(static: Agent, cfg) -> Agent:
        """Combine a static catalogue record with the matching ``AgentConfig``.

        ``cfg is None`` means the agent is not in this deployment's config.yaml
        — the deployment fields stay ``None``.
        """
        if cfg is None:
            return Agent(
                id=static.id,
                display_name=static.display_name,
                emoji=static.emoji,
                order=static.order,
                kind=static.kind,
                runtime_arn_env=static.runtime_arn_env,
                url_env=static.url_env,
                default_local_url=static.default_local_url,
            )
        return Agent(
            id=static.id,
            display_name=static.display_name,
            emoji=static.emoji,
            order=static.order,
            kind=static.kind,
            runtime_arn_env=static.runtime_arn_env,
            url_env=static.url_env,
            default_local_url=static.default_local_url,
            enabled=cfg.enabled,
            skills=list(cfg.skills),
            mcps=list(cfg.mcps),
            network_mode=cfg.network_mode,
        )

    # --- queries ---

    def all(self, *, kind: AgentKind | None = None) -> list[Agent]:
        """Every catalogue record (deployed or not), optionally filtered by kind.
        Order matches :data:`CATALOGUE`."""
        return [a for a in self._records if kind is None or a.kind == kind]

    def deployed(self, *, kind: AgentKind | None = None) -> list[Agent]:
        """Agents listed in this deployment's ``config.yaml`` (regardless of ``enabled``).

        Used by terraform (which resources to create) and the build script
        (which images to build).
        """
        return [a for a in self.all(kind=kind) if a.deployed]

    def active(self, *, kind: AgentKind | None = None) -> list[Agent]:
        """Agents that are deployed AND ``enabled=True``.

        Used by the orchestrator's fan-out and by ``a2a_factory`` to allow
        startup. ``kind="specialized"`` is the orchestrator's normal query.
        """
        return [a for a in self.all(kind=kind) if a.is_active]

    def disabled_in_config(self, *, kind: AgentKind | None = None) -> list[Agent]:
        """Agents that are deployed but ``enabled=False`` — built and running
        but the orchestrator should skip them and surface a 🚫 disabled evidence block."""
        return [a for a in self.all(kind=kind) if a.deployed and not a.is_active]

    def lookup(self, agent_id: str) -> Agent:
        """Return the :class:`Agent` with the given id. Raises ``KeyError`` if unknown.

        Used by the formatter when rendering an arbitrary agent id received in
        an :class:`AgentResult` — even ids not in the active set still need
        display info (e.g. an agent removed from ``config.yaml`` after a result
        was already in flight).
        """
        for a in self._records:
            if a.id == agent_id:
                return a
        raise KeyError(f"Unknown agent id: {agent_id!r}")


# ---------------------------------------------------------------------------
# Process-wide cache
# ---------------------------------------------------------------------------

_CACHED: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    """Return the process-wide :class:`AgentRegistry`, loading on first call.

    Loads the project's ``config.yaml`` via :func:`shared.config.load`, which
    is itself cached. Tests that need a different config should construct
    :class:`AgentRegistry` directly with their own :class:`ProjectConfig`.
    """
    global _CACHED
    if _CACHED is None:
        from shared.config import load as load_config

        _CACHED = AgentRegistry(load_config())
    return _CACHED


def reset_cache() -> None:
    """Clear the cached registry — for tests."""
    global _CACHED
    _CACHED = None
