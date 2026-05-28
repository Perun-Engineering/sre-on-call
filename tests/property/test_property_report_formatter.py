"""Property-based tests for the ReportFormatter class."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from agents.master.report_formatter import ReportFormatter
from shared.agents import get_registry
from shared.models import AgentFailure, AgentResult, AlertContext, Finding
from shared.report_renderer import SlackReportRenderer

# Specialized agent ids in canonical render order, sourced from the registry.
# Replaces the old `AGENT_ORDER` constant that lived in report_formatter.py.
AGENT_ORDER = [a.id for a in get_registry().all(kind="specialized")]

# Map of agent id -> (emoji, display_name) — replaces the old `AGENT_DISPLAY`
# dict; sourced from the registry so the property tests stay aligned with the
# single source of truth.
AGENT_DISPLAY = {
    a.id: (a.emoji, a.display_name)
    for a in get_registry().all(kind="specialized")
}

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Severity choices for findings
severities = st.sampled_from(["critical", "high", "medium", "low", "info", "warning"])

# Non-empty printable text for finding content / summaries
printable_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=80,
)

# A single Finding with random but valid fields
finding_strategy = st.builds(
    Finding,
    source=printable_text,
    timestamp=st.just("2025-01-15T14:32:00Z"),
    content=printable_text,
    severity=severities,
    metadata=st.just({}),
)

# A successful AgentResult with at least one finding
successful_result = st.builds(
    AgentResult,
    agent_name=st.just("placeholder"),  # overridden per-agent
    status=st.just("success"),
    findings=st.lists(finding_strategy, min_size=1, max_size=5),
    summary=printable_text,
    error_message=st.none(),
    duration_seconds=st.floats(min_value=0.1, max_value=60.0),
)

# An error AgentResult
error_result = st.builds(
    AgentResult,
    agent_name=st.just("placeholder"),
    status=st.just("error"),
    findings=st.just([]),
    summary=st.just(""),
    error_message=printable_text,
    duration_seconds=st.floats(min_value=0.1, max_value=60.0),
)

# An AgentFailure
agent_failure = st.builds(
    AgentFailure,
    agent_name=st.just("placeholder"),
    error_message=printable_text,
    timestamp=st.just("2025-01-15T14:33:00Z"),
)

# Each dispatched agent can be: success, error, or AgentFailure. (Absent is no
# longer a meaningful state — agents not in agent_results are treated as not
# dispatched and aren't rendered at all.)
agent_status_strategy = st.sampled_from(["success", "error", "failure"])


def _build_agent_result(agent_name: str, status_kind: str, draw):
    """Draw a concrete result for a given agent and status kind."""
    if status_kind == "success":
        result = draw(successful_result)
        # Override agent_name to match the key
        return AgentResult(
            agent_name=agent_name,
            status=result.status,
            findings=result.findings,
            summary=result.summary,
            error_message=result.error_message,
            duration_seconds=result.duration_seconds,
        )
    elif status_kind == "error":
        result = draw(error_result)
        return AgentResult(
            agent_name=agent_name,
            status=result.status,
            findings=result.findings,
            summary=result.summary,
            error_message=result.error_message,
            duration_seconds=result.duration_seconds,
        )
    elif status_kind == "failure":
        failure = draw(agent_failure)
        return AgentFailure(
            agent_name=agent_name,
            error_message=failure.error_message,
            timestamp=failure.timestamp,
        )
    else:
        # absent — agent timed out, not in results dict
        return None


@st.composite
def agent_result_sets(draw):
    """Generate a dict of agent results with random statuses for each of the 4 agents.

    Returns a tuple of (agent_results dict, status_map dict) so the test can
    know which agents were assigned which status.
    """
    status_map: dict[str, str] = {}
    results: dict[str, AgentResult | AgentFailure] = {}

    for agent_key in AGENT_ORDER:
        status_kind = draw(agent_status_strategy)
        status_map[agent_key] = status_kind
        result = _build_agent_result(agent_key, status_kind, draw)
        if result is not None:
            results[agent_key] = result

    return results, status_map


# Fixed alert context — the property is about agent results, not alert fields
ALERT_CONTEXT = AlertContext(
    investigation_id="inv-prop-test",
    platform="slack",
    channel_id="C12345",
    message_id="1705312320.000100",
    alert_text="Property test alert",
    alert_timestamp="2025-01-15 14:32:00 UTC",
    investigation_window=("2025-01-15 14:27:00 UTC", "2025-01-15 14:37:00 UTC"),
)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(data=agent_result_sets())
def test_failure_notices_for_non_successful_agents(
    data: tuple[dict[str, AgentResult | AgentFailure], dict[str, str]],
) -> None:
    """
    For any set of agent results where each of the 4 agents is randomly either
    successful (with findings), error, AgentFailure, or absent (timed out):

    1. The formatted report SHALL contain a failure notice string
       "⚠️ {Display Name} data unavailable" for every non-successful agent.
    2. The formatted report SHALL NOT contain a failure notice for any agent
       that returned successfully.
    """
    agent_results, status_map = data
    formatter = ReportFormatter()
    report = SlackReportRenderer().render_report(
        formatter.build_incident_sections(ALERT_CONTEXT, agent_results)
    )

    for agent_key in AGENT_ORDER:
        _, display_name = AGENT_DISPLAY[agent_key]
        failure_notice = f"⚠️ {display_name} data unavailable"
        status_kind = status_map[agent_key]

        if status_kind == "success":
            # Successful agents must NOT have a failure notice
            assert failure_notice not in report, (
                f"Successful agent '{agent_key}' should not have a failure notice "
                f"but found '{failure_notice}' in report"
            )
        else:
            # Non-successful agents (error, failure, absent) MUST have a failure notice
            assert failure_notice in report, (
                f"Non-successful agent '{agent_key}' (status={status_kind}) should "
                f"have failure notice '{failure_notice}' in report but it was missing"
            )


REQUIRED_SECTION_HEADERS = [
    "Severity",
    "Affected Services",
    "Time of Detection",
    "Summary",
    "Root Cause Hypothesis",
    "Evidence",
    "Impact Assessment",
    "Recommended Actions",
    "Links & References",
]


@settings(max_examples=100)
@given(data=agent_result_sets())
def test_incident_report_contains_all_required_sections(
    data: tuple[dict[str, AgentResult | AgentFailure], dict[str, str]],
) -> None:
    """
    For any set of agent results (including the empty set), the formatted
    Incident_Report SHALL contain all required section headers: Severity,
    Affected Services, Time of Detection, Summary, Root Cause Hypothesis,
    Evidence, Impact Assessment, Recommended Actions, and Links & References.
    """
    agent_results, _status_map = data
    formatter = ReportFormatter()
    report = SlackReportRenderer().render_report(
        formatter.build_incident_sections(ALERT_CONTEXT, agent_results)
    )

    for header in REQUIRED_SECTION_HEADERS:
        assert header in report, (
            f"Required section header '{header}' is missing from the incident report. "
            f"Report content:\n{report}"
        )


@st.composite
def agent_result_sets_with_successful(draw):
    """Generate agent result sets where at least one agent is successful with findings.

    Returns a tuple of (agent_results dict, status_map dict).
    Guarantees at least one agent has status "success" with non-empty findings.
    """
    status_map: dict[str, str] = {}
    results: dict[str, AgentResult | AgentFailure] = {}

    # Draw statuses for all agents
    for agent_key in AGENT_ORDER:
        status_kind = draw(agent_status_strategy)
        status_map[agent_key] = status_kind
        result = _build_agent_result(agent_key, status_kind, draw)
        if result is not None:
            results[agent_key] = result

    # Ensure at least one agent is successful with non-empty findings
    has_success = any(status_map[k] == "success" for k in AGENT_ORDER)
    if not has_success:
        # Force one random agent to be successful
        forced_key = draw(st.sampled_from(AGENT_ORDER))
        status_map[forced_key] = "success"
        result = _build_agent_result(forced_key, "success", draw)
        if result is not None:
            results[forced_key] = result

    return results, status_map


@settings(max_examples=100)
@given(data=agent_result_sets_with_successful())
def test_agent_findings_appear_in_evidence_section(
    data: tuple[dict[str, AgentResult | AgentFailure], dict[str, str]],
) -> None:
    """
    For any agent result with status "success" and a non-empty list of findings,
    each finding's content SHALL appear within the Evidence section of the
    formatted Incident_Report, grouped under the corresponding data source heading.
    """
    agent_results, status_map = data
    formatter = ReportFormatter()
    renderer = SlackReportRenderer()
    report = renderer.render_report(
        formatter.build_incident_sections(ALERT_CONTEXT, agent_results)
    )

    # Extract the Evidence section from the report
    evidence_start = report.find("*Evidence*")
    assert evidence_start != -1, "Evidence section not found in report"

    # Evidence section ends at the next top-level section header
    # (Impact Assessment follows Evidence in the report structure)
    evidence_end = report.find("*Impact Assessment*", evidence_start)
    if evidence_end == -1:
        evidence_section = report[evidence_start:]
    else:
        evidence_section = report[evidence_start:evidence_end]

    for agent_key in AGENT_ORDER:
        if status_map[agent_key] != "success":
            continue

        result = agent_results.get(agent_key)
        if not isinstance(result, AgentResult) or not result.findings:
            continue

        emoji, display_name = AGENT_DISPLAY[agent_key]

        # Find this agent's subsection within the Evidence section
        agent_heading = f"{emoji} *{display_name}*"
        heading_pos = evidence_section.find(agent_heading)
        assert heading_pos != -1, (
            f"Agent heading '{agent_heading}' not found in Evidence section "
            f"for successful agent '{agent_key}'"
        )

        # Determine the subsection for this agent: from heading to next agent heading
        agent_subsection_start = heading_pos + len(agent_heading)
        # Find the next agent heading (if any) to bound the subsection
        next_heading_pos = len(evidence_section)
        for other_key in AGENT_ORDER:
            if other_key == agent_key:
                continue
            other_emoji, other_display = AGENT_DISPLAY[other_key]
            other_heading = f"{other_emoji} *{other_display}*"
            pos = evidence_section.find(other_heading, agent_subsection_start)
            if pos != -1 and pos < next_heading_pos:
                next_heading_pos = pos

        agent_subsection = evidence_section[agent_subsection_start:next_heading_pos]

        # Verify each finding's content appears in this agent's subsection.
        # The renderer normalizes CommonMark to Slack mrkdwn (e.g. __x__ → *x*),
        # so we search for the *rendered* form of each finding's content rather
        # than the raw value.
        for finding in result.findings:
            rendered = renderer.normalize(finding.content)
            assert rendered in agent_subsection, (
                f"Finding content '{rendered}' (normalized from "
                f"{finding.content!r}) from agent '{agent_key}' "
                f"not found under its Evidence subsection.\n"
                f"Agent subsection:\n{agent_subsection}"
            )


# Strategy: agent names drawn from the 3 known agents (Prometheus deferred)
agent_name_strategy = st.sampled_from(["slack_scanner", "cloudwatch_logs", "eks"])


@st.composite
def enrichment_agent_result(draw):
    """Generate a random AgentResult with non-empty findings for enrichment update testing."""
    agent_name = draw(agent_name_strategy)
    findings = draw(st.lists(finding_strategy, min_size=1, max_size=5))
    summary = draw(printable_text)
    return agent_name, AgentResult(
        agent_name=agent_name,
        status="success",
        findings=findings,
        summary=summary,
        error_message=None,
        duration_seconds=draw(st.floats(min_value=0.1, max_value=60.0)),
    )


@settings(max_examples=100)
@given(data=enrichment_agent_result(), summary_text=printable_text)
def test_enrichment_update_identifies_source_agent_and_contains_findings(
    data: tuple[str, AgentResult],
    summary_text: str,
) -> None:
    """
    For any agent name from the 4 known agents and a non-empty list of findings,
    the formatted enrichment update SHALL contain the agent's display name in the
    header and SHALL contain each finding's platform-rendered content in the body.

    Tag: Feature: sre-on-call, Property 10: Enrichment update identifies source agent and contains findings
    """
    agent_name, agent_result = data
    renderer = SlackReportRenderer()
    formatter = ReportFormatter()
    update = renderer.render_enrichment(
        formatter.build_enrichment_sections(agent_name, agent_result, summary_text)
    )

    # The agent's display name must appear in the header
    _, display_name = AGENT_DISPLAY[agent_name]
    # The header line is: "📬 *Enrichment Update — {display_name}*"
    assert display_name in update, (
        f"Display name '{display_name}' for agent '{agent_name}' not found in "
        f"enrichment update header.\nUpdate:\n{update}"
    )

    # Each finding's content must appear in the body
    for finding in agent_result.findings:
        rendered_content = renderer.normalize(finding.content)
        assert rendered_content in update, (
            f"Finding content '{rendered_content}' not found in enrichment update body "
            f"for agent '{agent_name}'.\nUpdate:\n{update}"
        )
