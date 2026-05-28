"""Shared result-building utilities for agent tool functions."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields

from shared.models import (
    AgentMetadata,
    AgentResult,
    Finding,
    SnapshotReport,
    SnapshotSection,
)

_MAX_CONTENT_LENGTH: int = 200
AGENT_RESULT_PREFIX = "<<<AGENT_RESULT "
AGENT_RESULT_SUFFIX = " AGENT_RESULT>>>"
_AGENT_RESULT_RE = re.compile(
    re.escape(AGENT_RESULT_PREFIX) + r"(.*?)" + re.escape(AGENT_RESULT_SUFFIX),
    re.DOTALL,
)

SNAPSHOT_RESULT_PREFIX = "<<<SNAPSHOT_RESULT "
SNAPSHOT_RESULT_SUFFIX = " SNAPSHOT_RESULT>>>"
_SNAPSHOT_RESULT_RE = re.compile(
    re.escape(SNAPSHOT_RESULT_PREFIX) + r"(.*?)" + re.escape(SNAPSHOT_RESULT_SUFFIX),
    re.DOTALL,
)


@dataclass
class ToolResult:
    """Generic intermediate result from any agent tool execution."""

    findings: list[Finding] = field(default_factory=list)
    scanned_items: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


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
    """Produce a human-readable summary string from an AgentResult."""
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

    return "\n".join(lines) + "\n\n" + encode_agent_result(agent_result)


def encode_agent_result(agent_result: AgentResult) -> str:
    """Serialise an AgentResult for appending to an agent text response."""
    payload = json.dumps(asdict(agent_result), separators=(",", ":"))
    return f"{AGENT_RESULT_PREFIX}{payload}{AGENT_RESULT_SUFFIX}"


def extract_agent_result(text: str) -> tuple[str, AgentResult | None]:
    """Strip and decode an embedded AgentResult from *text* if present."""
    match = _AGENT_RESULT_RE.search(text)
    if match is None:
        return text, None

    cleaned = _AGENT_RESULT_RE.sub("", text).strip()
    try:
        payload = json.loads(match.group(1))
        return cleaned, _agent_result_from_dict(payload)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return cleaned, None


def find_agent_result_footer(text: str) -> str | None:
    """Return the raw embedded AgentResult footer from *text*, if present."""
    match = _AGENT_RESULT_RE.search(text)
    return match.group(0) if match is not None else None


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
    return AgentResult(
        agent_name=str(payload["agent_name"]),
        status=str(payload["status"]),
        findings=findings,
        summary=str(payload["summary"]),
        error_message=payload.get("error_message"),
        duration_seconds=float(payload.get("duration_seconds") or 0.0),
        metadata=metadata,
    )


def _finding_from_dict(payload: dict) -> Finding:
    metadata = payload.get("metadata")
    return Finding(
        source=str(payload["source"]),
        timestamp=str(payload["timestamp"]),
        content=str(payload["content"]),
        severity=str(payload["severity"]),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _metadata_from_dict(payload: dict) -> AgentMetadata:
    valid_keys = {f.name for f in fields(AgentMetadata)}
    return AgentMetadata(**{k: v for k, v in payload.items() if k in valid_keys})


# ---------------------------------------------------------------------------
# SnapshotReport transport — parallel to AgentResult, used by /status path
# ---------------------------------------------------------------------------


def format_snapshot_result(report: SnapshotReport) -> str:
    """Produce a human-readable snapshot summary string with embedded footer.

    The string is what the agent's ``capture_snapshot`` tool returns. The
    master orchestrator extracts the structured :class:`SnapshotReport` via
    :func:`extract_snapshot_report` from the trailing
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
    return "\n".join(lines) + "\n\n" + encode_snapshot_report(report)


def encode_snapshot_report(report: SnapshotReport) -> str:
    """Serialise a :class:`SnapshotReport` for appending to an agent text response."""
    payload = json.dumps(asdict(report), separators=(",", ":"))
    return f"{SNAPSHOT_RESULT_PREFIX}{payload}{SNAPSHOT_RESULT_SUFFIX}"


def extract_snapshot_report(text: str) -> tuple[str, SnapshotReport | None]:
    """Strip and decode an embedded :class:`SnapshotReport` from *text* if present.

    Returns ``(cleaned_text, report)``. ``report`` is ``None`` when the
    footer is absent or malformed; ``cleaned_text`` is *text* with the
    footer block removed and trailing whitespace trimmed.
    """
    match = _SNAPSHOT_RESULT_RE.search(text)
    if match is None:
        return text, None

    cleaned = _SNAPSHOT_RESULT_RE.sub("", text).strip()
    try:
        payload = json.loads(match.group(1))
        return cleaned, _snapshot_report_from_dict(payload)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return cleaned, None


def find_snapshot_report_footer(text: str) -> str | None:
    """Return the raw embedded :class:`SnapshotReport` footer from *text*, if present."""
    match = _SNAPSHOT_RESULT_RE.search(text)
    return match.group(0) if match is not None else None


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
