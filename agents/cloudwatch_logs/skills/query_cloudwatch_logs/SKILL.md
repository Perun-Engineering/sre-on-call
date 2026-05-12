---
name: query_cloudwatch_logs
description: Execute a CloudWatch Logs Insights query against derived log groups within an investigation window. Validates group existence, skips missing ones, returns structured findings.
tool: agents.cloudwatch_logs.tools:query_cloudwatch_logs
---
# When to use

Call this skill once per investigation. Derive log group names from the `AlertContext` (service, application identifier, environment prefix) before calling.

# Inputs

- `log_group_names` (required): list of log groups to query. Non-existent groups are skipped, not failures.
- `query` (required): a CloudWatch Logs Insights query string. Common patterns:
  - `fields @timestamp, @message | filter @message like /ERROR/`
  - `fields @timestamp, @message | sort @timestamp desc | limit 50`
  - `stats count(*) by bin(5m) as period`
- `start_time`, `end_time` (required): ISO-8601 boundaries of the investigation window.

# Output

For each existing log group: matched events. The agent should highlight error patterns, spikes, and correlations across groups.
