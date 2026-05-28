---
name: capture_snapshot
description: Capture a read-only snapshot of EKS cluster state — node readiness, pod phases across all namespaces, non-Running pods, and recent kube-system warning events.
tool: agents.eks.tools:capture_snapshot
---
# When to use

Call this skill when the user message is a JSON object with `task: "snapshot"` — the master agent dispatches `/status` requests this way. Pass `requested_at` from the master verbatim.

# Inputs

- `requested_at` (required): ISO 8601 timestamp from the master, used as the `captured_at` field of the returned `SnapshotReport`.

# Output

A short human-readable summary plus an embedded `SnapshotReport` footer that the master orchestrator extracts. Sections:

- **Cluster** — cluster name (from `EKS_CLUSTER_NAME`) and Kubernetes server `gitVersion`. Set as the very first section so an unreachable cluster surfaces immediately.
- **Nodes** — count by `Ready` / `NotReady` / `SchedulingDisabled`.
- **Pods** — count by phase across all namespaces (`Running` / `Pending` / `Failed` / `Unknown` / `Succeeded`).
- **Non-Running pods (top 10)** — `<namespace>/<name> · phase=<phase> · restarts=<n>`. Sorted by phase severity then restart count desc, truncated to 10 with a tail count when more exist.
- **kube-system warning events (last 5 min)** — top 5: `<reason>: <message> (<involvedObject.kind>/<name>)`.

# Anomaly criteria

`anomaly = True` when ANY of:

- A node is `NotReady`.
- Any pod is `Failed`.
- Any non-Running pod has `restarts >= 3`.
- Any kube-system warning event was emitted in the last 5 minutes.

The tool never raises — failure to load cluster config or query the API is folded into a single anomaly section, matching the no-raise contract of the alert path.
