###############################################################################
# State migration — re-key existing resources into module.sre_on_call.*
#
# These blocks let an existing local/remote state (resources at the old root
# addresses) move under the module with NO destroy/recreate. After the first
# apply they are inert and may be deleted. Counted resources move by base
# address; Terraform preserves their indices automatically.
###############################################################################

moved {
  from = aws_bedrockagentcore_agent_runtime.cloudwatch_logs
  to   = module.sre_on_call.aws_bedrockagentcore_agent_runtime.cloudwatch_logs
}

moved {
  from = aws_bedrockagentcore_agent_runtime.discord_scanner
  to   = module.sre_on_call.aws_bedrockagentcore_agent_runtime.discord_scanner
}

moved {
  from = aws_bedrockagentcore_agent_runtime.eks
  to   = module.sre_on_call.aws_bedrockagentcore_agent_runtime.eks
}

moved {
  from = aws_bedrockagentcore_agent_runtime.master
  to   = module.sre_on_call.aws_bedrockagentcore_agent_runtime.master
}

moved {
  from = aws_bedrockagentcore_agent_runtime.slack_scanner
  to   = module.sre_on_call.aws_bedrockagentcore_agent_runtime.slack_scanner
}

moved {
  from = aws_cloudwatch_log_group.lambda_adapter
  to   = module.sre_on_call.aws_cloudwatch_log_group.lambda_adapter
}

moved {
  from = aws_cloudwatch_metric_alarm.agentcore_errors
  to   = module.sre_on_call.aws_cloudwatch_metric_alarm.agentcore_errors
}

moved {
  from = aws_cloudwatch_metric_alarm.agentcore_latency_p95
  to   = module.sre_on_call.aws_cloudwatch_metric_alarm.agentcore_latency_p95
}

moved {
  from = aws_cloudwatch_metric_alarm.agentcore_throttles
  to   = module.sre_on_call.aws_cloudwatch_metric_alarm.agentcore_throttles
}

moved {
  from = aws_dynamodb_table.dedup
  to   = module.sre_on_call.aws_dynamodb_table.dedup
}

moved {
  from = aws_dynamodb_table.experiment_results
  to   = module.sre_on_call.aws_dynamodb_table.experiment_results
}

moved {
  from = aws_dynamodb_table.experiments
  to   = module.sre_on_call.aws_dynamodb_table.experiments
}

moved {
  from = aws_dynamodb_table.traces
  to   = module.sre_on_call.aws_dynamodb_table.traces
}

moved {
  from = aws_ecr_lifecycle_policy.agents
  to   = module.sre_on_call.aws_ecr_lifecycle_policy.agents
}

moved {
  from = aws_ecr_repository.agents
  to   = module.sre_on_call.aws_ecr_repository.agents
}

moved {
  from = aws_eks_access_entry.eks_agent
  to   = module.sre_on_call.aws_eks_access_entry.eks_agent
}

moved {
  from = aws_eks_access_policy_association.eks_agent_view
  to   = module.sre_on_call.aws_eks_access_policy_association.eks_agent_view
}

moved {
  from = aws_iam_role_policy_attachment.agentcore_full_access
  to   = module.sre_on_call.aws_iam_role_policy_attachment.agentcore_full_access
}

moved {
  from = aws_iam_role_policy.agentcore_runtime_exec
  to   = module.sre_on_call.aws_iam_role_policy.agentcore_runtime_exec
}

moved {
  from = aws_iam_role_policy.cloudwatch_logs_agent_logs
  to   = module.sre_on_call.aws_iam_role_policy.cloudwatch_logs_agent_logs
}

moved {
  from = aws_iam_role_policy.cloudwatch_logs_agent_metrics
  to   = module.sre_on_call.aws_iam_role_policy.cloudwatch_logs_agent_metrics
}

moved {
  from = aws_iam_role_policy.discord_scanner_agent_secrets
  to   = module.sre_on_call.aws_iam_role_policy.discord_scanner_agent_secrets
}

moved {
  from = aws_iam_role_policy.eks_agent_cluster_access
  to   = module.sre_on_call.aws_iam_role_policy.eks_agent_cluster_access
}

moved {
  from = aws_iam_role_policy.lambda_adapter_agentcore
  to   = module.sre_on_call.aws_iam_role_policy.lambda_adapter_agentcore
}

moved {
  from = aws_iam_role_policy.lambda_adapter_dynamodb
  to   = module.sre_on_call.aws_iam_role_policy.lambda_adapter_dynamodb
}

moved {
  from = aws_iam_role_policy.lambda_adapter_logs
  to   = module.sre_on_call.aws_iam_role_policy.lambda_adapter_logs
}

moved {
  from = aws_iam_role_policy.lambda_adapter_secrets
  to   = module.sre_on_call.aws_iam_role_policy.lambda_adapter_secrets
}

moved {
  from = aws_iam_role_policy.lambda_adapter_traces
  to   = module.sre_on_call.aws_iam_role_policy.lambda_adapter_traces
}

moved {
  from = aws_iam_role_policy.master_agent_a2a_invoke
  to   = module.sre_on_call.aws_iam_role_policy.master_agent_a2a_invoke
}

moved {
  from = aws_iam_role_policy.master_agent_agentcore_read
  to   = module.sre_on_call.aws_iam_role_policy.master_agent_agentcore_read
}

moved {
  from = aws_iam_role_policy.master_agent_dynamodb_describe
  to   = module.sre_on_call.aws_iam_role_policy.master_agent_dynamodb_describe
}

moved {
  from = aws_iam_role_policy.master_agent_experiment_results
  to   = module.sre_on_call.aws_iam_role_policy.master_agent_experiment_results
}

moved {
  from = aws_iam_role_policy.master_agent_secrets
  to   = module.sre_on_call.aws_iam_role_policy.master_agent_secrets
}

moved {
  from = aws_iam_role_policy.master_agent_traces
  to   = module.sre_on_call.aws_iam_role_policy.master_agent_traces
}

moved {
  from = aws_iam_role_policy.slack_scanner_agent_secrets
  to   = module.sre_on_call.aws_iam_role_policy.slack_scanner_agent_secrets
}

moved {
  from = aws_iam_role.cloudwatch_logs_agent
  to   = module.sre_on_call.aws_iam_role.cloudwatch_logs_agent
}

moved {
  from = aws_iam_role.discord_scanner_agent
  to   = module.sre_on_call.aws_iam_role.discord_scanner_agent
}

moved {
  from = aws_iam_role.eks_agent
  to   = module.sre_on_call.aws_iam_role.eks_agent
}

moved {
  from = aws_iam_role.lambda_adapter
  to   = module.sre_on_call.aws_iam_role.lambda_adapter
}

moved {
  from = aws_iam_role.master_agent
  to   = module.sre_on_call.aws_iam_role.master_agent
}

moved {
  from = aws_iam_role.slack_scanner_agent
  to   = module.sre_on_call.aws_iam_role.slack_scanner_agent
}

moved {
  from = aws_kms_alias.traces
  to   = module.sre_on_call.aws_kms_alias.traces
}

moved {
  from = aws_kms_key.traces
  to   = module.sre_on_call.aws_kms_key.traces
}

moved {
  from = aws_lambda_alias.lambda_adapter_live
  to   = module.sre_on_call.aws_lambda_alias.lambda_adapter_live
}

moved {
  from = aws_lambda_function_url.lambda_adapter
  to   = module.sre_on_call.aws_lambda_function_url.lambda_adapter
}

moved {
  from = aws_lambda_function.lambda_adapter
  to   = module.sre_on_call.aws_lambda_function.lambda_adapter
}

moved {
  from = aws_lambda_layer_version.deps
  to   = module.sre_on_call.aws_lambda_layer_version.deps
}

moved {
  from = aws_lambda_provisioned_concurrency_config.lambda_adapter
  to   = module.sre_on_call.aws_lambda_provisioned_concurrency_config.lambda_adapter
}

moved {
  from = aws_s3_bucket_lifecycle_configuration.traces
  to   = module.sre_on_call.aws_s3_bucket_lifecycle_configuration.traces
}

moved {
  from = aws_s3_bucket_public_access_block.traces
  to   = module.sre_on_call.aws_s3_bucket_public_access_block.traces
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.traces
  to   = module.sre_on_call.aws_s3_bucket_server_side_encryption_configuration.traces
}

moved {
  from = aws_s3_bucket_versioning.traces
  to   = module.sre_on_call.aws_s3_bucket_versioning.traces
}

moved {
  from = aws_s3_bucket.traces
  to   = module.sre_on_call.aws_s3_bucket.traces
}

moved {
  from = aws_secretsmanager_secret.discord_bot_token
  to   = module.sre_on_call.aws_secretsmanager_secret.discord_bot_token
}

moved {
  from = aws_secretsmanager_secret.discord_public_key
  to   = module.sre_on_call.aws_secretsmanager_secret.discord_public_key
}

moved {
  from = aws_secretsmanager_secret.slack_bot_token
  to   = module.sre_on_call.aws_secretsmanager_secret.slack_bot_token
}

moved {
  from = aws_secretsmanager_secret.slack_signing_secret
  to   = module.sre_on_call.aws_secretsmanager_secret.slack_signing_secret
}

moved {
  from = aws_security_group_rule.cluster_ingress_from_agent
  to   = module.sre_on_call.aws_security_group_rule.cluster_ingress_from_agent
}

moved {
  from = aws_security_group.eks_agent
  to   = module.sre_on_call.aws_security_group.eks_agent
}

moved {
  from = aws_sns_topic_subscription.agentcore_alarms_email
  to   = module.sre_on_call.aws_sns_topic_subscription.agentcore_alarms_email
}

moved {
  from = aws_sns_topic.agentcore_alarms
  to   = module.sre_on_call.aws_sns_topic.agentcore_alarms
}

moved {
  from = null_resource.lambda_function_stage
  to   = module.sre_on_call.null_resource.lambda_function_stage
}

moved {
  from = null_resource.lambda_layer_build
  to   = module.sre_on_call.null_resource.lambda_layer_build
}

