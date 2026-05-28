###############################################################################
# sre-on-call — AgentCore Runtime Deployments
#
# Uses the first-class `aws_bedrockagentcore_agent_runtime` resource
# (AWS provider >= 6.21). The Master Agent invokes specialized agents
# via boto3 `bedrock-agentcore.invoke_agent_runtime`; their ARNs are
# passed via *_AGENT_RUNTIME_ARN environment variables.
###############################################################################

# ── Variables ────────────────────────────────────────────────────────────────

variable "agent_container_registry" {
  description = "ECR registry URL for agent container images (e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com)"
  type        = string
}

variable "agent_image_tag" {
  description = "Container image tag for agent deployments"
  type        = string
  default     = "latest"
}

variable "model_id" {
  description = "Bedrock model ID (or cross-region inference profile) used by all agents"
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

# ── Locals ───────────────────────────────────────────────────────────────────

locals {
  agent_prefix = "${var.project_name}-${var.environment}"

  agent_images = {
    master          = "${var.agent_container_registry}/${var.project_name}-master:${var.agent_image_tag}"
    slack_scanner   = "${var.agent_container_registry}/${var.project_name}-slack-scanner:${var.agent_image_tag}"
    discord_scanner = "${var.agent_container_registry}/${var.project_name}-discord-scanner:${var.agent_image_tag}"
    cloudwatch_logs = "${var.agent_container_registry}/${var.project_name}-cloudwatch-logs:${var.agent_image_tag}"
    eks             = "${var.agent_container_registry}/${var.project_name}-eks:${var.agent_image_tag}"
  }

  # Single source of truth for which specialized agents are deployed and active.
  # `enabled` defaults to true if omitted (matches shared/config.py / shared/agents.py).
  # An agent absent from config.yaml is treated as not deployed (resources are
  # not created for it) — see shared/agents.py docstring for the lifecycle.
  config_yaml = yamldecode(file("${path.module}/../config.yaml"))
  agent_enabled = {
    for name in ["slack_scanner", "discord_scanner", "cloudwatch_logs", "eks"] :
    name => contains(keys(local.config_yaml.agents), name) && lookup(local.config_yaml.agents[name], "enabled", true)
  }
}

# ── Specialized Agents ──────────────────────────────────────────────────────

resource "aws_bedrockagentcore_agent_runtime" "slack_scanner" {
  count = local.agent_enabled["slack_scanner"] ? 1 : 0

  agent_runtime_name = replace("${local.agent_prefix}_slack_scanner", "-", "_")
  description        = "Scans Slack channel history for correlated alerts within an investigation window."
  role_arn           = aws_iam_role.slack_scanner_agent[0].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_images.slack_scanner
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  environment_variables = {
    AWS_REGION      = var.aws_region
    MODEL_ID        = var.model_id
    SLACK_BOT_TOKEN = aws_secretsmanager_secret.slack_bot_token.arn
  }

  tags = {
    Agent = "slack_scanner"
  }
}

resource "aws_bedrockagentcore_agent_runtime" "discord_scanner" {
  count = local.agent_enabled["discord_scanner"] ? 1 : 0

  agent_runtime_name = replace("${local.agent_prefix}_discord_scanner", "-", "_")
  description        = "Scans Discord channel history for correlated alerts within an investigation window."
  role_arn           = aws_iam_role.discord_scanner_agent[0].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_images.discord_scanner
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  environment_variables = {
    AWS_REGION        = var.aws_region
    MODEL_ID          = var.model_id
    DISCORD_BOT_TOKEN = aws_secretsmanager_secret.discord_bot_token.arn
  }

  tags = {
    Agent = "discord_scanner"
  }
}

resource "aws_bedrockagentcore_agent_runtime" "cloudwatch_logs" {
  count = local.agent_enabled["cloudwatch_logs"] ? 1 : 0

  agent_runtime_name = replace("${local.agent_prefix}_cloudwatch_logs", "-", "_")
  description        = "Queries AWS CloudWatch Logs Insights for relevant log entries within an investigation window."
  role_arn           = aws_iam_role.cloudwatch_logs_agent[0].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_images.cloudwatch_logs
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  environment_variables = {
    AWS_REGION = var.aws_region
    MODEL_ID   = var.model_id
  }

  tags = {
    Agent = "cloudwatch_logs"
  }
}

resource "aws_bedrockagentcore_agent_runtime" "eks" {
  count = local.agent_enabled["eks"] ? 1 : 0

  agent_runtime_name = replace("${local.agent_prefix}_eks", "-", "_")
  description        = "Gathers Kubernetes cluster state from Amazon EKS including pod status, events, logs, and node conditions."
  role_arn           = aws_iam_role.eks_agent[0].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_images.eks
    }
  }

  # eks-uat has endpointPublicAccess=false, so the agent must run inside
  # the cluster's VPC to reach the private API endpoint.
  network_configuration {
    network_mode = "VPC"

    network_mode_config {
      subnets         = data.aws_subnets.eks_private[0].ids
      security_groups = [aws_security_group.eks_agent[0].id]
    }
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  environment_variables = {
    AWS_REGION       = var.aws_region
    MODEL_ID         = var.model_id
    EKS_CLUSTER_NAME = var.eks_cluster_name
  }

  tags = {
    Agent = "eks"
  }
}

# ── Master Agent — orchestrator with the four specialized ARNs in its env ──

resource "aws_bedrockagentcore_agent_runtime" "master" {
  agent_runtime_name = replace("${local.agent_prefix}_master", "-", "_")
  description        = "Orchestrates incident investigations by fanning out to specialized agents, enforcing deadlines, synthesizing results, and posting reports to Slack/Discord."
  role_arn           = aws_iam_role.master_agent.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_images.master
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  environment_variables = merge(
    {
      AWS_REGION         = var.aws_region
      MODEL_ID           = var.model_id
      SLACK_BOT_TOKEN    = aws_secretsmanager_secret.slack_bot_token.arn
      DISCORD_BOT_TOKEN  = aws_secretsmanager_secret.discord_bot_token.arn
      TRACES_BUCKET_NAME = aws_s3_bucket.traces.bucket
      TRACES_TABLE_NAME  = aws_dynamodb_table.traces.name
    },
    local.agent_enabled["slack_scanner"] ? {
      SLACK_SCANNER_AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.slack_scanner[0].agent_runtime_arn
    } : {},
    local.agent_enabled["discord_scanner"] ? {
      DISCORD_SCANNER_AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.discord_scanner[0].agent_runtime_arn
    } : {},
    local.agent_enabled["cloudwatch_logs"] ? {
      CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.cloudwatch_logs[0].agent_runtime_arn
    } : {},
    local.agent_enabled["eks"] ? {
      EKS_AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.eks[0].agent_runtime_arn
    } : {},
  )

  tags = {
    Agent = "master"
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "master_agent_runtime_arn" {
  description = "Bedrock AgentCore runtime ARN of the master agent"
  value       = aws_bedrockagentcore_agent_runtime.master.agent_runtime_arn
}

output "specialized_agent_runtime_arns" {
  description = "Map of specialized agent name -> runtime ARN (only includes enabled agents)"
  value = merge(
    local.agent_enabled["slack_scanner"] ? {
      slack_scanner = aws_bedrockagentcore_agent_runtime.slack_scanner[0].agent_runtime_arn
    } : {},
    local.agent_enabled["discord_scanner"] ? {
      discord_scanner = aws_bedrockagentcore_agent_runtime.discord_scanner[0].agent_runtime_arn
    } : {},
    local.agent_enabled["cloudwatch_logs"] ? {
      cloudwatch_logs = aws_bedrockagentcore_agent_runtime.cloudwatch_logs[0].agent_runtime_arn
    } : {},
    local.agent_enabled["eks"] ? {
      eks = aws_bedrockagentcore_agent_runtime.eks[0].agent_runtime_arn
    } : {},
  )
}
