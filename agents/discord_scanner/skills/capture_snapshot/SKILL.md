---
name: capture_snapshot
description: Capture a read-only snapshot of Discord bot reachability — bot identity and guild/channel access count.
tool: agents.discord_scanner.tools:capture_snapshot
---
# When to use

Call this skill when the user message is a JSON object with `task: "snapshot"` — the master agent dispatches `/status` requests this way. Pass `requested_at` from the master verbatim.

# Inputs

- `requested_at` (required): ISO 8601 timestamp from the master, used as the `captured_at` field of the returned `SnapshotReport`.

# Output

A short human-readable summary plus an embedded `SnapshotReport` footer. Two probes:

- **Authentication** — `GET /users/@me`: bot identity (id, username, discriminator). Sets `anomaly=True` and an `anomaly_summary` when the call returns non-2xx or fails.
- **Guild access** — `GET /users/@me/guilds`: count of guilds the bot is a member of. A failure here is surfaced as a section line with ❌ but does NOT flip the report to anomaly — a healthy auth call with a transient guild listing failure is still operationally healthy.

The tool never raises — any unexpected exception is folded into the snapshot as an anomaly section, matching the no-raise contract of the alert path.
