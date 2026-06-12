"""Untrusted-content escaping in the chat-report renderer (issue #15).

Ingested content (Slack/Discord messages, log lines, k8s events) is
interpolated into incident reports. The renderer must neutralise
platform control sequences so that a planted ``<!channel>`` ping, a fake
``<url|label>`` link, or a ``@everyone`` mass-mention renders as inert
literal text rather than an active control sequence aimed at responders.
"""

from __future__ import annotations

from shared.report_renderer import (
    DiscordDialect,
    EvidenceBlock,
    EvidenceLine,
    FailureNoticeSections,
    InvestigationStartedSections,
    MarkupReportRenderer,
    ReportSections,
    SlackDialect,
    SlackReportRenderer,
    DiscordReportRenderer,
)


# ---------------------------------------------------------------------------
# Slack dialect — escape & < >
# ---------------------------------------------------------------------------


class TestSlackEscapeUntrusted:
    def test_escapes_control_chars(self):
        d = SlackDialect()
        assert d.escape_untrusted("<!channel> & <https://x|y>") == (
            "&lt;!channel&gt; &amp; &lt;https://x|y&gt;"
        )

    def test_ampersand_escaped_first_no_double_escaping(self):
        d = SlackDialect()
        # If `<` were escaped before `&`, the resulting `&lt;` would get its
        # `&` re-escaped to `&amp;lt;`. Order must be & then < then >.
        assert d.escape_untrusted("<") == "&lt;"

    def test_normalize_escapes_before_translating_markup(self):
        d = SlackDialect()
        # A planted mention survives as inert text; our own *bold* promotion
        # (from markdown heading/emphasis) still works afterwards.
        out = d.normalize("ping <!channel> now")
        assert "<!channel>" not in out
        assert "&lt;!channel&gt;" in out


# ---------------------------------------------------------------------------
# Slack report rendering — untrusted finding content
# ---------------------------------------------------------------------------


def _report_with_evidence_line(line: str) -> ReportSections:
    return ReportSections(
        severity="🔴 Critical",
        affected_services="api",
        time_of_detection="2026-06-10T00:00:00Z",
        summary="A summary.",
        root_cause="A cause.",
        evidence_blocks=[
            EvidenceBlock(
                emoji="💬",
                display_name="Slack Scanner",
                lines=[EvidenceLine(line)],
            )
        ],
        impact_assessment="Some impact.",
        recommended_actions="- Do the thing",
        links=[],
    )


def test_slack_report_renders_injected_mention_inert():
    malicious = "DB down <!channel> see <https://evil.example|click here>"
    out = SlackReportRenderer().render_report(_report_with_evidence_line(malicious))
    assert "<!channel>" not in out
    assert "<https://evil.example|click here>" not in out
    assert "&lt;!channel&gt;" in out


def test_slack_investigation_started_escapes_alert_text():
    sections = InvestigationStartedSections(
        alert_text="outage <!here> <@U999>",
        investigation_id="inv-1",
        dispatched=[("💬", "Slack Scanner")],
    )
    out = SlackReportRenderer().render_investigation_started(sections)
    assert "<!here>" not in out
    assert "<@U999>" not in out


def test_failure_notice_surfaces_investigation_id_via_render_dispatch():
    sections = FailureNoticeSections(
        investigation_id="inv-22",
        detail="The investigation stopped unexpectedly before posting its report.",
    )
    # Goes through the render() dispatcher, exercising the new union branch.
    out = SlackReportRenderer().render(sections)
    assert "Investigation Failed" in out
    assert "inv-22" in out


def test_failure_notice_detail_renders_inert_on_both_platforms():
    sections = FailureNoticeSections(
        investigation_id="inv-1",
        detail="boom <!channel> @everyone",
    )
    slack_out = SlackReportRenderer().render_failure_notice(sections)
    discord_out = DiscordReportRenderer().render_failure_notice(sections)
    assert "<!channel>" not in slack_out
    assert "@everyone" not in discord_out


# ---------------------------------------------------------------------------
# Discord dialect — neutralise mass-ping tokens
# ---------------------------------------------------------------------------


class TestDiscordEscapeUntrusted:
    def test_breaks_mass_mentions(self):
        d = DiscordDialect()
        out = d.escape_untrusted("@everyone @here please look")
        assert "@everyone" not in out
        assert "@here" not in out

    def test_discord_report_neutralises_everyone(self):
        out = DiscordReportRenderer().render_report(
            _report_with_evidence_line("alert @everyone")
        )
        assert "@everyone" not in out


# ---------------------------------------------------------------------------
# Discord masked-link truncation on paren-bearing URLs (#40)
#
# CloudWatch Logs Insights console URLs carry literal `(`, `)`, `~`, `'`, `*`
# in the `#logsV2:...~(...)` hash fragment — they can't be percent-encoded
# (AWS parses location.hash client-side). Discord's `[label](url)` parser
# stops the URL at the first literal `)`, truncating the deep link. Wrapping
# the URL in `<...>` bounds it explicitly so `)` no longer terminates it
# (and suppresses the embed unfurl as a bonus).
# ---------------------------------------------------------------------------

_CW_DEEPLINK = (
    "https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
    "#logsV2:logs-insights$3FqueryDetail$3D~(end~0~start~-3600~source~(~'/aws/eks/cluster))"
)


class TestDiscordParenLinks:
    def test_paren_url_wrapped_in_angle_brackets(self):
        d = DiscordDialect()
        out = d.format_link(_CW_DEEPLINK, "View logs")
        # Full URL survives, bounded by <...> so the literal `)` can't truncate.
        assert out == f"[View logs](<{_CW_DEEPLINK}>)"
        assert _CW_DEEPLINK in out

    def test_plain_url_unchanged(self):
        d = DiscordDialect()
        out = d.format_link("https://d/pages/inv.html?Signature=x", "📊 Interactive report")
        # No parens → keep the bare masked-link form (embed allowed).
        assert out == "[📊 Interactive report](https://d/pages/inv.html?Signature=x)"

    def test_slack_paren_url_unchanged(self):
        # Slack's <url|label> carries paren-bearing URLs faithfully — no change.
        d = SlackDialect()
        out = d.format_link(_CW_DEEPLINK, "View logs")
        assert out == f"<{_CW_DEEPLINK}|View logs>"


# ---------------------------------------------------------------------------
# Interactive page link (#33)
# ---------------------------------------------------------------------------


def _minimal_sections(
    *,
    severity: str = "🔴 Critical",
    affected_services: str = "rds",
    time_of_detection: str = "t",
    summary: str = "s",
    root_cause: str = "rc",
    evidence_blocks: list = [],
    impact_assessment: str = "i",
    recommended_actions: str = "a",
    links: list = [],
    **extra,
) -> ReportSections:
    return ReportSections(
        severity=severity,
        affected_services=affected_services,
        time_of_detection=time_of_detection,
        summary=summary,
        root_cause=root_cause,
        evidence_blocks=evidence_blocks,
        impact_assessment=impact_assessment,
        recommended_actions=recommended_actions,
        links=links,
        **extra,
    )


def test_slack_renders_interactive_page_link_when_set():
    out = MarkupReportRenderer(SlackDialect()).render_report(
        _minimal_sections(interactive_page_url="https://d/pages/inv.html?Signature=x")
    )
    assert "<https://d/pages/inv.html?Signature=x|📊 Interactive report>" in out


def test_discord_renders_interactive_page_link_when_set():
    out = MarkupReportRenderer(DiscordDialect()).render_report(
        _minimal_sections(interactive_page_url="https://d/pages/inv.html?Signature=x")
    )
    assert "[📊 Interactive report](https://d/pages/inv.html?Signature=x)" in out


def test_no_interactive_link_when_unset():
    out = MarkupReportRenderer(SlackDialect()).render_report(_minimal_sections())
    assert "Interactive report" not in out
