"""Unit tests for shared.dynamo_table — the DynamoDB persistence adapter."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from shared.dynamo_table import DynamoTable


@pytest.fixture()
def ddb():
    with mock_aws():
        resource: Any = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="t",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource


def _table(ddb, **kw) -> DynamoTable:
    return DynamoTable("t", dynamodb_resource=ddb, **kw)


def test_put_then_get_round_trips_an_item(ddb) -> None:
    table = _table(ddb)
    table.put({"pk": "a", "name": "alpha"})
    assert table.get({"pk": "a"}) == {"pk": "a", "name": "alpha"}


def test_get_returns_none_on_miss(ddb) -> None:
    assert _table(ddb).get({"pk": "absent"}) is None


def test_floats_round_trip_as_floats_not_decimals(ddb) -> None:
    table = _table(ddb)
    table.put({"pk": "a", "cost": 0.25, "nested": {"dur": 1.5}, "ttl": 100})
    item = table.get({"pk": "a"})
    assert item is not None
    assert item["cost"] == 0.25 and isinstance(item["cost"], float)
    assert item["nested"]["dur"] == 1.5 and isinstance(item["nested"]["dur"], float)
    # ints stay numerically intact (read back as float, callers int() as needed)
    assert int(item["ttl"]) == 100


def test_scan_all_drains_pagination(ddb) -> None:
    table = _table(ddb)
    for i in range(5):
        table.put({"pk": f"k{i}"})
    assert {i["pk"] for i in table.scan_all()} == {f"k{i}" for i in range(5)}


def test_scan_all_follows_last_evaluated_key() -> None:
    """The loop must drain ``LastEvaluatedKey`` across pages, not stop at page 1."""

    class _PagedTable:
        name = "t"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def scan(self, **kwargs):
            self.calls.append(kwargs)
            if "ExclusiveStartKey" not in kwargs:
                return {"Items": [{"pk": "p1"}], "LastEvaluatedKey": {"pk": "p1"}}
            return {"Items": [{"pk": "p2"}]}

    class _Res:
        def __init__(self, t): self._t = t
        def Table(self, _name): return self._t

    paged = _PagedTable()
    table = DynamoTable("t", dynamodb_resource=_Res(paged))
    assert [i["pk"] for i in table.scan_all()] == ["p1", "p2"]
    assert len(paged.calls) == 2


def test_scan_all_applies_filter(ddb) -> None:
    from boto3.dynamodb.conditions import Attr

    table = _table(ddb)
    table.put({"pk": "a", "kind": "keep"})
    table.put({"pk": "b", "kind": "drop"})
    got = list(table.scan_all(Attr("kind").eq("keep")))
    assert [i["pk"] for i in got] == ["a"]


def test_query_returns_gsi_matches() -> None:
    from boto3.dynamodb.conditions import Key

    with mock_aws():
        resource: Any = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="g",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "channel_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "channel-index",
                "KeySchema": [{"AttributeName": "channel_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        table = DynamoTable("g", dynamodb_resource=resource)
        table.put({"pk": "1", "channel_id": "C1"})
        table.put({"pk": "2", "channel_id": "C2"})
        hits = table.query("channel-index", Key("channel_id").eq("C1"))
        assert [h["pk"] for h in hits] == ["1"]


class TestFailOpen:
    """fail_open swallows AWS errors; the default re-raises."""

    def _missing_table(self, *, fail_open: bool) -> DynamoTable:
        with mock_aws():
            resource: Any = boto3.resource("dynamodb", region_name="us-east-1")
            # No create_table -> every operation raises ResourceNotFoundException.
            return DynamoTable("nope", dynamodb_resource=resource, fail_open=fail_open)

    def test_put_swallows_when_fail_open(self) -> None:
        assert self._missing_table(fail_open=True).put({"pk": "a"}) is None

    def test_get_returns_none_when_fail_open(self) -> None:
        assert self._missing_table(fail_open=True).get({"pk": "a"}) is None

    def test_scan_all_yields_nothing_when_fail_open(self) -> None:
        assert list(self._missing_table(fail_open=True).scan_all()) == []

    def test_put_raises_by_default(self) -> None:
        with pytest.raises(Exception):
            self._missing_table(fail_open=False).put({"pk": "a"})

    def test_scan_all_raises_by_default(self) -> None:
        with pytest.raises(Exception):
            list(self._missing_table(fail_open=False).scan_all())
