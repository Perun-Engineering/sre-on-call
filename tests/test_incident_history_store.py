"""Unit tests for shared.incident_history_store — outcome write + scan/cosine read."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from shared.incident_history_store import (
    HISTORY_PK_PREFIX,
    RECORD_TYPE,
    IncidentHistoryStore,
    IncidentOutcome,
)

TABLE = "sre-on-call-test-traces"


@pytest.fixture()
def dynamodb():
    """moto-mocked DDB table mirroring the real traces table key schema."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.Table(TABLE).meta.client.get_waiter("table_exists").wait(TableName=TABLE)
        yield ddb


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TRACES_TABLE_NAME", raising=False)


def _store(dynamodb) -> IncidentHistoryStore:
    return IncidentHistoryStore(
        table_name=TABLE, dynamodb_resource=dynamodb, region_name="us-east-1"
    )


def _outcome(investigation_id: str, embedding: list[float], **overrides) -> IncidentOutcome:
    base = dict(
        investigation_id=investigation_id,
        alert_text="CPU high on web-1",
        summary="Investigated; CPU saturation from a runaway worker.",
        embedding=embedding,
        platform="slack",
        channel_id="C123",
        message_id="171.5",
        alert_timestamp="2026-06-08T10:00:00+00:00",
        root_cause="Runaway worker pinned the CPU.",
        thread_link="https://example.slack.com/archives/C123/p171",
    )
    base.update(overrides)
    return IncidentOutcome(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def test_put_outcome_writes_history_item(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(_outcome("inv-1", [1.0, 0.0, 0.0]))

    item = dynamodb.Table(TABLE).get_item(
        Key={"pk": f"{HISTORY_PK_PREFIX}inv-1"}
    )["Item"]
    assert item["record_type"] == RECORD_TYPE
    assert item["investigation_id"] == "inv-1"
    assert item["alert_text"] == "CPU high on web-1"
    assert item["root_cause"] == "Runaway worker pinned the CPU."
    # channel id is stored under hist_channel_id, NOT channel_id, so these
    # rows never enter the manifest's channel GSI.
    assert "channel_id" not in item
    assert item["hist_channel_id"] == "C123"
    assert "ttl" in item


def test_put_outcome_omits_absent_optional_fields(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(
        _outcome("inv-2", [0.0, 1.0, 0.0], root_cause=None, thread_link=None)
    )
    item = dynamodb.Table(TABLE).get_item(
        Key={"pk": f"{HISTORY_PK_PREFIX}inv-2"}
    )["Item"]
    assert "root_cause" not in item
    assert "thread_link" not in item


def test_put_outcome_fail_open_on_error():
    class Boom:
        def Table(self, _name):
            class T:
                def put_item(self, **_kwargs):
                    raise RuntimeError("ddb down")

            return T()

    store = IncidentHistoryStore(table_name=TABLE, dynamodb_resource=Boom())
    # Must not raise.
    store.put_outcome(_outcome("inv-x", [1.0, 0.0]))


# ---------------------------------------------------------------------------
# Read — search_similar
# ---------------------------------------------------------------------------


def test_search_ranks_by_cosine_and_respects_top_k(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(_outcome("near", [1.0, 0.0, 0.0]))
    store.put_outcome(_outcome("mid", [0.7, 0.7, 0.0]))
    store.put_outcome(_outcome("far", [0.0, 0.0, 1.0]))

    hits = store.search_similar([1.0, 0.0, 0.0], top_k=2, min_score=0.1)
    assert [h.investigation_id for h in hits] == ["near", "mid"]
    assert hits[0].score > hits[1].score
    assert hits[0].root_cause == "Runaway worker pinned the CPU."


def test_search_excludes_current_investigation(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(_outcome("self", [1.0, 0.0, 0.0]))
    store.put_outcome(_outcome("other", [0.9, 0.1, 0.0]))

    hits = store.search_similar(
        [1.0, 0.0, 0.0], min_score=0.1, exclude_investigation_id="self"
    )
    assert [h.investigation_id for h in hits] == ["other"]


def test_search_filters_below_min_score(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(_outcome("orthogonal", [0.0, 1.0, 0.0]))

    assert store.search_similar([1.0, 0.0, 0.0], min_score=0.5) == []


def test_search_empty_store_returns_empty(dynamodb):
    store = _store(dynamodb)
    assert store.search_similar([1.0, 0.0, 0.0]) == []


def test_search_empty_query_returns_empty(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(_outcome("a", [1.0, 0.0, 0.0]))
    assert store.search_similar([]) == []


def test_search_ignores_non_history_rows(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(_outcome("hist", [1.0, 0.0, 0.0]))
    # A trace-manifest-style row (no record_type / embedding) shares the table.
    dynamodb.Table(TABLE).put_item(Item={"pk": "inv-manifest", "status": "completed"})

    hits = store.search_similar([1.0, 0.0, 0.0], min_score=0.1)
    assert [h.investigation_id for h in hits] == ["hist"]


# ---------------------------------------------------------------------------
# count_recent
# ---------------------------------------------------------------------------


def test_count_recent_counts_history_rows(dynamodb):
    store = _store(dynamodb)
    store.put_outcome(_outcome("a", [1.0, 0.0, 0.0]))
    store.put_outcome(_outcome("b", [0.0, 1.0, 0.0]))
    assert store.count_recent(days=30) == 2


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_from_env_unset_returns_none(monkeypatch):
    monkeypatch.delenv("TRACES_TABLE_NAME", raising=False)
    assert IncidentHistoryStore.from_env() is None


def test_from_env_set_returns_store(monkeypatch):
    monkeypatch.setenv("TRACES_TABLE_NAME", TABLE)
    with mock_aws():
        store = IncidentHistoryStore.from_env()
    assert store is not None


# ---------------------------------------------------------------------------
# Acceptance — investigation N stored, similar alert N+1 surfaces it (issue #30)
# ---------------------------------------------------------------------------


class _FixedEmbedder:
    def __init__(self, vector):
        self._vector = vector

    def embed(self, text):
        del text
        return self._vector


def test_write_then_read_surfaces_prior_incident(dynamodb):
    from agents.incident_history.tools import _find_similar

    store = _store(dynamodb)

    # Investigation N: a finished investigation is recorded.
    store.put_outcome(
        _outcome(
            "inv-N",
            [1.0, 0.0, 0.0],
            alert_text="OOMKilled on payments-api",
            root_cause="Memory limit too low after the 2.3 release.",
        )
    )

    # Investigation N+1: a near-identical alert embeds to a close vector.
    result = _find_similar(
        "OOMKilled on payments-api again",
        store=store,
        embedder=_FixedEmbedder([0.98, 0.02, 0.0]),
        min_score=0.5,
    )

    assert result.status == "success"
    assert len(result.findings) == 1
    assert "Memory limit too low" in result.findings[0].content


def test_empty_store_is_clean_no_results(dynamodb):
    from agents.incident_history.tools import _find_similar

    result = _find_similar(
        "first ever alert",
        store=_store(dynamodb),
        embedder=_FixedEmbedder([1.0, 0.0, 0.0]),
    )
    assert result.status == "success"
    assert result.findings == []
    assert "No similar past incidents" in result.summary
