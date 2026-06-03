###############################################################################
# sre-on-call — Networking
#
# Only the EKS agent needs VPC connectivity (the cluster has
# endpointPublicAccess=false). Other agents run in AgentCore PUBLIC mode.
###############################################################################

# ── Data Sources for the existing eks-uat cluster's VPC ─────────────────────

locals {
  eks_enabled = local.agent_enabled["eks"]
}

data "aws_eks_cluster" "target" {
  count = local.eks_enabled ? 1 : 0
  name  = var.eks_cluster_name
}

data "aws_vpc" "eks" {
  count = local.eks_enabled ? 1 : 0
  id    = data.aws_eks_cluster.target[0].vpc_config[0].vpc_id
}

# Pick subnets in the cluster's VPC that are NAT-routed (purpose=private)
# rather than the cluster's own subnet list — eks-uat registers its
# control-plane ENIs on `intra` subnets which have no default route, so
# placing the agent there leaves it unable to reach Bedrock or the
# AgentCore data plane. The agent only needs same-VPC reachability to the
# cluster's API ENIs, which it gets via the cluster SG ingress rule
# (cluster_ingress_from_agent below) plus VPC-local routing.
#
# AgentCore VPC mode only supports a subset of AZs in us-east-1
# (use1-az1, use1-az2, use1-az4); intersect with `purpose=private` so the
# agent runtime lands on NAT-routed subnets in supported AZs only.
data "aws_subnets" "eks_private" {
  count = local.eks_enabled ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.eks[0].id]
  }

  filter {
    name   = "availability-zone-id"
    values = ["use1-az1", "use1-az2", "use1-az4"]
  }

  filter {
    name   = "tag:purpose"
    values = ["private"]
  }
}

# ── Security Group for the EKS Agent ────────────────────────────────────────

resource "aws_security_group" "eks_agent" {
  count = local.eks_enabled ? 1 : 0

  name        = "${var.project_name}-${var.environment}-eks-agent-sg"
  description = "Allows the EKS agent to reach the cluster API server and AWS APIs."
  vpc_id      = data.aws_vpc.eks[0].id

  egress {
    description = "Allow all outbound (to cluster API + AWS endpoints)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-eks-agent-sg"
  }
}

# Allow the EKS agent SG to reach the cluster API on 443
resource "aws_security_group_rule" "cluster_ingress_from_agent" {
  count = local.eks_enabled ? 1 : 0

  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = data.aws_eks_cluster.target[0].vpc_config[0].cluster_security_group_id
  source_security_group_id = aws_security_group.eks_agent[0].id
  description              = "sre-on-call EKS agent to cluster API"
}

# ── EKS Access Entry for the agent's IAM role ───────────────────────────────
# eks-uat uses authenticationMode=API (no aws-auth ConfigMap). Grant the
# agent's IAM principal cluster-scoped read access via the standard
# AmazonEKSViewPolicy.

resource "aws_eks_access_entry" "eks_agent" {
  count = local.eks_enabled ? 1 : 0

  cluster_name  = var.eks_cluster_name
  principal_arn = aws_iam_role.eks_agent[0].arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "eks_agent_view" {
  count = local.eks_enabled ? 1 : 0

  cluster_name  = var.eks_cluster_name
  principal_arn = aws_iam_role.eks_agent[0].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.eks_agent]
}
