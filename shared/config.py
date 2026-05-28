"""Pydantic schema for config.yaml — the project's single source of truth.

Loaded once at agent startup (cached). Validates:
- Only known agents are listed (delegated to :func:`shared.agents.catalogue_ids`).
- Master agent is always enabled.
- EKS agent must use VPC network mode (its cluster API is private).
- MCP transports are restricted to streamable_http | sse | stdio.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import pydantic
import yaml

NetworkMode = Literal["PUBLIC", "VPC"]
Transport = Literal["streamable_http", "sse", "stdio"]


class MCPConfig(pydantic.BaseModel):
    name: str
    transport: Transport
    endpoint: str
    auth: str = "none"  # "none" | "iam" | "api_key:<logical_name>"


class AgentConfig(pydantic.BaseModel):
    enabled: bool = True
    skills: list[str] = pydantic.Field(default_factory=list)
    mcps: list[MCPConfig] = pydantic.Field(default_factory=list)
    network_mode: NetworkMode | None = None


class Defaults(pydantic.BaseModel):
    model_id: str
    network_mode: NetworkMode = "PUBLIC"


class ProjectConfig(pydantic.BaseModel):
    project: str
    environment: str
    defaults: Defaults
    agents: dict[str, AgentConfig]

    @pydantic.field_validator("agents")
    @classmethod
    def _known_agents_only(cls, agents: dict[str, AgentConfig]) -> dict[str, AgentConfig]:
        # Imported lazily to avoid load-order coupling with shared.agents,
        # which doesn't import shared.config at module-import time.
        from shared.agents import catalogue_ids

        unknown = set(agents) - catalogue_ids()
        if unknown:
            raise ValueError(f"Unknown agent names in config.yaml: {sorted(unknown)}")
        return agents

    @pydantic.model_validator(mode="after")
    def _master_always_enabled(self) -> "ProjectConfig":
        master = self.agents.get("master")
        if master is not None and not master.enabled:
            raise ValueError("master agent cannot be disabled — it is always built")
        return self

    @pydantic.model_validator(mode="after")
    def _eks_must_be_vpc(self) -> "ProjectConfig":
        eks = self.agents.get("eks")
        if eks is not None and eks.network_mode is not None and eks.network_mode != "VPC":
            raise ValueError("eks agent must use network_mode: VPC (cluster API is private)")
        return self


_CACHED: ProjectConfig | None = None


def load(path: str | Path | None = None) -> ProjectConfig:
    """Load and validate config.yaml. Cached after first call."""
    global _CACHED
    if _CACHED is None:
        target = Path(path) if path is not None else _find_config_yaml()
        with open(target) as fh:
            data = yaml.safe_load(fh)
        _CACHED = ProjectConfig(**data)
    return _CACHED


def reset_cache() -> None:
    """Clear the cached ProjectConfig — for tests that load multiple configs.

    Also clears the :class:`AgentRegistry` cache, since the registry is built
    against the cached :class:`ProjectConfig` and would otherwise reflect a
    stale config the next time it's queried.
    """
    global _CACHED
    _CACHED = None
    from shared import agents as _agents_module
    _agents_module.reset_cache()


def _find_config_yaml() -> Path:
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "config.yaml"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("config.yaml not found in CWD or any parent")


def _validate_cli() -> int:
    """`python -m shared.config validate` — return non-zero on failure."""
    try:
        cfg = load()
    except Exception as exc:
        print(f"config.yaml invalid: {exc}", file=sys.stderr)
        return 1
    print(f"config.yaml OK: project={cfg.project} env={cfg.environment} agents={sorted(cfg.agents)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] != "validate":
        print("usage: python -m shared.config validate", file=sys.stderr)
        sys.exit(2)
    sys.exit(_validate_cli())
