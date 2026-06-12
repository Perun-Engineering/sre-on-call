"""DynamoDB-backed deduplication store for alert investigations."""

from __future__ import annotations

import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Default table name; overridable for testing.
DEFAULT_TABLE_NAME = "sre-on-call-dedup"

# Dedup records expire after 24 hours.
_TTL_SECONDS = 86400


class DeduplicationStore:
    """Prevents duplicate investigations using DynamoDB conditional writes.

    Each alert is keyed by ``{platform}#{channel_id}#{message_id}``.  A conditional
    ``PutItem`` with ``attribute_not_exists(pk)`` ensures that only the
    first caller wins — subsequent attempts for the same key are treated
    as duplicates.
    """

    def __init__(
        self,
        table_name: str = DEFAULT_TABLE_NAME,
        dynamodb_resource: Any = None,
    ) -> None:
        resource: Any = (
            dynamodb_resource
            if dynamodb_resource is not None
            else boto3.resource("dynamodb")
        )
        self._table = resource.Table(table_name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_if_new(
        self,
        channel_id: str,
        message_id: str,
        investigation_id: str,
        platform: str = "slack",
    ) -> bool:
        """Attempt to record a new investigation.

        Returns ``True`` if the alert is new (item written successfully).
        Returns ``False`` if a record already exists for this alert
        (duplicate detected via ``ConditionalCheckFailedException``).
        """
        pk = f"{platform}#{channel_id}#{message_id}"
        now = int(time.time())

        try:
            self._table.put_item(
                Item={
                    "pk": pk,
                    "investigation_id": investigation_id,
                    "created_at": now,
                    "ttl": now + _TTL_SECONDS,
                    "status": "IN_PROGRESS",
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":  # type: ignore[typeddict-item]
                return False
            raise
