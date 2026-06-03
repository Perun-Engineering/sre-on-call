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
  description = "Path to config.yaml listing each agent's enabled skills/MCPs. Read at plan time to decide which specialized agents are deployed; must match the config.yaml baked into the agent images."
  type        = string
}

variable "source_root" {
  description = "Filesystem root containing lambda_adapter/, shared/, pyproject.toml and .venv/ — used to build the Lambda function zip and dependency layer. Defaults to the calling root module's directory."
  type        = string
  default     = ""
}
