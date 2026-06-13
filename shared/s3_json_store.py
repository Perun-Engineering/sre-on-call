"""S3JsonStore — the S3 JSON-blob persistence-adapter seam.

Wraps one bucket + ``s3`` client and owns the put/get-JSON mechanism the
:class:`~shared.trace_store.TraceStore` repeated for every object it writes
(events, manifest, chart series, page model, results map): ``json.dumps`` with a
dataclass/``Decimal``-aware default encoder, UTF-8 encode, ``put_object`` /
``get_object`` + ``json.loads``, and a per-instance fail-open policy.

Same fail-open contract as :class:`~shared.dynamo_table.DynamoTable`:
``fail_open=True`` logs-and-swallows (writes become no-ops, reads return
``None`` — covering both a missing key and any S3 error); ``fail_open=False``
(default) re-raises. The S3 key layout and any pre-serialization domain mapping
stay in the calling store; this adapter only knows "write/read a JSON object at
this key".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class S3JsonStore:
    """A single S3 bucket handle that reads/writes JSON objects by key."""

    def __init__(
        self,
        bucket: str,
        *,
        s3_client: Any = None,
        region_name: str | None = None,
        fail_open: bool = False,
    ) -> None:
        self._bucket = bucket
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")
        if s3_client is None:
            import boto3

            s3_client = boto3.client("s3", region_name=self._region)
        self._s3 = s3_client
        self._fail_open = fail_open

    def put_json(
        self, key: str, obj: Any, *, content_type: str = "application/json"
    ) -> None:
        """Serialise *obj* to JSON and write it to *key*."""
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(obj, default=json_default).encode("utf-8"),
                ContentType=content_type,
            )
        except Exception:
            if self._fail_open:
                logger.exception("S3JsonStore.put_json failed (key=%s)", key)
                return
            raise

    def get_json(self, key: str) -> dict | None:
        """Read + parse the JSON object at *key*. ``None`` on miss/error when fail-open."""
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception:
            if self._fail_open:
                logger.exception("S3JsonStore.get_json failed (key=%s)", key)
                return None
            raise


def json_default(obj: Any) -> Any:
    """Fallback for ``json.dumps`` — handles dataclasses and ``Decimal``."""
    if hasattr(obj, "to_json_dict"):
        return obj.to_json_dict()
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
