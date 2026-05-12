# EKS Agent

Gathers Kubernetes cluster state from Amazon EKS — pod status, recent events, pod descriptions, pod logs, and node conditions — for resources identified in alert context.

## Purpose

When the master agent fans out an investigation, the EKS agent identifies the relevant namespace and resource selectors (deployment names or label selectors) from the `AlertContext`, then queries the cluster's Kubernetes API for:

- Pod status (phase, restart count, node assignment).
- Recent events at namespace and pod scope.
- Pod descriptions (containers, conditions, last-state).
- Pod logs (tail).
- Node conditions (pressure, ready/not-ready).

It returns an `AgentResult` flagging pods in `CrashLoopBackOff`, `Pending`, or with non-zero restarts; warning events such as `FailedScheduling` and `OOMKilled`; and node-level pressure conditions.

## Skills

| Name | Description |
|---|---|
| `gather_eks_state` | Gather Kubernetes cluster state for specified resources in a namespace: pod status, recent events, pod descriptions, pod logs (tail), node conditions. Handles unreachable EKS API server gracefully. |

> Skills are declared in [`config.yaml`](../../config.yaml) under `agents.eks.skills` and resolved at startup by `shared.a2a_factory` from [`skills/gather_eks_state/SKILL.md`](skills/gather_eks_state/SKILL.md). The current implementation is one coarse-grained tool; splitting it into smaller skills (e.g. `list_pods`, `get_events`, `fetch_logs`) is a future option that the SKILL.md plumbing now makes cheap.

## MCPs

None.

## IAM

The `eks_agent` IAM role (see [`terraform/iam.tf`](../../terraform/iam.tf), plus the shared runtime-execution attachments in [`terraform/iam_agentcore.tf`](../../terraform/iam_agentcore.tf)) carries:

- `eks:DescribeCluster` on `arn:aws:eks:<region>:<account>:cluster/*` — required to retrieve the cluster endpoint and CA certificate.

The role is also registered as an EKS Access Entry on cluster `eks-uat` with the `AmazonEKSViewPolicy` (cluster-scoped). See `terraform/networking.tf` for the access-entry configuration.

## Network requirements

The EKS agent runs in `network_mode: VPC` because the target cluster's API endpoint is private (`endpointPublicAccess=false`). The agent's runtime is attached to the cluster's VPC (resolved from `eks_cluster_name` at apply time) on private subnets in supported AgentCore AZs (`use1-az1`, `use1-az2`, `use1-az4`). Reachability is one-way: the agent's SG has unrestricted egress, and an ingress rule on the *cluster's* security group (`cluster_ingress_from_agent` in `terraform/networking.tf`) allows 443 from the agent's SG.

## Local dev

```bash
AGENT=eks python -m shared.a2a_factory agents/eks
```

Defaults to port 9000. To run alongside the master agent, override: `A2A_PORT=9004 AGENT=eks python -m shared.a2a_factory agents/eks`.

For local dev against a real cluster, you need either:

- A populated `~/.kube/config` pointing at an accessible cluster, or
- `EKS_CLUSTER_NAME` plus AWS credentials with `eks:DescribeCluster` — the agent then loads kubeconfig from the EKS API directly via `_load_kube_config_from_eks` and authenticates with a SigV4-presigned `sts:GetCallerIdentity` URL (the same mechanism `aws eks get-token` uses).

A purely-mocked local run (no cluster) is supported by the unit tests in `tests/test_eks_agent.py`, which inject a fake K8s client.
