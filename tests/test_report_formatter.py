"""Unit tests for the ReportFormatter (section builders) + the Slack/Discord
markup dialects that render those sections.

After step 4 of the deepening migration, ``ReportFormatter`` only builds
platform-agnostic ``ReportSections``; rendering lives in
:class:`shared.platforms.ChatPlatform` implementations. These tests
combine the builder with a renderer locally to exercise the full
build-then-render pipeline.
"""

import pytest

from agents.master.report_formatter import ReportFormatter
from shared.agents import get_registry
from shared.models import AgentResult, AgentFailure, AlertContext, Finding
from shared.report_renderer import (
    DiscordDialect,
    DiscordReportRenderer,
    SlackDialect,
    SlackReportRenderer,
)


# Display lookup sourced from the registry — replaces the old `AGENT_DISPLAY`
# constant that lived in report_formatter.py.
AGENT_DISPLAY = {
    a.id: (a.emoji, a.display_name)
    for a in get_registry().all(kind="specialized")
}


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


def _make_finding(
    content="test finding", severity="info", source="test-source", metadata=None, link=None
):
    return Finding(
        source=source,
        timestamp="2025-01-15T14:32:00Z",
        content=content,
        severity=severity,
        metadata=metadata or {},
        link=link,
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
    facts = formatter.derive_facts(alert_context, results, pending=pending_agents)
    sections = formatter.build_incident_sections(
        facts, variant_label=alert_context.variant_label,
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

    def test_finding_link_renders_inline_slack(self, formatter, alert_context):
        """A finding's deep link renders inline on its evidence line (Slack)."""
        url = (
            "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
            "?region=us-east-1#logsV2:logs-insights$3FqueryDetail$3D~(end~'x))"
        )
        results = {
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("OOMKilled", severity="critical", link=url)],
            ),
        }
        report = _render_report(formatter, alert_context, results)
        # The content and the Slack-form link share one bullet line.
        assert f"- OOMKilled <{url}|🔗 view>" in report

    def test_finding_link_renders_inline_discord(self, formatter, alert_context):
        url = (
            "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
            "?region=us-east-1#logsV2:logs-insights$3FqueryDetail$3D~(end~'x))"
        )
        results = {
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("OOMKilled", severity="critical", link=url)],
            ),
        }
        report = _render_report(
            formatter, alert_context, results, renderer=DiscordReportRenderer(),
        )
        # The CloudWatch URL carries literal `)`; Discord's masked-link parser
        # would truncate `[label](url)` at the first `)`. The `<...>` wrapper
        # bounds the URL so the full deep link survives (#40).
        assert f"- OOMKilled [🔗 view](<{url}>)" in report

    def test_finding_without_link_renders_no_link_markup(self, formatter, alert_context):
        results = {
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs",
                [_make_finding("plain finding", source="lg")],
            ),
        }
        report = _render_report(formatter, alert_context, results)
        assert "- plain finding" in report
        assert "🔗 view" not in report

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


class TestRootCauseFallbackRendering:
    """Rec #1 — when synthesis is OFF, the Root Cause section is an honest
    symptoms / ruled-out / next-checks breakdown, not a fake hypothesis."""

    def test_chat_report_renders_honest_fallback(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("Alert in #ops", severity="high", source="ops")],
                "Found correlated alerts",
            ),
            "cloudwatch_logs": _make_success_result(
                "cloudwatch_logs", [], "no errors in window",
            ),
            "eks": _make_error_result("eks", "endpoint unreachable"),
        }
        report = _render_report(formatter, alert_context, results)

        assert "No single root cause established" in report
        assert "Symptoms observed" in report
        assert "Ruled out" in report
        assert "Next checks" in report
        # The clean agent is scope-limited, never asserted "healthy".
        assert "no notable findings in its queried scope" in report
        assert "healthy" not in report.lower()
        # The old fake-hypothesis dressing is gone.
        assert "Based on available evidence" not in report

    def test_discord_report_renders_honest_fallback(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("Alert in #ops", severity="high", source="ops")],
                "Found alerts",
            ),
        }
        report = _render_report(
            formatter, alert_context, results, renderer=DiscordReportRenderer(),
        )
        assert "No single root cause established" in report
        assert "Symptoms observed" in report


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



class TestDisabledInConfigEvidence:
    """Disabled-in-config agents render as 🚫 evidence blocks in the Incident Report."""

    def test_disabled_agent_renders_as_disabled_block(self, formatter, alert_context):
        # Discord scanner is disabled in the deployment; orchestrator passes
        # its id in `disabled_agents` (not in `agent_results`).
        sections = formatter.build_incident_sections(
            formatter.derive_facts(alert_context, {}, disabled={"discord_scanner"})
        )
        report = SlackReportRenderer().render_report(sections)

        assert "🎮 *Discord Scanner* 🚫" in report
        assert "is disabled in this deployment" in report
        # The disabled agent should NOT be presented as a failure.
        assert "⚠️ Discord Scanner data unavailable" not in report

    def test_disabled_agent_does_not_appear_in_started_notice(self, formatter, alert_context):
        # The Started notice lists what we're actively dispatching to.
        # Disabled agents only appear in the Incident Report's Evidence section.
        sections = formatter.build_started_sections(
            alert_context,
            dispatched_agents=["slack_scanner", "cloudwatch_logs", "eks"],
        )
        rendered = SlackReportRenderer().render_investigation_started(sections)

        assert "Slack Scanner" in rendered
        assert "CloudWatch Logs" in rendered
        assert "EKS Cluster State" in rendered
        # discord_scanner is not in the dispatched list — must not appear.
        assert "Discord Scanner" not in rendered

    def test_disabled_alongside_active_agents(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("Alert in #ops", source="ops")],
                "Found alerts",
            ),
        }
        sections = formatter.build_incident_sections(
            formatter.derive_facts(
                alert_context, results, disabled={"discord_scanner", "eks"},
            )
        )
        report = SlackReportRenderer().render_report(sections)

        # Active agent renders normally.
        assert "📡 *Slack Scanner*" in report
        assert "Alert in #ops" in report
        # Disabled agents render with 🚫.
        assert "🎮 *Discord Scanner* 🚫" in report
        assert "☸️ *EKS Cluster State* 🚫" in report


class TestRouterSkippedEvidence:
    """Router-skipped agents render as a distinct ➖ 'not investigated' block —
    never as a failure (issue #28)."""

    def test_skipped_agent_renders_as_distinct_block(self, formatter, alert_context):
        sections = formatter.build_incident_sections(
            formatter.derive_facts(
                alert_context, {},
                skipped={"discord_scanner": "no chat signal for a CPU alert"},
            )
        )
        report = SlackReportRenderer().render_report(sections)

        assert "🎮 *Discord Scanner* ➖" in report
        assert "not investigated" in report
        assert "no chat signal for a CPU alert" in report
        # A skip is NOT a failure.
        assert "⚠️ Discord Scanner data unavailable" not in report

    def test_skipped_renders_in_discord_dialect(self, formatter, alert_context):
        sections = formatter.build_incident_sections(
            formatter.derive_facts(
                alert_context, {}, skipped={"discord_scanner": "out of scope"},
            )
        )
        report = DiscordReportRenderer().render_report(sections)
        assert "➖" in report
        assert "not investigated" in report

    def test_skipped_alongside_active_and_disabled(self, formatter, alert_context):
        results = {
            "slack_scanner": _make_success_result(
                "slack_scanner",
                [_make_finding("Alert in #ops", source="ops")],
                "Found alerts",
            ),
        }
        sections = formatter.build_incident_sections(
            formatter.derive_facts(
                alert_context, results,
                disabled={"discord_scanner"},
                skipped={"eks": "alert is not k8s-related"},
            )
        )
        report = SlackReportRenderer().render_report(sections)

        assert "📡 *Slack Scanner*" in report
        assert "🎮 *Discord Scanner* 🚫" in report
        assert "☸️ *EKS Cluster State* ➖" in report

    def test_skipped_agent_does_not_add_manual_check_action(self, formatter, alert_context):
        # A skipped agent must not generate a "Manually check X" recommended
        # action — that's reserved for failures.
        sections = formatter.build_incident_sections(
            formatter.derive_facts(
                alert_context, {}, skipped={"eks": "out of scope"},
            )
        )
        assert "Manually check" not in sections.recommended_actions


class TestUnhealthyAgentEvidence:
    """Agents reporting status='unhealthy' render as 🚫 with an 'investigate
    agent configuration' nudge — distinct from status='error' which is a
    transient failure of one request."""

    def test_unhealthy_status_renders_as_disabled_block_with_reason(
        self, formatter, alert_context,
    ):
        unhealthy = AgentResult(
            agent_name="eks",
            status="unhealthy",
            findings=[],
            summary="",
            error_message="EKS cluster API unreachable from agent VPC",
        )
        sections = formatter.build_incident_sections(
            formatter.derive_facts(alert_context, {"eks": unhealthy})
        )
        report = SlackReportRenderer().render_report(sections)

        assert "☸️ *EKS Cluster State* 🚫" in report
        assert "reported unhealthy: EKS cluster API unreachable" in report
        assert "investigate agent configuration" in report
        # Unhealthy is NOT a "data unavailable" — those are transient errors.
        assert "⚠️ EKS Cluster State data unavailable" not in report

    def test_unhealthy_status_drives_recommended_action(
        self, formatter, alert_context,
    ):
        unhealthy = AgentResult(
            agent_name="eks",
            status="unhealthy",
            findings=[],
            summary="",
            error_message="missing IAM credentials",
        )
        sections = formatter.build_incident_sections(
            formatter.derive_facts(alert_context, {"eks": unhealthy})
        )
        report = SlackReportRenderer().render_report(sections)

        # Distinct from "Manually check X — automated data collection failed"
        # (which is the error path).
        assert "Investigate EKS Cluster State configuration" in report
        assert "agent reported unhealthy" in report

    def test_error_path_still_uses_warning_marker(
        self, formatter, alert_context,
    ):
        # Sanity: the existing 'error' path is unchanged — still ⚠️, not 🚫.
        error = _make_error_result("cloudwatch_logs", "Connection timeout")
        sections = formatter.build_incident_sections(
            formatter.derive_facts(alert_context, {"cloudwatch_logs": error})
        )
        report = SlackReportRenderer().render_report(sections)

        assert "⚠️ CloudWatch Logs data unavailable: Connection timeout" in report
        assert "🚫" not in report


# ---------------------------------------------------------------------------
# build_page_model tests (issue #33)
# ---------------------------------------------------------------------------

from shared.models import (  # noqa: E402
    AgentMetadata,
    AgentResult,
    ChartDescriptor,
    ChartSeries,
    Finding,
)
from shared.page_model import PageModel
from shared.report_renderer import AnalysisSection


def _chart_finding() -> Finding:
    desc = ChartDescriptor.create(
        source="cloudwatch_logs_insights", log_groups=["/aws/x"],
        query="fields @timestamp", start_epoch=1, end_epoch=2,
    )
    return Finding(
        source="cloudwatch_logs", timestamp="2026-06-12T00:00:00Z",
        content="error spike", severity="critical",
        link="https://console", chart=desc,
    )


class TestBuildPageModel:
    def test_threads_chart_id_when_series_present(self, formatter, alert_context):
        finding = _chart_finding()
        assert finding.chart is not None
        cid = finding.chart.chart_id
        results = {
            "cloudwatch_logs": AgentResult(
                agent_name="cloudwatch_logs", status="success",
                findings=[finding], summary="logs summarised",
                chart_series={cid: ChartSeries(points=[{"t": 1}])},
                metadata=AgentMetadata(),
            )
        }
        model = formatter.build_page_model(
            formatter.derive_facts(alert_context, results), analysis=None
        )
        assert isinstance(model, PageModel)
        assert model.chart_ids == [cid]
        block = next(b for b in model.evidence if b.display_name)
        assert block.chart_id == cid
        assert block.lines[0].text == "error spike"
        assert block.lines[0].link == "https://console"
        assert model.severity.lower().endswith("critical")

    def test_omits_chart_id_without_series(self, formatter, alert_context):
        finding = _chart_finding()
        results = {
            "cloudwatch_logs": AgentResult(
                agent_name="cloudwatch_logs", status="success",
                findings=[finding], summary="s", chart_series={},
                metadata=AgentMetadata(),
            )
        }
        model = formatter.build_page_model(
            formatter.derive_facts(alert_context, results), analysis=None
        )
        assert model.chart_ids == []
        assert all(b.chart_id is None for b in model.evidence)

    def test_passes_analysis_dict(self, formatter, alert_context):
        analysis = AnalysisSection(
            root_cause_hypothesis="rc", correlation="co",
            confidence="high", suggested_next_action="na",
        )
        model = formatter.build_page_model(
            formatter.derive_facts(alert_context, {}), analysis=analysis
        )
        assert model.analysis == {
            "root_cause_hypothesis": "rc", "correlation": "co",
            "confidence": "high", "suggested_next_action": "na",
            "causal_chain": [], "competing_hypotheses": [], "ruled_out": [],
        }

    def test_passes_causal_chain_fields_in_analysis_dict(self, formatter, alert_context):
        analysis = AnalysisSection(
            root_cause_hypothesis="rc", correlation="co",
            confidence="high", suggested_next_action="na",
            causal_chain=["a", "b"],
            competing_hypotheses=["alt"],
            ruled_out=["disconfirmed"],
        )
        model = formatter.build_page_model(
            formatter.derive_facts(alert_context, {}), analysis=analysis
        )
        assert model.analysis is not None
        assert model.analysis["causal_chain"] == ["a", "b"]
        assert model.analysis["competing_hypotheses"] == ["alt"]
        assert model.analysis["ruled_out"] == ["disconfirmed"]


class TestBuildPirSectionsAnalysis:
    """Rec #5 — the PIR carries the #27 root cause when the manifest has it."""

    def test_pir_uses_analysis_root_cause_when_present(self, formatter, alert_context):
        analysis = {
            "root_cause_hypothesis": "Payment pods OOMKilled under load",
            "correlation": "5xx spike aligns with exit-137 restarts",
            "confidence": "high",
            "suggested_next_action": "Raise the memory limit",
            "causal_chain": ["traffic surge", "OOMKilled", "5xx spike"],
            "competing_hypotheses": [],
            "ruled_out": ["network partition (no SG changes)"],
        }
        facts = formatter.derive_facts(alert_context, {})
        sections = formatter.build_pir_sections(facts, analysis=analysis)
        # The synthesized hypothesis drives the PIR root cause, not the
        # deterministic "No single root cause established" fallback.
        assert "Payment pods OOMKilled under load" in sections.root_cause
        assert "No single root cause established" not in sections.root_cause
        # #3 extensions surface when present.
        assert "traffic surge" in sections.root_cause
        assert "network partition" in sections.root_cause

    def test_pir_degrades_to_fallback_without_analysis(self, formatter, alert_context):
        facts = formatter.derive_facts(alert_context, {})
        sections = formatter.build_pir_sections(facts)
        # No analysis → the honest deterministic fallback (Rec #1) still renders.
        assert "No single root cause established" in sections.root_cause

    def test_pir_degrades_when_analysis_is_none(self, formatter, alert_context):
        facts = formatter.derive_facts(alert_context, {})
        sections = formatter.build_pir_sections(facts, analysis=None)
        assert "No single root cause established" in sections.root_cause


def test_build_incident_sections_carries_interactive_page_url(formatter, alert_context):
    sections = formatter.build_incident_sections(
        formatter.derive_facts(alert_context, {}),
        interactive_page_url="https://d/pages/inv.html?x=1",
    )
    assert sections.interactive_page_url == "https://d/pages/inv.html?x=1"


# ---------------------------------------------------------------------------
# resolve_page_model tests (issue #55)
# ---------------------------------------------------------------------------


class TestResolvePageModel:
    def _base_page(self) -> dict:
        return {
            "schema_version": 1,
            "investigation_id": "inv-1",
            "generated_at": "2026-06-12T14:00:00Z",
            "status": "completed",
            "analysis": {"root_cause_hypothesis": "rc"},
            "evidence": [{"emoji": "🟢", "display_name": "EKS",
                          "status": "ok", "lines": [], "chart_id": None}],
            "chart_ids": ["abc"],
            "timeline": [{"timestamp": "2026-06-12T14:00:00Z", "source": "alert",
                          "kind": "alert", "label": "High CPU",
                          "severity": None, "chart_id": None}],
        }

    def test_flips_status_to_resolved(self, formatter):
        out = formatter.resolve_page_model(
            self._base_page(), resolved_at="2026-06-12T15:00:00Z",
        )
        assert out["status"] == "resolved"

    def test_appends_resolution_event(self, formatter):
        out = formatter.resolve_page_model(
            self._base_page(), resolved_at="2026-06-12T15:00:00Z",
            narrative="/postmortem db failover completed",
        )
        assert len(out["timeline"]) == 2
        event = out["timeline"][-1]
        assert event["kind"] == "resolution"
        assert event["timestamp"] == "2026-06-12T15:00:00Z"
        assert event["label"] == "db failover completed"

    def test_bare_command_uses_default_label(self, formatter):
        out = formatter.resolve_page_model(
            self._base_page(), resolved_at="t", narrative="/postmortem",
        )
        assert out["timeline"][-1]["label"] == "Incident resolved"

    def test_empty_narrative_uses_default_label(self, formatter):
        out = formatter.resolve_page_model(self._base_page(), resolved_at="t")
        assert out["timeline"][-1]["label"] == "Incident resolved"

    def test_preserves_analysis_evidence_and_charts(self, formatter):
        page = self._base_page()
        out = formatter.resolve_page_model(page, resolved_at="t")
        assert out["analysis"] == {"root_cause_hypothesis": "rc"}
        assert out["evidence"] == page["evidence"]
        assert out["chart_ids"] == ["abc"]

    def test_does_not_mutate_input(self, formatter):
        page = self._base_page()
        formatter.resolve_page_model(page, resolved_at="t")
        assert page["status"] == "completed"
        assert len(page["timeline"]) == 1

    def test_tolerates_missing_timeline(self, formatter):
        page = self._base_page()
        del page["timeline"]
        out = formatter.resolve_page_model(page, resolved_at="t")
        assert out["timeline"][-1]["kind"] == "resolution"
