---
name: capture_status_snapshot
description: Capture a read-only status snapshot of all infrastructure observed by the SRE agents and post it to chat. Used for the operator-driven `/status` command.
tool: agents.master.tools:capture_status_snapshot
---
# When to use

When the user message is a JSON object with `task: "snapshot"`. The Lambda intake dispatches `/status` slash-command invocations this way. Pass the full JSON payload verbatim as `snapshot_request_json`.

# Inputs

- `snapshot_request_json` (required): the user message verbatim. Must be a JSON object with:
  - `task`: must equal `"snapshot"`.
  - `platform`: chat platform name (`"slack"` or `"discord"`).
  - `channel_id`: channel where the snapshot should be posted.
  - `user_id`: who invoked `/status` (logged, not posted).
  - `requested_at`: ISO 8601 timestamp from the Lambda intake — anchors the snapshot window.

# Output

The tool returns immediately with a short acknowledgement once the fan-out is dispatched. The orchestrator runs in the background, queries every active specialized agent under a 30-second hard cutoff, and posts a `SnapshotSections` payload directly to chat. Do not respond with prose — the tool handles the entire user-visible interaction.

# Behaviour

- **Master section**: synthesised synchronously from the agent registry — every catalogue entry is rendered with a state badge (🟢 active / 🟫 disabled / ⚪ not deployed). No fan-out for the master itself.
- **Per-agent fan-out**: each active specialized agent receives an A2A `message/send` with `{"task": "snapshot", "requested_at": <ISO>}` and is expected to return a `SnapshotReport` via the `<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>` footer.
- **Failures** are absorbed: timeout → ❌ block, error → ❌ block. Disabled-in-config agents render as 🚫 blocks. Anomalies (`SnapshotReport.anomaly == True`) render as ⚠️ with the agent's `anomaly_summary` as an italic line.
- **Top-line summary** is deterministic — no extra Claude call.
- **Posting**: top-level (not a thread reply) — `/status` is an operational broadcast.
