"""Agent telemetry helpers — token usage capture and cost computation.

Specialized agents emit a metadata footer in the final A2A text response so
the orchestrator can surface model + token + cost in the Incident Report.
The footer is delimited via the :data:`AGENT_METADATA` :class:`AgentFooter`
instance below so it can be cleanly stripped before display.
"""

from __future__ import annotations

from dataclasses import fields

from shared.agent_footer import AgentFooter
from shared.models import AgentMetadata


# Per-million-token pricing for Bedrock-hosted Claude models (USD).
# Keep this small and explicit; unknown models yield ``cost_usd=None``.
_MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # Claude Haiku 4.5
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.0, 5.0),
    "anthropic.claude-haiku-4-5-20251001-v1:0": (1.0, 5.0),
    # Claude Sonnet 4
    "us.anthropic.claude-sonnet-4-20250514-v1:0": (3.0, 15.0),
    "anthropic.claude-sonnet-4-20250514-v1:0": (3.0, 15.0),
    # Claude Sonnet 4.6 (master orchestrator — issue #46)
    "us.anthropic.claude-sonnet-4-6-20250929-v1:0": (3.0, 15.0),
    "anthropic.claude-sonnet-4-6-20250929-v1:0": (3.0, 15.0),
}


def compute_cost_usd(
    model_id: str | None, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Compute USD cost from token counts using a small built-in price table.

    Returns ``None`` when the model isn't priced or token counts are missing.
    """
    if model_id is None or input_tokens is None or output_tokens is None:
        return None
    pricing = _MODEL_PRICING_USD_PER_MTOK.get(model_id)
    if pricing is None:
        return None
    in_price, out_price = pricing
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def _metadata_from_dict(payload: dict) -> AgentMetadata:
    """Reconstruct an :class:`AgentMetadata` from a JSON-decoded dict.

    Unknown keys are silently filtered out; missing keys fall back to the
    dataclass defaults. Used as the parser callable for :data:`AGENT_METADATA`.
    """
    valid_keys = {f.name for f in fields(AgentMetadata)}
    return AgentMetadata(**{k: v for k, v in payload.items() if k in valid_keys})


# ---------------------------------------------------------------------------
# AgentFooter instance — the marker-delimited transport for AgentMetadata.
# ---------------------------------------------------------------------------


AGENT_METADATA: AgentFooter[AgentMetadata] = AgentFooter(
    "AGENT_METADATA", parse=_metadata_from_dict,
)
