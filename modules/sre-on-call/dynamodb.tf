###############################################################################
# sre-on-call — DynamoDB Deduplication Table
#
# Requirements: 2.1, 2.2, 2.3
#   - Lambda_Adapter queries Deduplication_Store for existing entries
#   - Duplicate alerts are discarded without invoking Master_Agent
#   - New alerts are recorded before invoking Master_Agent
#
# Table schema (from design):
#   Partition key: pk (String) — format: {channel_id}#{message_ts}
#   TTL attribute: ttl — set to created_at + 86400 (24h expiry)
#   Billing: PAY_PER_REQUEST (on-demand)
###############################################################################

resource "aws_dynamodb_table" "dedup" {
  name         = "${var.project_name}-dedup"
  billing_mode = "PAY_PER_REQUEST"

  # Partition key
  hash_key = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Enable TTL on the `ttl` attribute for automatic 24-hour record expiry
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = {
    Name = "${var.project_name}-dedup"
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "dedup_table_name" {
  description = "Name of the DynamoDB deduplication table"
  value       = aws_dynamodb_table.dedup.name
}

output "dedup_table_arn" {
  description = "ARN of the DynamoDB deduplication table"
  value       = aws_dynamodb_table.dedup.arn
}
