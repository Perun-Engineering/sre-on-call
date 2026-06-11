"""Tests for the Incident History Agent tools — similar-incident lookup + snapshot."""

from __future__ import annotations

from agents.incident_history.tools import (
    AGENT_NAME,
    _capture_snapshot,
    _find_similar,
)
from shared.agent_footer import AgentFooter
from shared.incident_history_store import SimilarIncident
from shared.tool_result import AGENT_RESULT, SNAPSHOT_RESULT


class _FakeEmbedder:
    def __init__(self, vector):
        self._vector = vector

    def embed(self, text):
        return self._vector


class _FakeStore:
    def __init__(self, hits=None, count=0):
        self._hits = hits or []
        self._count = count
        self.search_calls = []

    def search_similar(self, embedding, *, top_k=3, min_score=0.5, exclude_investigation_id=None):
        self.search_calls.append((embedding, top_k, min_score))
        return self._hits

    def count_recent(self, *, days=30):
        return self._count


def _hit(**overrides) -> SimilarIncident:
    base = dict(
        investigation_id="abcdef12-3456",
        alert_text="Disk usage 95% on node-7",
        summary="Disk filled by unrotated logs.",
        score=0.91,
        alert_timestamp="2026-06-08T10:00:00+00:00",
        root_cause="Log rotation was disabled.",
        thread_link="https://example.slack.com/archives/C1/p171",
    )
    base.update(overrides)
    return SimilarIncident(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find_similar_incidents
# ---------------------------------------------------------------------------


def test_find_similar_returns_findings_for_matches():
    store = _FakeStore(hits=[_hit(), _hit(investigation_id="99999999", score=0.6)])
    result = _find_similar(
        "disk almost full", store=store, embedder=_FakeEmbedder([1.0, 0.0])
    )
    assert result.status == "success"
    assert result.agent_name == AGENT_NAME
    assert len(result.findings) == 2
    first = result.findings[0]
    assert "Root cause: Log rotation was disabled." in first.content
    assert first.link == "https://example.slack.com/archives/C1/p171"
    assert first.metadata["investigation_id"] == "abcdef12-3456"


def test_find_similar_empty_is_clean_success_not_error():
    store = _FakeStore(hits=[])
    result = _find_similar(
        "novel alert", store=store, embedder=_FakeEmbedder([0.1, 0.2])
    )
    assert result.status == "success"
    assert result.findings == []
    assert "No similar past incidents" in result.summary


def test_find_similar_unhealthy_when_not_configured():
    # Either dependency missing => the deployment isn't wired for history.
    r1 = _find_similar("x", store=None, embedder=_FakeEmbedder([1.0]))
    r2 = _find_similar("x", store=_FakeStore(), embedder=None)
    assert r1.status == "unhealthy"
    assert r2.status == "unhealthy"


def test_find_similar_error_when_embedding_fails():
    store = _FakeStore(hits=[_hit()])
    result = _find_similar("x", store=store, embedder=_FakeEmbedder(None))
    assert result.status == "error"
    assert result.findings == []
    # Store is never scanned when there's no query vector.
    assert store.search_calls == []


# ---------------------------------------------------------------------------
# capture_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_reports_recent_count():
    report = _capture_snapshot("2026-06-11T00:00:00+00:00", store=_FakeStore(count=7))
    assert report.agent_name == AGENT_NAME
    assert report.anomaly is False
    assert "7 incident(s) recorded" in report.sections[0].lines[0]


def test_snapshot_count_unavailable_renders_warning():
    report = _capture_snapshot("2026-06-11T00:00:00+00:00", store=_FakeStore(count=None))
    assert "unavailable" in report.sections[0].lines[0]


def test_snapshot_not_configured():
    report = _capture_snapshot("2026-06-11T00:00:00+00:00", store=None)
    assert report.anomaly is False
    assert "not configured" in report.sections[0].lines[0]


# ---------------------------------------------------------------------------
# Footers round-trip through the master's decoders
# ---------------------------------------------------------------------------


def test_result_footer_round_trips():
    from agents.incident_history.tools import _find_similar as fs
    from shared.tool_result import format_result

    store = _FakeStore(hits=[_hit()])
    text = format_result(fs("disk full", store=store, embedder=_FakeEmbedder([1.0, 0.0])))
    footer: AgentFooter = AGENT_RESULT
    _, decoded = footer.extract(text)
    assert decoded is not None
    assert decoded.agent_name == AGENT_NAME
    assert len(decoded.findings) == 1


def test_snapshot_footer_round_trips():
    from shared.tool_result import format_snapshot_result

    text = format_snapshot_result(
        _capture_snapshot("2026-06-11T00:00:00+00:00", store=_FakeStore(count=3))
    )
    _, decoded = SNAPSHOT_RESULT.extract(text)
    assert decoded is not None
    assert decoded.agent_name == AGENT_NAME
