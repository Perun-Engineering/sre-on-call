"""Tests for shared.embeddings — Titan seam, pack/unpack, cosine, gating."""

from __future__ import annotations

import io
import json

import pytest

from shared import embeddings
from shared.embeddings import (
    EmbeddingClient,
    cosine_similarity,
    pack_embedding,
    unpack_embedding,
)


# ---------------------------------------------------------------------------
# pack / unpack
# ---------------------------------------------------------------------------


def test_pack_unpack_roundtrip():
    vec = [0.1, -0.2, 0.3, 0.4, -0.5]
    restored = unpack_embedding(pack_embedding(vec))
    assert len(restored) == len(vec)
    for a, b in zip(vec, restored):
        assert a == pytest.approx(b, abs=1e-6)


def test_unpack_accepts_binary_like():
    # boto3 returns its own Binary wrapper; bytes(Binary) yields the payload.
    class FakeBinary:
        def __init__(self, value: bytes) -> None:
            self.value = value

        def __bytes__(self) -> bytes:
            return self.value

    packed = pack_embedding([1.0, 2.0, 3.0])
    restored = unpack_embedding(FakeBinary(packed))
    assert restored == pytest.approx([1.0, 2.0, 3.0])


# ---------------------------------------------------------------------------
# cosine
# ---------------------------------------------------------------------------


def test_cosine_identical_is_one():
    v = [1.0, 2.0, 3.0]
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_is_minus_one():
    assert cosine_similarity([1.0, 1.0], [-1.0, -1.0]) == pytest.approx(-1.0)


def test_cosine_degenerate_inputs_return_zero():
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0  # length mismatch
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero norm


# ---------------------------------------------------------------------------
# EmbeddingClient.embed
# ---------------------------------------------------------------------------


class _FakeBedrock:
    def __init__(self, embedding):
        self._embedding = embedding
        self.calls: list[dict] = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        body = {"embedding": self._embedding, "inputTextTokenCount": 3}
        return {"body": io.BytesIO(json.dumps(body).encode())}


def test_embed_happy_path():
    fake = _FakeBedrock([0.5, 0.5, 0.5])
    client = EmbeddingClient(bedrock_client=fake, dimensions=3)
    out = client.embed("disk full on node-7")
    assert out == pytest.approx([0.5, 0.5, 0.5])
    # Request carries the configured dimensions + normalize flag.
    sent = json.loads(fake.calls[0]["body"])
    assert sent["dimensions"] == 3
    assert sent["normalize"] is True
    assert sent["inputText"] == "disk full on node-7"


def test_embed_empty_text_returns_none_without_calling_bedrock():
    fake = _FakeBedrock([1.0])
    client = EmbeddingClient(bedrock_client=fake)
    assert client.embed("   ") is None
    assert fake.calls == []


def test_embed_failure_is_fail_open():
    class Boom:
        def invoke_model(self, **_kwargs):
            raise RuntimeError("throttled")

    client = EmbeddingClient(bedrock_client=Boom())
    assert client.embed("anything") is None


def test_embed_missing_embedding_field_returns_none():
    class NoEmbedding:
        def invoke_model(self, **_kwargs):
            return {"body": io.BytesIO(json.dumps({"inputTextTokenCount": 1}).encode())}

    client = EmbeddingClient(bedrock_client=NoEmbedding())
    assert client.embed("anything") is None


# ---------------------------------------------------------------------------
# from_env gating
# ---------------------------------------------------------------------------


def test_from_env_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("INCIDENT_HISTORY_ENABLED", raising=False)
    assert EmbeddingClient.from_env() is None


def test_from_env_enabled_reads_model_and_dims(monkeypatch):
    monkeypatch.setenv("INCIDENT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "512")
    client = EmbeddingClient.from_env()
    assert client is not None
    assert client.dimensions == 512


def test_from_env_bad_dimensions_falls_back(monkeypatch):
    monkeypatch.setenv("INCIDENT_HISTORY_ENABLED", "1")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "not-a-number")
    client = EmbeddingClient.from_env()
    assert client is not None
    assert client.dimensions == embeddings.DEFAULT_EMBEDDING_DIMENSIONS
