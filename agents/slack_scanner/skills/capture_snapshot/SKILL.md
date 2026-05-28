---
name: capture_snapshot
description: Capture a read-only snapshot of Slack workspace reachability — bot authentication and channel-membership count.
tool: agents.slack_scanner.tools:capture_snapshot
---
# When to use

Call this skill when the user message is a JSON object with `task: "snapshot"` — the master agent dispatches `/status` requests this way. Pass `requested_at` from the master verbatim.

# Inputs

- `requested_at` (required): ISO 8601 timestamp from the master, used as the `captured_at` field of the returned `SnapshotReport`.

# Output

A short human-readable summary plus an embedded `SnapshotReport` footer that the master orchestrator extracts. Two probes:

- **Authentication** — `auth.test` results: workspace name, team id, bot user, bot id, workspace URL. Sets `anomaly=True` and an `anomaly_summary` when `auth.test` raises or returns non-`ok`.
- **Channel access** — `users.conversations` count: number of channels the bot is a member of. A failure here is surfaced as a section line with ❌ but does NOT flip the report to anomaly — a healthy auth.test with a transient channels-listing failure is still operationally healthy.

The tool never raises — any unexpected exception is folded into the snapshot as an anomaly section, matching the no-raise contract of the alert path.
