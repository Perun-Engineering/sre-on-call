###############################################################################
# sre-on-call — Complete example / reference root
#
# A thin consumer of the reusable module at ../../modules/sre-on-call. It owns
# the provider + backend (which the module must not declare) and supplies the
# Lambda build source + agent composition from the repo root (two levels up).
#
# This doubles as a migration target for the original flat `terraform/` root:
# the moved.tf blocks re-key existing state under module.sre_on_call.* with no
# destroy/recreate. See README.md.
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.21"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      ManagedBy   = "terraform"
      Environment = var.environment
    }
  }
}

module "sre_on_call" {
  source = "../../modules/sre-on-call"

  aws_region                = var.aws_region
  environment               = var.environment
  project_name              = var.project_name
  eks_cluster_name          = var.eks_cluster_name
  agent_container_registry  = var.agent_container_registry
  agent_image_tag           = var.agent_image_tag
  model_id                  = var.model_id
  routing_model_id          = var.routing_model_id
  synthesis_model_id        = var.synthesis_model_id
  followup_model_id         = var.followup_model_id
  enable_bedrock_guardrail  = var.enable_bedrock_guardrail
  slack_trigger_emoji       = var.slack_trigger_emoji
  alarm_email_subscriptions = var.alarm_email_subscriptions

  # A/B control-arm seams (#29) — empty for normal deploys.
  experiment_results_table_name  = var.experiment_results_table_name
  additional_master_runtime_arns = var.additional_master_runtime_arns

  # Build the Lambda zip/layer and read agent composition from the repo root.
  source_root = "${path.root}/../.."
  config_path = "${path.root}/../../config.yaml"
}
