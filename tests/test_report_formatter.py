"""Unit tests for the ReportFormatter (section builders) + the Slack/Discord
markup dialects that render those sections.

After step 4 of the deepening migration, ``ReportFormatter`` only builds
platform-agnostic ``ReportSections``; rendering lives in
:class:`shared.platforms.ChatPlatform` implementations. These tests
combine the builder with a renderer locally to exercise the full
build-then-render pipeline.
"""

import pytest

from agents.master.report_formatter import ReportFormatter, AGENT_DISPLAY
from shared.models import AgentResult, AgentFailure, AlertContext, Finding
from shared.report_renderer import (
    DiscordDialect,
    DiscordReportRenderer,
    SlackDialect,
    SlackReportRenderer,
)


@pytest.fixture
def formatter():
    return ReportFormatter()


@pytest.fixture
def slack_renderer():
    return SlackReportRenderer()


@pytest.fixture
def alert_context():
    return AlertContext(
        investigation_id="inv-001",
        platform="slack",
        channel_id="C12345",
        message_id="1705312320.000100",
        alert_text="High CPU usage on service-api",
        alert_timestamp="2025-01-15 14:32:00 UTC",
        investigation_window=("2025-01-15 14:27:00 UTC", "2025-01-15 14:37:00 UTC"),
    )


def _make_finding(content="test finding", severity="info", source="test-source", metadata=None):
    return Finding(
        source=source,
        timestamp="2025-01-15T14:32:00Z",
        content=content,
        severity=severity,
        metadata=metadata or {},
    )


def _make_success_result(agent_name, findings=None, summary="Agent summary"):
    return AgentResult(
        agent_name=agent_name,
        status="success",
        findings=findings or [],
        summary=summary,
        duration_seconds=5.0,
    )


def _make_error_result(agent_name, error_message="endpoint unreachable"):
    return AgentResult(
        agent_name=agent_name,
        status="error",
        findings=[],
        summary="",
        error_message=error_message,
        duration_seconds=10.0,
    )


def _render_report(formatter, alert_context, results, pending_agents=None, renderer=None):
    """Helper: build incident sections then render via Slack mrkdwn."""
    sections = formatter.build_incident_sections(
        alert_context, results, pending_agents,
    )
    return (renderer or SlackReportRenderer()).render_report(sections)


def _render_enrichment(formatter, source_agent, new_findings, initial_summary,
                       variant_label=None, renderer=None):
    """Helper: build enrichment sections then render."""
    sections = formatter.build_enrichment_sections(
        source_agent, new_findings, initial_summary, variant_label,
    )
    return (renderer or SlackReportRenderer()).render_enrichment(sections)


class TestFormatIncidentReport:
    """Tests for build_incident_sections + render_report (Slack dialect)."""

    def test_all_agents_successful(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("Correlated alert in #ops", source="ops-channel")],
                "Found correlated alerts",
            ),
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("OOM errors in service-api", severity="critical", source="log-group")],
                "OOM errors detected",
            ),
            "eks": _make_success_result(
                "eks",
                [_make_finding("Pod restarts: 5 in last 10m", severity="warning", source="pod-api")],
                "Pod instability detected",
            ),
        }

        report = _render_report(formatter, alert_context, results)

        # All required sections present
        assert "🚨 *Incident Report*" in report
        assert "*Severity:*" in report
        assert "*Affected Services:*" in report
        assert "*Time of Detection:*" in report
        assert "*Summary*" in report
        assert "*Root Cause Hypothesis*" in report
        assert "*Evidence*" in report
        assert "*Impact Assessment*" in report
        assert "*Recommended Actions*" in report
        assert "*Links & References*" in report

        # Findings appear in evidence
        assert "Correlated alert in #ops" in report
        assert "OOM errors in service-api" in report
        assert "Pod restarts: 5 in last 10m" in report

        # No failure notices for successful agents
        assert "⚠️ Slack Scanner data unavailable" not in report
        assert "⚠️ CloudWatch Logs data unavailable" not in report
        assert "⚠️ EKS Cluster State data unavailable" not in report

        # Severity should be critical (highest finding)
        assert "🔴 Critical" in report

    def test_some_agents_failed(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("Alert in #ops", source="ops")],
                "Found alerts",
            ),
            "cloudwatch_logs": _make_error_result("cloudwatch_logs", "endpoint unreachable"),
        }
        # eks is still pending — orchestrator dispatched it but it hasn't responded
        report = _render_report(
            formatter, alert_context, results, pending_agents={"eks"},
        )

        # Successful agent findings present
        assert "Alert in #ops" in report

        # Failure notice for the configured-but-failed agent
        assert "⚠️ CloudWatch Logs data unavailable" in report
        # Pending agent rendered as still investigating, not failed
        assert "⏳" in report
        assert "EKS Cluster State" in report

        # No failure notice for successful agent
        assert "⚠️ Slack Scanner data unavailable" not in report

    def test_not_configured_agents_are_omitted(self, formatter, alert_context):
        """Agents the orchestrator didn't dispatch must not appear at all."""
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("Alert in #ops", source="ops")],
                "Found alerts",
            ),
        }
        report = _render_report(formatter, alert_context, results)
        # Only the dispatched agent shows up
        assert "Slack Scanner" in report
        # CloudWatch and EKS were never dispatched → not even mentioned
        assert "CloudWatch Logs" not in report
        assert "EKS Cluster State" not in report

    def test_no_agents_dispatched(self, formatter, alert_context):
        """When no agents are configured, evidence section says so explicitly."""
        report = _render_report(formatter, alert_context, {})
        assert "*Evidence*" in report
        assert "No agents were configured" in report
        # No agent names appear
        assert "Slack Scanner" not in report
        assert "CloudWatch Logs" not in report
        assert "EKS Cluster State" not in report

    def test_time_of_detection_matches_alert(self, formatter, alert_context):
        report = _render_report(formatter, alert_context, {})
        assert f"*Time of Detection:* {alert_context.alert_timestamp}" in report

    def test_severity_defaults_to_low(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("info finding", severity="info")],
            ),
        }
        report = _render_report(formatter, alert_context, results)
        assert "🔵 Low" in report

    def test_severity_escalates_to_highest(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("low finding", severity="low")],
            ),
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("high finding", severity="high")],
            ),
        }
        report = _render_report(formatter, alert_context, results)
        assert "🟠 High" in report

    def test_evidence_grouped_by_agent(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("slack finding 1"), _make_finding("slack finding 2")],
            ),
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("cw finding 1")],
            ),
        }
        report = _render_report(formatter, alert_context, results)

        # Verify agent section headers
        assert "📡 *Slack Scanner*" in report
        assert "📋 *CloudWatch Logs*" in report

        # Verify findings are present
        assert "slack finding 1" in report
        assert "slack finding 2" in report
        assert "cw finding 1" in report

    def test_links_from_metadata(self, formatter, alert_context):
        results = {
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [
                    _make_finding(
                        "OOM errors",
                        source="log-group",
                        metadata={"url": "https://console.aws.amazon.com/cloudwatch/home"},
                    )
                ],
            ),
        }
        report = _render_report(formatter, alert_context, results)
        assert "https://console.aws.amazon.com/cloudwatch/home" in report

    def test_agent_error_with_error_message(self, formatter, alert_context):
        results = {
            "cloudwatch_logs": _make_error_result("cloudwatch_logs", "connection timeout"),
        }
        report = _render_report(formatter, alert_context, results)
        assert "⚠️ CloudWatch Logs data unavailable: connection timeout" in report


class TestFormatEnrichmentUpdate:
    """Tests for build_enrichment_sections + render_enrichment (Slack dialect)."""

    def test_enrichment_update_contains_agent_name(self, formatter):
        findings = _make_success_result(
            "cloudwatch_logs",
            [_make_finding("Late log data")],
            "CloudWatch data now available",
        )
        update = _render_enrichment(
            formatter, "cloudwatch_logs", findings, "Initial summary",
        )
        assert "CloudWatch Logs" in update

    def test_enrichment_update_contains_findings(self, formatter):
        findings = _make_success_result(
            "eks",
            [_make_finding("Pod recovered"), _make_finding("Node healthy")],
            "EKS cluster stabilized",
        )
        update = _render_enrichment(formatter, "eks", findings, "Initial summary")
        assert "Pod recovered" in update
        assert "Node healthy" in update

    def test_enrichment_update_structure(self, formatter):
        findings = _make_success_result(
            "cloudwatch_logs",
            [_make_finding("New log entry")],
            "Logs now available",
        )
        update = _render_enrichment(
            formatter, "cloudwatch_logs", findings, "Initial summary",
        )
        assert "📬 *Enrichment Update" in update
        assert "*New Findings:*" in update
        assert "*Updated Assessment:*" in update


class TestSlackDialectNormalization:
    """SlackDialect rewrites agent-produced CommonMark into Slack mrkdwn."""

    def setup_method(self):
        self.dialect = SlackDialect()

    def test_double_asterisk_bold_becomes_single(self):
        assert self.dialect.normalize("**Investigation ID:** d5b") == (
            "*Investigation ID:* d5b"
        )

    def test_double_underscore_bold_becomes_single_asterisk(self):
        assert self.dialect.normalize("__urgent__ now") == "*urgent* now"

    def test_atx_heading_becomes_bold_line(self):
        text = "## Investigation Summary\n\nbody text"
        assert self.dialect.normalize(text) == "*Investigation Summary*\n\nbody text"

    def test_higher_heading_levels_also_collapse_to_bold(self):
        text = "### Alert Context\n\n- item"
        assert self.dialect.normalize(text) == "*Alert Context*\n\n- item"

    def test_inline_link_uses_slack_pipe_form(self):
        text = "see [the dashboard](https://grafana/x) please"
        assert self.dialect.normalize(text) == (
            "see <https://grafana/x|the dashboard> please"
        )

    def test_collapses_excessive_blank_lines(self):
        assert self.dialect.normalize("a\n\n\n\nb") == "a\n\nb"

    def test_inline_heading_glued_to_sentence_is_promoted(self):
        # LLMs occasionally emit "...end of sentence.## Heading" without a
        # newline; the rewriter forces the break and then bolds the heading.
        text = "Let me start the investigation.## Investigation Summary\n\nbody"
        result = self.dialect.normalize(text)
        assert "## Investigation Summary" not in result
        assert "*Investigation Summary*" in result

    def test_empty_string_unchanged(self):
        assert self.dialect.normalize("") == ""


class TestDiscordDialectNormalization:
    """Discord supports CommonMark natively — normalize is mostly pass-through."""

    def setup_method(self):
        self.dialect = DiscordDialect()

    def test_preserves_double_asterisk_bold(self):
        assert "**bold**" in self.dialect.normalize("**bold** stays")

    def test_preserves_atx_headings(self):
        assert self.dialect.normalize("## Heading\nbody") == "## Heading\nbody"

    def test_preserves_inline_link_syntax(self):
        text = "see [dash](https://x) please"
        assert self.dialect.normalize(text) == text


class TestRendererSlackNormalizationIntegration:
    """End-to-end: agent-produced markdown should render as Slack mrkdwn."""

    def test_agent_summary_markdown_is_translated_in_report(self, formatter, alert_context):
        results = {
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("OOM errors", source="log-group")],
                summary=(
                    "## Investigation Summary\n\n"
                    "**Trigger Time:** 09:12 UTC. See "
                    "[dashboard](https://grafana/x)."
                ),
            ),
        }
        report = _render_report(formatter, alert_context, results)

        # Slack renders ## headings as plain text — the dialect promotes
        # them to bold instead.
        assert "## Investigation Summary" not in report
        assert "*Investigation Summary*" in report
        # Double-asterisk bold collapses to single-asterisk mrkdwn.
        assert "**Trigger Time:**" not in report
        assert "*Trigger Time:*" in report
        # Inline links are rewritten to Slack's <url|label> form.
        assert "[dashboard](https://grafana/x)" not in report
        assert "<https://grafana/x|dashboard>" in report

    def test_discord_renderer_keeps_commonmark(self, formatter, alert_context):
        results: dict[str, AgentResult | AgentFailure] = {
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("OOM errors", source="log-group")],
                summary="## Investigation Summary\n\n**Trigger Time:** 09:12 UTC.",
            ),
        }
        report = _render_report(
            formatter, alert_context, results, renderer=DiscordReportRenderer(),
        )

        # Discord supports both — they pass through untouched.
        assert "## Investigation Summary" in report
        assert "**Trigger Time:**" in report
