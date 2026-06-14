"""Map-reduce integration tier over the Haiku summarizer (issue #49).

The EKS and CloudWatch specialized agents both turn bulk observability payloads
(pod event/log tails, Logs Insights rows) into compact per-chunk digests, falling
back to raw findings when summarizing isn't worthwhile or fails. That control
flow — the min-volume gate, the per-chunk digest-or-raw decision, and the
exemplar composition — is identical across the two agents; only the per-payload
specifics differ (how to chunk, how to render a digest/raw finding, how to pick
exemplars).

This module owns the shared algorithm (:func:`execute_digest`) parameterized by a
:class:`DigestSource`, mirroring ``execute_channel_scan`` / ``ChannelMessageSource``
in :mod:`shared.channel_scan`. The map fan-out itself (``Summarizer``,
``SummarizeChunk``, ``Digest``, ``BedrockLogSummarizer``) stays in
:mod:`shared.log_summarizer`; this is the reduce-side integration.

The algorithm is fail-open at every branch: no summarizer, sub-gate volume, or a
chunk whose digest failed all fall back to raw findings, so the result is never
empty for the wrong reason and undigested volume is the safe degradation.
"""

from __future__ import annotations

from typing import Protocol

from shared.log_summarizer import SUMMARIZER_MIN_LINES, Digest, SummarizeChunk, Summarizer
from shared.models import Finding


class DigestSource(Protocol):
    """The seam between :func:`execute_digest` and one agent's bulk payload.

    Constructed per-payload; ``chunks()`` is called once and may memoize the
    chunk-key→raw-items map the render methods read back. Three input methods
    feed the algorithm and three render methods turn its decisions into findings.
    """

    def volume(self) -> int:
        """Approximate line volume, compared against the min-volume gate."""
        ...

    def raw_findings(self) -> list[Finding]:
        """Every item as a raw finding — the gate-miss / no-summarizer path."""
        ...

    def chunks(self) -> list[SummarizeChunk]:
        """The chunks to summarize, each keyed so a digest maps back to it."""
        ...

    def digest_finding(self, digest: Digest, chunk: SummarizeChunk) -> Finding:
        """Render one successful chunk digest into a finding."""
        ...

    def chunk_raw_findings(self, chunk_key: str) -> list[Finding]:
        """Render one chunk's items as raw findings — the per-chunk fallback."""
        ...

    def exemplars(self, kept_raw: set[str]) -> list[Finding]:
        """Top-severity raw exemplars drawn from the chunks *not* in ``kept_raw``.

        ``kept_raw`` is the set of chunk keys that fell back to raw; their items
        are already shown literally, so exemplars come from the digested-away rest.
        """
        ...


def execute_digest(
    source: DigestSource,
    summarizer: Summarizer | None,
    *,
    min_lines: int = SUMMARIZER_MIN_LINES,
) -> list[Finding]:
    """Digest a source's bulk payload, falling back to raw at every failure.

    With no summarizer or sub-gate volume, returns the source's raw findings
    unchanged. Otherwise summarizes each chunk: a digest with text becomes one
    digest finding; a failed or missing digest falls back to that chunk's raw
    findings. Top-severity exemplars from the digested-away chunks are appended.
    """
    if summarizer is None or source.volume() < min_lines:
        return source.raw_findings()

    chunks = source.chunks()
    digests = {d.key: d for d in summarizer.summarize(chunks)}

    findings: list[Finding] = []
    kept_raw: set[str] = set()
    for chunk in chunks:
        digest = digests.get(chunk.key)
        if digest is not None and digest.text:
            findings.append(source.digest_finding(digest, chunk))
        else:
            findings.extend(source.chunk_raw_findings(chunk.key))
            kept_raw.add(chunk.key)

    findings.extend(source.exemplars(kept_raw))
    return findings
