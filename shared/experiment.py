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
    # Cost/token totals summed across the variant's agents (issue #26). ``None``
    # when no agent supplied telemetry — e.g. rows written before this field
    # existed still load, with the cost column left blank in the judge report.
    total_cost_usd: float | None = None
    total_tokens: int | None = None


# Rubric dimensions the LLM judge scores each variant pair on (issue #26).
JUDGEMENT_DIMENSIONS = ("coverage", "severity", "actionability", "noise")


@dataclass
class Judgement:
    """An LLM judge's pairwise verdict comparing two variant reports.

    ``overall_winner`` and every value in ``dimension_winners`` is one of
    ``"a"``, ``"b"``, or ``"tie"``. A dimension resolves to ``"tie"`` when the
    judge's pick flips with presentation order (position bias) — see
    :class:`shared.experiment_judge.ExperimentJudge`.
    """

    experiment_id: str
    investigation_id: str
    overall_winner: str  # "a" | "b" | "tie"
    dimension_winners: dict[str, str]  # dimension -> "a" | "b" | "tie"
    judge_model_id: str
    rationale: str = ""
    timestamp: str = ""  # ISO 8601
