---
name: discover_log_groups
description: List the account's real CloudWatch log groups and return those matching alert keywords (ranked, capped). Run this first so queries target log groups that actually exist instead of guessed names.
tool: agents.cloudwatch_logs.tools:discover_log_groups
---
# When to use

Always call this **first**, before `query_cloudwatch_logs`. Never invent or
guess conventional log group names — derive keywords from the `AlertContext`
(service/application names, cluster name, namespace, resource identifiers) and
let this tool tell you which log groups actually exist.

# Inputs

- `keywords` (required): tokens from the alert used as case-insensitive
  substring filters against real log group names. Pass several — e.g. for a
  pod alert on cluster `eks-uat` in namespace `monitoring`, pass
  `["eks-uat", "monitoring", "<service or workload name>"]`.

# Output

A list of real log group names to feed into `query_cloudwatch_logs`, ranked by
how many keywords they match. If nothing matches, the tool says so plainly —
report that no relevant CloudWatch log groups exist (the logs are likely
shipped elsewhere) rather than guessing names that will be skipped.
