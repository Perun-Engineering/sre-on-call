"""Agent telemetry helpers — token usage capture and cost computation.

Specialized agents emit a metadata footer in the final A2A text response so
the orchestrator can surface model + token + cost in the Incident Report. The
footer is delimited so it can be cleanly stripped before display.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, fields

from shared.models import AgentMetadata


# Delimiters for the metadata footer. Chosen to be unambiguous in plain text
# (no Markdown collisions) and survive A2A JSON-RPC text-part round-trips.
METADATA_PREFIX = "<<<AGENT_METADATA "
METADATA_SUFFIX = " AGENT_METADATA>>>"

_METADATA_RE = re.compile(
    re.escape(METADATA_PREFIX) + r"(.*?)" + re.escape(METADATA_SUFFIX),
    re.DOTALL,
)


# Per-million-token pricing for Bedrock-hosted Claude models (USD).
# Keep this small and explicit; unknown models yield ``cost_usd=None``.
_MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # Claude Haiku 4.5
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.0, 5.0),
    "anthropic.claude-haiku-4-5-20251001-v1:0": (1.0, 5.0),
    # Claude Sonnet 4
    "us.anthropic.claude-sonnet-4-20250514-v1:0": (3.0, 15.0),
    "anthropic.claude-sonnet-4-20250514-v1:0": (3.0, 15.0),
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


def encode_metadata_footer(metadata: AgentMetadata) -> str:
    """Serialise an :class:`AgentMetadata` for appending to the agent's response."""
    payload = json.dumps(asdict(metadata), separators=(",", ":"))
    return f"{METADATA_PREFIX}{payload}{METADATA_SUFFIX}"


def extract_metadata(text: str) -> tuple[str, AgentMetadata | None]:
    """Strip and decode the metadata footer from an agent's response text.

    Returns ``(clean_text, metadata)``. ``metadata`` is ``None`` when no
    footer is present. Malformed footers are silently dropped — they're
    telemetry, not load-bearing data.
    """
    match = _METADATA_RE.search(text)
    if match is None:
        return text, None
    cleaned = _METADATA_RE.sub("", text).strip()
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return cleaned, None

    valid_keys = {f.name for f in fields(AgentMetadata)}
    metadata = AgentMetadata(**{k: v for k, v in payload.items() if k in valid_keys})
    return cleaned, metadata
