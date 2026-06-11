"""CloudWatch Logs Agent tools — query CloudWatch Logs Insights.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 9.5
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError
from strands import tool

from shared.deep_links import cloudwatch_logs_insights_url
from shared.models import ChartDescriptor, ChartSeries, Finding, SnapshotReport, SnapshotSection
from shared.tool_result import (
    ToolResult,
    build_agent_result,
    format_result,
    format_snapshot_result,
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
_CHART_MAX_POINTS: int = 1000


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


def _chart_snapshots_enabled() -> bool:
    """Whether to attach chart descriptors + series. Default on; ``"false"`` disables."""
    return os.environ.get("CHART_SNAPSHOTS_ENABLED", "true").strip().lower() != "false"


def _series_kind(query: str) -> str:
    """Best-effort: a ``stats … by bin(…)`` query yields a binned series."""
    lowered = query.lower()
    return "binned" if "stats" in lowered and "bin(" in lowered else "log_rows"


def _rows_to_points(rows: list[list[dict]]) -> list[dict]:
    """Flatten Logs Insights result rows into ``{field: value}`` dicts."""
    return [{item["field"]: item["value"] for item in row} for row in rows]


def _results_to_findings(
    results: list[list[dict]],
    log_group_names: list[str],
    link: str | None = None,
    chart: ChartDescriptor | None = None,
) -> list[Finding]:
    """Convert Logs Insights result rows into Finding objects.

    ``link``, when supplied, is the Logs Insights console deep link for the
    query/window that produced these rows; it is attached to every finding so
    the report can offer a one-click path back to the source data.
    """
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
                link=link,
                chart=chart,
            )
        )

    return findings


def _logs_insights_link(
    client,
    log_group_names: list[str],
    query_string: str,
    start_time: int,
    end_time: int,
) -> str | None:
    """Build the Logs Insights console deep link for a query, fail-open.

    Returns ``None`` when the client's region is unknown or URL construction
    raises — a missing link must never sink the investigation.
    """
    region = getattr(getattr(client, "meta", None), "region_name", None)
    if not isinstance(region, str) or not region:
        return None
    try:
        return cloudwatch_logs_insights_url(
            region, log_group_names, query_string, start_time, end_time,
        )
    except Exception:  # noqa: BLE001 — link is best-effort; never block on it
        logger.warning("Failed to build CloudWatch Logs Insights deep link", exc_info=True)
        return None


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
    deep_link = _logs_insights_link(
        client, existing, query_string, start_time, end_time,
    )

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
                link=deep_link,
            )
        )

    chart: ChartDescriptor | None = None
    if _chart_snapshots_enabled() and status == "Complete" and rows:
        chart = ChartDescriptor.create(
            source="cloudwatch_logs_insights",
            log_groups=existing,
            query=query_string,
            start_epoch=start_time,
            end_epoch=end_time,
        )
        points = _rows_to_points(rows)
        truncated = len(points) > _CHART_MAX_POINTS
        if truncated:
            logger.warning(
                "Chart series %s truncated: %d rows exceed cap %d",
                chart.chart_id, len(points), _CHART_MAX_POINTS,
            )
            points = points[:_CHART_MAX_POINTS]
        result.chart_series[chart.chart_id] = ChartSeries(
            points=points,
            series_kind=_series_kind(query_string),
            truncated=truncated,
        )

    result.findings.extend(
        _results_to_findings(rows, existing, link=deep_link, chart=chart)
    )

    return result


# ---------------------------------------------------------------------------
# /sre-snapshot snapshot — top log groups by ingestion (i-a)
# ---------------------------------------------------------------------------


_SNAPSHOT_LOOKBACK_MINUTES: int = 15
_SNAPSHOT_TOP_N: int = 10
_SNAPSHOT_MAX_GROUPS_SCANNED: int = 500
_SNAPSHOT_METRIC_BATCH_SIZE: int = 500
_SNAPSHOT_INSIGHTS_QUERY = (
    "filter @message like /(?i)error|exception|fail/ "
    "| stats count() by @logGroup"
)


@tool
def capture_snapshot(requested_at: str) -> str:
    """Capture a snapshot of the top 10 CloudWatch Log groups by ingestion.

    Implementation (i-a from the spec):

    1. ``DescribeLogGroups`` (paginated) to enumerate up to 500 groups.
    2. Single ``GetMetricData`` call against ``AWS/Logs/IncomingBytes`` to
       sum bytes per group over the last 15 minutes.
    3. Sort, take the top 10.
    4. Bounded Logs Insights query against just those 10 groups to count
       error/exception/fail lines.

    The tool never raises — any failure is folded into the snapshot.

    Args:
        requested_at: ISO 8601 timestamp from the master, used as the
            ``captured_at`` field of the returned report.

    Returns:
        A short human-readable string ending with a
        ``<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>`` footer.
    """
    try:
        logs_client = boto3.client("logs")
        cw_client = boto3.client("cloudwatch")
    except Exception as exc:
        return format_snapshot_result(
            SnapshotReport(
                agent_name="cloudwatch_logs",
                captured_at=requested_at,
                sections=[
                    SnapshotSection(
                        label="Top log groups by ingestion (last 15 min)",
                        lines=[f"❌ failed to construct AWS clients: {exc}"],
                    )
                ],
                anomaly=True,
                anomaly_summary=f"CloudWatch Logs snapshot failed: {exc}",
            )
        )

    report = _execute_capture_snapshot(
        logs_client, cw_client, requested_at=requested_at,
    )
    return format_snapshot_result(report)


def _execute_capture_snapshot(
    logs_client,
    cw_client,
    *,
    requested_at: str,
    now: datetime | None = None,
    top_n: int = _SNAPSHOT_TOP_N,
    max_groups_scanned: int = _SNAPSHOT_MAX_GROUPS_SCANNED,
) -> SnapshotReport:
    """Pure snapshot builder. All I/O goes through *logs_client* / *cw_client*.

    Tests pass mock boto3 clients to drive every branch: empty account,
    happy path with metric ranking, anomaly when any top-N has errors,
    Insights query failure (soft — top-N still renders), and primary
    probe failures (DescribeLogGroups / GetMetricData).
    """
    section_label = f"Top log groups by ingestion (last {_SNAPSHOT_LOOKBACK_MINUTES} min)"
    now = now or datetime.now(tz=timezone.utc)
    start = now - timedelta(minutes=_SNAPSHOT_LOOKBACK_MINUTES)

    # Step 1: enumerate log groups (paginated).
    try:
        groups = _list_log_groups(logs_client, max_groups=max_groups_scanned)
    except ClientError as exc:
        return _snapshot_anomaly(
            requested_at,
            section_label,
            f"❌ describe_log_groups failed: {_client_error_message(exc)}",
        )
    except Exception as exc:
        return _snapshot_anomaly(
            requested_at,
            section_label,
            f"❌ describe_log_groups failed: {exc}",
        )

    if not groups:
        return SnapshotReport(
            agent_name="cloudwatch_logs",
            captured_at=requested_at,
            sections=[
                SnapshotSection(
                    label=section_label,
                    lines=["(no log groups in this account/region)"],
                )
            ],
            anomaly=False,
        )

    # Step 2: GetMetricData for IncomingBytes per group.
    try:
        volumes = _bytes_per_group(cw_client, groups, start, now)
    except ClientError as exc:
        return _snapshot_anomaly(
            requested_at,
            section_label,
            f"❌ get_metric_data failed: {_client_error_message(exc)}",
        )
    except Exception as exc:
        return _snapshot_anomaly(
            requested_at,
            section_label,
            f"❌ get_metric_data failed: {exc}",
        )

    # Step 3: sort, take top N (drop zero-volume groups).
    ranked = sorted(
        ((name, vol) for name, vol in volumes.items() if vol > 0),
        key=lambda kv: kv[1],
        reverse=True,
    )[:top_n]

    if not ranked:
        return SnapshotReport(
            agent_name="cloudwatch_logs",
            captured_at=requested_at,
            sections=[
                SnapshotSection(
                    label=section_label,
                    lines=["(no log group received any traffic in the window)"],
                )
            ],
            anomaly=False,
        )

    # Step 4: bounded Insights query for error counts on the top N.
    error_counts: dict[str, int] = {}
    error_query_failed = False
    try:
        error_counts = _query_error_counts(
            logs_client,
            [name for name, _ in ranked],
            start,
            now,
        )
    except (ClientError, Exception):
        # Soft failure — render top-N without error counts. Don't flip anomaly
        # just because we couldn't measure errors.
        error_query_failed = True
        logger.warning("CloudWatch snapshot Insights query failed", exc_info=True)

    # Build section lines.
    lines: list[str] = []
    for name, bytes_val in ranked:
        line = f"{name} · {_humanize_bytes(bytes_val)}"
        if name in error_counts:
            line += f" · {error_counts[name]} errors"
        elif error_query_failed:
            line += " · ⚠️ error count unavailable"
        lines.append(line)

    # Anomaly: any of the top N has error_count > 0.
    anomaly_count = sum(1 for c in error_counts.values() if c > 0)
    anomaly = anomaly_count > 0
    anomaly_summary = (
        f"{anomaly_count} log group(s) with errors in last "
        f"{_SNAPSHOT_LOOKBACK_MINUTES} min"
        if anomaly
        else None
    )

    return SnapshotReport(
        agent_name="cloudwatch_logs",
        captured_at=requested_at,
        sections=[SnapshotSection(label=section_label, lines=lines)],
        anomaly=anomaly,
        anomaly_summary=anomaly_summary,
    )


def _list_log_groups(client, *, max_groups: int) -> list[str]:
    """Enumerate log group names via paginated ``describe_log_groups``."""
    names: list[str] = []
    next_token: str | None = None
    while True:
        kwargs = {"limit": 50}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.describe_log_groups(**kwargs)
        for lg in response.get("logGroups", []):
            name = lg.get("logGroupName")
            if name:
                names.append(name)
                if len(names) >= max_groups:
                    return names
        next_token = response.get("nextToken")
        if not next_token:
            break
    return names


def _bytes_per_group(
    cw_client,
    log_group_names: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, float]:
    """Single-window IncomingBytes sum per log group via ``get_metric_data``.

    Chunked into 500-query batches to respect the API's per-call limit.
    Returns ``{name: bytes}``; missing/zero-volume groups simply don't
    appear with a non-zero value.
    """
    period_seconds = int((end - start).total_seconds()) or 60
    totals: dict[str, float] = {}
    for batch_start in range(0, len(log_group_names), _SNAPSHOT_METRIC_BATCH_SIZE):
        batch = log_group_names[batch_start : batch_start + _SNAPSHOT_METRIC_BATCH_SIZE]
        queries = []
        id_to_name: dict[str, str] = {}
        for i, name in enumerate(batch):
            qid = f"q{batch_start + i}"
            id_to_name[qid] = name
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Logs",
                        "MetricName": "IncomingBytes",
                        "Dimensions": [
                            {"Name": "LogGroupName", "Value": name}
                        ],
                    },
                    "Period": period_seconds,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            })
        response = cw_client.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
        )
        for r in response.get("MetricDataResults", []):
            qid = r.get("Id")
            name = id_to_name.get(qid)
            if name is None:
                continue
            values = r.get("Values") or []
            totals[name] = sum(values)
    return totals


def _query_error_counts(
    client,
    log_group_names: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """Run a bounded Insights query for error-line counts on the given groups.

    Uses the ``@logGroup`` aliasing field so we can map results back to
    log group names without parsing ARNs.
    """
    response = client.start_query(
        logGroupNames=log_group_names,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=_SNAPSHOT_INSIGHTS_QUERY,
    )
    query_id = response["queryId"]
    status, rows = _poll_query_results(client, query_id)
    if status != "Complete":
        return {}

    counts: dict[str, int] = {}
    for row in rows:
        row_dict = {item["field"]: item["value"] for item in row}
        log_group = row_dict.get("@logGroup", "")
        count_str = row_dict.get("count()") or row_dict.get("count(*)") or "0"
        try:
            counts[log_group] = int(count_str)
        except (TypeError, ValueError):
            counts[log_group] = 0
    return counts


def _humanize_bytes(n: float) -> str:
    """Format a byte count as `12.3 MB`-style string."""
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def _client_error_message(exc: ClientError) -> str:
    err = getattr(exc, "response", {}).get("Error", {}) if exc.response else {}
    code = err.get("Code") or "Unknown"
    message = err.get("Message") or str(exc)
    return f"{code}: {message}"


def _snapshot_anomaly(
    requested_at: str, section_label: str, line: str
) -> SnapshotReport:
    """Build a single-section anomaly snapshot for primary-probe failures."""
    return SnapshotReport(
        agent_name="cloudwatch_logs",
        captured_at=requested_at,
        sections=[SnapshotSection(label=section_label, lines=[line])],
        anomaly=True,
        anomaly_summary=line.lstrip("❌ ").strip(),
    )
