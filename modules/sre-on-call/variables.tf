###############################################################################
# sre-on-call — Input Variables
###############################################################################

# ── General ──────────────────────────────────────────────────────────────────

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

# ── EKS Cluster (existing, used for the EKS agent's VPC + auth) ─────────────

variable "eks_cluster_name" {
  description = "Name of the existing EKS cluster the EKS agent will inspect. Required when the eks agent is enabled in config.yaml; ignored otherwise."
  type        = string
  default     = ""
}


# ── Lambda ───────────────────────────────────────────────────────────────────

variable "lambda_memory_size" {
  description = "Memory allocation for the Lambda adapter function (MB)"
  type        = number
  default     = 256
}

variable "lambda_timeout" {
  description = "Timeout for the Lambda adapter function (seconds)"
  type        = number
  default     = 30
}

variable "lambda_provisioned_concurrency" {
  description = "Number of provisioned concurrent executions for cold start mitigation"
  type        = number
  default     = 1
}


# ── Module source wiring ─────────────────────────────────────────────────────

variable "config_path" {
  description = "Path to config.yaml listing each agent's enabled skills/MCPs. Read at plan time to decide which specialized agents are deployed, and published to SSM (aws_ssm_parameter.config) for runtimes to read at cold-start."
  type        = string
}

variable "source_root" {
  description = "Filesystem root containing lambda_adapter/, shared/, pyproject.toml and .venv/ — used to build the Lambda function zip and dependency layer. Defaults to the calling root module's directory."
  type        = string
  default     = ""
}


# ── Change-correlation: Grafana deploy-annotation source (rec #6) ─────────────

variable "enable_grafana_change_source" {
  description = "Provision a Grafana service-account token secret and grant the Master_Agent read access to it, so the master can query Grafana deploy annotations (\"what changed?\") via an MCP server. Opt-in (default off) until deployment-annotator is confirmed running. Only creates the empty secret + IAM grant; the secret value must be populated out-of-band and the grafana MCP block added to config.yaml by the orchestrator."
  type        = bool
  default     = false
}
