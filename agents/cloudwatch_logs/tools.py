"""CloudWatch Logs Agent tools — query CloudWatch Logs Insights.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 9.5
"""

from __future__ import annotations

import logging
import time

import boto3
from botocore.exceptions import ClientError
from strands import tool

from shared.models import Finding
from shared.tool_result import (
    ToolResult,
    build_agent_result,
    format_result,
    severity_from_text,
)

logger = logging.getLogger(__name__)

_QUERY_POLL_TIMEOUT_SECONDS: int = 30
_QUERY_POLL_INTERVAL_SECONDS: float = 1.0
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "Complete",
    "Failed",
    "Cancelled",
    "Timeout",
})


def _get_existing_log_groups(
    client,
    log_group_names: list[str],
) -> tuple[list[str], list[str]]:
    """Validate which log groups exist. Returns (existing, missing)."""
    existing: list[str] = []
    missing: list[str] = []

    for name in log_group_names:
        try:
            response = client.describe_log_groups(logGroupNamePrefix=name)
            found = any(
                lg["logGroupName"] == name
                for lg in response.get("logGroups", [])
            )
            if found:
                existing.append(name)
            else:
                missing.append(name)
        except ClientError as exc:
            logger.warning("Error checking log group %s: %s", name, exc)
            missing.append(name)

    return existing, missing


def _poll_query_results(
    client,
    query_id: str,
    timeout: int = _QUERY_POLL_TIMEOUT_SECONDS,
) -> tuple[str, list[list[dict]]]:
    """Poll get_query_results until terminal status. Returns (status, results)."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        response = client.get_query_results(queryId=query_id)
        status = response.get("status", "Unknown")

        if status in _TERMINAL_STATUSES:
            return status, response.get("results", [])

        time.sleep(_QUERY_POLL_INTERVAL_SECONDS)

    try:
        response = client.get_query_results(queryId=query_id)
        return "Timeout", response.get("results", [])
    except ClientError:
        return "Timeout", []


def _execute_insights_query(
    client,
    log_group_names: list[str],
    query_string: str,
    start_time: int,
    end_time: int,
) -> tuple[str, list[list[dict]]]:
    """Start a Logs Insights query and poll for results."""
    response = client.start_query(
        logGroupNames=log_group_names,
        startTime=start_time,
        endTime=end_time,
        queryString=query_string,
    )
    query_id = response["queryId"]
    return _poll_query_results(client, query_id)


def _results_to_findings(
    results: list[list[dict]],
    log_group_names: list[str],
) -> list[Finding]:
    """Convert Logs Insights result rows into Finding objects."""
    findings: list[Finding] = []
    source_label = ", ".join(log_group_names)

    for row in results:
        row_dict = {item["field"]: item["value"] for item in row}
        timestamp = row_dict.get("@timestamp", "")
        message = row_dict.get("@message", "")
        log_ptr = row_dict.get("@ptr", "")

        findings.append(
            Finding(
                source=source_label,
                timestamp=timestamp,
                content=message,
                severity=severity_from_text(message),
                metadata={
                    "log_groups": log_group_names,
                    "ptr": log_ptr,
                    **{
                        k: v
                        for k, v in row_dict.items()
                        if k not in ("@timestamp", "@message", "@ptr")
                    },
                },
            )
        )

    return findings


@tool
def query_cloudwatch_logs(
    log_group_names: list[str],
    query_string: str,
    start_time: int,
    end_time: int,
) -> str:
    """Execute a CloudWatch Logs Insights query against derived log groups.

    Validates log group existence before querying. Non-existent log groups
    are skipped gracefully with a warning. Returns query results and a log
    analysis summary.

    Args:
        log_group_names: Log group names to query.
        query_string: CloudWatch Logs Insights query string.
        start_time: Start of the query window (epoch seconds).
        end_time: End of the query window (epoch seconds).

    Returns:
        A human-readable summary string for the LLM to consume.
    """
    client = boto3.client("logs")
    result = _execute_query(client, log_group_names, query_string, start_time, end_time)
    return format_result(build_agent_result("cloudwatch_logs", result))


def _execute_query(
    client,
    log_group_names: list[str],
    query_string: str,
    start_time: int,
    end_time: int,
) -> ToolResult:
    """Core query logic — all I/O goes through *client*.

    Args:
        client: A boto3 CloudWatch Logs client.
        log_group_names: Log group names to query.
        query_string: CloudWatch Logs Insights query string.
        start_time: Start of the query window (epoch seconds).
        end_time: End of the query window (epoch seconds).
    """
    result = ToolResult()

    if not log_group_names:
        result.errors.append("No log group names provided.")
        return result

    try:
        existing, missing = _get_existing_log_groups(client, log_group_names)
    except ClientError as exc:
        result.errors.append(f"Failed to validate log groups: {exc}")
        return result

    for name in missing:
        result.findings.append(
            Finding(
                source=name,
                timestamp="",
                content=f"Log group '{name}' does not exist — skipped.",
                severity="warning",
                metadata={"skipped": True, "log_group": name},
            )
        )

    if not existing:
        result.errors.append(
            "None of the requested log groups exist: "
            + ", ".join(log_group_names)
        )
        return result

    result.scanned_items = existing

    try:
        status, rows = _execute_insights_query(
            client, existing, query_string, start_time, end_time,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        result.errors.append(
            f"Logs Insights query failed ({error_code}): {exc}"
        )
        return result

    if status == "Failed":
        result.errors.append("Logs Insights query failed.")
        return result

    if status == "Cancelled":
        result.errors.append("Logs Insights query was cancelled.")
        return result

    if status == "Timeout":
        result.findings.append(
            Finding(
                source=", ".join(existing),
                timestamp="",
                content="Query timed out — partial results may be included.",
                severity="warning",
                metadata={"timeout": True},
            )
        )

    result.findings.extend(_results_to_findings(rows, existing))

    return result
