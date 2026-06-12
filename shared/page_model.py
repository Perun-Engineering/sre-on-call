"""The render contract between the master and the #33 incident-page renderer.

The master builds a :class:`PageModel` at finalize (Phase 7) and writes it to
``page_model.json`` in the investigation's trace prefix. The renderer Lambda
reads exactly this file (plus the referenced ``charts/<id>.json`` series) — it
never parses A2A events or couples to the trace manifest schema. Keep this
dataclass JSON-stable: add fields freely, never repurpose existing ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = 1


@dataclass
class PageEvidenceLine:
    """One rendered evidence line — finding text plus an optional deep link."""

    text: str
    link: str | None = None

    def to_json_dict(self) -> dict:
        return {"text": self.text, "link": self.link}


@dataclass
class PageEvidenceBlock:
    """Per-agent evidence block mirrored from the chat report's Evidence section."""

    emoji: str
    display_name: str
    status: str  # EvidenceStatus: ok|error|pending|disabled|skipped
    lines: list[PageEvidenceLine] = field(default_factory=list)
    chart_id: str | None = None  # set when this agent's findings carry chart data

    def to_json_dict(self) -> dict:
        return {
            "emoji": self.emoji,
            "display_name": self.display_name,
            "status": self.status,
            "lines": [line.to_json_dict() for line in self.lines],
            "chart_id": self.chart_id,
        }


@dataclass
class PageModel:
    """Everything the renderer needs to draw one investigation page."""

    schema_version: int
    investigation_id: str
    generated_at: str  # ISO 8601
    alert_text: str
    severity: str  # e.g. "🔴 Critical"
    affected_services: str
    time_of_detection: str
    status: str  # manifest status: completed|partial|failed; resolved after PIR (#55)
    summary: str
    root_cause: str
    analysis: dict | None  # AnalysisSection fields, or None
    evidence: list[PageEvidenceBlock] = field(default_factory=list)
    chart_ids: list[str] = field(default_factory=list)
    # Ordered incident timeline (#34): a list of TimelineEvent json dicts, or
    # None when no timeline was built. The renderer draws it as a scrubbable
    # strip whose events focus the linked chart window on click.
    timeline: list[dict] | None = None

    def to_json_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "investigation_id": self.investigation_id,
            "generated_at": self.generated_at,
            "alert_text": self.alert_text,
            "severity": self.severity,
            "affected_services": self.affected_services,
            "time_of_detection": self.time_of_detection,
            "status": self.status,
            "summary": self.summary,
            "root_cause": self.root_cause,
            "analysis": self.analysis,
            "evidence": [b.to_json_dict() for b in self.evidence],
            "chart_ids": list(self.chart_ids),
            "timeline": self.timeline,
        }
