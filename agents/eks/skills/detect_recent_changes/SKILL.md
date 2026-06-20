---
name: detect_recent_changes
description: Change-correlation for EKS — answer "what changed right before the alert?" by reading each implicated Deployment's rollout state (spec vs observed generation, Progressing condition), container image tags, and retained ReplicaSet history. Reports a ranked lead like "image v1→v2 at 12:20, ~10 min before the alert". Read-only.
tool: agents.eks.tools:detect_recent_changes
---
# When to use

Call this skill FIRST, ONCE, before `gather_eks_state`, whenever the alert names a Kubernetes workload. "What changed right before the alert?" is the first question any operator asks, and a recent rollout is the single strongest lead. It is one cheap read — do not loop it.

# Inputs

- `namespace` (required): the K8s namespace of the implicated workload(s).
- `workloads` (required): a list of Deployment names implicated by the alert (e.g. `["payment-api"]`).
- `alert_time` (optional): the ISO 8601 timestamp the alert fired, so the lead can report how many minutes before the alert the rollout happened.

# Output

Findings flagging:
- A recent rollout with an image change (`image v1→v2 at HH:MM, ~N min before alert`) — the ranked lead.
- A rollout with no image change (config/scale/template-only or restart).
- A rollout still in progress (spec generation ahead of observed generation).

# Honesty discipline

When no rollout is retained, the tool reports **"no recent rollout found within retained history"** — NEVER "nothing changed". `revisionHistoryLimit` pruning means the absence of a retained ReplicaSet is not proof the workload was stable. Report the absence honestly; do not let it become a license to conclude the workload was unchanged.
