###############################################################################
# A/B Experiment Tables
#
# Two tables:
#   1. experiments — stores ExperimentConfig records (pk: EXPERIMENT#{id})
#   2. experiment-results — stores per-variant investigation results
#      (pk: {experiment_id}#{investigation_id}#{variant_id})
###############################################################################

resource "aws_dynamodb_table" "experiments" {
  name         = "${var.project_name}-experiments"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  tags = {
    Name = "${var.project_name}-experiments"
  }
}

resource "aws_dynamodb_table" "experiment_results" {
  name         = "${var.project_name}-experiment-results"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = {
    Name = "${var.project_name}-experiment-results"
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "experiments_table_name" {
  description = "Name of the DynamoDB experiments table"
  value       = aws_dynamodb_table.experiments.name
}

output "experiments_table_arn" {
  description = "ARN of the DynamoDB experiments table"
  value       = aws_dynamodb_table.experiments.arn
}

output "experiment_results_table_name" {
  description = "Name of the DynamoDB experiment results table"
  value       = aws_dynamodb_table.experiment_results.name
}

output "experiment_results_table_arn" {
  description = "ARN of the DynamoDB experiment results table"
  value       = aws_dynamodb_table.experiment_results.arn
}
