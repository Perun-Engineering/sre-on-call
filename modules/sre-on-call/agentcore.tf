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

variable "enable_bedrock_guardrail" {
  description = "Attach a Bedrock Guardrail (prompt-attack filtering) to every agent's model invocations. Defence-in-depth against prompt injection in ingested incident content; adds per-call latency and cost. Opt-in."
  type        = bool
  default     = false
}

variable "enable_analysis_synthesis" {
  description = "Enable the master's post-harvest LLM synthesis of a root-cause Analysis section in the Incident Report. Fail-open: a synthesis error/timeout posts the report without the section. Adds one LLM call per report (and per late result)."
  type        = bool
  default     = true
}

variable "synthesis_model_id" {
  description = "Bedrock model ID for the master's Analysis synthesis call. Empty string falls back to the master's MODEL_ID. Set to a Sonnet-class model for higher-quality root-cause reasoning while dispatch stays on the cheaper model."
  type        = string
  default     = ""
}

variable "enable_alert_routing" {
  description = "Enable the master's pre-dispatch LLM routing (issue #28): pick which active agents to query per alert and attach per-agent investigation hints. Fail-open: any routing error dispatches every active agent (today's behavior). Router-skipped agents render as a distinct 'not investigated' state in the report."
  type        = bool
  default     = true
}

variable "routing_model_id" {
  description = "Bedrock model ID for the master's routing call. Empty string falls back to the master's MODEL_ID. Set to a Sonnet-class model for better triage judgment while dispatch stays cheap."
  type        = string
  default     = ""
}

variable "enable_followup_round" {
  description = "Enable the master's Stage 2 bounded follow-up round (issue #28): after the initial harvest, optionally run one additional targeted dispatch to a few agents with refined hints. Fail-open and hard-capped so the 5-minute cutoff always holds. Defaults off until validated in dev."
  type        = bool
  default     = false
}

variable "enable_chart_snapshots" {
  description = "Enable the cloudwatch_logs agent to attach chart descriptors to findings and ship the harvested query rows as a series, which the master snapshots to the trace bucket under charts/<id>.json for the interactive incident page (#32). Fail-open and additive; persistence is gated by the traces bucket being configured. Set false to suppress the extra A2A payload + S3 objects."
  type        = bool
  default     = true
}

variable "followup_model_id" {
  description = "Bedrock model ID for the master's follow-up planning call. Empty string falls back to the master's MODEL_ID."
  type        = string
  default     = ""
}

variable "followup_max_agents" {
  description = "Hard cap on the number of agents dispatched in the single Stage 2 follow-up round."
  type        = number
  default     = 2
}

variable "embedding_model_id" {
  description = "Bedrock model ID for alert-text embeddings used by the incident_history similar-incident lookup (issue #30). Titan Text Embeddings V2 by default."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

# ── Locals ───────────────────────────────────────────────────────────────────

locals {
  agent_prefix = "${var.project_name}-${var.environment}"

  # Guardrail env injected into every runtime when enabled; empty map (no-op)
  # otherwise. shared.a2a_factory._resolve_model reads these to bind the model
  # to the guardrail — unset means no guardrail, preserving prior behaviour.
  guardrail_env = var.enable_bedrock_guardrail ? {
    BEDROCK_GUARDRAIL_ID      = aws_bedrock_guardrail.agents[0].guardrail_id
    BEDROCK_GUARDRAIL_VERSION = aws_bedrock_guardrail.agents[0].version
  } : {}

  # Base env injected into every runtime: guardrail (if any) plus the SSM
  # parameter shared.config reads config.yaml from at cold-start. Editing
  # config.yaml + `terraform apply` re-publishes the parameter; agents pick up
  # the change on next cold-start with no image rebuild.
  base_env = merge(local.guardrail_env, {
    CONFIG_SSM_PARAMETER = aws_ssm_parameter.config.name
  })

  agent_images = {
    master           = "${var.agent_container_registry}/${var.project_name}-master:${var.agent_image_tag}"
    slack_scanner    = "${var.agent_container_registry}/${var.project_name}-slack-scanner:${var.agent_image_tag}"
    discord_scanner  = "${var.agent_container_registry}/${var.project_name}-discord-scanner:${var.agent_image_tag}"
    cloudwatch_logs  = "${var.agent_container_registry}/${var.project_name}-cloudwatch-logs:${var.agent_image_tag}"
    eks              = "${var.agent_container_registry}/${var.project_name}-eks:${var.agent_image_tag}"
    incident_history = "${var.agent_container_registry}/${var.project_name}-incident-history:${var.agent_image_tag}"
  }

  # Single source of truth for which specialized agents are deployed and active.
  # `enabled` defaults to true if omitted (matches shared/config.py / shared/agents.py).
  # An agent absent from config.yaml is treated as not deployed (resources are
  # not created for it) — see shared/agents.py docstring for the lifecycle.
  config_yaml = yamldecode(file(var.config_path))
  agent_enabled = {
    for name in ["slack_scanner", "discord_scanner", "cloudwatch_logs", "eks", "incident_history"] :
    name => contains(keys(local.config_yaml.agents), name) && lookup(local.config_yaml.agents[name], "enabled", true)
  }
}

# ── Guardrail (opt-in) ────────────────────────────────────────────────────

# Prompt-attack filtering for every agent. Ingested incident content
# (Slack/Discord messages, CloudWatch logs, EKS events) is untrusted; the
# PROMPT_ATTACK filter screens it on input before the model reasons over it.
# PROMPT_ATTACK is an input-only filter, so output_strength must be NONE.
resource "aws_bedrock_guardrail" "agents" {
  count = var.enable_bedrock_guardrail ? 1 : 0

  name                      = "${local.agent_prefix}-agents"
  description               = "Prompt-attack filtering for sre-on-call agents (ingested incident content is untrusted)."
  blocked_input_messaging   = "This input was blocked by the content guardrail."
  blocked_outputs_messaging = "This response was blocked by the content guardrail."

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }

  tags = {
    Project = var.project_name
  }
}

# ── Externalized agent config ───────────────────────────────────────────────

# config.yaml published to SSM so runtimes read it at cold-start instead of from
# a baked-in image copy. Same file Terraform reads for deploy scoping above, so
# config.yaml stays the single source of truth.
resource "aws_ssm_parameter" "config" {
  name        = "/${var.project_name}/${var.environment}/config"
  description = "sre-on-call agent config.yaml — read by shared.config at runtime."
  type        = "String"
  value       = file(var.config_path)

  tags = {
    Project = var.project_name
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

  environment_variables = merge(local.base_env, {
    AWS_REGION      = var.aws_region
    MODEL_ID        = var.model_id
    SLACK_BOT_TOKEN = aws_secretsmanager_secret.slack_bot_token.arn
  })

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

  environment_variables = merge(local.base_env, {
    AWS_REGION        = var.aws_region
    MODEL_ID          = var.model_id
    DISCORD_BOT_TOKEN = aws_secretsmanager_secret.discord_bot_token.arn
  })

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

  environment_variables = merge(local.base_env, {
    AWS_REGION              = var.aws_region
    MODEL_ID                = var.model_id
    CHART_SNAPSHOTS_ENABLED = var.enable_chart_snapshots ? "true" : "false"
  })

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

  environment_variables = merge(local.base_env, {
    AWS_REGION       = var.aws_region
    MODEL_ID         = var.model_id
    EKS_CLUSTER_NAME = var.eks_cluster_name
  })

  tags = {
    Agent = "eks"
  }
}

resource "aws_bedrockagentcore_agent_runtime" "incident_history" {
  count = local.agent_enabled["incident_history"] ? 1 : 0

  agent_runtime_name = replace("${local.agent_prefix}_incident_history", "-", "_")
  description        = "Surfaces past investigations whose alert text is similar to the current alert, ranked by Titan embedding similarity over the trace archive."
  role_arn           = aws_iam_role.incident_history_agent[0].arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = local.agent_images.incident_history
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "A2A"
  }

  environment_variables = merge(local.base_env, {
    AWS_REGION               = var.aws_region
    MODEL_ID                 = var.model_id
    TRACES_TABLE_NAME        = aws_dynamodb_table.traces.name
    INCIDENT_HISTORY_ENABLED = "true"
    EMBEDDING_MODEL_ID       = var.embedding_model_id
  })

  tags = {
    Agent = "incident_history"
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
    local.base_env,
    {
      AWS_REGION         = var.aws_region
      MODEL_ID           = var.model_id
      SLACK_BOT_TOKEN    = aws_secretsmanager_secret.slack_bot_token.arn
      DISCORD_BOT_TOKEN  = aws_secretsmanager_secret.discord_bot_token.arn
      TRACES_BUCKET_NAME = aws_s3_bucket.traces.bucket
      TRACES_TABLE_NAME  = aws_dynamodb_table.traces.name
      SYNTHESIS_ENABLED  = var.enable_analysis_synthesis ? "true" : "false"
      # Issue #28 — pre-dispatch routing + Stage 2 follow-up round. Both are
      # independently fail-open in the orchestrator; these env vars only gate
      # whether the call site is attempted at all.
      ALERT_ROUTING_ENABLED  = var.enable_alert_routing ? "true" : "false"
      FOLLOWUP_ROUND_ENABLED = var.enable_followup_round ? "true" : "false"
      FOLLOWUP_MAX_AGENTS    = tostring(var.followup_max_agents)
    },
    var.synthesis_model_id != "" ? {
      SYNTHESIS_MODEL_ID = var.synthesis_model_id
    } : {},
    var.routing_model_id != "" ? {
      ROUTING_MODEL_ID = var.routing_model_id
    } : {},
    var.followup_model_id != "" ? {
      FOLLOWUP_MODEL_ID = var.followup_model_id
    } : {},
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
    # When incident_history is deployed, the master both dispatches to it
    # (runtime ARN) and turns on its own Phase 8 write path: embed the alert
    # (INCIDENT_HISTORY_ENABLED + EMBEDDING_MODEL_ID) and store the outcome in
    # the traces table (TRACES_TABLE_NAME, already set above).
    local.agent_enabled["incident_history"] ? {
      INCIDENT_HISTORY_AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.incident_history[0].agent_runtime_arn
      INCIDENT_HISTORY_ENABLED           = "true"
      EMBEDDING_MODEL_ID                 = var.embedding_model_id
    } : {},
    # Issue #33 — interactive incident page. When provisioned, the master signs
    # CloudFront URLs (private key in Secrets Manager) and links them in reports.
    # All of this infra lives in pages.tf, gated by var.enable_incident_page.
    var.enable_incident_page ? {
      INCIDENT_PAGE_ENABLED             = "true"
      INCIDENT_PAGE_BASE_URL            = "https://${aws_cloudfront_distribution.incident_page[0].domain_name}"
      CLOUDFRONT_KEY_PAIR_ID            = aws_cloudfront_public_key.incident_page[0].id
      CLOUDFRONT_PRIVATE_KEY_SECRET_ARN = aws_secretsmanager_secret.incident_page_private_key[0].arn
      INCIDENT_PAGE_URL_TTL_SECONDS     = tostring(var.incident_page_url_ttl_seconds)
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
    local.agent_enabled["incident_history"] ? {
      incident_history = aws_bedrockagentcore_agent_runtime.incident_history[0].agent_runtime_arn
    } : {},
  )
}
