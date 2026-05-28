"""Platform-specific report rendering.

ReportFormatter builds platform-agnostic ReportSections; a ReportRenderer
turns those sections into the markup expected by each chat platform.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

EvidenceStatus = Literal["ok", "pending", "error", "disabled"]
EnrichmentStatus = Literal["ok", "error"]


@dataclass
class EvidenceBlock:
    """One agent's evidence subsection."""

    emoji: str
    display_name: str
    lines: list[str]  # Pre-built content lines (no markup)
    metadata_line: str | None = None  # Optional one-liner with model/tokens/cost
    status: EvidenceStatus = "ok"  # drives header marker


@dataclass
class InvestigationStartedSections:
    """Platform-agnostic "investigation kicked off" announcement."""

    alert_text: str
    investigation_id: str
    dispatched: list[tuple[str, str]]  # [(emoji, display_name), ...]


@dataclass
class ReportSections:
    """Platform-agnostic structured report data."""

    severity: str  # e.g. "🔴 Critical"
    affected_services: str
    time_of_detection: str
    summary: str
    root_cause: str
    evidence_blocks: list[EvidenceBlock]
    impact_assessment: str
    recommended_actions: str
    links: list[tuple[str, str]]  # [(url, label), ...] — renderer formats per-platform
    variant_label: str | None = None  # e.g. "[A: Claude Sonnet]"
    totals_line: str | None = None  # Optional aggregate "tokens=…in/…out · cost=$…"
    # Per-agent raw pieces. When non-empty, the renderer prefers these and
    # normalizes each piece before joining — preserves heading promotion
    # for content that ends up in mid-line position (e.g. inside bullets).
    # The legacy ``summary`` / ``root_cause`` strings remain as a fallback
    # for callers that haven't been updated.
    summary_parts: list[str] = field(default_factory=list)
    root_cause_parts: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class EnrichmentSections:
    """Platform-agnostic enrichment update data."""

    emoji: str
    display_name: str
    findings_lines: list[str]
    updated_assessment: str
    variant_label: str | None = None
    metadata_line: str | None = None  # Optional one-liner with model/tokens/cost
    status: EnrichmentStatus = "ok"


@dataclass
class PIRSections:
    """Platform-agnostic Post-Incident Report data."""

    incident_summary: str
    timeline: str  # Pre-formatted timeline text
    root_cause: str
    impact: str
    action_items: str
    lessons_learned: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ReportRenderer(Protocol):
    """Renders structured report data into platform-specific markup."""

    def render_report(self, sections: ReportSections) -> str: ...
    def render_enrichment(self, sections: EnrichmentSections) -> str: ...
    def render_pir(self, sections: PIRSections) -> str: ...
    def render_investigation_started(
        self, sections: InvestigationStartedSections
    ) -> str: ...
    def normalize(self, text: str) -> str:
        """Translate agent-produced CommonMark into this platform's markup.

        Exposed so the formatter can normalize raw agent text upstream,
        before composing it into bulleted lists or other structured content
        where the per-line heuristics in the dialect's regex no longer fire.
        """
        ...


# ---------------------------------------------------------------------------
# Markup dialect — the only thing that varies between platforms
# ---------------------------------------------------------------------------


# Patterns used by the per-dialect normalizers to translate agent-produced
# CommonMark/GFM into platform-native markup.
_RE_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_RE_MD_BOLD_AST = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_MD_BOLD_UND = re.compile(r"__(.+?)__", re.DOTALL)
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_RE_BLANK_LINES = re.compile(r"\n{3,}")
# LLMs sometimes glue prose onto a heading without a newline ("…done.## Next
# Section"). The line-anchored heading regex misses that, so we force a break
# before any `#{1,6} word` whose `#`-run sits mid-line. Restricted to a `#`
# preceded by sentence-ending punctuation to avoid mauling URL fragments.
_RE_INLINE_HEADING_BREAK = re.compile(r"(?<=[.!?:])(#{1,6}[ \t]+)")


@dataclass(frozen=True)
class MarkupDialect:
    """Tokens that differ between chat-platform markup languages."""

    bold_open: str
    bold_close: str
    separator: str

    def bold(self, text: str) -> str:
        return f"{self.bold_open}{text}{self.bold_close}"

    def format_link(self, url: str, label: str) -> str:
        """Format a hyperlink.  Subclasses override via class-level override."""
        raise NotImplementedError  # pragma: no cover

    def normalize(self, text: str) -> str:
        """Translate agent-produced CommonMark into this platform's markup.

        Default: leave the text alone — used for Discord, whose Markdown is a
        superset of what agents emit.  Slack overrides because its mrkdwn
        flavour rejects ATX headings and uses single-asterisk bold.
        """
        return text


class SlackDialect(MarkupDialect):
    """Slack mrkdwn tokens."""

    def __init__(self) -> None:
        super().__init__(bold_open="*", bold_close="*", separator="━━━━━━━━━━━━━━━━━━━")

    def format_link(self, url: str, label: str) -> str:
        return f"<{url}|{label}>"

    def normalize(self, text: str) -> str:
        if not text:
            return text
        # Force a line break before mid-line headings glued to prose.
        text = _RE_INLINE_HEADING_BREAK.sub(r"\n\n\1", text)
        # Headings render verbatim in Slack (no #-syntax). Promote them to bold.
        text = _RE_MD_HEADING.sub(lambda m: self.bold(m.group(1)), text)
        # Slack mrkdwn bold is single-asterisk, not double; same for __bold__.
        text = _RE_MD_BOLD_AST.sub(lambda m: self.bold(m.group(1)), text)
        text = _RE_MD_BOLD_UND.sub(lambda m: self.bold(m.group(1)), text)
        # Inline links use Slack's <url|label> form.
        text = _RE_MD_LINK.sub(
            lambda m: self.format_link(m.group(2), m.group(1)), text,
        )
        return _RE_BLANK_LINES.sub("\n\n", text)


class DiscordDialect(MarkupDialect):
    """Discord standard Markdown tokens."""

    def __init__(self) -> None:
        super().__init__(bold_open="**", bold_close="**", separator="───────────────────")

    def format_link(self, url: str, label: str) -> str:
        return f"[{label}]({url})"

    def normalize(self, text: str) -> str:
        if not text:
            return text
        return _RE_BLANK_LINES.sub("\n\n", text)


# ---------------------------------------------------------------------------
# Single renderer parameterised by dialect
# ---------------------------------------------------------------------------


class MarkupReportRenderer:
    """Renders reports using a pluggable MarkupDialect."""

    def __init__(self, dialect: MarkupDialect) -> None:
        self._d = dialect

    def normalize(self, text: str) -> str:
        return self._d.normalize(text)

    def render_report(self, sections: ReportSections) -> str:
        d = self._d
        header = f"🚨 {d.bold('Incident Report')}"
        if sections.variant_label:
            header = f"📊 {d.bold(f'[{sections.variant_label}]')} {header}"
        parts: list[str] = [
            header,
            d.separator,
            f"{d.bold('Severity:')} {sections.severity}",
            f"{d.bold('Affected Services:')} {sections.affected_services}",
            f"{d.bold('Time of Detection:')} {sections.time_of_detection}",
        ]
        if sections.totals_line:
            parts.append(f"{d.bold('Investigation Cost:')} {sections.totals_line}")
        parts += [
            "",
            d.bold("Summary"),
            self._render_summary(sections),
            "",
            d.bold("Root Cause Hypothesis"),
            self._render_root_cause(sections),
            "",
            d.bold("Evidence"),
            self._render_evidence(sections.evidence_blocks),
            "",
            d.bold("Impact Assessment"),
            d.normalize(sections.impact_assessment),
            "",
            d.bold("Recommended Actions"),
            sections.recommended_actions,
            "",
            d.bold("Links & References"),
            self._render_links(sections.links),
        ]
        return "\n".join(parts)

    def _render_summary(self, sections: ReportSections) -> str:
        """Prefer per-agent summary_parts when present; normalize each piece
        independently before joining so heading promotion still fires for
        content that would otherwise end up mid-line in the joined string.
        """
        if sections.summary_parts:
            return " ".join(self._d.normalize(p) for p in sections.summary_parts)
        return self._d.normalize(sections.summary)

    def _render_root_cause(self, sections: ReportSections) -> str:
        """Prefer per-agent root_cause_parts: each (display, raw_summary) is
        normalized in isolation, then prefixed with ``- {display}: `` and
        bulleted under the standard preamble.
        """
        if sections.root_cause_parts:
            bullets = [
                f"- {display}: {self._d.normalize(raw)}"
                for display, raw in sections.root_cause_parts
            ]
            return "Based on available evidence:\n" + "\n".join(bullets)
        return self._d.normalize(sections.root_cause)

    def render_enrichment(self, sections: EnrichmentSections) -> str:
        d = self._d
        marker = "⚠️ " if sections.status == "error" else "📬 "
        title = (
            f"Late Result (failed) — {sections.display_name}"
            if sections.status == "error"
            else f"Enrichment Update — {sections.display_name}"
        )
        header = f"{marker}{d.bold(title)}"
        if sections.variant_label:
            header = f"📊 {d.bold(f'[{sections.variant_label}]')} {header}"
        parts: list[str] = [
            header,
            d.separator,
        ]
        if sections.metadata_line:
            parts.append(sections.metadata_line)
        section_label = "Error" if sections.status == "error" else "New Findings"
        parts += ["", d.bold(f"{section_label}:")]
        for line in sections.findings_lines:
            parts.append(f"- {d.normalize(line)}")
        parts += [
            "",
            d.bold("Updated Assessment:"),
            d.normalize(sections.updated_assessment),
        ]
        return "\n".join(parts)

    def render_investigation_started(
        self, sections: InvestigationStartedSections
    ) -> str:
        d = self._d
        parts: list[str] = [
            f"🔎 {d.bold('Investigation Started')}",
            d.separator,
            f"{d.bold('Alert:')} {sections.alert_text}",
            f"{d.bold('Investigation ID:')} {sections.investigation_id}",
            "",
            d.bold("Querying agents:"),
        ]
        for emoji, display_name in sections.dispatched:
            parts.append(f"- {emoji} {display_name}")
        parts.append("")
        parts.append(
            "_Initial report in up to 60s; late results until the 5-minute cutoff._"
        )
        return "\n".join(parts)

    def render_pir(self, sections: PIRSections) -> str:
        d = self._d
        parts: list[str] = [
            f"📋 {d.bold('Post-Incident Report')}",
            d.separator,
            "",
            d.bold("Incident Summary"),
            d.normalize(sections.incident_summary),
            "",
            d.bold("Timeline"),
            sections.timeline,
            "",
            d.bold("Root Cause"),
            d.normalize(sections.root_cause),
            "",
            d.bold("Impact"),
            d.normalize(sections.impact),
            "",
            d.bold("Action Items"),
            sections.action_items,
            "",
            d.bold("Lessons Learned"),
            d.normalize(sections.lessons_learned),
        ]
        return "\n".join(parts)

    def _render_evidence(self, blocks: list[EvidenceBlock]) -> str:
        d = self._d
        if not blocks:
            return "_No agents were configured for this investigation._"
        parts: list[str] = []
        for b in blocks:
            status_marker = ""
            if b.status == "pending":
                status_marker = " ⏳"
            elif b.status == "error":
                status_marker = " ⚠️"
            elif b.status == "disabled":
                status_marker = " 🚫"
            parts.append(f"\n{b.emoji} {d.bold(b.display_name)}{status_marker}")
            if b.metadata_line:
                parts.append(f"_{b.metadata_line}_")
            for line in b.lines:
                parts.append(f"- {d.normalize(line)}")
        return "\n".join(parts)

    def _render_links(self, links: list[tuple[str, str]]) -> str:
        if not links:
            return "- No additional links available"
        return "\n".join(f"- {self._d.format_link(url, label)}" for url, label in links)


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------


def SlackReportRenderer() -> MarkupReportRenderer:  # noqa: N802
    """Create a Slack mrkdwn renderer."""
    return MarkupReportRenderer(SlackDialect())


def DiscordReportRenderer() -> MarkupReportRenderer:  # noqa: N802
    """Create a Discord Markdown renderer."""
    return MarkupReportRenderer(DiscordDialect())


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_RENDERER_REGISTRY: dict[str, Callable[[], MarkupReportRenderer]] = {
    "slack": SlackReportRenderer,
    "discord": DiscordReportRenderer,
}


def create_report_renderer(platform: str) -> ReportRenderer:
    """Create a ReportRenderer for the given platform."""
    factory = _RENDERER_REGISTRY.get(platform)
    if factory is None:
        raise ValueError(
            f"Unsupported platform: {platform!r}. "
            f"Supported: {', '.join(_RENDERER_REGISTRY)}"
        )
    return factory()
