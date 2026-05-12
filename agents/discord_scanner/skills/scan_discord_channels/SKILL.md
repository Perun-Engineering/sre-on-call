---
name: scan_discord_channels
description: Scan up to 10 Discord channels for alerts, notifications, and error messages from bots within a configurable investigation window.
tool: agents.discord_scanner.tools:scan_discord_channels
---
# When to use

Call this skill once per investigation. The master agent's `AlertContext` provides the alert timestamp; the agent's environment provides the watched channel IDs.

# Inputs

- `alert_timestamp` (required): ISO-8601 timestamp of the original alert.
- `channel_ids` (required): list of Discord channel IDs to scan. Maximum 10.

# Output

A list of correlated findings: bot-authored messages with alert-like content from each channel, within a window centered on `alert_timestamp`. Empty list if no correlations exist — that itself is signal.
