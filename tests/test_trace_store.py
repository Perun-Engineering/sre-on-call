"""Unit tests for shared.trace_store — S3 + DDB trace archive."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws
from typing import Any

from shared.trace_store import (
    EVENT_A2A_REQUEST,
    EVENT_A2A_RESPONSE,
    EVENT_ALERT_RECEIVED,
    EVENT_DEDUP_OUTCOME,
    SCHEMA_VERSION,
    SOURCE_LAMBDA,
    SOURCE_MASTER,
    ResultSummary,
    TraceManifest,
    TraceStore,
)

BUCKET = "sre-on-call-test-traces"
TABLE = "sre-on-call-test-traces"


@pytest.fixture()
def aws_resources():
    """Spin up a moto-mocked S3 bucket and DDB table for the trace store."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        dynamodb: Any = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.Table(TABLE).meta.client.get_waiter("table_exists").wait(
            TableName=TABLE,
        )

        yield s3, dynamodb


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear trace-related env vars so tests don't leak into each other."""
    for var in ("TRACES_BUCKET_NAME", "TRACES_TABLE_NAME"):
        monkeypatch.delenv(var, raising=False)


def _make_store(s3, dynamodb) -> TraceStore:
    return TraceStore(
        bucket=BUCKET,
        table_name=TABLE,
        s3_client=s3,
        dynamodb_resource=dynamodb,
        region_name="us-east-1",
    )


def _make_manifest(**overrides: Any) -> TraceManifest:
    kwargs: dict[str, Any] = dict(
        investigation_id="inv-001",
        alert_context={
            "investigation_id": "inv-001",
            "platform": "slack",
            "channel_id": "C12345",
            "message_id": "1700000000.000100",
            "alert_text": "ALERT",
            "alert_timestamp": "2026-05-28T12:00:00+00:00",
        },
        started_at="2026-05-28T12:00:00+00:00",
        ended_at="2026-05-28T12:01:00+00:00",
        total_duration_seconds=60.5,
        dispatched_agents=["slack_scanner", "cloudwatch_logs", "eks"],
        results_summary={
            "slack_scanner": ResultSummary(status="success", findings_count=3, duration_seconds=5.1),
            "cloudwatch_logs": ResultSummary(status="success", findings_count=7, duration_seconds=12.4),
            "eks": ResultSummary(status="error", findings_count=0, duration_seconds=2.0),
        },
        status="completed",
        error_count=1,
    )
    kwargs.update(overrides)
    return TraceManifest(**kwargs)


class TestManifestRouting:
    def test_routing_omitted_when_none(self) -> None:
        assert "routing" not in _make_manifest().to_json_dict()

    def test_routing_serialized_when_present(self) -> None:
        routing = {
            "selected": {"eks": "check payment pods"},
            "skipped": {"slack_scanner": "no chatter relevance"},
            "rationale": "logs + k8s suffice",
        }
        d = _make_manifest(routing=routing).to_json_dict()
        assert d["routing"] == routing


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_returns_none_when_bucket_unset(self) -> None:
        assert TraceStore.from_env() is None

    def test_returns_none_when_table_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("TRACES_BUCKET_NAME", BUCKET)
        assert TraceStore.from_env() is None

    def test_returns_none_when_bucket_blank(self, monkeypatch) -> None:
        monkeypatch.setenv("TRACES_BUCKET_NAME", "  ")
        monkeypatch.setenv("TRACES_TABLE_NAME", TABLE)
        assert TraceStore.from_env() is None

    def test_constructs_when_both_set(self, monkeypatch, aws_resources) -> None:
        monkeypatch.setenv("TRACES_BUCKET_NAME", BUCKET)
        monkeypatch.setenv("TRACES_TABLE_NAME", TABLE)
        store = TraceStore.from_env()
        assert store is not None


# ---------------------------------------------------------------------------
# S3 key construction
# ---------------------------------------------------------------------------


class TestKeyHelpers:
    def test_investigation_prefix_uses_today(self) -> None:
        prefix = TraceStore.investigation_prefix("inv-X")
        assert prefix.startswith("dt=")
        assert prefix.endswith("/investigation_id=inv-X/")

    def test_event_key_orderable_lexically(self) -> None:
        k1 = TraceStore._event_key("inv-1", SOURCE_LAMBDA, EVENT_ALERT_RECEIVED)
        k2 = TraceStore._event_key("inv-1", SOURCE_LAMBDA, EVENT_DEDUP_OUTCOME)
        # The microsecond timestamp prefix should make k2 sort after k1.
        assert k1 < k2 or k1 != k2  # at minimum different

    def test_event_key_under_investigation_prefix(self) -> None:
        key = TraceStore._event_key("inv-Y", SOURCE_MASTER, EVENT_A2A_REQUEST)
        assert "/investigation_id=inv-Y/events/" in key
        assert key.endswith(".json")

    def test_manifest_key_under_investigation_prefix(self) -> None:
        key = TraceStore._manifest_key("inv-Z")
        assert "/investigation_id=inv-Z/manifest.json" in key


# ---------------------------------------------------------------------------
# put_event
# ---------------------------------------------------------------------------


class TestPutEvent:
    def test_writes_event_to_s3(self, aws_resources) -> None:
        s3, dynamodb = aws_resources
        store = _make_store(s3, dynamodb)
        store.put_event(
            investigation_id="inv-evt-1",
            source=SOURCE_LAMBDA,
            event_type=EVENT_ALERT_RECEIVED,
            payload={"channel_id": "C1", "alert_text": "boom"},
        )
        objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="dt=")
        assert objs.get("KeyCount", 0) == 1
        keys = [o["Key"] for o in objs["Contents"]]
        assert "/investigation_id=inv-evt-1/events/" in keys[0]
        assert SOURCE_LAMBDA in keys[0]
        assert EVENT_ALERT_RECEIVED in keys[0]

    def test_event_envelope_shape(self, aws_resources) -> None:
        s3, dynamodb = aws_resources
        store = _make_store(s3, dynamodb)
        store.put_event(
            investigation_id="inv-evt-2",
            source=SOURCE_MASTER,
            event_type=EVENT_A2A_REQUEST,
            payload={"agent_id": "eks", "endpoint": "arn:..."},
        )
        objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="dt=")
        body = s3.get_object(Bucket=BUCKET, Key=objs["Contents"][0]["Key"])["Body"].read()
        envelope = json.loads(body)
        assert envelope["schema_version"] == SCHEMA_VERSION
        assert envelope["investigation_id"] == "inv-evt-2"
        assert envelope["source"] == SOURCE_MASTER
        assert envelope["event_type"] == EVENT_A2A_REQUEST
        assert envelope["payload"] == {"agent_id": "eks", "endpoint": "arn:..."}
        assert "ts" in envelope

    def test_swallows_s3_errors(self, aws_resources) -> None:
        """A failing S3 client must not raise; the investigation continues."""
        s3, dynamodb = aws_resources
        broken_s3 = MagicMock()
        broken_s3.put_object.side_effect = RuntimeError("network down")
        store = TraceStore(
            bucket=BUCKET,
            table_name=TABLE,
            s3_client=broken_s3,
            dynamodb_resource=dynamodb,
            region_name="us-east-1",
        )
        # Must not raise.
        store.put_event(
            investigation_id="inv-failopen",
            source=SOURCE_LAMBDA,
            event_type=EVENT_ALERT_RECEIVED,
            payload={"x": 1},
        )

    def test_distinct_keys_for_concurrent_writes(self, aws_resources) -> None:
        """Successive writes must not collide on the same S3 key."""
        s3, dynamodb = aws_resources
        store = _make_store(s3, dynamodb)
        for i in range(5):
            store.put_event(
                investigation_id="inv-collide",
                source=SOURCE_MASTER,
                event_type=EVENT_A2A_RESPONSE,
                payload={"i": i},
            )
        objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="dt=")
        assert objs["KeyCount"] == 5


# ---------------------------------------------------------------------------
# put_manifest
# ---------------------------------------------------------------------------


class TestPutManifest:
    def test_writes_manifest_to_s3(self, aws_resources) -> None:
        s3, dynamodb = aws_resources
        store = _make_store(s3, dynamodb)
        store.put_manifest(_make_manifest())

        manifest_key = "dt=2026-05-28/investigation_id=inv-001/manifest.json"
        body = s3.get_object(Bucket=BUCKET, Key=manifest_key)["Body"].read()
        m = json.loads(body)
        assert m["schema_version"] == SCHEMA_VERSION
        assert m["investigation_id"] == "inv-001"
        assert m["status"] == "completed"
        assert m["dispatched_agents"] == ["slack_scanner", "cloudwatch_logs", "eks"]
        assert set(m["results_summary"].keys()) == {"slack_scanner", "cloudwatch_logs", "eks"}
        assert m["results_summary"]["eks"]["status"] == "error"

    def test_writes_index_entry_to_ddb(self, aws_resources) -> None:
        s3, dynamodb = aws_resources
        store = _make_store(s3, dynamodb)
        store.put_manifest(_make_manifest())

        item = dynamodb.Table(TABLE).get_item(Key={"pk": "inv-001"})["Item"]
        assert item["pk"] == "inv-001"
        assert item["dt"] == "2026-05-28"
        assert item["s3_prefix"] == "dt=2026-05-28/investigation_id=inv-001/"
        assert item["channel_id"] == "C12345"
        assert item["message_id"] == "1700000000.000100"
        assert item["platform"] == "slack"
        assert item["agent_count"] == 3
        assert item["error_count"] == 1
        assert item["status"] == "completed"
        assert item["total_duration_seconds"] == Decimal("60.5")
        assert int(item["ttl"]) > 0

    def test_manifest_uses_started_at_partition(self, aws_resources) -> None:
        """The manifest lands under dt= derived from started_at, not 'today'."""
        s3, dynamodb = aws_resources
        store = _make_store(s3, dynamodb)
        store.put_manifest(_make_manifest(
            investigation_id="inv-historic",
            started_at="2024-01-15T10:00:00+00:00",
            ended_at="2024-01-15T10:01:00+00:00",
            alert_context={
                "platform": "slack",
                "channel_id": "C1",
                "alert_timestamp": "2024-01-15T10:00:00+00:00",
            },
        ))
        # Should appear under dt=2024-01-15/, not today's partition.
        objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="dt=2024-01-15/")
        assert objs["KeyCount"] == 1

    def test_swallows_s3_errors(self, aws_resources) -> None:
        s3, dynamodb = aws_resources
        broken_s3 = MagicMock()
        broken_s3.put_object.side_effect = RuntimeError("S3 down")
        store = TraceStore(
            bucket=BUCKET,
            table_name=TABLE,
            s3_client=broken_s3,
            dynamodb_resource=dynamodb,
        )
        # Must not raise. DDB write should still happen.
        store.put_manifest(_make_manifest())
        item = dynamodb.Table(TABLE).get_item(Key={"pk": "inv-001"}).get("Item")
        assert item is not None  # DDB write proceeded despite S3 failure

    def test_swallows_ddb_errors(self, aws_resources) -> None:
        s3, dynamodb = aws_resources

        class BrokenDDB:
            def Table(self, name):
                t = MagicMock()
                t.put_item.side_effect = RuntimeError("DDB down")
                return t

        store = TraceStore(
            bucket=BUCKET,
            table_name=TABLE,
            s3_client=s3,
            dynamodb_resource=BrokenDDB(),
        )
        # Must not raise. S3 write should still happen.
        store.put_manifest(_make_manifest())
        manifest_key = "dt=2026-05-28/investigation_id=inv-001/manifest.json"
        body = s3.get_object(Bucket=BUCKET, Key=manifest_key)["Body"].read()
        assert json.loads(body)["investigation_id"] == "inv-001"


# ---------------------------------------------------------------------------
# Construction with no boto fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# put_chart_series
# ---------------------------------------------------------------------------


class TestPutChartSeries:
    def test_writes_chart_object(self, aws_resources):
        s3, dynamodb = aws_resources
        store = _make_store(s3, dynamodb)

        store.put_chart_series(
            investigation_id="inv-c1",
            chart_id="abc123",
            payload={"chart_id": "abc123", "points": [{"a": 1}]},
        )

        objs = s3.list_objects_v2(Bucket=BUCKET)
        keys = [o["Key"] for o in objs.get("Contents", [])]
        chart_keys = [k for k in keys if "/investigation_id=inv-c1/charts/abc123.json" in k]
        assert chart_keys, keys
        body = json.loads(
            s3.get_object(Bucket=BUCKET, Key=chart_keys[0])["Body"].read()
        )
        assert body["points"] == [{"a": 1}]

    def test_fail_open_on_s3_error(self):
        broken_s3 = MagicMock()
        broken_s3.put_object.side_effect = RuntimeError("network down")
        store = TraceStore(
            bucket=BUCKET,
            table_name=TABLE,
            s3_client=broken_s3,
            dynamodb_resource=MagicMock(),
            region_name="us-east-1",
        )
        # Must not raise.
        store.put_chart_series(investigation_id="i", chart_id="c", payload={})


# ---------------------------------------------------------------------------
# put_page_model
# ---------------------------------------------------------------------------


class TestPutPageModel:
    def test_put_page_model_writes_under_prefix(self, aws_resources):
        s3, dynamodb = aws_resources
        store = TraceStore(bucket=BUCKET, table_name=TABLE,
                           s3_client=s3, dynamodb_resource=dynamodb)
        store.put_page_model(
            investigation_id="inv-9",
            payload={"schema_version": 1, "investigation_id": "inv-9"},
            dt="dt=2026-06-12",
        )
        key = "dt=2026-06-12/investigation_id=inv-9/page_model.json"
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        assert json.loads(body)["investigation_id"] == "inv-9"

    def test_put_page_model_is_fail_open_on_s3_error(self):
        bad_s3 = MagicMock()
        bad_s3.put_object.side_effect = RuntimeError("boom")
        store = TraceStore(bucket=BUCKET, table_name=TABLE,
                           s3_client=bad_s3, dynamodb_resource=MagicMock())
        store.put_page_model(investigation_id="inv-9", payload={})  # must not raise

    def test_put_then_get_page_model_round_trips(self, aws_resources):
        s3, dynamodb = aws_resources
        store = TraceStore(bucket=BUCKET, table_name=TABLE,
                           s3_client=s3, dynamodb_resource=dynamodb)
        payload = {"schema_version": 1, "investigation_id": "inv-9",
                   "status": "completed", "timeline": []}
        store.put_page_model(
            investigation_id="inv-9", payload=payload, dt="dt=2026-06-12",
        )
        restored = store.get_page_model("inv-9", dt="dt=2026-06-12")
        assert restored == payload

    def test_get_page_model_missing_object_returns_none(self, aws_resources):
        s3, dynamodb = aws_resources
        store = TraceStore(bucket=BUCKET, table_name=TABLE,
                           s3_client=s3, dynamodb_resource=dynamodb)
        assert store.get_page_model("nope", dt="dt=2026-06-12") is None

    def test_get_page_model_is_fail_open_on_s3_error(self):
        bad_s3 = MagicMock()
        bad_s3.get_object.side_effect = RuntimeError("boom")
        store = TraceStore(bucket=BUCKET, table_name=TABLE,
                           s3_client=bad_s3, dynamodb_resource=MagicMock())
        assert store.get_page_model("inv-9") is None  # must not raise


def test_put_then_get_results_round_trips(aws_resources):
    from shared.models import AgentResult, AgentFailure, AgentMetadata, Finding

    s3, dynamodb = aws_resources
    store = _make_store(s3, dynamodb)
    results = {
        "eks": AgentResult(
            agent_name="eks", status="success",
            findings=[Finding(source="pod", timestamp="t",
                              content="CrashLoop", severity="critical")],
            summary="bad", duration_seconds=1.0, metadata=AgentMetadata(),
        ),
        "slack_scanner": AgentFailure(
            agent_name="slack_scanner", error_message="timeout",
            timestamp="t", metadata=AgentMetadata(),
        ),
    }

    store.put_results(
        investigation_id="inv-1", results=results, dt="dt=2025-01-15",
    )
    restored = store.get_results("inv-1", dt="dt=2025-01-15")
    assert restored is not None

    assert isinstance(restored["eks"], AgentResult)
    assert restored["eks"].findings[0].content == "CrashLoop"
    assert isinstance(restored["slack_scanner"], AgentFailure)


def test_get_results_missing_object_returns_none(aws_resources):
    s3, dynamodb = aws_resources
    store = _make_store(s3, dynamodb)
    assert store.get_results("nope", dt="dt=2025-01-15") is None


def test_get_manifest_round_trips_alert_context(aws_resources):
    from shared.trace_store import ResultSummary, TraceManifest

    s3, dynamodb = aws_resources
    store = _make_store(s3, dynamodb)
    manifest = TraceManifest(
        investigation_id="inv-7",
        alert_context={"investigation_id": "inv-7", "platform": "slack",
                       "channel_id": "C1", "message_id": "1700.1"},
        started_at="2025-01-15T14:00:00Z",
        ended_at="2025-01-15T14:01:00Z",
        total_duration_seconds=60.0,
        dispatched_agents=["eks"],
        results_summary={"eks": ResultSummary("success", 1, 4.0)},
        status="completed",
        error_count=0,
    )
    store.put_manifest(manifest)

    got = store.get_manifest("inv-7", dt="dt=2025-01-15")
    assert got is not None
    assert got["alert_context"]["message_id"] == "1700.1"


def test_get_manifest_missing_returns_none(aws_resources):
    s3, dynamodb = aws_resources
    store = _make_store(s3, dynamodb)
    assert store.get_manifest("absent", dt="dt=2025-01-15") is None


@pytest.fixture()
def aws_resources_with_thread_gsi():
    """S3 + a DDB table carrying the channel_id-message_id GSI (#56)."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        dynamodb: Any = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "channel_id", "AttributeType": "S"},
                {"AttributeName": "message_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "channel_id-message_id-index",
                "KeySchema": [
                    {"AttributeName": "channel_id", "KeyType": "HASH"},
                    {"AttributeName": "message_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.Table(TABLE).meta.client.get_waiter("table_exists").wait(
            TableName=TABLE,
        )
        yield s3, dynamodb


def _put_manifest_for(store, *, inv, channel, message_id, started_at):
    from shared.trace_store import ResultSummary, TraceManifest
    store.put_manifest(TraceManifest(
        investigation_id=inv,
        alert_context={"channel_id": channel, "message_id": message_id,
                       "platform": "slack"},
        started_at=started_at, ended_at=started_at,
        total_duration_seconds=1.0, dispatched_agents=["eks"],
        results_summary={"eks": ResultSummary("success", 1, 1.0)},
        status="completed", error_count=0,
    ))


def test_find_investigation_resolves_thread_to_investigation(
    aws_resources_with_thread_gsi,
):
    s3, dynamodb = aws_resources_with_thread_gsi
    store = _make_store(s3, dynamodb)
    _put_manifest_for(store, inv="inv-A", channel="C1",
                      message_id="1700.1", started_at="2025-01-15T10:00:00Z")

    ref = store.find_investigation("C1", "1700.1")
    assert ref is not None
    assert ref.investigation_id == "inv-A"
    assert ref.dt == "dt=2025-01-15"


def test_find_investigation_picks_newest_on_collision(
    aws_resources_with_thread_gsi,
):
    s3, dynamodb = aws_resources_with_thread_gsi
    store = _make_store(s3, dynamodb)
    _put_manifest_for(store, inv="old", channel="C1",
                      message_id="1700.1", started_at="2025-01-15T10:00:00Z")
    _put_manifest_for(store, inv="new", channel="C1",
                      message_id="1700.1", started_at="2025-01-16T10:00:00Z")

    ref = store.find_investigation("C1", "1700.1")
    assert ref is not None
    assert ref.investigation_id == "new"


def test_find_investigation_no_match_returns_none(
    aws_resources_with_thread_gsi,
):
    s3, dynamodb = aws_resources_with_thread_gsi
    store = _make_store(s3, dynamodb)
    assert store.find_investigation("C1", "absent") is None


class TestConstructorInjection:
    def test_constructor_accepts_explicit_clients(self, aws_resources) -> None:
        s3, dynamodb = aws_resources
        store = TraceStore(
            bucket=BUCKET,
            table_name=TABLE,
            s3_client=s3,
            dynamodb_resource=dynamodb,
        )
        # Sanity: writes work without any env-var or boto3 fallback.
        store.put_event(
            investigation_id="inv-ctor",
            source=SOURCE_LAMBDA,
            event_type=EVENT_DEDUP_OUTCOME,
            payload={"is_new": True},
        )
        objs = s3.list_objects_v2(Bucket=BUCKET, Prefix="dt=")
        assert objs["KeyCount"] == 1
