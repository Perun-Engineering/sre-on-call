"""Tests for the digest-tier seam (issue #49 consolidation).

``execute_digest`` owns the fail-open matrix the EKS and CloudWatch agents
previously duplicated inline: the min-volume gate, the per-chunk digest-or-raw
decision, and exemplar composition. A ``DigestSource`` adapter supplies the
per-agent specifics (chunking, finding construction, exemplar selection); this
suite proves the algorithm once against a configurable fake source.
"""

from __future__ import annotations

from shared.digest_tier import execute_digest
from shared.log_summarizer import Digest, SummarizeChunk
from shared.models import Finding


def _f(tag: str, severity: str = "info") -> Finding:
    return Finding(source="t", timestamp="", content=tag, severity=severity, metadata={})


class FakeSummarizer:
    """Maps each chunk key to a digest text (or ``None`` to simulate failure)."""

    def __init__(self, results: dict[str, str | None]) -> None:
        self._results = results

    def summarize(self, chunks: list[SummarizeChunk]) -> list[Digest]:
        return [Digest(key=c.key, text=self._results.get(c.key)) for c in chunks]


class FakeDigestSource:
    """Configurable ``DigestSource`` that records which render paths fired."""

    def __init__(self, *, vol: int, chunks: list[SummarizeChunk]) -> None:
        self._vol = vol
        self._chunks = chunks
        self.calls: list[object] = []

    def volume(self) -> int:
        return self._vol

    def raw_findings(self) -> list[Finding]:
        self.calls.append("raw_all")
        return [_f("RAW-ALL")]

    def chunks(self) -> list[SummarizeChunk]:
        self.calls.append("chunks")
        return self._chunks

    def digest_finding(self, digest: Digest, chunk: SummarizeChunk) -> Finding:
        return _f(f"DIGEST:{chunk.key}:{digest.text}")

    def chunk_raw_findings(self, chunk_key: str) -> list[Finding]:
        self.calls.append(f"raw:{chunk_key}")
        return [_f(f"RAW:{chunk_key}")]

    def exemplars(self, kept_raw: set[str]) -> list[Finding]:
        self.calls.append(("exemplars", frozenset(kept_raw)))
        return [_f("EX")]


def test_no_summarizer_returns_all_raw_without_chunking() -> None:
    src = FakeDigestSource(vol=100, chunks=[SummarizeChunk("chunk-0", "x", "info")])

    findings = execute_digest(src, None)

    assert [f.content for f in findings] == ["RAW-ALL"]
    assert src.calls == ["raw_all"]  # never touched chunks / summarizer


def test_volume_below_gate_returns_all_raw() -> None:
    src = FakeDigestSource(vol=5, chunks=[SummarizeChunk("chunk-0", "x", "info")])

    findings = execute_digest(src, FakeSummarizer({}), min_lines=30)

    assert [f.content for f in findings] == ["RAW-ALL"]
    assert src.calls == ["raw_all"]


def test_successful_digest_replaces_raw_and_appends_exemplars() -> None:
    src = FakeDigestSource(vol=100, chunks=[SummarizeChunk("chunk-0", "x", "info")])

    findings = execute_digest(src, FakeSummarizer({"chunk-0": "DIGESTED"}), min_lines=30)

    assert [f.content for f in findings] == ["DIGEST:chunk-0:DIGESTED", "EX"]
    # nothing kept raw → exemplars drawn from the (digested-away) chunk
    assert ("exemplars", frozenset()) in src.calls


def test_failed_digest_falls_back_to_chunk_raw() -> None:
    src = FakeDigestSource(vol=100, chunks=[SummarizeChunk("chunk-0", "x", "info")])

    # digest text None ⇒ the chunk failed ⇒ raw fallback, chunk key kept raw
    findings = execute_digest(src, FakeSummarizer({"chunk-0": None}), min_lines=30)

    assert [f.content for f in findings] == ["RAW:chunk-0", "EX"]
    assert ("exemplars", frozenset({"chunk-0"})) in src.calls


def test_mixed_chunks_digest_some_raw_others() -> None:
    src = FakeDigestSource(
        vol=200,
        chunks=[
            SummarizeChunk("chunk-0", "a", "info"),
            SummarizeChunk("chunk-1", "b", "info"),
        ],
    )

    findings = execute_digest(
        src, FakeSummarizer({"chunk-0": "OK", "chunk-1": None}), min_lines=30,
    )

    assert [f.content for f in findings] == ["DIGEST:chunk-0:OK", "RAW:chunk-1", "EX"]
    # only chunk-1 fell back ⇒ exemplars exclude it, drawn from chunk-0
    assert ("exemplars", frozenset({"chunk-1"})) in src.calls
