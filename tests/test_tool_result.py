"""Tests for shared ToolResult/AgentResult formatting helpers."""

from shared.models import (
    AgentMetadata,
    AgentResult,
    Finding,
    SnapshotReport,
    SnapshotSection,
)
from shared.tool_result import (
    encode_snapshot_report,
    extract_agent_result,
    extract_snapshot_report,
    find_snapshot_report_footer,
    format_result,
    format_snapshot_result,
)


def test_format_result_embeds_structured_agent_result_footer():
    """Tool output carries both readable text and recoverable structured data."""
    agent_result = AgentResult(
        agent_name="eks",
        status="success",
        findings=[
            Finding(
                source="pod/api-123",
                timestamp="2025-01-15T14:32:00Z",
                content="Pod api-123: phase=Failed",
                severity="critical",
                metadata={"kind": "pod_status", "pod": "api-123"},
            )
        ],
        summary="Inspected 1 item(s). Found 1 finding(s).",
    )

    text = format_result(agent_result)
    clean_text, recovered = extract_agent_result(text)

    assert "Status: success" in clean_text
    assert "Pod api-123: phase=Failed" in clean_text
    assert recovered == agent_result


# ---------------------------------------------------------------------------
# SnapshotReport footer roundtrip
# ---------------------------------------------------------------------------


def _basic_report(anomaly: bool = False) -> SnapshotReport:
    return SnapshotReport(
        agent_name="slack_scanner",
        captured_at="2026-05-28T19:00:00+00:00",
        sections=[
            SnapshotSection(
                label="Authentication",
                lines=["workspace: Acme (T123)", "bot user: sre-bot (U456)"],
            ),
            SnapshotSection(
                label="Channel access",
                lines=["bot is a member of 12 channel(s)"],
            ),
        ],
        anomaly=anomaly,
        anomaly_summary="Slack auth.test failed: invalid_auth" if anomaly else None,
    )


def test_format_snapshot_result_includes_human_lines_and_footer():
    text = format_snapshot_result(_basic_report())
    # Human-readable lines
    assert "Snapshot of slack_scanner captured at 2026-05-28T19:00:00+00:00" in text
    assert "Authentication:" in text
    assert "  - workspace: Acme (T123)" in text
    assert "  - bot is a member of 12 channel(s)" in text
    # Footer marker present
    assert "<<<SNAPSHOT_RESULT " in text
    assert " SNAPSHOT_RESULT>>>" in text


def test_format_snapshot_result_anomaly_line():
    text = format_snapshot_result(_basic_report(anomaly=True))
    assert "⚠️ Slack auth.test failed: invalid_auth" in text


def test_format_snapshot_result_empty_section_renders_no_data():
    report = SnapshotReport(
        agent_name="slack_scanner",
        captured_at="2026-05-28T19:00:00+00:00",
        sections=[SnapshotSection(label="Authentication", lines=[])],
    )
    text = format_snapshot_result(report)
    assert "Authentication:" in text
    assert "  (no data)" in text


def test_extract_snapshot_report_round_trip():
    report = _basic_report(anomaly=True)
    text = format_snapshot_result(report)
    cleaned, recovered = extract_snapshot_report(text)
    # Footer is stripped from the cleaned text
    assert "<<<SNAPSHOT_RESULT " not in cleaned
    assert "SNAPSHOT_RESULT>>>" not in cleaned
    # Round-trip recovers the typed SnapshotReport
    assert recovered == report


def test_extract_snapshot_report_returns_none_when_absent():
    cleaned, recovered = extract_snapshot_report("plain text, no footer here")
    assert cleaned == "plain text, no footer here"
    assert recovered is None


def test_extract_snapshot_report_handles_malformed_json():
    bad = "preamble\n\n<<<SNAPSHOT_RESULT not-json SNAPSHOT_RESULT>>>"
    cleaned, recovered = extract_snapshot_report(bad)
    # Footer is stripped even when JSON is invalid
    assert "<<<SNAPSHOT_RESULT" not in cleaned
    assert recovered is None


def test_find_snapshot_report_footer_returns_raw_block():
    text = format_snapshot_result(_basic_report())
    footer = find_snapshot_report_footer(text)
    assert footer is not None
    assert footer.startswith("<<<SNAPSHOT_RESULT ")
    assert footer.endswith(" SNAPSHOT_RESULT>>>")


def test_find_snapshot_report_footer_returns_none_when_absent():
    assert find_snapshot_report_footer("nothing here") is None


def test_encode_snapshot_report_uses_compact_separators():
    # Empty sections so the only `:` / `,` in the JSON come from json.dumps itself,
    # not from any user-supplied content like "workspace: Acme".
    report = SnapshotReport(
        agent_name="slack_scanner",
        captured_at="2026-05-28T19:00:00+00:00",
        sections=[],
    )
    encoded = encode_snapshot_report(report)
    # Compact JSON: no whitespace after `,` or `:` separators.
    assert ", " not in encoded
    assert ": " not in encoded


def test_extract_snapshot_report_preserves_metadata_when_round_tripped():
    report = SnapshotReport(
        agent_name="slack_scanner",
        captured_at="2026-05-28T19:00:00+00:00",
        sections=[],
        metadata=AgentMetadata(model_id="claude-haiku-4-5", input_tokens=100),
    )
    text = format_snapshot_result(report)
    _, recovered = extract_snapshot_report(text)
    assert recovered is not None
    assert recovered.metadata.model_id == "claude-haiku-4-5"
    assert recovered.metadata.input_tokens == 100

