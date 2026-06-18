###############################################################################
# sre-on-call complete example — Input Variables
#
# Defaults mirror the module's own defaults so this root is runnable with only
# `agent_container_registry` supplied (the one required input).
###############################################################################

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Project name used as a prefix for resource naming"
  type        = string
  default     = "sre-on-call"
}

variable "eks_cluster_name" {
  description = "Name of the existing EKS cluster the EKS agent inspects. Required when the eks agent is enabled in config.yaml; ignored otherwise."
  type        = string
  default     = ""
}

variable "experiment_results_table_name" {
  description = "A/B control-arm override: results table the master writes to. Empty (default) = this stack's own table. See modules/sre-on-call/dynamodb_experiments.tf and docs/scorecard-runbook.md."
  type        = string
  default     = ""
}

variable "additional_master_runtime_arns" {
  description = "Extra master AgentCore runtime ARNs this stack's intake Lambda may invoke (A/B control arm). See docs/scorecard-runbook.md."
  type        = list(string)
  default     = []
}

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

variable "routing_model_id" {
  description = "Bedrock model ID for the master's pre-dispatch routing call (#28). Empty falls back to MODEL_ID. Set to a Sonnet-class model for better triage judgment."
  type        = string
  default     = ""
}

variable "synthesis_model_id" {
  description = "Bedrock model ID for the master's Analysis synthesis call (#27). Empty falls back to MODEL_ID. Set to a Sonnet-class model for higher-quality root-cause reasoning."
  type        = string
  default     = ""
}

variable "followup_model_id" {
  description = "Bedrock model ID for the master's Stage 2 follow-up planning call (#28). Empty falls back to MODEL_ID."
  type        = string
  default     = ""
}

variable "enable_bedrock_guardrail" {
  description = "Attach a Bedrock Guardrail (prompt-attack filtering) to every agent's model invocations. Defence-in-depth; adds per-call latency and cost. Opt-in."
  type        = bool
  default     = false
}

variable "slack_trigger_emoji" {
  description = "Emoji name (without colons) whose reaction on a Slack message triggers an investigation."
  type        = string
  default     = "sre-on-call"
}

variable "alarm_email_subscriptions" {
  description = "Email addresses subscribed to the AgentCore CloudWatch alarm SNS topic"
  type        = list(string)
  default     = []
}
