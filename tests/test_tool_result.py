"""Tests for shared ToolResult/AgentResult formatting helpers."""

from shared.models import AgentResult, Finding
from shared.tool_result import extract_agent_result, format_result


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
