###############################################################################
# Re-export the module's outputs at the root so existing tooling
# (docs, scripts: `terraform output -raw lambda_function_url`, etc.) keeps
# working against this root unchanged.
###############################################################################

output "agentcore_alarm_names" {
  value = module.sre_on_call.agentcore_alarm_names
}

output "agentcore_alarms_topic_arn" {
  value = module.sre_on_call.agentcore_alarms_topic_arn
}

output "cloudwatch_logs_agent_role_arn" {
  value = module.sre_on_call.cloudwatch_logs_agent_role_arn
}

output "dedup_table_arn" {
  value = module.sre_on_call.dedup_table_arn
}

output "dedup_table_name" {
  value = module.sre_on_call.dedup_table_name
}

output "discord_bot_token_secret_arn" {
  value = module.sre_on_call.discord_bot_token_secret_arn
}

output "discord_public_key_secret_arn" {
  value = module.sre_on_call.discord_public_key_secret_arn
}

output "discord_scanner_agent_role_arn" {
  value = module.sre_on_call.discord_scanner_agent_role_arn
}

output "ecr_registry_url" {
  value = module.sre_on_call.ecr_registry_url
}

output "ecr_repository_uris" {
  value = module.sre_on_call.ecr_repository_uris
}

output "eks_agent_role_arn" {
  value = module.sre_on_call.eks_agent_role_arn
}

output "experiment_results_table_arn" {
  value = module.sre_on_call.experiment_results_table_arn
}

output "experiment_results_table_name" {
  value = module.sre_on_call.experiment_results_table_name
}

output "experiments_table_arn" {
  value = module.sre_on_call.experiments_table_arn
}

output "experiments_table_name" {
  value = module.sre_on_call.experiments_table_name
}

output "lambda_adapter_role_arn" {
  value = module.sre_on_call.lambda_adapter_role_arn
}

output "lambda_alias_arn" {
  value = module.sre_on_call.lambda_alias_arn
}

output "lambda_function_arn" {
  value = module.sre_on_call.lambda_function_arn
}

output "lambda_function_name" {
  value = module.sre_on_call.lambda_function_name
}

output "lambda_function_url" {
  value = module.sre_on_call.lambda_function_url
}

output "master_agent_role_arn" {
  value = module.sre_on_call.master_agent_role_arn
}

output "master_agent_runtime_arn" {
  value = module.sre_on_call.master_agent_runtime_arn
}

output "slack_bot_token_secret_arn" {
  value = module.sre_on_call.slack_bot_token_secret_arn
}

output "slack_scanner_agent_role_arn" {
  value = module.sre_on_call.slack_scanner_agent_role_arn
}

output "slack_signing_secret_arn" {
  value = module.sre_on_call.slack_signing_secret_arn
}

output "specialized_agent_runtime_arns" {
  value = module.sre_on_call.specialized_agent_runtime_arns
}

output "traces_bucket_arn" {
  value = module.sre_on_call.traces_bucket_arn
}

output "traces_bucket_name" {
  value = module.sre_on_call.traces_bucket_name
}

output "traces_kms_key_arn" {
  value = module.sre_on_call.traces_kms_key_arn
}

output "traces_table_arn" {
  value = module.sre_on_call.traces_table_arn
}

output "traces_table_name" {
  value = module.sre_on_call.traces_table_name
}

