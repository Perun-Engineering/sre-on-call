"""Property-based tests for DynamoDB deduplication store."""

from __future__ import annotations

import uuid

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from lambda_adapter.dedup import DEFAULT_TABLE_NAME, DeduplicationStore


def _create_dedup_table():
    """Create a mocked DynamoDB table and return the resource."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName=DEFAULT_TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb


# Strategies: channel IDs and message timestamps as non-empty printable strings.
# Slack channel IDs are typically like "C01ABCDEF" and message_ts like "1234567890.123456",
# but the dedup store works with any string pair.
channel_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=30,
)

message_timestamps = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
    min_size=1,
    max_size=30,
)


@settings(max_examples=100)
@given(channel_id=channel_ids, message_ts=message_timestamps)
def test_deduplication_idempotence(channel_id: str, message_ts: str) -> None:
    """
    For any (channel_id, message_ts) pair:
    1. The first call to ``record_if_new`` SHALL return True (new alert).
    2. A second call with the same pair SHALL return False (duplicate).
    3. The store state after the second call SHALL be identical to the state
       after the first call (same investigation_id, same created_at, etc.).
    """
    with mock_aws():
        dynamodb = _create_dedup_table()
        store = DeduplicationStore(dynamodb_resource=dynamodb)
        table = dynamodb.Table(DEFAULT_TABLE_NAME)

        first_inv_id = str(uuid.uuid4())
        second_inv_id = str(uuid.uuid4())

        # First call — should be accepted as new
        assert store.record_if_new(channel_id, message_ts, first_inv_id) is True

        # Capture store state after first write
        pk = f"slack#{channel_id}#{message_ts}"
        item_after_first = table.get_item(Key={"pk": pk})["Item"]  # type: ignore[typeddict-item]

        # Second call with the same pair — should be rejected as duplicate
        assert store.record_if_new(channel_id, message_ts, second_inv_id) is False

        # Capture store state after second write attempt
        item_after_second = table.get_item(Key={"pk": pk})["Item"]  # type: ignore[typeddict-item]

        # Store state must be unchanged: same investigation_id, created_at, ttl, status
        assert item_after_second == item_after_first
        assert item_after_second["investigation_id"] == first_inv_id
