"""Haiku map-reduce summarizer tier for data-heavy agent tools (issue #49).

The EKS and CloudWatch specialized agents harvest bulk payloads — hundreds of
log lines per Insights query, dozens of pod-event streams plus log tails per pod
— and today turn *each* raw item into its own :class:`~shared.models.Finding`.
``format_result`` renders all of them into the string the (Sonnet) planner reads
back, so the planner pays full input-token cost to read undigested volume.

This module is the *map* half of a map-reduce: it fans out **parallel Haiku**
:meth:`converse` calls over the bulk, returning one compact digest per chunk.
The planner is the *reduce* — it reasons over the digests. The mechanism lives
inside one tool; it does not touch the A2A surface or the ``_execute_*`` test
contract.

Everything is fail-open:

- a chunk whose Converse call errors or times out yields ``Digest(text=None)``,
  and the caller falls back to that chunk's raw findings; and
- a summarizer that cannot build a Bedrock client digests *nothing* (all
  ``None``) rather than raising into the tool path — so it is safe to always
  construct one.

Haiku summarizer calls run in tool code, outside the Strands planner loop, so
their tokens never reach ``result.metrics.accumulated_usage``. The thread-safe
request-scoped accumulator (:func:`reset_usage` / :func:`record_usage` /
:func:`drain_usage`) carries them out to the telemetry footer instead.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Haiku is the cheap summarization tier regardless of the agent's own (Sonnet)
# model. Overridable per-deploy; the default matches the project's Haiku id.
DEFAULT_SUMMARIZER_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Tunables — module constants, env-overridable via the ``_int_env`` reads below.
SUMMARIZER_MIN_LINES = 30          # min bulk lines before summarizing is worth it
SUMMARIZER_CHUNK_SIZE = 50         # raw lines per parallel Haiku call
SUMMARIZER_MAX_WORKERS = 6         # cap on concurrent Converse calls
SUMMARIZER_TIMEOUT_SECONDS = 8.0   # per-chunk Converse budget
SUMMARIZER_MAX_TOKENS = 512        # digest output cap

def summarizer_model_id() -> str:
    """The model id the summarizer bills against, for cost attribution."""
    return os.environ.get("SUMMARIZER_MODEL_ID") or DEFAULT_SUMMARIZER_MODEL_ID


_SYSTEM_PROMPT = (
    "You compress raw observability data into a dense incident digest for an "
    "investigation agent. Given log lines or Kubernetes events, return a few "
    "terse sentences naming the dominant error patterns, their counts, the "
    "components involved, and anything anomalous. Preserve concrete signals "
    "(error codes, exit codes, pod/container names, exception types). Never "
    "invent data not present in the input. No preamble — digest only."
)


@dataclass
class SummarizeChunk:
    """One unit of bulk text to digest.

    ``key`` identifies the chunk (e.g. ``"chunk-0"`` or ``"pod/web-abc"``) and is
    echoed onto the resulting :class:`Digest` so the caller can map a digest back
    to the raw items it covers. ``severity`` is the max severity across the
    chunk's raw items, carried onto the digest finding.
    """

    key: str
    text: str
    severity: str


@dataclass
class Digest:
    """The result of summarizing one chunk. ``text=None`` ⇒ the chunk failed."""

    key: str
    text: str | None


class Summarizer(Protocol):
    """Seam over the parallel summarization fan-out, injectable for tests."""

    def summarize(self, chunks: list[SummarizeChunk]) -> list[Digest]: ...


# ---------------------------------------------------------------------------
# Request-scoped token accumulator. The executor resets it under its invocation
# lock before the agent runs (loop thread, no summarizer threads live yet);
# record_usage is called from the parallel fan-out (lock-guarded); drain_usage
# runs after the loop completes and all threads have joined.
# ---------------------------------------------------------------------------

_usage_lock = threading.Lock()
_usage_input = 0
_usage_output = 0
_usage_calls = 0


def reset_usage() -> None:
    """Zero the request-scoped summarizer token accumulator."""
    global _usage_input, _usage_output, _usage_calls
    with _usage_lock:
        _usage_input = 0
        _usage_output = 0
        _usage_calls = 0


def record_usage(input_tokens: int | None, output_tokens: int | None) -> None:
    """Add one Converse call's token usage to the accumulator (thread-safe)."""
    global _usage_input, _usage_output, _usage_calls
    with _usage_lock:
        _usage_input += int(input_tokens or 0)
        _usage_output += int(output_tokens or 0)
        _usage_calls += 1


def drain_usage() -> tuple[int | None, int | None]:
    """Return ``(input_tokens, output_tokens)`` accumulated, or ``(None, None)``.

    ``(None, None)`` when no summarizer call recorded usage this request, so a
    non-summarizing investigation leaves the telemetry fields blank rather than
    reporting a misleading zero.
    """
    with _usage_lock:
        if _usage_calls == 0:
            return None, None
        return _usage_input, _usage_output


class BedrockLogSummarizer:
    """Default :class:`Summarizer` — parallel ``bedrock-runtime`` Converse fan-out.

    The boto3 client is injectable for tests. In production it is built lazily on
    first use; a build failure is swallowed and every chunk digests to ``None``.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        client=None,
        max_workers: int = SUMMARIZER_MAX_WORKERS,
        timeout_seconds: float = SUMMARIZER_TIMEOUT_SECONDS,
    ) -> None:
        self._model_id = model_id or DEFAULT_SUMMARIZER_MODEL_ID
        self._client = client
        self._client_built = client is not None
        self._max_workers = max(1, max_workers)
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "BedrockLogSummarizer":
        """Build a summarizer from the environment (model + concurrency knobs).

        The client is built lazily on first use and fail-open, so this never
        raises — a tool can always construct one unconditionally.
        """
        return cls(model_id=os.environ.get("SUMMARIZER_MODEL_ID") or None)

    def _get_client(self):
        """Return the bedrock-runtime client, building it lazily, fail-open."""
        if self._client_built:
            return self._client
        self._client_built = True
        try:
            import boto3
            from botocore.config import Config

            region = os.environ.get("AWS_REGION", "us-east-1")
            # Bound the socket so a hung Converse can't outlive the per-chunk
            # timeout and block the ThreadPoolExecutor shutdown. No retries —
            # under latency we fail the chunk open to raw, not amplify load.
            config = Config(
                region_name=region,
                read_timeout=self._timeout_seconds,
                connect_timeout=min(self._timeout_seconds, 3.0),
                retries={"max_attempts": 0},
            )
            self._client = boto3.client("bedrock-runtime", config=config)
        except Exception:  # noqa: BLE001 — construction must never raise into tools
            logger.warning("Could not build bedrock-runtime client for summarizer", exc_info=True)
            self._client = None
        return self._client

    def summarize(self, chunks: list[SummarizeChunk]) -> list[Digest]:
        if not chunks:
            return []
        client = self._get_client()
        if client is None:
            return [Digest(key=c.key, text=None) for c in chunks]

        results: list[Digest | None] = [None] * len(chunks)
        workers = min(self._max_workers, len(chunks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._digest_one, client, chunk): i
                for i, chunk in enumerate(chunks)
            }
            for future, i in futures.items():
                try:
                    results[i] = future.result(timeout=self._timeout_seconds)
                except (FuturesTimeout, Exception):  # noqa: BLE001 — per-chunk fail-open
                    logger.warning(
                        "Summarizer chunk %s failed; falling back to raw", chunks[i].key,
                        exc_info=True,
                    )
                    results[i] = Digest(key=chunks[i].key, text=None)
        return [r if r is not None else Digest(key=chunks[i].key, text=None)
                for i, r in enumerate(results)]

    def _digest_one(self, client, chunk: SummarizeChunk) -> Digest:
        """Run one Converse call for a single chunk and record its usage."""
        response = client.converse(
            modelId=self._model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": chunk.text}]}],
            inferenceConfig={"maxTokens": SUMMARIZER_MAX_TOKENS, "temperature": 0.0},
        )
        usage = response.get("usage") or {}
        record_usage(usage.get("inputTokens"), usage.get("outputTokens"))
        blocks = (
            response.get("output", {}).get("message", {}).get("content", [])
        )
        text = next(
            (b["text"] for b in blocks if isinstance(b, dict) and b.get("text")),
            None,
        )
        return Digest(key=chunk.key, text=text)
