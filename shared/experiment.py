"""A/B testing data models for experiment-driven investigations."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentVariantConfig:
    """Configuration for a single agent variant in an experiment."""

    endpoint: str  # A2A endpoint URL or AgentCore agent ID
    model_id: str | None = None  # Bedrock model override
    system_prompt: str | None = None  # System prompt override


@dataclass
class PipelineVariant:
    """A complete investigation pipeline configuration for one side of an A/B test."""

    variant_id: str  # "a" or "b"
    label: str  # Human-readable, e.g. "Claude Sonnet"
    master_endpoint: str  # AgentCore agent ID for the orchestrator
    agents: dict[str, AgentVariantConfig] = field(default_factory=dict)


@dataclass
class ExperimentConfig:
    """Defines an A/B experiment across two pipeline variants."""

    experiment_id: str
    name: str  # e.g. "claude-vs-nova-march"
    status: str  # "active", "paused", "completed"
    variant_a: PipelineVariant
    variant_b: PipelineVariant
    created_at: str  # ISO 8601
    updated_at: str  # ISO 8601


@dataclass
class ExperimentResult:
    """Stores the outcome of one variant's investigation for later comparison."""

    experiment_id: str
    investigation_id: str
    variant_id: str  # "a" or "b"
    report: str
    agent_durations: dict[str, float] = field(default_factory=dict)
    total_duration_seconds: float = 0.0
    timestamp: str = ""  # ISO 8601
