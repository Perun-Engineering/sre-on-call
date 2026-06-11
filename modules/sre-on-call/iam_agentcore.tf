###############################################################################
# AgentCore Runtime Execution permissions for all 5 agent roles
#
# Each runtime needs to: pull its container image from ECR, write logs to
# /aws/bedrock-agentcore/runtimes/*, emit X-Ray traces and CloudWatch
# metrics, invoke Bedrock models, and obtain workload tokens.
###############################################################################

locals {
  # Master is always present; specialized roles are folded in only when enabled.
  agent_role_names = merge(
    { master = aws_iam_role.master_agent.name },
    local.agent_enabled["slack_scanner"] ? { slack_scanner = aws_iam_role.slack_scanner_agent[0].name } : {},
    local.agent_enabled["discord_scanner"] ? { discord_scanner = aws_iam_role.discord_scanner_agent[0].name } : {},
    local.agent_enabled["cloudwatch_logs"] ? { cloudwatch_logs = aws_iam_role.cloudwatch_logs_agent[0].name } : {},
    local.agent_enabled["eks"] ? { eks = aws_iam_role.eks_agent[0].name } : {},
    local.agent_enabled["incident_history"] ? { incident_history = aws_iam_role.incident_history_agent[0].name } : {},
  )
}

# ── Managed policy attachment ──────────────────────────────────────────────

resource "aws_iam_role_policy_attachment" "agentcore_full_access" {
  for_each = local.agent_role_names

  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/BedrockAgentCoreFullAccess"
}

# ── Shared inline policy ───────────────────────────────────────────────────

data "aws_iam_policy_document" "agentcore_runtime_exec" {
  statement {
    sid     = "ECRImageAccess"
    effect  = "Allow"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [
      for r in aws_ecr_repository.agents : r.arn
    ]
  }

  statement {
    sid       = "ECRTokenAccess"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "CloudWatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*",
    ]
  }

  statement {
    sid    = "XRayTracing"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "CloudWatchMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }

  statement {
    sid    = "BedrockModelInvocation"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      # Required when var.enable_bedrock_guardrail binds the model to a
      # guardrail (the InvokeModel call then carries a guardrail identifier).
      # Harmless no-op when no guardrail is attached.
      "bedrock:ApplyGuardrail",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "SSMConfigParameter"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.config.arn]
  }

  statement {
    sid    = "GetAgentAccessToken"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/*",
    ]
  }
}

resource "aws_iam_role_policy" "agentcore_runtime_exec" {
  for_each = local.agent_role_names

  name   = "agentcore-runtime-exec"
  role   = each.value
  policy = data.aws_iam_policy_document.agentcore_runtime_exec.json
}
