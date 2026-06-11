"""Incident-history outcome records — similar-incident memory (issue #30).

Each finished investigation drops one compact *outcome record* so a future,
similar alert can surface "this fired before, here's what it was and how it
resolved." Records live in the **existing traces DynamoDB table** (zero new
infra) under their own ``history#<investigation_id>`` partition key, so they
never collide with the trace manifest item (``pk == <investigation_id>``) the
:mod:`shared.trace_store` writes for the same investigation.

Two paths share this module:

* **Write** (master, Phase 7) — :meth:`IncidentHistoryStore.put_outcome` stores
  the alert text, its Titan embedding (packed ``float32`` bytes), a one-line
  summary, the synthesized root cause, and a back-reference to the originating
  thread.
* **Read** (``incident_history`` agent) — :meth:`search_similar` scans the
  history items, ranks by cosine similarity against the current alert's
  embedding, and returns the top matches. This is the **replaceable seam**:
  the brute-force DDB scan is trivial at current volume; swap the body for S3
  Vectors / a Bedrock Knowledge Base later without touching the agent.

Everything is **fail-open**: a write error is logged and swallowed; a read
error yields an empty list. Similar-incident lookup must never block or fail an
investigation.

DynamoDB item shape (history partition)
---------------------------------------

::

    pk:               "history#<investigation_id>"   (HASH key, shared table)
    record_type:      "incident_outcome"
    investigation_id: "<uuid>"
    alert_text:       <str>
    summary:          <str>
    root_cause:       <str>            (absent when synthesis produced none)
    platform:         <str>
    hist_channel_id:  <str>            (deliberately NOT `channel_id` — keeps
                                        these rows out of the manifest GSI)
    message_id:       <str>
    thread_link:      <str>            (absent when no link could be built)
    alert_timestamp:  <str ISO 8601>
    recorded_at:      <str ISO 8601>
    embedding:        <bytes>          (little-endian float32, see shared.embeddings)
    ttl:              <int unix seconds, +90 days>
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from shared.embeddings import cosine_similarity, pack_embedding, unpack_embedding

logger = logging.getLogger(__name__)

HISTORY_PK_PREFIX = "history#"
RECORD_TYPE = "incident_outcome"

# Outcome records age out with the rest of the trace archive (90 days).
_DDB_TTL_SECONDS = 90 * 86400

# Defaults for the read path; the agent tool may override.
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.5


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class IncidentOutcome:
    """A compact record of one finished investigation, written at Phase 7."""

    investigation_id: str
    alert_text: str
    summary: str
    embedding: list[float]
    platform: str = ""
    channel_id: str = ""
    message_id: str = ""
    alert_timestamp: str = ""
    root_cause: str | None = None
    thread_link: str | None = None


@dataclass
class SimilarIncident:
    """One ranked hit from :meth:`IncidentHistoryStore.search_similar`."""

    investigation_id: str
    alert_text: str
    summary: str
    score: float
    alert_timestamp: str = ""
    root_cause: str | None = None
    thread_link: str | None = None


class SimilarIncidentSearch(Protocol):
    """The read seam the ``incident_history`` agent depends on.

    Any backend (DDB scan today; S3 Vectors / Bedrock KB later) that implements
    this can be dropped in without touching the agent tool.
    """

    def search_similar(
        self,
        embedding: list[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
        exclude_investigation_id: str | None = None,
    ) -> list[SimilarIncident]: ...


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class IncidentHistoryStore:
    """Read/write incident-outcome records in the shared traces table.

    Construct directly for tests (pass a mocked ``dynamodb_resource``), or use
    :meth:`from_env` in production paths — it returns ``None`` when
    ``TRACES_TABLE_NAME`` is unset so callers short-circuit cleanly. No method
    raises.
    """

    def __init__(
        self,
        *,
        table_name: str,
        dynamodb_resource: Any = None,
        region_name: str | None = None,
    ) -> None:
        self._table_name = table_name
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")
        if dynamodb_resource is None:
            import boto3

            dynamodb_resource = boto3.resource("dynamodb", region_name=self._region)
        self._table = dynamodb_resource.Table(table_name)

    @classmethod
    def from_env(cls) -> IncidentHistoryStore | None:
        """Build from ``TRACES_TABLE_NAME``; ``None`` when unset (history off)."""
        table = os.environ.get("TRACES_TABLE_NAME", "").strip()
        if not table:
            return None
        try:
            return cls(table_name=table)
        except Exception:
            logger.exception("Failed to construct IncidentHistoryStore from env")
            return None

    @staticmethod
    def history_pk(investigation_id: str) -> str:
        return f"{HISTORY_PK_PREFIX}{investigation_id}"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put_outcome(self, outcome: IncidentOutcome) -> None:
        """Store one outcome record. Fail-open: logs and swallows any error."""
        now = int(time.time())
        item: dict[str, Any] = {
            "pk": self.history_pk(outcome.investigation_id),
            "record_type": RECORD_TYPE,
            "investigation_id": outcome.investigation_id,
            "alert_text": outcome.alert_text,
            "summary": outcome.summary,
            "platform": outcome.platform,
            "hist_channel_id": outcome.channel_id,
            "message_id": outcome.message_id,
            "alert_timestamp": outcome.alert_timestamp,
            "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
            "embedding": pack_embedding(outcome.embedding),
            "ttl": now + _DDB_TTL_SECONDS,
        }
        if outcome.root_cause:
            item["root_cause"] = outcome.root_cause
        if outcome.thread_link:
            item["thread_link"] = outcome.thread_link

        try:
            self._table.put_item(Item=item)
        except Exception:
            logger.exception(
                "IncidentHistoryStore.put_outcome failed (investigation_id=%s)",
                outcome.investigation_id,
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def search_similar(
        self,
        embedding: list[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
        exclude_investigation_id: str | None = None,
    ) -> list[SimilarIncident]:
        """Return the *top_k* most cosine-similar past incidents.

        Brute-force scan over the history items, ranked in pure Python. The
        current investigation (``exclude_investigation_id``) is filtered out so
        an alert never matches itself. Returns ``[]`` on any error or empty
        store — never raises.
        """
        if not embedding:
            return []
        try:
            items = self._scan_history_items()
        except Exception:
            logger.exception("IncidentHistoryStore.search_similar scan failed")
            return []

        scored: list[SimilarIncident] = []
        for item in items:
            investigation_id = str(item.get("investigation_id", ""))
            if exclude_investigation_id and investigation_id == exclude_investigation_id:
                continue
            blob = item.get("embedding")
            if blob is None:
                continue
            try:
                vector = unpack_embedding(blob)
            except Exception:
                continue
            score = cosine_similarity(embedding, vector)
            if score < min_score:
                continue
            scored.append(
                SimilarIncident(
                    investigation_id=investigation_id,
                    alert_text=str(item.get("alert_text", "")),
                    summary=str(item.get("summary", "")),
                    score=score,
                    alert_timestamp=str(item.get("alert_timestamp", "")),
                    root_cause=(
                        str(item["root_cause"]) if item.get("root_cause") else None
                    ),
                    thread_link=(
                        str(item["thread_link"]) if item.get("thread_link") else None
                    ),
                )
            )

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[: max(0, top_k)]

    def count_recent(self, *, days: int = 30) -> int | None:
        """Count outcome records recorded within the last *days*.

        Used by the agent's snapshot tool. Returns ``None`` on error so the
        caller can render "unknown" rather than a misleading zero.
        """
        cutoff = (
            datetime.now(tz=timezone.utc) - _timedelta_days(days)
        ).isoformat()
        try:
            count = 0
            for item in self._scan_history_items():
                if str(item.get("recorded_at", "")) >= cutoff:
                    count += 1
            return count
        except Exception:
            logger.exception("IncidentHistoryStore.count_recent scan failed")
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scan_history_items(self) -> list[dict]:
        """Paginated Scan of the history partition (``record_type`` filter)."""
        from boto3.dynamodb.conditions import Attr

        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("record_type").eq(RECORD_TYPE)
            & Attr("embedding").exists(),
        }
        while True:
            response = self._table.scan(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=days)
