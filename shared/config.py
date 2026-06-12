"""Pydantic schema for config.yaml — the project's single source of truth.

Loaded once at agent startup (cached). The raw YAML comes from SSM Parameter
Store when ``CONFIG_SSM_PARAMETER`` is set (AWS runtimes — Terraform publishes
config.yaml's content there, so toggling an agent's ``enabled`` takes effect on
the next cold-start with no image rebuild) and from the working-tree
``config.yaml`` otherwise (local dev and tests, unchanged). Validates:
- Only known agents are listed (delegated to :func:`shared.agents.catalogue_ids`).
- Master agent is always enabled.
- EKS agent must use VPC network mode (its cluster API is private).
- MCP transports are restricted to streamable_http | sse | stdio.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

import pydantic
import yaml

NetworkMode = Literal["PUBLIC", "VPC"]
Transport = Literal["streamable_http", "sse", "stdio"]

# Env var naming the SSM parameter that holds config.yaml's content. Set by
# Terraform on every AgentCore runtime; unset for local dev and tests.
CONFIG_SSM_ENV = "CONFIG_SSM_PARAMETER"


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
    # Optional per-agent Bedrock model. When set, this agent resolves to it
    # (winning over the deploy-wide ``MODEL_ID`` env); when absent, the agent
    # falls back to ``MODEL_ID`` env / ``defaults.model_id``. Lets the master
    # run a Sonnet-class model while scanners stay on Haiku.
    model_id: str | None = None
    # When set, this agent runs as a bounded iterative investigator (issue
    # #58): the A2A factory registers a :class:`shared.bounded_loop.BoundedLoopHook`
    # that caps the agent at this many tool-use cycles and enforces the
    # master-granted ``deadline_seconds``. Absent → single-pass (today's
    # behaviour). Set for eks + cloudwatch_logs, which drill on what each pass
    # surfaces; scanners/prometheus/incident_history stay single-pass.
    max_tool_cycles: int | None = None

    @pydantic.field_validator("max_tool_cycles")
    @classmethod
    def _positive_cycles(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("max_tool_cycles must be >= 1 when set")
        return value


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
    """Load and validate config.yaml. Cached after first call.

    With an explicit ``path``, reads that file directly. Otherwise the raw YAML
    is resolved by :func:`_load_raw_yaml` (SSM in AWS, repo file locally).
    """
    global _CACHED
    if _CACHED is None:
        if path is not None:
            raw = Path(path).read_text()
        else:
            raw = _load_raw_yaml()
        _CACHED = ProjectConfig(**yaml.safe_load(raw))
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


def _load_raw_yaml() -> str:
    """Return the raw config YAML text from SSM (AWS) or the repo file (local).

    SSM wins when ``CONFIG_SSM_PARAMETER`` is set so a config change applied by
    Terraform is picked up on the next cold-start without rebuilding images.
    Fails closed (no baked fallback) when SSM is configured but unreachable —
    an agent cannot run without its config.
    """
    parameter = os.environ.get(CONFIG_SSM_ENV)
    if parameter:
        return _fetch_ssm_parameter(parameter)
    return _find_config_yaml().read_text()


def _fetch_ssm_parameter(name: str) -> str:
    """Fetch an SSM parameter's value. boto3 is imported lazily so local dev and
    tests that never touch SSM don't pay the client-construction cost."""
    import boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("ssm", region_name=region) if region else boto3.client("ssm")
    response = client.get_parameter(Name=name)
    return response["Parameter"]["Value"]


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
