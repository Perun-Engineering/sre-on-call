"""Incident History Agent tools — similar-incident lookup from past investigations.

Read side of issue #30. The master writes a compact outcome record per finished
investigation (:mod:`shared.incident_history_store`); this agent embeds the
current alert and surfaces the most similar past incidents as findings —
"this fired before on <date>, root cause was X, here's the thread."

Both tools are fail-open and never raise:

* ``find_similar_incidents`` — the alert-path entry point. No matches is a
  clean ``success`` ("no similar past incidents"), not an error. The agent is
  reported **unhealthy** only when the deployment isn't wired for history at
  all (no traces table / embeddings disabled).
* ``capture_snapshot`` — the ``/sre-snapshot`` entry point; reports how many
  incidents have been recorded recently.
"""

from __future__ import annotations

import logging

from strands import tool

from shared.embeddings import Embedder, EmbeddingClient
from shared.incident_history_store import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    IncidentHistoryStore,
    SimilarIncident,
    SimilarIncidentSearch,
)
from shared.models import AgentResult, Finding, SnapshotReport, SnapshotSection
from shared.tool_result import (
    build_unhealthy_agent_result,
    format_result,
    format_snapshot_result,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "incident_history"
_ALERT_PREVIEW_CHARS = 160


# ---------------------------------------------------------------------------
# find_similar_incidents — alert path
# ---------------------------------------------------------------------------


@tool
def find_similar_incidents(alert_text: str) -> str:
    """Find past incidents whose alert text is similar to the current alert.

    Embeds *alert_text* with Titan and ranks stored incident-outcome records by
    cosine similarity. Returns the top matches with their recorded root cause
    and a link back to the original incident thread.

    Args:
        alert_text: The full text of the current alert.

    Returns:
        A human-readable summary string for the LLM to relay verbatim.
    """
    store = IncidentHistoryStore.from_env()
    embedder = EmbeddingClient.from_env()
    return format_result(_find_similar(alert_text, store=store, embedder=embedder))


def _find_similar(
    alert_text: str,
    *,
    store: SimilarIncidentSearch | None,
    embedder: Embedder | None,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> AgentResult:
    """Core lookup logic with the store + embedder injected (testable).

    Distinguishes three outcomes:

    * **unhealthy** — history isn't configured in this deployment (no traces
      table or embeddings disabled). Operator-actionable, not a per-request
      failure.
    * **error** — configured, but embedding the current alert failed.
    * **success** — searched the history; zero matches is still success.
    """
    if store is None or embedder is None:
        return build_unhealthy_agent_result(
            AGENT_NAME,
            "incident history is not configured in this deployment "
            "(traces table or embeddings unavailable)",
        )

    embedding = embedder.embed(alert_text)
    if embedding is None:
        return AgentResult(
            agent_name=AGENT_NAME,
            status="error",
            findings=[],
            summary="Could not embed the alert text for similarity search.",
            error_message="embedding unavailable",
        )

    hits = store.search_similar(embedding, top_k=top_k, min_score=min_score)
    if not hits:
        return AgentResult(
            agent_name=AGENT_NAME,
            status="success",
            findings=[],
            summary="No similar past incidents found.",
        )

    findings = [_finding_from_hit(hit) for hit in hits]
    return AgentResult(
        agent_name=AGENT_NAME,
        status="success",
        findings=findings,
        summary=(
            f"Found {len(hits)} similar past incident(s); "
            f"closest match {hits[0].score:.0%} similar."
        ),
    )


def _finding_from_hit(hit: SimilarIncident) -> Finding:
    """Render one ranked hit as a Finding for the Incident Report."""
    when = _short_date(hit.alert_timestamp)
    root_cause = hit.root_cause or "not recorded"
    preview = hit.alert_text.strip().replace("\n", " ")[:_ALERT_PREVIEW_CHARS]
    content = (
        f"Similar alert (~{hit.score:.0%} match) fired {when}: \"{preview}\". "
        f"Root cause: {root_cause}."
    )
    if hit.summary:
        content += f" {hit.summary}"
    return Finding(
        source=f"incident {hit.investigation_id[:8]}",
        timestamp=hit.alert_timestamp,
        content=content,
        severity="info",
        metadata={
            "investigation_id": hit.investigation_id,
            "similarity": round(hit.score, 4),
        },
        link=hit.thread_link,
    )


def _short_date(iso_ts: str) -> str:
    """Best-effort ``YYYY-MM-DD`` from an ISO timestamp; raw string on failure."""
    if not iso_ts:
        return "an earlier date"
    return iso_ts[:10]


# ---------------------------------------------------------------------------
# capture_snapshot — /sre-snapshot path
# ---------------------------------------------------------------------------


_SNAPSHOT_LOOKBACK_DAYS = 30


@tool
def capture_snapshot(requested_at: str) -> str:
    """Capture a snapshot of recent incident-history volume.

    Args:
        requested_at: ISO 8601 timestamp from the master, used as the
            ``captured_at`` field of the returned report.

    Returns:
        A short human-readable string ending with a snapshot footer.
    """
    store = IncidentHistoryStore.from_env()
    return format_snapshot_result(_capture_snapshot(requested_at, store=store))


def _capture_snapshot(
    requested_at: str, *, store: IncidentHistoryStore | None
) -> SnapshotReport:
    """Pure snapshot builder with the store injected (testable)."""
    label = f"Incident memory (last {_SNAPSHOT_LOOKBACK_DAYS} days)"
    if store is None:
        return SnapshotReport(
            agent_name=AGENT_NAME,
            captured_at=requested_at,
            sections=[
                SnapshotSection(
                    label=label,
                    lines=["(incident history not configured in this deployment)"],
                )
            ],
            anomaly=False,
        )

    count = store.count_recent(days=_SNAPSHOT_LOOKBACK_DAYS)
    line = (
        "⚠️ count unavailable"
        if count is None
        else f"{count} incident(s) recorded"
    )
    return SnapshotReport(
        agent_name=AGENT_NAME,
        captured_at=requested_at,
        sections=[SnapshotSection(label=label, lines=[line])],
        anomaly=False,
    )
