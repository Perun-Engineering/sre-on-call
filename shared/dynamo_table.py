"""DynamoTable — the DynamoDB persistence-adapter seam beneath the domain stores.

Wraps one boto3 ``Table`` handle and owns the mechanism each store used to
hand-roll: region resolution + lazy client build, recursive number marshalling
(``float`` -> ``Decimal`` on write; ``Decimal`` -> ``float`` on read), a single
paginated ``scan_all`` that drains ``LastEvaluatedKey``, and a per-instance
fail-open policy.

Fail-open is fixed at construction. ``fail_open=True`` logs-and-swallows every
error (writes become no-ops, reads return ``None``/empty) — for the
investigation-path stores where persistence must never block an investigation.
``fail_open=False`` (the default) re-raises — for the offline experiment stores
backing the judge CLI, where a silent failure would corrupt a scorecard.

Callers speak plain Python: pass ``float`` and get ``float`` back; the boto3
"Float types are not supported" footgun and the ``Decimal`` read-side casts live
here, once, instead of at every store. Integers stay integers on write; on read
every number arrives as ``float`` (DynamoDB returns all numbers as ``Decimal``),
so callers that need an ``int`` cast it — exactly as they did before.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class DynamoTable:
    """A single DynamoDB table handle with marshalling + fail-open built in."""

    def __init__(
        self,
        table_name: str,
        *,
        dynamodb_resource: Any = None,
        region_name: str | None = None,
        fail_open: bool = False,
    ) -> None:
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")
        if dynamodb_resource is None:
            import boto3

            dynamodb_resource = boto3.resource("dynamodb", region_name=self._region)
        self._name = table_name
        self._table = dynamodb_resource.Table(table_name)
        self._fail_open = fail_open

    def put(self, item: dict[str, Any]) -> None:
        """Write one item. Floats are marshalled to ``Decimal`` automatically."""
        try:
            self._table.put_item(Item=_to_decimal(item))
        except Exception:
            if self._fail_open:
                logger.exception("DynamoTable.put failed (table=%s)", self._name)
                return
            raise

    def get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        """Fetch one item by key. ``None`` on miss; numbers come back as floats."""
        try:
            resp = self._table.get_item(Key=key)
        except Exception:
            if self._fail_open:
                logger.exception("DynamoTable.get failed (table=%s)", self._name)
                return None
            raise
        item = resp.get("Item")
        return _from_decimal(item) if item is not None else None

    def scan_all(
        self, filter_expression: Any = None
    ) -> Iterator[dict[str, Any]]:
        """Yield every item, transparently draining ``LastEvaluatedKey``."""
        kwargs: dict[str, Any] = {}
        if filter_expression is not None:
            kwargs["FilterExpression"] = filter_expression
        try:
            while True:
                resp = self._table.scan(**kwargs)
                for item in resp.get("Items", []):
                    yield _from_decimal(item)
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                kwargs["ExclusiveStartKey"] = last_key
        except Exception:
            if self._fail_open:
                logger.exception("DynamoTable.scan_all failed (table=%s)", self._name)
                return
            raise

    def query(self, index_name: str, key_condition: Any) -> list[dict[str, Any]]:
        """Query a GSI by key condition; returns the matched items (single page)."""
        try:
            resp = self._table.query(
                IndexName=index_name, KeyConditionExpression=key_condition
            )
        except Exception:
            if self._fail_open:
                logger.exception("DynamoTable.query failed (table=%s)", self._name)
                return []
            raise
        return [_from_decimal(i) for i in resp.get("Items", [])]


def _to_decimal(obj: Any) -> Any:
    """Recursively convert ``float`` -> ``Decimal`` for DynamoDB writes.

    ``int``/``bool``/``bytes``/``str`` pass through untouched — only floats are
    rejected by the boto3 resource API.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_decimal(v) for v in obj]
    return obj


def _from_decimal(obj: Any) -> Any:
    """Recursively convert ``Decimal`` -> ``float`` for read paths."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_decimal(v) for v in obj]
    return obj
