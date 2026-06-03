###############################################################################
# sre-on-call — Trace Archive (S3 events + DynamoDB index)
#
# Stores per-investigation traces written by the Lambda intake and the
# Master orchestrator via shared/trace_store.py. Every write is fail-open
# in the application layer; this terraform provisions the storage so the
# writes have somewhere to land.
#
# See `shared/trace_store.py` (module docstring) for the schema, S3
# layout, and DDB index shape.
###############################################################################

# ── Variables ────────────────────────────────────────────────────────────────

variable "trace_archive_retention_days" {
  description = "Days to keep raw trace objects in S3 before expiry. Standard tier for the first 30 days; transitions to STANDARD_IA at day 30."
  type        = number
  default     = 365
}

# ── KMS CMK for the trace bucket ────────────────────────────────────────────
# A dedicated key keeps the trace data isolatable in IAM and lets you rotate
# without touching anything else. Annual rotation is enabled.

resource "aws_kms_key" "traces" {
  description             = "Encryption key for ${var.project_name} trace archive (S3 + DDB index)"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name = "${var.project_name}-traces"
  }
}

resource "aws_kms_alias" "traces" {
  name          = "alias/${var.project_name}-${var.environment}-traces"
  target_key_id = aws_kms_key.traces.key_id
}

# ── S3 bucket ────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "traces" {
  bucket = "${var.project_name}-${var.environment}-traces-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name = "${var.project_name}-traces"
  }
}

resource "aws_s3_bucket_public_access_block" "traces" {
  bucket = aws_s3_bucket.traces.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "traces" {
  bucket = aws_s3_bucket.traces.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.traces.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "traces" {
  bucket = aws_s3_bucket.traces.id
  versioning_configuration {
    # Disabled by default — traces are append-only and immutable by
    # convention. Enable later if compliance requires deletion auditability.
    status = "Disabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "traces" {
  bucket = aws_s3_bucket.traces.id

  rule {
    id     = "trace-retention"
    status = "Enabled"

    filter {
      prefix = "dt="
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = var.trace_archive_retention_days
    }
  }
}

# ── DynamoDB index table ─────────────────────────────────────────────────────
#
# Thin lookup index. Partition by investigation_id; the analytical surface
# lives in S3 and is queried via Athena/Glue. TTL on `ttl` matches the S3
# expiration so the two stores age out together.
#
# A GSI on (channel_id, alert_timestamp) supports the postmortem flow:
# "give me all investigations in this channel, newest first."

resource "aws_dynamodb_table" "traces" {
  name         = "${var.project_name}-traces"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "channel_id"
    type = "S"
  }

  attribute {
    name = "alert_timestamp"
    type = "S"
  }

  global_secondary_index {
    name            = "channel_id-alert_timestamp-index"
    projection_type = "ALL"

    key_schema {
      attribute_name = "channel_id"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "alert_timestamp"
      key_type       = "RANGE"
    }
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.traces.arn
  }

  tags = {
    Name = "${var.project_name}-traces"
  }
}

# ── IAM: Lambda adapter — write trace events on alert intake ────────────────

resource "aws_iam_role_policy" "lambda_adapter_traces" {
  name = "trace-archive-write"
  role = aws_iam_role.lambda_adapter.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PutTraceEventsLambda"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.traces.arn}/*"
      },
      {
        Sid    = "TraceArchiveKMS"
        Effect = "Allow"
        Action = [
          "kms:GenerateDataKey",
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = aws_kms_key.traces.arn
      },
    ]
  })
}

# ── IAM: Master agent — write trace events + manifest + DDB index ───────────

resource "aws_iam_role_policy" "master_agent_traces" {
  name = "trace-archive-write"
  role = aws_iam_role.master_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PutTraceEventsMaster"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.traces.arn}/*"
      },
      {
        Sid    = "PutTraceIndex"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
        ]
        Resource = aws_dynamodb_table.traces.arn
      },
      {
        Sid    = "TraceArchiveKMS"
        Effect = "Allow"
        Action = [
          "kms:GenerateDataKey",
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = aws_kms_key.traces.arn
      },
    ]
  })
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "traces_bucket_name" {
  description = "Name of the S3 bucket holding per-investigation trace events + manifests"
  value       = aws_s3_bucket.traces.bucket
}

output "traces_bucket_arn" {
  description = "ARN of the trace archive S3 bucket"
  value       = aws_s3_bucket.traces.arn
}

output "traces_table_name" {
  description = "Name of the DynamoDB trace index table"
  value       = aws_dynamodb_table.traces.name
}

output "traces_table_arn" {
  description = "ARN of the DynamoDB trace index table"
  value       = aws_dynamodb_table.traces.arn
}

output "traces_kms_key_arn" {
  description = "ARN of the KMS CMK used to encrypt the trace archive"
  value       = aws_kms_key.traces.arn
}
