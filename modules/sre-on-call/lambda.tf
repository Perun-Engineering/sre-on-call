###############################################################################
# sre-on-call — Lambda Function & Function URL
#
# Requirements: 1.2, 9.1
#   - Lambda_Adapter responds to Slack/Discord within 3 seconds (provisioned concurrency)
#   - Lambda_Adapter exposed via public function URL for webhook ingestion
#   - Lambda_Adapter has access to Slack/Discord secrets, DynamoDB tables,
#     and AgentCore invoke permission
###############################################################################

# ── Build: Function package (lambda_adapter/ + shared/) ─────────────────────
# The runtime needs both packages; archive_file alone can only zip a single
# directory, so a small build step assembles the staging directory first.

resource "null_resource" "lambda_function_stage" {
  triggers = {
    # Re-stage when any source file changes
    sources = sha1(join("", [
      for f in fileset(var.source_root, "{lambda_adapter,shared}/**/*.py") :
      filesha1("${var.source_root}/${f}")
    ]))
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      STAGE='${path.module}/.build/lambda_function'
      rm -rf "$STAGE"
      mkdir -p "$STAGE"
      cp -R '${var.source_root}/lambda_adapter' "$STAGE/lambda_adapter"
      cp -R '${var.source_root}/shared' "$STAGE/shared"
      find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
    EOT
  }
}

data "archive_file" "lambda_adapter" {
  type        = "zip"
  source_dir  = "${path.module}/.build/lambda_function"
  output_path = "${path.module}/.build/lambda_adapter.zip"

  depends_on = [null_resource.lambda_function_stage]
}

# ── Build: Lambda layer with Python deps ────────────────────────────────────
# Layer size limit (unzipped) is 250 MB. We keep this lean by installing
# only what the Lambda actually needs — strands and the agents are NOT in
# this layer (they ship inside the AgentCore containers).

resource "null_resource" "lambda_layer_build" {
  triggers = {
    pyproject = filesha1("${var.source_root}/pyproject.toml")
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      LAYER='${path.module}/.build/layer'
      rm -rf "$LAYER"
      mkdir -p "$LAYER/python"
      '${var.source_root}/.venv/bin/python' -m pip install --quiet --no-cache-dir \
          --platform manylinux2014_x86_64 \
          --implementation cp \
          --python-version 3.12 \
          --only-binary=:all: \
          --target "$LAYER/python" \
          "slack_sdk>=3.27.0" \
          "boto3>=1.34.0" \
          "cryptography>=42.0.0"
      find "$LAYER" -type d -name __pycache__ -prune -exec rm -rf {} +
      find "$LAYER" -type d -name "*.dist-info" -prune -exec rm -rf {} +
    EOT
  }
}

data "archive_file" "lambda_layer" {
  type        = "zip"
  source_dir  = "${path.module}/.build/layer"
  output_path = "${path.module}/.build/lambda_layer.zip"

  depends_on = [null_resource.lambda_layer_build]
}

resource "aws_lambda_layer_version" "deps" {
  layer_name          = "${var.project_name}-${var.environment}-deps"
  filename            = data.archive_file.lambda_layer.output_path
  source_code_hash    = data.archive_file.lambda_layer.output_base64sha256
  compatible_runtimes = ["python3.12"]
}

# ── CloudWatch Log Group ────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "lambda_adapter" {
  name              = "/aws/lambda/${var.project_name}-${var.environment}-lambda-adapter"
  retention_in_days = 30

  tags = {
    Name = "${var.project_name}-lambda-adapter-logs"
  }
}

# ── Lambda Function ─────────────────────────────────────────────────────────

resource "aws_lambda_function" "lambda_adapter" {
  function_name = "${var.project_name}-${var.environment}-lambda-adapter"
  description   = "Slack/Discord webhook adapter — verifies signatures, deduplicates, and invokes Master Agent"

  filename         = data.archive_file.lambda_adapter.output_path
  source_code_hash = data.archive_file.lambda_adapter.output_base64sha256

  runtime = "python3.12"
  handler = "lambda_adapter.handler.lambda_handler"

  role        = aws_iam_role.lambda_adapter.arn
  memory_size = var.lambda_memory_size
  timeout     = var.lambda_timeout

  publish = true # Required for provisioned concurrency (creates versioned deployments)

  layers = [aws_lambda_layer_version.deps.arn]

  environment {
    variables = {
      SLACK_SIGNING_SECRET     = aws_secretsmanager_secret.slack_signing_secret.arn
      SLACK_BOT_TOKEN          = aws_secretsmanager_secret.slack_bot_token.arn
      DISCORD_PUBLIC_KEY       = aws_secretsmanager_secret.discord_public_key.arn
      DISCORD_BOT_TOKEN        = aws_secretsmanager_secret.discord_bot_token.arn
      DEDUP_TABLE_NAME         = aws_dynamodb_table.dedup.name
      EXPERIMENTS_TABLE_NAME   = aws_dynamodb_table.experiments.name
      MASTER_AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.master.agent_runtime_arn
      TRACES_BUCKET_NAME       = aws_s3_bucket.traces.bucket
      TRACES_TABLE_NAME        = aws_dynamodb_table.traces.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda_adapter]

  tags = {
    Name = "${var.project_name}-lambda-adapter"
  }
}

# ── Lambda Alias (for provisioned concurrency) ──────────────────────────────

resource "aws_lambda_alias" "lambda_adapter_live" {
  name             = "live"
  description      = "Live alias for provisioned concurrency"
  function_name    = aws_lambda_function.lambda_adapter.function_name
  function_version = aws_lambda_function.lambda_adapter.version

  lifecycle {
    # When the alias moves to a newly published version, AWS auto-adds
    # routing weights to the previous version to keep it serving until
    # provisioned concurrency is ready on the new one. Ignore that drift
    # here; null_resource.clear_alias_weights performs the cutover instead.
    ignore_changes = [routing_config]
  }
}

# Cut the live alias fully over to the freshly published version. AWS's
# provisioned-concurrency safe-deployment leaves AdditionalVersionWeights on
# the previous version, which (a) keeps old code serving and (b) makes
# PutProvisionedConcurrencyConfig fail ("Alias with weights can not be used
# with Provisioned Concurrency"). Clearing the weights resolves both. Keyed on
# the function version so it only runs when a new version is published.
resource "null_resource" "clear_alias_weights" {
  triggers = {
    function_version = aws_lambda_function.lambda_adapter.version
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      aws lambda update-alias \
        --region '${var.aws_region}' \
        --function-name '${aws_lambda_function.lambda_adapter.function_name}' \
        --name '${aws_lambda_alias.lambda_adapter_live.name}' \
        --routing-config '{}' >/dev/null
    EOT
  }

  depends_on = [aws_lambda_alias.lambda_adapter_live]
}

# ── Provisioned Concurrency ─────────────────────────────────────────────────
# Requirement 1.2: Lambda must respond within 3 seconds. Provisioned
# concurrency keeps warm execution environments ready, eliminating cold
# start latency that could breach the webhook acknowledgment deadline.

resource "aws_lambda_provisioned_concurrency_config" "lambda_adapter" {
  function_name                     = aws_lambda_function.lambda_adapter.function_name
  qualifier                         = aws_lambda_alias.lambda_adapter_live.name
  provisioned_concurrent_executions = var.lambda_provisioned_concurrency

  # Weights must be cleared before this runs — AWS rejects provisioned
  # concurrency on a weighted alias.
  depends_on = [null_resource.clear_alias_weights]
}

# ── Lambda Function URL (Public Endpoint for Webhook Ingestion) ─────────────
# The function URL uses auth_type NONE because both Slack and Discord
# authenticate via cryptographic signature verification inside the Lambda
# handler itself (HMAC-SHA256 for Slack, Ed25519 for Discord).

resource "aws_lambda_function_url" "lambda_adapter" {
  function_name      = aws_lambda_function.lambda_adapter.function_name
  qualifier          = aws_lambda_alias.lambda_adapter_live.name
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["POST"]
    # AWS normalizes Lambda function-URL CORS header names to lowercase on read,
    # so declare them lowercase to avoid a perpetual case-only plan diff.
    allow_headers = [
      "content-type",
      # Slack headers
      "x-slack-signature",
      "x-slack-request-timestamp",
      # Discord headers
      "x-signature-ed25519",
      "x-signature-timestamp",
    ]
    max_age = 86400
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "lambda_function_name" {
  description = "Name of the Lambda adapter function"
  value       = aws_lambda_function.lambda_adapter.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda adapter function"
  value       = aws_lambda_function.lambda_adapter.arn
}

output "lambda_function_url" {
  description = "Public function URL endpoint for Slack/Discord webhooks"
  value       = aws_lambda_function_url.lambda_adapter.function_url
}

output "lambda_alias_arn" {
  description = "ARN of the live Lambda alias (with provisioned concurrency)"
  value       = aws_lambda_alias.lambda_adapter_live.arn
}
