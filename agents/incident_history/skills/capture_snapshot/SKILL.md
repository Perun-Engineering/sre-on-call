---
name: capture_snapshot
description: Report how many incident-outcome records have been written to the incident-history store in the last 30 days.
tool: agents.incident_history.tools:capture_snapshot
---
# When to use

Call this skill when the user message is a JSON object with `task: "snapshot"` — the master agent dispatches `/sre-snapshot` requests this way. Pass `requested_at` from the master verbatim.

# Inputs

- `requested_at` (required): ISO 8601 timestamp from the master, used as the `captured_at` field of the returned `SnapshotReport`.

# Output

A short human-readable summary plus an embedded `SnapshotReport` footer. One section:

- **Incident memory (last 30 days)** — `<n> incident(s) recorded`, or a note when the history store is not configured in this deployment.

# Anomaly criteria

Never flags an anomaly — incident-memory volume is informational, not a health signal. The tool never raises.
