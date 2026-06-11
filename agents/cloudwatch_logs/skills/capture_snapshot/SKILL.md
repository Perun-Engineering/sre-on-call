---
name: capture_snapshot
description: Capture a snapshot of the top 10 CloudWatch Log groups by ingestion volume in the last 15 minutes, plus an error-line count for each via a bounded Logs Insights query.
tool: agents.cloudwatch_logs.tools:capture_snapshot
---
# When to use

Call this skill when the user message is a JSON object with `task: "snapshot"` — the master agent dispatches `/sre-snapshot` requests this way. Pass `requested_at` from the master verbatim.

# Inputs

- `requested_at` (required): ISO 8601 timestamp from the master, used as the `captured_at` field of the returned `SnapshotReport`.

# Output

A short human-readable summary plus an embedded `SnapshotReport` footer. One section:

- **Top 10 log groups by ingestion (last 15 min)** — `<group_name> · <bytes_humanized> · <n> errors`. Bytes come from `AWS/Logs/IncomingBytes` via a single `GetMetricData` call (paginated only when the account has more than 500 log groups). Error counts come from one bounded Logs Insights query against just those top 10 groups (`filter @message like /(?i)error|exception|fail/ | stats count() by @logGroup`).

# Anomaly criteria

`anomaly = True` when any of the top 10 groups has `error_count > 0`. The error analysis is best-effort — if the bounded Insights query fails, the section still renders the top-10 ranking by bytes alone, with no anomaly flagged.

The tool never raises — failure to enumerate log groups, fetch metrics, or run the Insights query is folded into a section line and (when it concerns the primary probe) flips the report to anomaly.
