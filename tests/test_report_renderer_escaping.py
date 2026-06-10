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
    InvestigationStartedSections,
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
                lines=[line],
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
