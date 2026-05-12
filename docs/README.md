# Documentation

Top-level docs for the sre-on-call project.

## Operations

- [Deployment](deployment.md) — build images → push to ECR → terraform apply → secret hydration.
- [Testing](testing.md) — synthetic webhook + real Slack alert procedures.
- [Architecture diagram](architecture.svg) — generated from `architecture.d2`.

## Agents

| Agent | Purpose |
|---|---|
| [Master](../agents/master/README.md) | Orchestrates investigations across specialized agents, enforces deadlines, posts the Incident Report. |
| [Slack Scanner](../agents/slack_scanner/README.md) | Scans Slack channel history for correlated alerts within an investigation window. |
| [Discord Scanner](../agents/discord_scanner/README.md) | Scans Discord channel history for correlated alerts within an investigation window. |
| [CloudWatch Logs](../agents/cloudwatch_logs/README.md) | Queries AWS CloudWatch Logs Insights for log entries around the incident. |
| [EKS](../agents/eks/README.md) | Gathers Kubernetes cluster state (pods, events, logs, node conditions). |

## Domain vocabulary

See [`CONTEXT.md`](../CONTEXT.md) at the repo root for the canonical term definitions (`AlertContext`, `Finding`, `AgentResult`, `ToolResult`, `WebhookAdapter`, `ChatPoster`, `ReportRenderer`, `ChannelMessageSource`).

