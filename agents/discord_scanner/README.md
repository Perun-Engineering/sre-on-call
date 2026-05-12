# Discord Scanner Agent

Scans Discord channel history for correlated alerts, notifications, and error messages from bots within an investigation window centered on the incident timestamp.

## Purpose

When the master agent fans out an investigation, the Discord Scanner receives the `AlertContext`, queries the Discord REST API for messages in up to 10 watched channels around the alert timestamp, filters for bot-authored messages with alert-like content, and returns an `AgentResult` containing the correlated findings.

## Skills

| Name | Description |
|---|---|
| `scan_discord_channels` | Scan up to 10 Discord channels for alerts, notifications, and error messages from bots within a configurable investigation window. |

> Skills are declared in [`config.yaml`](../../config.yaml) under `agents.discord_scanner.skills` and resolved at startup by `shared.a2a_factory` from [`skills/scan_discord_channels/SKILL.md`](skills/scan_discord_channels/SKILL.md).

## MCPs

None.

## IAM

The `discord_scanner_agent` IAM role (see [`terraform/iam.tf`](../../terraform/iam.tf), plus the shared runtime-execution attachments in [`terraform/iam_agentcore.tf`](../../terraform/iam_agentcore.tf)) carries:

- `secretsmanager:GetSecretValue` on the Discord bot-token secret — required to call the Discord REST API.

## Local dev

```bash
AGENT=discord_scanner python -m shared.a2a_factory agents/discord_scanner
```

Defaults to port 9000. To run alongside the master agent, override: `A2A_PORT=9002 AGENT=discord_scanner python -m shared.a2a_factory agents/discord_scanner`.

`DISCORD_BOT_TOKEN` must be set to either a Secrets Manager ARN (resolved at runtime) or a literal token (for local dev).

> `config.yaml` carries an `enabled` flag per agent (`shared/config.py:AgentConfig.enabled`) but the build/deploy plumbing (Dockerfile per-agent matrix and `terraform/agentcore.tf`) does not yet read it — disabling the agent here today still leaves the runtime deployed. Use `ENABLED_AGENTS` on the master to scope fan-out instead.
