"""Unit tests for the SnapshotReport / SnapshotSections data model and the
``MarkupReportRenderer.render_snapshot`` output for both dialects.

These tests cover slice 1 of the ``/sre-snapshot`` command: data model + renderer
scaffolding. No agent-side or master-side behaviour is exercised here —
just the dataclasses and the dialect-aware rendering of pre-built
``SnapshotSections`` payloads.
"""

from __future__ import annotations

from shared.models import AgentMetadata, SnapshotReport, SnapshotSection
from shared.report_renderer import (
    DiscordReportRenderer,
    SlackReportRenderer,
    SnapshotBlock,
    SnapshotSections,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block(
    *,
    emoji: str = "☸️",
    display_name: str = "EKS Cluster State",
    header_line: str = "model=claude · network=VPC · skills=capture_snapshot",
    sections: list[SnapshotSection] | None = None,
    status: str = "ok",
    error_message: str | None = None,
) -> SnapshotBlock:
    return SnapshotBlock(
        emoji=emoji,
        display_name=display_name,
        header_line=header_line,
        sections=sections or [
            SnapshotSection(label="Nodes", lines=["10 Ready · 0 NotReady"]),
            SnapshotSection(label="Pods", lines=["312 Running · 0 Failed"]),
        ],
        status=status,  # type: ignore[arg-type]
        error_message=error_message,
    )


def _sections(blocks: list[SnapshotBlock] | None = None) -> SnapshotSections:
    return SnapshotSections(
        requested_at="2026-05-28T19:00:00+00:00",
        summary_line="🩺 1/1 agents healthy",
        blocks=blocks if blocks is not None else [_block()],
    )


# ---------------------------------------------------------------------------
# Dataclass shapes
# ---------------------------------------------------------------------------


class TestSnapshotSection:
    def test_basic_construction(self):
        s = SnapshotSection(label="Nodes", lines=["10 Ready"])
        assert s.label == "Nodes"
        assert s.lines == ["10 Ready"]

    def test_empty_lines_allowed(self):
        s = SnapshotSection(label="Pods", lines=[])
        assert s.lines == []


class TestSnapshotReport:
    def test_anomaly_defaults_false(self):
        r = SnapshotReport(
            agent_name="eks",
            captured_at="2026-05-28T19:00:00+00:00",
            sections=[],
        )
        assert r.anomaly is False
        assert r.anomaly_summary is None

    def test_default_metadata_is_empty_agent_metadata(self):
        r = SnapshotReport(
            agent_name="eks",
            captured_at="2026-05-28T19:00:00+00:00",
            sections=[],
        )
        assert isinstance(r.metadata, AgentMetadata)
        assert r.metadata.model_id is None

    def test_anomaly_with_summary(self):
        r = SnapshotReport(
            agent_name="eks",
            captured_at="2026-05-28T19:00:00+00:00",
            sections=[SnapshotSection(label="Pods", lines=["3 CrashLoopBackOff"])],
            anomaly=True,
            anomaly_summary="EKS reports 3 pods CrashLoopBackOff",
        )
        assert r.anomaly is True
        assert r.anomaly_summary == "EKS reports 3 pods CrashLoopBackOff"


class TestSnapshotBlock:
    def test_defaults_to_ok_status(self):
        b = _block()
        assert b.status == "ok"
        assert b.error_message is None
        assert b.anomaly_summary is None

    def test_can_be_marked_anomaly(self):
        b = _block(status="anomaly")
        assert b.status == "anomaly"

    def test_error_block_carries_message(self):
        b = _block(status="error", error_message="timed out after 30s")
        assert b.status == "error"
        assert b.error_message == "timed out after 30s"

    def test_anomaly_block_can_carry_summary(self):
        b = SnapshotBlock(
            emoji="📡",
            display_name="Slack Scanner",
            header_line="model=claude · network=PUBLIC · skills=capture_snapshot",
            sections=[],
            status="anomaly",
            anomaly_summary="Slack auth.test failed: invalid_auth",
        )
        assert b.anomaly_summary == "Slack auth.test failed: invalid_auth"


class TestSnapshotSections:
    def test_basic_construction(self):
        s = _sections()
        assert s.requested_at == "2026-05-28T19:00:00+00:00"
        assert "1/1 agents healthy" in s.summary_line
        assert len(s.blocks) == 1

    def test_no_blocks_allowed(self):
        s = SnapshotSections(
            requested_at="2026-05-28T19:00:00+00:00",
            summary_line="🩺 0/0 agents",
            blocks=[],
        )
        assert s.blocks == []


# ---------------------------------------------------------------------------
# Slack dialect rendering
# ---------------------------------------------------------------------------


class TestSlackRenderSnapshot:
    def test_header_and_summary_line_present(self):
        out = SlackReportRenderer().render_snapshot(_sections())
        assert "🩺 *Status Snapshot*" in out
        assert "*Captured at:* 2026-05-28T19:00:00+00:00" in out
        assert "🩺 1/1 agents healthy" in out

    def test_block_renders_emoji_display_name_and_header_line(self):
        out = SlackReportRenderer().render_snapshot(_sections())
        assert "☸️ *EKS Cluster State*" in out
        assert "_model=claude · network=VPC · skills=capture_snapshot_" in out

    def test_block_renders_section_label_and_lines_as_bullets(self):
        out = SlackReportRenderer().render_snapshot(_sections())
        assert "*Nodes*" in out
        assert "- 10 Ready · 0 NotReady" in out
        assert "*Pods*" in out
        assert "- 312 Running · 0 Failed" in out

    def test_anomaly_status_renders_warning_marker(self):
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="anomaly")]),
        )
        assert "☸️ *EKS Cluster State* ⚠️" in out

    def test_anomaly_summary_renders_as_italic_line(self):
        block = SnapshotBlock(
            emoji="📡",
            display_name="Slack Scanner",
            header_line="model=claude · network=PUBLIC",
            sections=[SnapshotSection(label="Authentication", lines=["❌ auth.test failed"])],
            status="anomaly",
            anomaly_summary="Slack auth.test failed: invalid_auth",
        )
        out = SlackReportRenderer().render_snapshot(_sections(blocks=[block]))
        # Italic line directly under the header line
        assert "_Slack auth.test failed: invalid_auth_" in out
        # Section bodies still render under the anomaly summary
        assert "*Authentication*" in out
        assert "- ❌ auth.test failed" in out

    def test_anomaly_without_summary_renders_no_summary_line(self):
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="anomaly")]),  # no anomaly_summary
        )
        # No spurious italic summary line
        # (header_line is also italic, so check there's exactly one italic line)
        italic_lines = [line for line in out.splitlines() if line.startswith("_") and line.endswith("_")]
        assert len(italic_lines) == 1  # only the header_line

    def test_error_status_renders_x_marker_and_error_message(self):
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="error", error_message="timed out after 30s")]),
        )
        assert "☸️ *EKS Cluster State* ❌" in out
        assert "- timed out after 30s" in out
        # Section bodies must NOT render for error blocks
        assert "*Nodes*" not in out
        assert "*Pods*" not in out

    def test_error_status_with_no_message_falls_back(self):
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="error", error_message=None)]),
        )
        assert "❌" in out
        assert "- no response" in out

    def test_disabled_status_renders_disabled_marker_and_notice(self):
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="disabled")]),
        )
        assert "🚫" in out
        assert "Disabled in config.yaml" in out
        # Section bodies must NOT render for disabled blocks
        assert "*Nodes*" not in out
        assert "*Pods*" not in out

    def test_empty_section_lines_render_no_data_placeholder(self):
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(sections=[SnapshotSection(label="Nodes", lines=[])])]),
        )
        assert "*Nodes*" in out
        assert "_No data._" in out

    def test_multiple_blocks_render_in_order(self):
        master = _block(emoji="🎯", display_name="Master Agent")
        eks = _block(emoji="☸️", display_name="EKS Cluster State")
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[master, eks]),
        )
        master_idx = out.index("Master Agent")
        eks_idx = out.index("EKS Cluster State")
        assert master_idx < eks_idx

    def test_no_blocks_renders_header_only(self):
        out = SlackReportRenderer().render_snapshot(
            SnapshotSections(
                requested_at="2026-05-28T19:00:00+00:00",
                summary_line="🩺 0/0 agents",
                blocks=[],
            ),
        )
        assert "🩺 *Status Snapshot*" in out
        assert "*Captured at:*" in out
        assert "0/0 agents" in out
        # No agent display names
        assert "EKS" not in out
        assert "Master" not in out

    def test_empty_header_line_is_skipped(self):
        out = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(header_line="")]),
        )
        # No empty italics block
        assert "__" not in out
        # Block name still renders
        assert "EKS Cluster State" in out

    def test_uses_slack_single_asterisk_bold(self):
        out = SlackReportRenderer().render_snapshot(_sections())
        # Slack mrkdwn uses single *bold*, not double **bold**
        assert "*Status Snapshot*" in out
        assert "**Status Snapshot**" not in out


# ---------------------------------------------------------------------------
# Discord dialect rendering
# ---------------------------------------------------------------------------


class TestDiscordRenderSnapshot:
    def test_header_and_summary_line_present(self):
        out = DiscordReportRenderer().render_snapshot(_sections())
        assert "🩺 **Status Snapshot**" in out
        assert "**Captured at:** 2026-05-28T19:00:00+00:00" in out
        assert "🩺 1/1 agents healthy" in out

    def test_uses_discord_double_asterisk_bold(self):
        out = DiscordReportRenderer().render_snapshot(_sections())
        # Discord MD uses **bold**, not single *bold*
        assert "**Status Snapshot**" in out
        # Make sure we're not accidentally emitting Slack-style single-asterisk bold
        # for the headline (single asterisks render as italics in Discord MD).
        assert "*Status Snapshot*" not in out.replace("**Status Snapshot**", "")

    def test_anomaly_marker_consistent_across_dialects(self):
        slack = SlackReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="anomaly")]),
        )
        discord = DiscordReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="anomaly")]),
        )
        assert "⚠️" in slack
        assert "⚠️" in discord

    def test_error_marker_consistent_across_dialects(self):
        discord = DiscordReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="error", error_message="boom")]),
        )
        assert "❌" in discord
        assert "- boom" in discord

    def test_disabled_marker_consistent_across_dialects(self):
        discord = DiscordReportRenderer().render_snapshot(
            _sections(blocks=[_block(status="disabled")]),
        )
        assert "🚫" in discord
        assert "Disabled in config.yaml" in discord


# ---------------------------------------------------------------------------
# Cross-dialect parity
# ---------------------------------------------------------------------------


class TestDialectParity:
    """Both dialects must surface the same logical content — only markup
    differs."""

    def test_same_blocks_same_text_content(self):
        sections = _sections(blocks=[_block(), _block(emoji="🎯", display_name="Master")])
        slack = SlackReportRenderer().render_snapshot(sections)
        discord = DiscordReportRenderer().render_snapshot(sections)

        for needle in (
            "Status Snapshot",
            "Captured at:",
            "Nodes",
            "Pods",
            "10 Ready · 0 NotReady",
            "312 Running · 0 Failed",
            "EKS Cluster State",
            "Master",
        ):
            assert needle in slack, f"{needle!r} missing from Slack render"
            assert needle in discord, f"{needle!r} missing from Discord render"
