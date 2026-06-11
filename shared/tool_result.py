"""Shared result-building utilities for agent tool functions.

Owns the two transport-payload dataclasses an agent's text response carries
to the master orchestrator: :class:`shared.models.AgentResult` (alert path)
and :class:`shared.models.SnapshotReport` (``/sre-snapshot`` path). Each is round-
tripped through an :class:`shared.agent_footer.AgentFooter` instance defined
at the bottom of this module — :data:`AGENT_RESULT` and :data:`SNAPSHOT_RESULT`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.agent_footer import AgentFooter, neutralize_markers
from shared.models import (
    AgentMetadata,
    AgentResult,
    ChartDescriptor,
    ChartSeries,
    Finding,
    SnapshotReport,
    SnapshotSection,
)

_MAX_CONTENT_LENGTH: int = 200


@dataclass
class ToolResult:
    """Generic intermediate result from any agent tool execution."""

    findings: list[Finding] = field(default_factory=list)
    scanned_items: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    chart_series: dict[str, ChartSeries] = field(default_factory=dict)  # chart_id -> series


def severity_from_text(text: str) -> str:
    """Heuristically determine severity from message/log text."""
    lower = text.lower()
    if "critical" in lower or "fatal" in lower:
        return "critical"
    if "error" in lower or "exception" in lower or "failure" in lower:
        return "warning"
    return "info"


def build_agent_result(agent_name: str, result: ToolResult) -> AgentResult:
    """Convert a ToolResult into the canonical AgentResult."""
    if result.errors and not result.scanned_items:
        return AgentResult(
            agent_name=agent_name,
            status="error",
            findings=result.findings,
            summary=f"{agent_name} failed.",
            error_message="; ".join(result.errors),
        )

    parts = [
        f"Inspected {len(result.scanned_items)} item(s). "
        f"Found {len(result.findings)} finding(s)."
    ]
    if result.errors:
        parts.append(f"Errors: {'; '.join(result.errors)}")

    return AgentResult(
        agent_name=agent_name,
        status="success",
        findings=result.findings,
        summary=" ".join(parts),
        error_message="; ".join(result.errors) if result.errors else None,
        chart_series=result.chart_series,
    )


def build_unhealthy_agent_result(agent_name: str, reason: str) -> AgentResult:
    """Build an :class:`AgentResult` marking an agent as fundamentally unhealthy.

    Distinct from :func:`build_agent_result` with ``status="error"``: an
    ``error`` is a request-level failure (transient — the orchestrator's
    "Recommended Actions" suggests "manually check / retry"). An
    ``unhealthy`` result indicates the agent cannot do work in this deployment
    at all (operator-actionable — the recommended action is "investigate agent
    configuration"). Renders as a 🚫 disabled-style evidence block, matching
    the static disabled-in-config presentation.

    Use from a specialized agent's ``@tool`` entry point when its setup-time
    check fails (e.g. EKS cluster API unreachable from the agent's network,
    Prometheus URL not configured, missing IAM credentials).
    """
    return AgentResult(
        agent_name=agent_name,
        status="unhealthy",
        findings=[],
        summary=f"{agent_name} reported unhealthy: {reason}",
        error_message=reason,
    )


def format_result(agent_result: AgentResult) -> str:
    """Produce a human-readable summary string from an AgentResult.

    The trailing block is the :data:`AGENT_RESULT` footer; the master
    orchestrator strips and decodes it via :meth:`AgentFooter.extract`.
    """
    lines: list[str] = [f"Status: {agent_result.status}"]
    lines.append(agent_result.summary)

    if agent_result.error_message:
        lines.append(f"Errors: {agent_result.error_message}")

    if agent_result.findings:
        lines.append("\nFindings:")
        for f in agent_result.findings:
            lines.append(
                f"  [{f.severity.upper()}] ({f.source} @ {f.timestamp}): "
                f"{f.content[:_MAX_CONTENT_LENGTH]}"
            )

    # Neutralise marker delimiters in the readable body (which interpolates
    # untrusted finding content) so a planted footer marker can't suppress the
    # legitimate footer appended below. The footer itself is encoded separately.
    return neutralize_markers("\n".join(lines)) + "\n\n" + AGENT_RESULT.encode(agent_result)


def format_snapshot_result(report: SnapshotReport) -> str:
    """Produce a human-readable snapshot summary string with embedded footer.

    The string is what the agent's ``capture_snapshot`` tool returns. The
    master orchestrator extracts the structured :class:`SnapshotReport` via
    :meth:`AgentFooter.extract` on :data:`SNAPSHOT_RESULT` from the trailing
    ``<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>`` block.
    """
    lines: list[str] = [
        f"Snapshot of {report.agent_name} captured at {report.captured_at}",
    ]
    if report.anomaly:
        lines.append(f"⚠️ {report.anomaly_summary or 'anomaly detected'}")
    for section in report.sections:
        lines.append("")
        lines.append(f"{section.label}:")
        if section.lines:
            for entry in section.lines:
                lines.append(f"  - {entry}")
        else:
            lines.append("  (no data)")
    return neutralize_markers("\n".join(lines)) + "\n\n" + SNAPSHOT_RESULT.encode(report)


# ---------------------------------------------------------------------------
# Parsers — private. Must be defined before the AgentFooter instances below
# (the instances bind these callables at module-load time).
# ---------------------------------------------------------------------------


def _agent_result_from_dict(payload: dict) -> AgentResult:
    findings = [
        _finding_from_dict(item)
        for item in payload.get("findings", [])
        if isinstance(item, dict)
    ]
    metadata_payload = payload.get("metadata")
    metadata = (
        _metadata_from_dict(metadata_payload)
        if isinstance(metadata_payload, dict)
        else AgentMetadata()
    )
    chart_series = _chart_series_map_from_dict(payload.get("chart_series"))
    return AgentResult(
        agent_name=str(payload["agent_name"]),
        status=str(payload["status"]),
        findings=findings,
        summary=str(payload["summary"]),
        error_message=payload.get("error_message"),
        duration_seconds=float(payload.get("duration_seconds") or 0.0),
        metadata=metadata,
        chart_series=chart_series,
    )


def _finding_from_dict(payload: dict) -> Finding:
    metadata = payload.get("metadata")
    link = payload.get("link")
    chart_payload = payload.get("chart")
    chart = (
        _chart_descriptor_from_dict(chart_payload)
        if isinstance(chart_payload, dict)
        else None
    )
    return Finding(
        source=str(payload["source"]),
        timestamp=str(payload["timestamp"]),
        content=str(payload["content"]),
        severity=str(payload["severity"]),
        metadata=metadata if isinstance(metadata, dict) else {},
        link=str(link) if link is not None else None,
        chart=chart,
    )


def _chart_descriptor_from_dict(payload: dict) -> ChartDescriptor:
    return ChartDescriptor(
        chart_id=str(payload["chart_id"]),
        source=str(payload["source"]),
        log_groups=[str(g) for g in payload.get("log_groups", [])],
        query=str(payload["query"]),
        start_epoch=int(payload["start_epoch"]),
        end_epoch=int(payload["end_epoch"]),
    )


def _chart_series_from_dict(payload: dict) -> ChartSeries:
    points = payload.get("points")
    return ChartSeries(
        points=[p for p in points if isinstance(p, dict)]
        if isinstance(points, list)
        else [],
        series_kind=str(payload.get("series_kind", "log_rows")),
        truncated=bool(payload.get("truncated", False)),
    )


def _chart_series_map_from_dict(payload: object) -> dict[str, ChartSeries]:
    """Reconstruct the ``chart_id -> ChartSeries`` map, dropping bad entries."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, ChartSeries] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        try:
            out[str(key)] = _chart_series_from_dict(value)
        except Exception:  # noqa: BLE001 — silent-drop per footer contract
            continue
    return out


def _metadata_from_dict(payload: dict) -> AgentMetadata:
    from dataclasses import fields

    valid_keys = {f.name for f in fields(AgentMetadata)}
    return AgentMetadata(**{k: v for k, v in payload.items() if k in valid_keys})


def _snapshot_report_from_dict(payload: dict) -> SnapshotReport:
    sections = [
        SnapshotSection(
            label=str(item["label"]),
            lines=[str(line) for line in item.get("lines", [])],
        )
        for item in payload.get("sections", [])
        if isinstance(item, dict)
    ]
    metadata_payload = payload.get("metadata")
    metadata = (
        _metadata_from_dict(metadata_payload)
        if isinstance(metadata_payload, dict)
        else AgentMetadata()
    )
    anomaly_summary = payload.get("anomaly_summary")
    return SnapshotReport(
        agent_name=str(payload["agent_name"]),
        captured_at=str(payload["captured_at"]),
        sections=sections,
        anomaly=bool(payload.get("anomaly", False)),
        anomaly_summary=str(anomaly_summary) if anomaly_summary is not None else None,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# AgentFooter instances — the marker-delimited transport for AgentResult and
# SnapshotReport. Defined alongside their dataclasses + parsers; the footer
# module exposes only the generic AgentFooter class, no instances.
# ---------------------------------------------------------------------------


AGENT_RESULT: AgentFooter[AgentResult] = AgentFooter(
    "AGENT_RESULT", parse=_agent_result_from_dict,
)

SNAPSHOT_RESULT: AgentFooter[SnapshotReport] = AgentFooter(
    "SNAPSHOT_RESULT", parse=_snapshot_report_from_dict,
)
