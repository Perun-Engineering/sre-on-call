###############################################################################
# sre-on-call — Secrets Manager Resources
#
# Requirements: 9.1, 9.2, 9.3
#   - Lambda_Adapter needs Slack/Discord tokens for posting acknowledgments
#   - Master_Agent needs Slack/Discord tokens for posting reports
#   - Slack_Scanner_Agent needs Slack Bot Token with channels:history scope
#   - Discord_Scanner_Agent needs Discord Bot Token
#   - Lambda_Adapter needs Slack Signing Secret and Discord Public Key for
#     signature verification
#
# These secrets are created as empty placeholders. Actual values must be
# populated out-of-band (e.g., via AWS Console or CLI) after deployment.
###############################################################################

# ── Slack Secrets ────────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "slack_bot_token" {
  count = local.slack_enabled ? 1 : 0

  name        = "${var.project_name}-${var.environment}-slack-bot-token"
  description = "Slack Bot OAuth token (used by Lambda_Adapter, Master_Agent, Slack_Scanner_Agent)"

  tags = {
    Name = "${var.project_name}-slack-bot-token"
  }
}

resource "aws_secretsmanager_secret" "slack_signing_secret" {
  count = local.slack_enabled ? 1 : 0

  name        = "${var.project_name}-${var.environment}-slack-signing-secret"
  description = "Slack Signing Secret for verifying webhook signatures (used by Lambda_Adapter)"

  tags = {
    Name = "${var.project_name}-slack-signing-secret"
  }
}

# ── Discord Secrets ──────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "discord_public_key" {
  count = local.discord_enabled ? 1 : 0

  name        = "${var.project_name}-${var.environment}-discord-public-key"
  description = "Discord application public key for Ed25519 signature verification (used by Lambda_Adapter)"

  tags = {
    Name = "${var.project_name}-discord-public-key"
  }
}

resource "aws_secretsmanager_secret" "discord_bot_token" {
  count = local.discord_enabled ? 1 : 0

  name        = "${var.project_name}-${var.environment}-discord-bot-token"
  description = "Discord Bot token (used by Lambda_Adapter, Master_Agent, Discord_Scanner_Agent)"

  tags = {
    Name = "${var.project_name}-discord-bot-token"
  }
}

# ── Grafana Change-Correlation Secret (rec #6, opt-in) ───────────────────────
#
# Holds a Grafana service-account token the Master_Agent uses to query deploy
# annotations ("what changed?") via the grafana MCP server. Created empty —
# populate out-of-band like the chat secrets above. Gated by
# var.enable_grafana_change_source (default off) so a normal deployment carries
# no Grafana footprint. The grafana MCP block in config.yaml references this
# token through an env var (api_key:<ENV_VAR>); wiring that env var + the MCP
# block is the orchestrator's responsibility, not this module.

resource "aws_secretsmanager_secret" "grafana_token" {
  count = var.enable_grafana_change_source ? 1 : 0

  name        = "${var.project_name}-${var.environment}-grafana-token"
  description = "Grafana service-account token for deploy-annotation change correlation (used by Master_Agent)"

  tags = {
    Name = "${var.project_name}-grafana-token"
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "grafana_token_secret_arn" {
  description = "ARN of the Grafana service-account token secret (null when the Grafana change source is disabled)"
  value       = try(aws_secretsmanager_secret.grafana_token[0].arn, null)
}

output "slack_bot_token_secret_arn" {
  description = "ARN of the Slack Bot Token secret (null when Slack is disabled)"
  value       = try(aws_secretsmanager_secret.slack_bot_token[0].arn, null)
}

output "slack_signing_secret_arn" {
  description = "ARN of the Slack Signing Secret (null when Slack is disabled)"
  value       = try(aws_secretsmanager_secret.slack_signing_secret[0].arn, null)
}

output "discord_public_key_secret_arn" {
  description = "ARN of the Discord Public Key secret (null when Discord is disabled)"
  value       = try(aws_secretsmanager_secret.discord_public_key[0].arn, null)
}

output "discord_bot_token_secret_arn" {
  description = "ARN of the Discord Bot Token secret (null when Discord is disabled)"
  value       = try(aws_secretsmanager_secret.discord_bot_token[0].arn, null)
}
