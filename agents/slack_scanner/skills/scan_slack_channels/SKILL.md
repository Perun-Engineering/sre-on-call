---
name: scan_slack_channels
description: Scan up to 10 Slack channels for alerts, notifications, and error messages from integration bots within a configurable investigation window.
tool: agents.slack_scanner.tools:scan_slack_channels
---
# When to use

Call this skill once per investigation. The master agent's `AlertContext` provides the alert timestamp; the agent's environment provides the watched channel IDs.

# Inputs

- `alert_timestamp` (required): ISO-8601 timestamp of the original alert.
- `bot_channel_ids` (required): list of Slack channel IDs (e.g. `["C01ABC", "C02DEF"]`) to scan. Maximum 10.

# Output

A list of correlated findings: bot-authored messages with alert-like content from each channel, within a window centered on `alert_timestamp`. Empty list if no correlations exist — that itself is signal.
