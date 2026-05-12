# CloudWatch Logs Agent

Queries AWS CloudWatch Logs Insights for relevant log entries within an investigation window. Derives log group names from alert context, validates their existence, and returns log analysis summaries.

## Purpose

When the master agent fans out an investigation, the CloudWatch Logs agent derives log group names from the `AlertContext` (service, application identifier, environment prefix), validates that each group exists, runs a Logs Insights query for the window around the alert timestamp, and returns an `AgentResult` containing the matched log entries and aggregated patterns.

The agent skips non-existent log groups gracefully — partial results from existing groups are still returned.

## Skills

| Name | Description |
|---|---|
| `query_cloudwatch_logs` | Execute a CloudWatch Logs Insights query against derived log groups within an investigation window. Validates group existence, skips missing ones, returns structured findings. |

> Skills are declared in [`config.yaml`](../../config.yaml) under `agents.cloudwatch_logs.skills` and resolved at startup by `shared.a2a_factory` from [`skills/query_cloudwatch_logs/SKILL.md`](skills/query_cloudwatch_logs/SKILL.md).

## MCPs

| Name | Transport | Endpoint | Auth |
|---|---|---|---|
| `aws_docs` | `streamable_http` | `https://knowledge-mcp.global.api.aws` | none |

Configured in [`config.yaml`](../../config.yaml) under `agents.cloudwatch_logs.mcps`. `shared.mcp_loader` opens the connection at startup, harvests its tools, and exposes them to the agent alongside the native `query_cloudwatch_logs` skill.

## IAM

The `cloudwatch_logs_agent` IAM role (see [`terraform/iam.tf`](../../terraform/iam.tf), plus the shared runtime-execution attachments in [`terraform/iam_agentcore.tf`](../../terraform/iam_agentcore.tf)) carries:

- `logs:DescribeLogGroups` — required to validate that derived log group names exist.
- `logs:StartQuery`, `logs:GetQueryResults` — required to run Insights queries against log groups in the deployment account/region.

Resource scope is `arn:aws:logs:<region>:<account>:log-group:*` (any group in-account).

## Local dev

```bash
AGENT=cloudwatch_logs python -m shared.a2a_factory agents/cloudwatch_logs
```

Defaults to port 9000. To run alongside the master agent, override: `A2A_PORT=9003 AGENT=cloudwatch_logs python -m shared.a2a_factory agents/cloudwatch_logs`.

`AWS_REGION` must be set; the agent reads logs from this region. AWS credentials must allow `logs:*` for the target account.
