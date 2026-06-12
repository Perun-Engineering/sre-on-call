"""Per-investigation trace archive — S3 events + DynamoDB index.

Each Slack/Discord alert produces one *investigation* with a stable
``investigation_id``. Across the investigation we record every meaningful
step (alert received, A2A request/response, termination) as an append-only
event in S3, plus a single manifest + DDB index entry at the end.

The store is **fail-open**: any S3 or DynamoDB error is logged and
swallowed. Tracing must never block or fail an investigation. Callers can
also build the store via :meth:`TraceStore.from_env` and get ``None`` back
when the bucket env var isn't set — that lets local-dev paths skip
tracing without conditional logic at every call site.

S3 layout
---------

::

    s3://${bucket}/
        dt=YYYY-MM-DD/investigation_id=<uuid>/
            manifest.json
            events/<ts>-<source>-<event_type>-<uuid8>.json

* ``dt=YYYY-MM-DD/`` — Hive-style partition for Athena/Glue.
* ``investigation_id=<uuid>/`` — one prefix per investigation.
* ``manifest.json`` — written once at the end of the investigation.
* ``events/`` — append-only log; one S3 object per event. The lexical
  ``<ts>`` prefix on each filename gives chronological sort without any
  cross-process coordination.

Event envelope (``events/*.json``)
----------------------------------

Every event is a JSON object with this shape::

    {
        "schema_version": 1,
        "investigation_id": "<uuid>",
        "ts": "<ISO 8601>",
        "source": "lambda_adapter" | "master_orchestrator",
        "event_type": "<see below>",
        "payload": { ... event-specific ... }
    }

Event types
-----------

* ``alert_received`` — emitted by the Lambda intake when a webhook is
  classified as an alert. ``payload`` contains the verbatim
  :class:`AlertContext` fields (excluding any redacted text fields).

* ``dedup_outcome`` — emitted by the Lambda intake immediately after the
  dedup check. ``payload = {"is_new": bool}``. Dropped duplicates still
  produce this event so postmortem tooling can see the decision.

* ``a2a_request`` — emitted by the master orchestrator just before each
  ``invoke_agent_runtime`` (or A2A HTTP) call.
  ``payload = {"agent_id", "endpoint", "request": <a2a json-rpc envelope>}``.

* ``a2a_response`` — emitted by the master orchestrator after the A2A
  call returns (or fails).
  ``payload = {"agent_id", "duration_seconds", "status", "response"?, "error"?}``.
  ``response`` carries the raw A2A response on success; ``error`` carries
  the exception class + message on failure.

* ``investigation_terminated`` — emitted by the master orchestrator when
  the hard cutoff fires.
  ``payload = {"pending_agents": [...], "elapsed_seconds": float}``.

Manifest (``manifest.json``)
----------------------------

::

    {
        "schema_version": 1,
        "investigation_id": "<uuid>",
        "alert_context": { ... AlertContext dict ... },
        "started_at": "<ISO 8601>",
        "ended_at": "<ISO 8601>",
        "total_duration_seconds": <float>,
        "dispatched_agents": ["slack_scanner", ...],
        "results_summary": {
            "slack_scanner": {
                "status": "success" | "error" | "timeout",
                "findings_count": <int>,
                "duration_seconds": <float>
            },
            ...
        },
        "status": "completed" | "partial" | "failed",
        "error_count": <int>
    }

DynamoDB index entry
--------------------

The manifest write triggers a single DDB PutItem so common lookup paths
(by ``investigation_id`` or by ``(channel_id, alert_timestamp)``) don't
require S3 LIST or Athena. Schema::

    pk:                       <investigation_id>          (HASH key)
    dt:                       "YYYY-MM-DD"
    s3_prefix:                "dt=YYYY-MM-DD/investigation_id=<uuid>/"
    channel_id:               <str>
    platform:                 <str>
    alert_timestamp:          <str ISO 8601>
    started_at:               <str ISO 8601>
    ended_at:                 <str ISO 8601>
    total_duration_seconds:   <Decimal>
    agent_count:              <int>
    error_count:              <int>
    status:                   "completed" | "partial" | "failed"
    ttl:                      <int unix seconds, +90 days>

Schema versioning
-----------------

Every event and the manifest carry ``schema_version: 1``. Add new fields
freely; never remove or repurpose existing ones. To make a breaking
change, bump the version and write v2 alongside v1 — keep both readable
for at least the retention window.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from shared.tool_result import results_from_dict, results_to_dict

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1

# Index entries expire 90 days after manifest write.
_DDB_TTL_SECONDS = 90 * 86400

# Sources permitted in the event envelope. Kept as a constant for
# discoverability; not enforced — fail-open writes accept any string.
SOURCE_LAMBDA = "lambda_adapter"
SOURCE_MASTER = "master_orchestrator"

# Event types — see module docstring for the payload shape of each.
EVENT_ALERT_RECEIVED = "alert_received"
EVENT_DEDUP_OUTCOME = "dedup_outcome"
EVENT_A2A_REQUEST = "a2a_request"
EVENT_A2A_RESPONSE = "a2a_response"
EVENT_INVESTIGATION_TERMINATED = "investigation_terminated"
# Issue #28 — the master's pre-dispatch routing decision (selected agents +
# per-agent hints + skipped agents/reasons) and the Stage 2 follow-up decision.
EVENT_ROUTING_DECISION = "routing_decision"
EVENT_FOLLOWUP_DECISION = "followup_decision"


# ---------------------------------------------------------------------------
# Manifest dataclass
# ---------------------------------------------------------------------------


@dataclass
class ResultSummary:
    """Per-agent rollup carried in :class:`TraceManifest.results_summary`."""

    status: str  # "success" | "error" | "timeout"
    findings_count: int
    duration_seconds: float


@dataclass
class TraceManifest:
    """The end-of-investigation manifest written to S3 + indexed in DDB.

    All fields are populated by the master orchestrator at termination.
    """

    investigation_id: str
    alert_context: dict
    started_at: str
    ended_at: str
    total_duration_seconds: float
    dispatched_agents: list[str]
    results_summary: dict[str, ResultSummary]
    status: str  # "completed" | "partial" | "failed"
    error_count: int
    schema_version: int = SCHEMA_VERSION
    # Issue #28 — the router's decision for this investigation:
    # {"selected": {id: hint}, "skipped": {id: reason}, "rationale": str}.
    # None when routing was disabled or fell open (dispatched all agents).
    routing: dict | None = None
    # Issue #34 — the ordered incident timeline (list of TimelineEvent json
    # dicts). None when no timeline was built. Archived alongside the manifest
    # so a closed investigation can be replayed without the page model.
    timeline: list[dict] | None = None

    def to_json_dict(self) -> dict:
        """Serialise to a JSON-safe dict (e.g. for ``s3:PutObject``)."""
        d = {
            "schema_version": self.schema_version,
            "investigation_id": self.investigation_id,
            "alert_context": self.alert_context,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total_duration_seconds": self.total_duration_seconds,
            "dispatched_agents": self.dispatched_agents,
            "results_summary": {
                k: asdict(v) for k, v in self.results_summary.items()
            },
            "status": self.status,
            "error_count": self.error_count,
        }
        if self.routing is not None:
            d["routing"] = self.routing
        if self.timeline is not None:
            d["timeline"] = self.timeline
        return d


# ---------------------------------------------------------------------------
# TraceStore
# ---------------------------------------------------------------------------


@dataclass
class InvestigationRef:
    """A resolved pointer from a thread to its investigation (#56)."""

    investigation_id: str
    dt: str  # the ``dt=YYYY-MM-DD`` S3 partition the objects live under


class TraceStore:
    """Append-only S3 trace archive with a DynamoDB lookup index.

    Construct directly for tests (passing mocked ``s3_client`` and
    ``dynamodb_resource``), or use :meth:`from_env` in production paths to
    read the bucket + table names from env vars. ``from_env`` returns
    ``None`` if ``TRACES_BUCKET_NAME`` is unset, letting callers
    short-circuit cleanly::

        store = TraceStore.from_env()
        if store is not None:
            store.put_event(...)

    Every write is wrapped in ``try``/``except`` and logs on failure.
    Methods never raise.
    """

    def __init__(
        self,
        *,
        bucket: str,
        table_name: str,
        s3_client: Any = None,
        dynamodb_resource: Any = None,
        region_name: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._table_name = table_name
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")

        if s3_client is None:
            import boto3

            s3_client = boto3.client("s3", region_name=self._region)
        self._s3 = s3_client

        if dynamodb_resource is None:
            import boto3

            dynamodb_resource = boto3.resource("dynamodb", region_name=self._region)
        self._table = dynamodb_resource.Table(table_name)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> TraceStore | None:
        """Build a :class:`TraceStore` from env vars.

        Reads ``TRACES_BUCKET_NAME`` and ``TRACES_TABLE_NAME``. Returns
        ``None`` if either is unset — callers should treat that as
        "tracing disabled in this deployment".
        """
        bucket = os.environ.get("TRACES_BUCKET_NAME", "").strip()
        table = os.environ.get("TRACES_TABLE_NAME", "").strip()
        if not bucket or not table:
            return None
        try:
            return cls(bucket=bucket, table_name=table)
        except Exception:
            logger.exception("Failed to construct TraceStore from env")
            return None

    # ------------------------------------------------------------------
    # S3 key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _today_partition() -> str:
        """Return the Hive-partition string for today, UTC: ``dt=YYYY-MM-DD``."""
        return f"dt={datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}"

    @staticmethod
    def investigation_prefix(investigation_id: str, *, dt: str | None = None) -> str:
        """Return the S3 prefix for *investigation_id*'s objects.

        Uses today's UTC date as the ``dt=`` partition unless *dt* is
        supplied (useful when re-deriving the prefix from a manifest's
        ``started_at``).
        """
        partition = dt or TraceStore._today_partition()
        return f"{partition}/investigation_id={investigation_id}/"

    @classmethod
    def _event_key(
        cls,
        investigation_id: str,
        source: str,
        event_type: str,
    ) -> str:
        """Build a chronologically-sortable S3 key for a single event."""
        # Microsecond precision + short uuid avoids collisions across
        # concurrent fan-out tasks without requiring sequence coordination.
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = uuid.uuid4().hex[:8]
        prefix = cls.investigation_prefix(investigation_id)
        return f"{prefix}events/{ts}-{source}-{event_type}-{suffix}.json"

    @classmethod
    def _manifest_key(cls, investigation_id: str, *, dt: str | None = None) -> str:
        """Build the canonical manifest key for *investigation_id*."""
        return f"{cls.investigation_prefix(investigation_id, dt=dt)}manifest.json"

    @classmethod
    def _charts_key(
        cls, investigation_id: str, chart_id: str, *, dt: str | None = None
    ) -> str:
        """Build the S3 key for a chart series under the investigation prefix."""
        return f"{cls.investigation_prefix(investigation_id, dt=dt)}charts/{chart_id}.json"

    @classmethod
    def _page_model_key(
        cls, investigation_id: str, *, dt: str | None = None
    ) -> str:
        """Build the S3 key for the #33 page model under the investigation prefix."""
        return f"{cls.investigation_prefix(investigation_id, dt=dt)}page_model.json"

    @classmethod
    def _results_key(
        cls, investigation_id: str, *, dt: str | None = None
    ) -> str:
        """S3 key for the full per-agent results map (#56 PIR recovery)."""
        return f"{cls.investigation_prefix(investigation_id, dt=dt)}results.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put_event(
        self,
        *,
        investigation_id: str,
        source: str,
        event_type: str,
        payload: dict,
    ) -> None:
        """Write a single event JSON object to S3.

        Fail-open: logs and swallows any error. Returns nothing.
        """
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "investigation_id": investigation_id,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "source": source,
            "event_type": event_type,
            "payload": payload,
        }
        key = self._event_key(investigation_id, source, event_type)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(envelope, default=_json_default).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception(
                "TraceStore.put_event failed (investigation_id=%s, type=%s)",
                investigation_id, event_type,
            )

    def put_chart_series(
        self,
        *,
        investigation_id: str,
        chart_id: str,
        payload: dict,
        dt: str | None = None,
    ) -> None:
        """Write a chart series JSON object under ``charts/<chart_id>.json``.

        Fail-open: logs and swallows any S3 error. Used by the master in
        Phase 7 to snapshot the data behind descriptor-carrying findings so
        the interactive incident page (#33) can draw graphs from an immutable
        record that outlives CloudWatch retention.
        """
        key = self._charts_key(investigation_id, chart_id, dt=dt)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(payload, default=_json_default).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception(
                "TraceStore.put_chart_series failed "
                "(investigation_id=%s, chart_id=%s)",
                investigation_id, chart_id,
            )

    def put_page_model(
        self,
        *,
        investigation_id: str,
        payload: dict,
        dt: str | None = None,
    ) -> None:
        """Write ``page_model.json`` — the render trigger + input for the #33 page.

        Written by the master in Phase 7 *after* the manifest and chart series,
        so the S3 ObjectCreated notification on this key guarantees the
        referenced ``charts/<id>.json`` already exist. Fail-open: logs and
        swallows any S3 error.
        """
        key = self._page_model_key(investigation_id, dt=dt)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(payload, default=_json_default).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception(
                "TraceStore.put_page_model failed (investigation_id=%s)",
                investigation_id,
            )

    def get_page_model(
        self, investigation_id: str, *, dt: str | None = None
    ) -> dict | None:
        """Read ``page_model.json`` written by :meth:`put_page_model`.

        Used by the PIR flow (#55) to finalize an existing incident page in
        place — flip its status to ``resolved`` and append the resolution
        chapter — rather than rebuilding it from scratch (which would lose the
        synthesized Analysis block). Returns ``None`` on any miss/error
        (fail-open): no archived page means there is simply nothing to
        finalize.
        """
        key = self._page_model_key(investigation_id, dt=dt)
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception:
            logger.exception(
                "TraceStore.get_page_model failed (investigation_id=%s)",
                investigation_id,
            )
            return None

    def put_results(
        self,
        *,
        investigation_id: str,
        results: dict,
        dt: str | None = None,
    ) -> None:
        """Archive the full per-agent results map as ``results.json``.

        Enables the PIR flow (#56) to rebuild the incident report from the
        original findings — events/manifest only keep summaries. Fail-open.
        """
        key = self._results_key(investigation_id, dt=dt)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(
                    results_to_dict(results), default=_json_default,
                ).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception(
                "TraceStore.put_results failed (investigation_id=%s)",
                investigation_id,
            )

    def get_results(
        self, investigation_id: str, *, dt: str | None = None
    ) -> dict | None:
        """Read + rebuild the results map written by :meth:`put_results`.

        Returns ``None`` on any miss/error (fail-open).
        """
        key = self._results_key(investigation_id, dt=dt)
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            payload = json.loads(obj["Body"].read())
        except Exception:
            logger.exception(
                "TraceStore.get_results failed (investigation_id=%s)",
                investigation_id,
            )
            return None
        return results_from_dict(payload)

    THREAD_INDEX_NAME = "channel_id-message_id-index"

    def find_investigation(
        self, channel_id: str, thread_ts: str
    ) -> InvestigationRef | None:
        """Resolve a chat thread to its originating investigation (#56).

        Queries the channel_id+message_id GSI (the alert message is the
        thread root, so ``thread_ts == alert_context.message_id``). On
        multiple hits — a re-alert in the same thread — returns the newest
        by ``started_at``. Fail-open → ``None``.
        """
        from boto3.dynamodb.conditions import Key

        try:
            resp = self._table.query(
                IndexName=self.THREAD_INDEX_NAME,
                KeyConditionExpression=(
                    Key("channel_id").eq(str(channel_id))
                    & Key("message_id").eq(str(thread_ts))
                ),
            )
            items = resp.get("Items", [])
        except Exception:
            logger.exception(
                "TraceStore.find_investigation query failed "
                "(channel_id=%s, thread_ts=%s)",
                channel_id, thread_ts,
            )
            return None

        if not items:
            return None
        newest = max(items, key=lambda i: str(i.get("started_at", "")))
        pk = newest.get("pk")
        dt = newest.get("dt")
        if not pk or not dt:
            return None
        return InvestigationRef(
            investigation_id=str(pk), dt=f"dt={dt}",
        )

    def get_manifest(
        self, investigation_id: str, *, dt: str | None = None
    ) -> dict | None:
        """Read the manifest JSON for *investigation_id* (fail-open → None).

        Used by the PIR flow (#56) to recover the original ``alert_context``.
        """
        key = self._manifest_key(investigation_id, dt=dt)
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception:
            logger.exception(
                "TraceStore.get_manifest failed (investigation_id=%s)",
                investigation_id,
            )
            return None

    def put_manifest(self, manifest: TraceManifest) -> None:
        """Write the manifest to S3 and index entry to DynamoDB.

        Fail-open: logs and swallows S3 / DDB errors independently — a
        DDB failure does not skip the S3 write or vice versa.
        """
        # Derive the dt= partition from started_at so the manifest lands
        # in the same prefix as the events written earlier today.
        dt = _dt_partition_from_iso(manifest.started_at)
        key = self._manifest_key(manifest.investigation_id, dt=dt)

        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(
                    manifest.to_json_dict(), default=_json_default,
                ).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception(
                "TraceStore.put_manifest S3 write failed (investigation_id=%s)",
                manifest.investigation_id,
            )

        try:
            self._table.put_item(
                Item=_index_item(manifest, s3_prefix=self.investigation_prefix(
                    manifest.investigation_id, dt=dt,
                )),
            )
        except Exception:
            logger.exception(
                "TraceStore.put_manifest DDB index write failed "
                "(investigation_id=%s)",
                manifest.investigation_id,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """Fallback for ``json.dumps`` — handles dataclasses and Decimal."""
    if hasattr(obj, "to_json_dict"):
        return obj.to_json_dict()
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _dt_partition_from_iso(iso_ts: str) -> str:
    """Parse an ISO 8601 timestamp and return ``dt=YYYY-MM-DD`` (UTC)."""
    try:
        dt_obj = datetime.fromisoformat(iso_ts)
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=timezone.utc)
        return f"dt={dt_obj.astimezone(timezone.utc).strftime('%Y-%m-%d')}"
    except Exception:
        return TraceStore._today_partition()


def _index_item(manifest: TraceManifest, *, s3_prefix: str) -> dict:
    """Build the DynamoDB item for the index entry.

    Decimal is required for floats per DynamoDB's type system.
    """
    now = int(time.time())
    ctx = manifest.alert_context
    return {
        "pk": manifest.investigation_id,
        "dt": s3_prefix.split("/", 1)[0].split("=", 1)[1],
        "s3_prefix": s3_prefix,
        "channel_id": str(ctx.get("channel_id", "")),
        "message_id": str(ctx.get("message_id", "")),
        "platform": str(ctx.get("platform", "")),
        "alert_timestamp": str(ctx.get("alert_timestamp", "")),
        "started_at": manifest.started_at,
        "ended_at": manifest.ended_at,
        "total_duration_seconds": Decimal(str(manifest.total_duration_seconds)),
        "agent_count": len(manifest.dispatched_agents),
        "error_count": manifest.error_count,
        "status": manifest.status,
        "ttl": now + _DDB_TTL_SECONDS,
    }
