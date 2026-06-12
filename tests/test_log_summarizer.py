"""Tests for the Haiku map-reduce summarizer seam (issue #49).

The summarizer fans out parallel Bedrock Converse calls over bulk log/event
chunks and returns one digest per chunk. Everything is fail-open: a failed
chunk yields a ``Digest`` with ``text=None`` so the caller falls back to raw,
and a summarizer that cannot build a client digests nothing rather than raising
into the tool path.

These tests inject a mock boto3-style client (with a ``.converse`` method) so no
live model is called.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from shared.log_summarizer import (
    BedrockLogSummarizer,
    Digest,
    SummarizeChunk,
    drain_usage,
    record_usage,
    reset_usage,
)


def _mock_client(transform=lambda text: f"SUMMARY<{text}>"):
    """A boto3 bedrock-runtime-style client whose ``converse`` echoes a digest.

    The returned text is derived from the user prompt (which embeds the chunk
    text) so a test can tie each digest back to the chunk that produced it,
    regardless of the order the parallel calls complete in.
    """
    client = MagicMock()

    def _converse(**kwargs):
        user_text = kwargs["messages"][-1]["content"][-1]["text"]
        return {
            "output": {"message": {"content": [{"text": transform(user_text)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13},
        }

    client.converse.side_effect = _converse
    return client


def test_summarize_returns_one_digest_per_chunk_preserving_order() -> None:
    summarizer = BedrockLogSummarizer(model_id="haiku-test", client=_mock_client())
    chunks = [
        SummarizeChunk(key="chunk-0", text="raw-zero", severity="critical"),
        SummarizeChunk(key="chunk-1", text="raw-one", severity="warning"),
    ]

    digests = summarizer.summarize(chunks)

    assert [d.key for d in digests] == ["chunk-0", "chunk-1"]
    assert isinstance(digests[0], Digest)
    # Each digest is tied to its own chunk's text — proves mapping survives the
    # nondeterministic completion order of the parallel fan-out.
    assert "raw-zero" in (digests[0].text or "")
    assert "raw-one" in (digests[1].text or "")


def test_one_failing_chunk_falls_back_to_none_others_succeed() -> None:
    client = _mock_client()

    def _converse(**kwargs):
        text = kwargs["messages"][-1]["content"][-1]["text"]
        if "poison" in text:
            raise RuntimeError("model exploded")
        return {
            "output": {"message": {"content": [{"text": f"SUMMARY<{text}>"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 3},
        }

    client.converse.side_effect = _converse
    summarizer = BedrockLogSummarizer(model_id="haiku-test", client=client)
    chunks = [
        SummarizeChunk("ok", "good-lines", "info"),
        SummarizeChunk("bad", "poison-lines", "critical"),
    ]

    digests = {d.key: d for d in summarizer.summarize(chunks)}

    assert digests["bad"].text is None  # failed chunk → caller falls back to raw
    assert "good-lines" in (digests["ok"].text or "")  # neighbour unaffected


def test_client_construction_failure_digests_nothing(monkeypatch) -> None:
    import boto3

    monkeypatch.setattr(
        boto3, "client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no creds"))
    )
    summarizer = BedrockLogSummarizer(model_id="haiku-test")  # no client injected → lazy build
    chunks = [SummarizeChunk("c0", "lines", "warning")]

    digests = summarizer.summarize(chunks)

    assert digests == [Digest(key="c0", text=None)]  # all-None, never raises


def test_summarize_empty_chunks_returns_empty() -> None:
    summarizer = BedrockLogSummarizer(model_id="haiku-test", client=_mock_client())
    assert summarizer.summarize([]) == []


def test_usage_accumulates_across_chunks_and_drains() -> None:
    reset_usage()
    summarizer = BedrockLogSummarizer(model_id="haiku-test", client=_mock_client())
    chunks = [
        SummarizeChunk("c0", "a", "info"),
        SummarizeChunk("c1", "b", "info"),
        SummarizeChunk("c2", "c", "info"),
    ]

    summarizer.summarize(chunks)

    # _mock_client reports 10 in / 3 out per call × 3 chunks.
    assert drain_usage() == (30, 9)


def test_drain_without_any_calls_is_none() -> None:
    reset_usage()
    record_usage(0, 0)  # a call that happened to cost nothing still counts as a call
    assert drain_usage() == (0, 0)
    reset_usage()
    assert drain_usage() == (None, None)  # no calls at all → blank, not a misleading 0
