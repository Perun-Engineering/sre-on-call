---
name: gather_eks_state
description: Gather Kubernetes cluster state for specified resources in a namespace — pod status, recent events, pod descriptions, pod logs (tail), node conditions. Handles unreachable EKS API server gracefully.
tool: agents.eks.tools:gather_eks_state
---
# When to use

Call this skill when the alert mentions a Kubernetes workload. A first call gathers broad cluster state — pod status, events, descriptions, logs, node conditions — for the namespace and resource selectors. If a pod is crashlooping, pending, or restarting, follow up with a focused call narrowed to that workload (its deployment name or label selector) to drill into its logs and events, rather than reporting the first pass as-is. Stop once you can explain the alert or your budget is spent.

# Inputs

- `namespace` (required): the K8s namespace.
- `resource_selectors` (required): a list of either deployment names or label selectors (e.g. `["payment-api", "app=billing"]`).

# Output

Findings flagging:
- Pods in `Pending`, `CrashLoopBackOff`, or with non-zero restarts.
- Warning events such as `FailedScheduling` and `OOMKilled`.
- Node-level pressure conditions.
- Excerpts from pod logs that match error patterns.

> A future PR will split this into 5 atomic skills (`list_pods`, `get_events`, `fetch_logs`, `node_conditions`, `pod_descriptions`). For now this is a single coarse tool.
