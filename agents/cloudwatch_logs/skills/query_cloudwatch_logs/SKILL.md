---
name: query_cloudwatch_logs
description: Execute a CloudWatch Logs Insights query against log groups returned by discover_log_groups within an investigation window. Validates group existence, skips missing ones, returns structured findings.
tool: agents.cloudwatch_logs.tools:query_cloudwatch_logs
---
# When to use

Run **after** `discover_log_groups` — query only the real log group names it returned; never guess names here. Run an initial query, then if the results look suspicious — an error spike, an unexpected pattern, a gap — drill in with a focused follow-up call (a tighter window around the spike, an added filter, or a `stats … by bin()` to quantify it) rather than reporting the first pass as-is. When the first pass is ambiguous, gather don't guess: run a discriminating follow-up — re-query the most recent window or `stats count(*) by bin(...)` — to confirm whether the condition is still firing now versus already resolved, instead of reporting a stale first pass. Stop once you can explain the alert or your budget is spent.

# Inputs

- `log_group_names` (required): list of log groups to query. Non-existent groups are skipped, not failures.
- `query` (required): a CloudWatch Logs Insights query string. Common patterns:
  - `fields @timestamp, @message | filter @message like /ERROR/`
  - `fields @timestamp, @message | sort @timestamp desc | limit 50`
  - `stats count(*) by bin(5m) as period`
- `start_time`, `end_time` (required): ISO-8601 boundaries of the investigation window.

# Output

For each existing log group: matched events. The agent should highlight error patterns, spikes, and correlations across groups.
