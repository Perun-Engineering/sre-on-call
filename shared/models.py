"""Core data models for sre-on-call."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class AgentMetadata:
    """Per-invocation telemetry attached to every agent result.

    Populated by the orchestrator (model_id, started_at, completed_at) and by
    the specialized agent itself (token usage, cost). Any field may be ``None``
    when the source can't supply it — e.g. a timed-out agent has no token
    counts, and local-dev paths don't always know the model id.
    """

    model_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


@dataclass
class AlertContext:
    """Context extracted from a chat alert message, passed through the investigation pipeline."""

    investigation_id: str  # UUID
    platform: str  # "slack", "discord", etc.
    channel_id: str  # Platform-specific channel/room ID
    message_id: str  # Platform-specific message ID (Slack ts, Discord message ID)
    alert_text: str  # Full text of the alert message
    alert_timestamp: str  # ISO 8601 timestamp of the alert
    investigation_window: tuple[str, str]  # (start_iso, end_iso) — ±5min from alert
    platform_metadata: dict = field(default_factory=dict)  # Platform-specific data
    experiment_id: str | None = None  # Set when running under an A/B experiment
    variant_id: str | None = None  # "a" or "b"; None when no experiment
    variant_label: str | None = None  # Human-readable variant name
    # Per-agent investigation hint set by the master's router (issue #28):
    # a focused, one-sentence steer (suspected pods, candidate log groups,
    # time-window emphasis) injected just before dispatch. None for the
    # unrouted/today's path; carried verbatim on the A2A payload.
    investigation_hints: str | None = None


@dataclass
class Finding:
    """A single finding from a specialized agent's investigation."""

    source: str  # e.g., channel name, log group, metric name
    timestamp: str  # ISO 8601
    content: str  # The finding content
    severity: str  # "critical", "warning", "info"
    metadata: dict = field(default_factory=dict)  # Source-specific metadata
    link: str | None = None  # Deep link into the data source the finding came from
    chart: ChartDescriptor | None = None  # set when this finding's query is chartable


@dataclass
class ChartDescriptor:
    """A tiny, source-agnostic pointer to a chartable query.

    Rides every :class:`Finding` produced from one charted query; findings
    from the same query share a ``chart_id``. The descriptor answers *which
    chart*; the series data (:class:`ChartSeries`) is carried once per
    ``chart_id`` on :class:`AgentResult.chart_series` and joined back here.
    """

    chart_id: str  # deterministic — see create()
    source: str  # e.g. "cloudwatch_logs_insights"
    log_groups: list[str]
    query: str
    start_epoch: int
    end_epoch: int

    @classmethod
    def create(
        cls,
        *,
        source: str,
        log_groups: list[str],
        query: str,
        start_epoch: int,
        end_epoch: int,
    ) -> ChartDescriptor:
        """Build a descriptor with a deterministic ``chart_id``.

        The id is ``sha256(source|sorted(log_groups)|query|start|end)`` hex,
        truncated to 16 chars. Deterministic so identical queries dedup to one
        chart file and the renderer (#33) can recompute it without storage.
        """
        key = (
            f"{source}|{'|'.join(sorted(log_groups))}|{query}"
            f"|{start_epoch}|{end_epoch}"
        )
        chart_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return cls(
            chart_id=chart_id,
            source=source,
            log_groups=list(log_groups),
            query=query,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
        )


@dataclass
class ChartSeries:
    """The harvested series for one ``chart_id`` — the data behind a chart.

    Stored once per ``chart_id`` on :class:`AgentResult.chart_series` (so many
    findings sharing a chart don't duplicate the rows). ``points`` are the raw
    query rows as ``{field: value}`` dicts; the renderer decides how to plot
    them. ``series_kind`` is a best-effort hint and ``truncated`` flags a
    capped series.
    """

    points: list[dict] = field(default_factory=list)
    series_kind: str = "log_rows"  # or "binned"
    truncated: bool = False


@dataclass
class AgentResult:
    """Result returned by a specialized agent to the Master Agent via A2A protocol."""

    agent_name: str  # e.g., "slack_scanner", "prometheus"
    status: str  # "success" or "error"
    findings: list[Finding]  # Structured findings
    summary: str  # Natural language analysis summary
    error_message: str | None = None  # Populated when status == "error"
    duration_seconds: float = 0.0  # How long the agent took
    metadata: AgentMetadata = field(default_factory=AgentMetadata)
    chart_series: dict[str, ChartSeries] = field(default_factory=dict)  # chart_id -> series


@dataclass
class AgentFailure:
    """Represents a failed or timed-out agent."""

    agent_name: str
    error_message: str
    timestamp: str  # ISO 8601
    metadata: AgentMetadata = field(default_factory=AgentMetadata)


@dataclass
class CommandRequest:
    """A slash command received from a chat platform (e.g. /postmortem)."""

    platform: str  # "slack", "discord"
    command: str  # e.g. "/postmortem"
    text: str  # Arguments after the command
    channel_id: str
    user_id: str
    thread_ts: str | None  # Thread context; None if invoked outside a thread
    response_url: str  # Platform callback URL for async responses
    platform_metadata: dict = field(default_factory=dict)


@dataclass
class SnapshotSection:
    """A labelled list of pre-rendered bullet lines within an agent's snapshot.

    The agent's ``capture_snapshot`` tool produces these as the structural
    units of its :class:`SnapshotReport`. The renderer lays each one out as
    a bold label followed by ``- {line}`` bullets, with no further
    transformation — agents own their own truncation and humanisation.
    """

    label: str
    lines: list[str]


@dataclass
class SnapshotReport:
    """A specialized agent's snapshot of its observed infrastructure.

    Returned by the agent's ``capture_snapshot`` tool. Carries the agent's
    name, the wall-clock instant the snapshot was taken, the labelled
    per-section content lines, and an anomaly flag the master uses to
    drive the section's status marker (✅ ok / ⚠️ anomaly) in the rendered
    ``SnapshotSections``. The renderer-side counterpart is
    :class:`shared.report_renderer.SnapshotBlock`, which the master
    composes by adding registry-derived display info and a status marker.
    """

    agent_name: str
    captured_at: str  # ISO 8601
    sections: list[SnapshotSection]
    anomaly: bool = False
    anomaly_summary: str | None = None  # one-liner for the deterministic top-line summary
    metadata: AgentMetadata = field(default_factory=AgentMetadata)
