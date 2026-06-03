# Slack Scanner Agent

Scans Slack channel history for correlated alerts, notifications, and error messages from integration bots within an investigation window centered on the incident timestamp.

## Purpose

When the master agent fans out an investigation, the Slack Scanner receives the `AlertContext`, queries the Slack Web API for messages in up to 10 watched channels around the alert timestamp, filters for bot-authored messages with alert-like content, and returns an `AgentResult` containing the correlated findings.

## Skills

| Name | Description |
|---|---|
| `scan_slack_channels` | Scan up to 10 Slack channels for alerts, notifications, and error messages from integration bots within a configurable investigation window. |

> Skills are declared in [`config.yaml`](../../config.yaml) under `agents.slack_scanner.skills` and resolved at startup by `shared.a2a_factory` from [`skills/scan_slack_channels/SKILL.md`](skills/scan_slack_channels/SKILL.md).

## MCPs

None.

## IAM

The `slack_scanner_agent` IAM role (see [`modules/sre-on-call/iam.tf`](../../modules/sre-on-call/iam.tf), plus the shared runtime-execution attachments in [`modules/sre-on-call/iam_agentcore.tf`](../../modules/sre-on-call/iam_agentcore.tf)) carries:

- `secretsmanager:GetSecretValue` on the Slack bot-token secret — required to call `conversations.history` and related Slack Web API methods.

## Slack scopes

The bot token referenced via `SLACK_BOT_TOKEN` must have at least:

- `channels:history` — read message history from public channels the bot is in.
- `groups:history` — read message history from private channels the bot is in.
- `app_mentions:read`, `chat:write` — already required by the Lambda adapter.

If you change the scope set, reinstall the Slack app and refresh the secret in Secrets Manager — Slack tokens are static and don't pick up new scopes until reinstall.

## Local dev

```bash
AGENT=slack_scanner python -m shared.a2a_factory agents/slack_scanner
```

Defaults to port 9000. To run alongside the master agent, override: `A2A_PORT=9001 AGENT=slack_scanner python -m shared.a2a_factory agents/slack_scanner`.

`SLACK_BOT_TOKEN` must be set to either a Secrets Manager ARN (resolved at runtime) or a literal token (for local dev).
