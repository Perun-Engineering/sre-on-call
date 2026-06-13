"""Text-embedding seam for similar-incident lookup (issue #30).

Wraps Amazon Titan Text Embeddings V2 behind a tiny, **fail-open** client so
both the write path (master orchestrator embedding an alert at Phase 7) and the
read path (the ``incident_history`` agent embedding the current alert before a
vector scan) share one implementation. Any error — no Bedrock access, throttle,
malformed response — yields ``None`` and the caller degrades gracefully:
similar-incident lookup is an enrichment, never a hard dependency.

The module also owns the wire format for storing a vector in DynamoDB. Vectors
are packed as little-endian ``float32`` bytes (the DynamoDB Binary type) rather
than a list of ``Decimal`` — ~4 KB for a 1024-dim vector instead of ~30 KB, and
no lossy float→Decimal→float round-trip. :func:`cosine_similarity` is pure
Python so the scan-and-rank read path needs no numpy.

Resolution / gating (``from_env``):

* ``INCIDENT_HISTORY_ENABLED`` — master gate; ``from_env`` returns ``None``
  unless truthy, so local dev and tests never reach for Bedrock by accident.
* ``EMBEDDING_MODEL_ID`` — defaults to ``amazon.titan-embed-text-v2:0``.
* ``EMBEDDING_DIMENSIONS`` — Titan V2 supports 256 / 512 / 1024; defaults 1024.
"""

from __future__ import annotations

import json
import logging
import math
import os
from array import array
from typing import Any, Protocol

from shared.env import truthy

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """The embedding seam consumers depend on (Titan today, swappable later)."""

    def embed(self, text: str) -> list[float] | None: ...

DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
DEFAULT_EMBEDDING_DIMENSIONS = 1024


# ---------------------------------------------------------------------------
# Wire format + math (pure)
# ---------------------------------------------------------------------------


def pack_embedding(vector: list[float]) -> bytes:
    """Pack a float vector into little-endian ``float32`` bytes for DynamoDB."""
    return array("f", vector).tobytes()


def unpack_embedding(blob: Any) -> list[float]:
    """Unpack ``float32`` bytes (or a boto3 ``Binary``) back into a float list."""
    arr = array("f")
    arr.frombytes(bytes(blob))
    return arr.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; ``0.0`` on degenerate input.

    Returns ``0.0`` when the lengths differ or either vector has zero norm,
    so a malformed stored vector can never raise into the ranking loop.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


# ---------------------------------------------------------------------------
# EmbeddingClient
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """Fail-open wrapper over Titan Text Embeddings.

    Construct directly for tests (pass a mocked ``bedrock_client``), or use
    :meth:`from_env` in production paths. :meth:`embed` returns ``None`` on any
    failure; it never raises.
    """

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
        dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
        bedrock_client: Any = None,
        region_name: str | None = None,
    ) -> None:
        self._model_id = model_id
        self._dimensions = dimensions
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._client = bedrock_client

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @classmethod
    def from_env(cls) -> EmbeddingClient | None:
        """Build a client from the environment, or ``None`` when disabled.

        Gated on ``INCIDENT_HISTORY_ENABLED`` so embedding (and the Bedrock
        permission it needs) is explicitly opt-in. The boto3 client is created
        lazily on first :meth:`embed`, so returning a client here costs nothing.
        """
        if not truthy(os.environ.get("INCIDENT_HISTORY_ENABLED")):
            return None
        model_id = os.environ.get("EMBEDDING_MODEL_ID") or DEFAULT_EMBEDDING_MODEL_ID
        dims_raw = os.environ.get("EMBEDDING_DIMENSIONS")
        try:
            dimensions = int(dims_raw) if dims_raw else DEFAULT_EMBEDDING_DIMENSIONS
        except ValueError:
            dimensions = DEFAULT_EMBEDDING_DIMENSIONS
        return cls(model_id=model_id, dimensions=dimensions)

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def embed(self, text: str) -> list[float] | None:
        """Embed *text* with Titan, returning the vector or ``None`` on failure.

        Empty/whitespace text short-circuits to ``None`` — there is nothing to
        compare an empty alert against.
        """
        if not text or not text.strip():
            return None
        body = json.dumps(
            {
                "inputText": text,
                "dimensions": self._dimensions,
                "normalize": True,
            }
        )
        try:
            response = self._get_client().invoke_model(
                modelId=self._model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(response["body"].read())
            embedding = payload.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                logger.warning("Titan returned no embedding for the alert text.")
                return None
            return [float(x) for x in embedding]
        except Exception:
            logger.warning("Embedding call failed; skipping similarity.", exc_info=True)
            return None
