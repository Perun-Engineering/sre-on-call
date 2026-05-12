"""Unit tests for lambda_adapter.dedup — DynamoDB deduplication store."""

from __future__ import annotations

import time
import uuid

import boto3
import pytest
from moto import mock_aws

from lambda_adapter.dedup import DeduplicationStore, DEFAULT_TABLE_NAME, _TTL_SECONDS


@pytest.fixture()
def dynamodb_table():
    """Create a mocked DynamoDB table for the dedup store."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName=DEFAULT_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=DEFAULT_TABLE_NAME)
        yield dynamodb


class TestDeduplicationStore:
    """Tests for DeduplicationStore.record_if_new."""

    def test_new_alert_returns_true(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        result = store.record_if_new("C123", "1234567890.000001", str(uuid.uuid4()))
        assert result is True

    def test_duplicate_alert_returns_false(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        inv_id = str(uuid.uuid4())
        store.record_if_new("C123", "1234567890.000001", inv_id)
        result = store.record_if_new("C123", "1234567890.000001", str(uuid.uuid4()))
        assert result is False

    def test_different_channel_same_ts_is_new(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        store.record_if_new("C111", "1234567890.000001", str(uuid.uuid4()))
        result = store.record_if_new("C222", "1234567890.000001", str(uuid.uuid4()))
        assert result is True

    def test_same_channel_different_ts_is_new(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        store.record_if_new("C123", "1111111111.000001", str(uuid.uuid4()))
        result = store.record_if_new("C123", "2222222222.000002", str(uuid.uuid4()))
        assert result is True

    def test_partition_key_format(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        store.record_if_new("C123", "9999999999.000001", str(uuid.uuid4()))
        table = dynamodb_table.Table(DEFAULT_TABLE_NAME)
        resp = table.get_item(Key={"pk": "slack#C123#9999999999.000001"})
        assert "Item" in resp

    def test_item_has_correct_attributes(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        inv_id = str(uuid.uuid4())
        before = int(time.time())
        store.record_if_new("C123", "1234567890.000001", inv_id)
        after = int(time.time())

        table = dynamodb_table.Table(DEFAULT_TABLE_NAME)
        item = table.get_item(Key={"pk": "slack#C123#1234567890.000001"})["Item"]

        assert item["investigation_id"] == inv_id
        assert item["status"] == "IN_PROGRESS"
        assert before <= int(item["created_at"]) <= after
        assert int(item["ttl"]) == int(item["created_at"]) + _TTL_SECONDS

    def test_duplicate_does_not_overwrite_original(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        original_id = str(uuid.uuid4())
        store.record_if_new("C123", "1234567890.000001", original_id)
        store.record_if_new("C123", "1234567890.000001", str(uuid.uuid4()))

        table = dynamodb_table.Table(DEFAULT_TABLE_NAME)
        item = table.get_item(Key={"pk": "slack#C123#1234567890.000001"})["Item"]
        assert item["investigation_id"] == original_id

    def test_ttl_is_24_hours_after_created_at(self, dynamodb_table) -> None:
        store = DeduplicationStore(dynamodb_resource=dynamodb_table)
        store.record_if_new("C123", "1234567890.000001", str(uuid.uuid4()))

        table = dynamodb_table.Table(DEFAULT_TABLE_NAME)
        item = table.get_item(Key={"pk": "slack#C123#1234567890.000001"})["Item"]
        assert int(item["ttl"]) - int(item["created_at"]) == 86400
