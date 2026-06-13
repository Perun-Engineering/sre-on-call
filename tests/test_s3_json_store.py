"""Unit tests for shared.s3_json_store — the S3 JSON-blob persistence adapter."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import boto3
import pytest
from moto import mock_aws

from shared.s3_json_store import S3JsonStore


@pytest.fixture()
def s3():
    with mock_aws():
        client: Any = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield client


def _store(s3, **kw) -> S3JsonStore:
    return S3JsonStore("test-bucket", s3_client=s3, **kw)


def test_put_then_get_round_trips_json(s3) -> None:
    store = _store(s3)
    store.put_json("k.json", {"hello": "world", "n": 3})
    assert store.get_json("k.json") == {"hello": "world", "n": 3}


def test_encoder_handles_dataclasses_and_decimal(s3) -> None:
    @dataclass
    class Inner:
        x: int

    store = _store(s3)
    store.put_json("k.json", {"obj": Inner(x=1), "amount": Decimal("0.5")})
    assert store.get_json("k.json") == {"obj": {"x": 1}, "amount": 0.5}


def test_encoder_prefers_to_json_dict(s3) -> None:
    class Custom:
        def to_json_dict(self) -> dict:
            return {"custom": True}

    store = _store(s3)
    store.put_json("k.json", {"c": Custom()})
    assert store.get_json("k.json") == {"c": {"custom": True}}


class TestFailOpen:
    """fail_open swallows missing keys + S3 errors; the default re-raises."""

    def test_get_missing_key_returns_none_when_fail_open(self, s3) -> None:
        assert _store(s3, fail_open=True).get_json("absent.json") is None

    def test_get_missing_key_raises_by_default(self, s3) -> None:
        with pytest.raises(Exception):
            _store(s3, fail_open=False).get_json("absent.json")

    def test_put_to_missing_bucket_swallows_when_fail_open(self) -> None:
        with mock_aws():
            client: Any = boto3.client("s3", region_name="us-east-1")
            store = S3JsonStore("nope", s3_client=client, fail_open=True)
            assert store.put_json("k.json", {"a": 1}) is None

    def test_put_to_missing_bucket_raises_by_default(self) -> None:
        with mock_aws():
            client: Any = boto3.client("s3", region_name="us-east-1")
            store = S3JsonStore("nope", s3_client=client, fail_open=False)
            with pytest.raises(Exception):
                store.put_json("k.json", {"a": 1})
