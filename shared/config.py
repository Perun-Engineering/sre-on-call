"""Pydantic schema for config.yaml — the project's single source of truth.

Loaded once at agent startup (cached). Validates:
- Only known agents are listed.
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

KNOWN_AGENTS: frozenset[str] = frozenset({
    "master", "slack_scanner", "discord_scanner", "cloudwatch_logs", "eks",
})


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
        unknown = set(agents) - KNOWN_AGENTS
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
    """Clear the cached ProjectConfig — for tests that load multiple configs."""
    global _CACHED
    _CACHED = None


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
