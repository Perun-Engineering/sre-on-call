###############################################################################
# sre-on-call — IAM Roles (Least Privilege)
#
# Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
#   - Lambda_Adapter: Slack/Discord secret access, AgentCore invoke,
#     DynamoDB dedup + experiments access, CloudWatch Logs
#   - Master_Agent: A2A invoke permission, Slack/Discord token access,
#     experiment results write
#   - Slack_Scanner_Agent: Slack Bot Token access
#   - Discord_Scanner_Agent: Discord Bot Token access
#   - Prometheus_Agent: VPC connectivity only, no AWS service permissions
#   - CloudWatch_Logs_Agent: logs:StartQuery, logs:GetQueryResults,
#     logs:DescribeLogGroups
#   - EKS_Agent: IAM role mapped to K8s read-only ClusterRole, VPC access
###############################################################################

# ── Data Sources ─────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}

# ── 1. Lambda_Adapter Role (Requirement 9.1) ────────────────────────────────

resource "aws_iam_role" "lambda_adapter" {
  name = "${var.project_name}-${var.environment}-lambda-adapter"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-lambda-adapter-role"
  }
}

resource "aws_iam_role_policy" "lambda_adapter_secrets" {
  name = "secrets-access"
  role = aws_iam_role.lambda_adapter.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSlackAndDiscordSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.slack_bot_token.arn,
          aws_secretsmanager_secret.slack_signing_secret.arn,
          aws_secretsmanager_secret.discord_public_key.arn,
          aws_secretsmanager_secret.discord_bot_token.arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda_adapter_dynamodb" {
  name = "dynamodb-access"
  role = aws_iam_role.lambda_adapter.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBDedup"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem"
        ]
        Resource = aws_dynamodb_table.dedup.arn
      },
      {
        Sid    = "DynamoDBExperimentsRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Scan"
        ]
        Resource = aws_dynamodb_table.experiments.arn
      }
    ]
  })
}

# AgentCore invoke permission for triggering the Master_Agent.
resource "aws_iam_role_policy" "lambda_adapter_agentcore" {
  name = "agentcore-invoke"
  role = aws_iam_role.lambda_adapter.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeMasterAgent"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        Resource = [
          aws_bedrockagentcore_agent_runtime.master.agent_runtime_arn,
          "${aws_bedrockagentcore_agent_runtime.master.agent_runtime_arn}/*",
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "lambda_adapter_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.lambda_adapter.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda_adapter.arn}:*"
      }
    ]
  })
}

# ── 2. Master_Agent Role (Requirement 9.2) ──────────────────────────────────
#
# The Master_Agent needs:
#   - A2A invoke permission to call the five Specialized_Agents
#   - Secrets Manager read access for Slack/Discord tokens (posting reports)
#   - DynamoDB write for experiment results

resource "aws_iam_role" "master_agent" {
  name = "${var.project_name}-${var.environment}-master-agent"

  # NOTE: Replace with the actual AgentCore Runtime service principal.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-master-agent-role"
  }
}

resource "aws_iam_role_policy" "master_agent_a2a_invoke" {
  # Skip the policy entirely when no specialized agents are enabled —
  # IAM rejects a statement with an empty Resource list.
  count = anytrue([for v in local.agent_enabled : v]) ? 1 : 0

  name = "a2a-invoke"
  role = aws_iam_role.master_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeSpecializedAgents"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        Resource = flatten([
          for arn in concat(
            local.agent_enabled["slack_scanner"] ? [aws_bedrockagentcore_agent_runtime.slack_scanner[0].agent_runtime_arn] : [],
            local.agent_enabled["discord_scanner"] ? [aws_bedrockagentcore_agent_runtime.discord_scanner[0].agent_runtime_arn] : [],
            local.agent_enabled["cloudwatch_logs"] ? [aws_bedrockagentcore_agent_runtime.cloudwatch_logs[0].agent_runtime_arn] : [],
            local.agent_enabled["eks"] ? [aws_bedrockagentcore_agent_runtime.eks[0].agent_runtime_arn] : [],
          ) : [arn, "${arn}/*"]
        ])
      }
    ]
  })
}

# Read AgentCore runtime status for the master's section in the /status
# snapshot ("READY" / "CREATING" / "FAILED" / etc. per deployed runtime).
# Same resource set as the a2a-invoke policy so the master can introspect
# every runtime it is allowed to call.
resource "aws_iam_role_policy" "master_agent_agentcore_read" {
  count = anytrue([for v in local.agent_enabled : v]) ? 1 : 0

  name = "agentcore-runtime-read"
  role = aws_iam_role.master_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadAgentRuntimeStatus"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetAgentRuntime",
        ]
        Resource = flatten([
          for arn in concat(
            local.agent_enabled["slack_scanner"] ? [aws_bedrockagentcore_agent_runtime.slack_scanner[0].agent_runtime_arn] : [],
            local.agent_enabled["discord_scanner"] ? [aws_bedrockagentcore_agent_runtime.discord_scanner[0].agent_runtime_arn] : [],
            local.agent_enabled["cloudwatch_logs"] ? [aws_bedrockagentcore_agent_runtime.cloudwatch_logs[0].agent_runtime_arn] : [],
            local.agent_enabled["eks"] ? [aws_bedrockagentcore_agent_runtime.eks[0].agent_runtime_arn] : [],
          ) : [arn, "${arn}/*"]
        ])
      }
    ]
  })
}

# DynamoDB reachability check for the master's section in the /status
# snapshot. DescribeTable is read-only and surfaces TableStatus so the
# operator can see "ACTIVE" / "UPDATING" / etc. without granting any
# data-plane permissions on these tables to the master role.
resource "aws_iam_role_policy" "master_agent_dynamodb_describe" {
  name = "dynamodb-describe"
  role = aws_iam_role.master_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DescribeStateTablesForStatus"
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeTable"
        ]
        Resource = [
          aws_dynamodb_table.dedup.arn,
          aws_dynamodb_table.experiments.arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "master_agent_secrets" {
  name = "secrets-access"
  role = aws_iam_role.master_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadChatTokens"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.slack_bot_token.arn,
          aws_secretsmanager_secret.discord_bot_token.arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "master_agent_experiment_results" {
  name = "experiment-results-write"
  role = aws_iam_role.master_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteExperimentResults"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem"
        ]
        Resource = aws_dynamodb_table.experiment_results.arn
      }
    ]
  })
}

# ── 3. Slack_Scanner_Agent Role (Requirement 9.3) ───────────────────────────

resource "aws_iam_role" "slack_scanner_agent" {
  count = local.agent_enabled["slack_scanner"] ? 1 : 0

  name = "${var.project_name}-${var.environment}-slack-scanner-agent"

  # NOTE: Replace with the actual AgentCore Runtime service principal.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-slack-scanner-agent-role"
  }
}

resource "aws_iam_role_policy" "slack_scanner_agent_secrets" {
  count = local.agent_enabled["slack_scanner"] ? 1 : 0

  name = "secrets-access"
  role = aws_iam_role.slack_scanner_agent[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSlackBotToken"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.slack_bot_token.arn
        ]
      }
    ]
  })
}

# ── 4. Discord_Scanner_Agent Role ───────────────────────────────────────────
#
# The Discord_Scanner_Agent needs:
#   - Secrets Manager read access for Discord Bot Token

resource "aws_iam_role" "discord_scanner_agent" {
  count = local.agent_enabled["discord_scanner"] ? 1 : 0

  name = "${var.project_name}-${var.environment}-discord-scanner-agent"

  # NOTE: Replace with the actual AgentCore Runtime service principal.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-discord-scanner-agent-role"
  }
}

resource "aws_iam_role_policy" "discord_scanner_agent_secrets" {
  count = local.agent_enabled["discord_scanner"] ? 1 : 0

  name = "secrets-access"
  role = aws_iam_role.discord_scanner_agent[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadDiscordBotToken"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.discord_bot_token.arn
        ]
      }
    ]
  })
}

# ── 5. (deferred) Prometheus_Agent Role — agent not deployed in this iteration. ──

# ── 6. CloudWatch_Logs_Agent Role (Requirement 9.5) ─────────────────────────

resource "aws_iam_role" "cloudwatch_logs_agent" {
  count = local.agent_enabled["cloudwatch_logs"] ? 1 : 0

  name = "${var.project_name}-${var.environment}-cloudwatch-logs-agent"

  # NOTE: Replace with the actual AgentCore Runtime service principal.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cloudwatch-logs-agent-role"
  }
}

resource "aws_iam_role_policy" "cloudwatch_logs_agent_logs" {
  count = local.agent_enabled["cloudwatch_logs"] ? 1 : 0

  name = "cloudwatch-logs-query"
  role = aws_iam_role.cloudwatch_logs_agent[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogsQuery"
        Effect = "Allow"
        Action = [
          "logs:StartQuery",
          "logs:GetQueryResults",
          "logs:DescribeLogGroups"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"
      }
    ]
  })
}

# Read CloudWatch metrics for the /status snapshot (top log groups by
# IncomingBytes in the last 15 min). The cloudwatch:GetMetricData action
# does NOT support resource-level permissions per the AWS service docs —
# this is the AWS-recommended way to grant it.
resource "aws_iam_role_policy" "cloudwatch_logs_agent_metrics" {
  count = local.agent_enabled["cloudwatch_logs"] ? 1 : 0

  name = "cloudwatch-metrics-read"
  role = aws_iam_role.cloudwatch_logs_agent[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "GetMetricDataForLogVolumeRanking"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData"
        ]
        Resource = "*"
      }
    ]
  })
}

# ── 7. EKS_Agent Role (Requirement 9.6) ─────────────────────────────────────

resource "aws_iam_role" "eks_agent" {
  count = local.agent_enabled["eks"] ? 1 : 0

  name = "${var.project_name}-${var.environment}-eks-agent"

  # NOTE: Replace with the actual AgentCore Runtime service principal.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "bedrock-agentcore.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-eks-agent-role"
  }
}

resource "aws_iam_role_policy" "eks_agent_cluster_access" {
  count = local.agent_enabled["eks"] ? 1 : 0

  name = "eks-cluster-access"
  role = aws_iam_role.eks_agent[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EKSDescribeCluster"
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster"
        ]
        Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/*"
      }
    ]
  })
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "lambda_adapter_role_arn" {
  description = "ARN of the Lambda_Adapter IAM role"
  value       = aws_iam_role.lambda_adapter.arn
}

output "master_agent_role_arn" {
  description = "ARN of the Master_Agent IAM role"
  value       = aws_iam_role.master_agent.arn
}

output "slack_scanner_agent_role_arn" {
  description = "ARN of the Slack_Scanner_Agent IAM role (null when slack_scanner is disabled)"
  value       = try(aws_iam_role.slack_scanner_agent[0].arn, null)
}

output "discord_scanner_agent_role_arn" {
  description = "ARN of the Discord_Scanner_Agent IAM role (null when discord_scanner is disabled)"
  value       = try(aws_iam_role.discord_scanner_agent[0].arn, null)
}

output "cloudwatch_logs_agent_role_arn" {
  description = "ARN of the CloudWatch_Logs_Agent IAM role (null when cloudwatch_logs is disabled)"
  value       = try(aws_iam_role.cloudwatch_logs_agent[0].arn, null)
}

output "eks_agent_role_arn" {
  description = "ARN of the EKS_Agent IAM role (null when eks is disabled)"
  value       = try(aws_iam_role.eks_agent[0].arn, null)
}
