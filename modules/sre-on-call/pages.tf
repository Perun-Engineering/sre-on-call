###############################################################################
# sre-on-call — Interactive Incident Page (issue #33)
#
# Optional, OFF by default. When `enable_incident_page = true`, this provisions
# the static incident-page delivery path:
#
#   master  → writes pages/<id>/page_model.json to the trace bucket
#   S3 event → triggers the page_renderer Lambda
#   renderer → reads page_model.json + charts/*.json (KMS-encrypted, so it holds
#              kms:Decrypt), renders, writes pages/<id>.html using SSE-S3 (AES256)
#   CloudFront (OAC + signed URLs via a trusted key group) serves pages/<id>.html
#
# KEY DESIGN: rendered pages and the placeholder `generating.html` are written
# with SSE-S3 (AES256), NOT the trace CMK, so CloudFront's OAC needs NO KMS
# permission — only the renderer Lambda touches the CMK (to read inputs).
###############################################################################

# ── Variables ────────────────────────────────────────────────────────────────

variable "enable_incident_page" {
  description = "Provision the #33 interactive incident page (CloudFront distribution, RSA signing keypair in Secrets Manager, and the page_renderer Lambda). OFF by default — turning it on creates billable CloudFront + Lambda + Secrets Manager resources."
  type        = bool
  default     = false
}

variable "incident_page_url_ttl_seconds" {
  description = "Validity window (seconds) for the CloudFront signed URLs the master mints for incident pages. Default 7 days."
  type        = number
  default     = 604800
}

# ── RSA keypair + CloudFront key group (signed-URL trust) ───────────────────

resource "tls_private_key" "incident_page" {
  count     = var.enable_incident_page ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_cloudfront_public_key" "incident_page" {
  count       = var.enable_incident_page ? 1 : 0
  name        = "${var.project_name}-${var.environment}-incident-page"
  comment     = "Verifies signed URLs the master mints for incident pages"
  encoded_key = tls_private_key.incident_page[0].public_key_pem
}

resource "aws_cloudfront_key_group" "incident_page" {
  count   = var.enable_incident_page ? 1 : 0
  name    = "${var.project_name}-${var.environment}-incident-page"
  comment = "Trusted key group for incident-page signed URLs"
  items   = [aws_cloudfront_public_key.incident_page[0].id]
}

# ── Secrets Manager: the private signing key (read by the master at runtime) ─

resource "aws_secretsmanager_secret" "incident_page_private_key" {
  count       = var.enable_incident_page ? 1 : 0
  name        = "${var.project_name}-${var.environment}-incident-page-private-key"
  description = "RSA private key the master uses to sign CloudFront incident-page URLs (paired with aws_cloudfront_public_key.incident_page)."
}

resource "aws_secretsmanager_secret_version" "incident_page_private_key" {
  count         = var.enable_incident_page ? 1 : 0
  secret_id     = aws_secretsmanager_secret.incident_page_private_key[0].id
  secret_string = tls_private_key.incident_page[0].private_key_pem
}

# ── Placeholder page served while a render is in flight ─────────────────────
# AES256 (SSE-S3), NOT the trace CMK — so CloudFront's OAC can read it with no
# KMS permission. Served on 403/404 (object not yet written) via the custom
# error responses below; auto-refreshes until the real page lands.

resource "aws_s3_object" "generating_page" {
  count        = var.enable_incident_page ? 1 : 0
  bucket       = aws_s3_bucket.traces.id
  key          = "generating.html"
  content_type = "text/html; charset=utf-8"
  content      = <<-HTML
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta http-equiv="refresh" content="5">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Generating incident page…</title>
      <style>
        body { font-family: system-ui, sans-serif; background: #0d1117; color: #c9d1d9;
               display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
        .box { text-align: center; }
        .spin { width: 32px; height: 32px; margin: 0 auto 1rem; border: 3px solid #30363d;
                border-top-color: #58a6ff; border-radius: 50%; animation: r 1s linear infinite; }
        @keyframes r { to { transform: rotate(360deg); } }
      </style>
    </head>
    <body>
      <div class="box">
        <div class="spin"></div>
        <p>Generating incident page…</p>
      </div>
    </body>
    </html>
  HTML

  # SSE-S3, not the trace CMK — keeps CloudFront KMS-free.
  server_side_encryption = "AES256"
}

# ── CloudFront: OAC + distribution ──────────────────────────────────────────

resource "aws_cloudfront_origin_access_control" "incident_page" {
  count                             = var.enable_incident_page ? 1 : 0
  name                              = "${var.project_name}-${var.environment}-incident-page"
  description                       = "OAC for the incident-page CloudFront distribution to read the trace bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "incident_page" {
  count   = var.enable_incident_page ? 1 : 0
  enabled = true
  comment = "${var.project_name}-${var.environment} interactive incident pages"

  origin {
    domain_name              = aws_s3_bucket.traces.bucket_regional_domain_name
    origin_id                = "traces-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.incident_page[0].id
  }

  default_cache_behavior {
    target_origin_id       = "traces-s3"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    trusted_key_groups     = [aws_cloudfront_key_group.incident_page[0].id]

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }
  }

  # Object not yet written (or denied) → serve the self-refreshing placeholder.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/generating.html"
    error_caching_min_ttl = 5
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/generating.html"
    error_caching_min_ttl = 5
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Name = "${var.project_name}-incident-page"
  }
}

# ── Bucket policy: let CloudFront (this distribution only) read pages ───────
# The trace bucket has no other bucket policy; this is the sole one. Scoped to
# pages/* and generating.html, and to this distribution's ARN via SourceArn.

resource "aws_s3_bucket_policy" "incident_page_cf_read" {
  count  = var.enable_incident_page ? 1 : 0
  bucket = aws_s3_bucket.traces.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontReadIncidentPages"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource = [
          "${aws_s3_bucket.traces.arn}/pages/*",
          "${aws_s3_bucket.traces.arn}/generating.html",
        ]
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.incident_page[0].arn
          }
        }
      },
    ]
  })
}

# ── Renderer Lambda: page_model.json → pages/<id>.html ──────────────────────

resource "null_resource" "page_renderer_stage" {
  count = var.enable_incident_page ? 1 : 0

  triggers = {
    sources = sha1(join("", [
      for f in fileset(var.source_root, "page_renderer/**/*.{py,js}") :
      filesha1("${var.source_root}/${f}")
    ]))
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -eu
      STAGE='${path.module}/.build/page_renderer'
      rm -rf "$STAGE"
      mkdir -p "$STAGE"
      cp -R '${var.source_root}/page_renderer' "$STAGE/page_renderer"
      find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
      find "$STAGE" -type d -name tests -prune -exec rm -rf {} +
    EOT
  }
}

data "archive_file" "page_renderer" {
  count       = var.enable_incident_page ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/.build/page_renderer"
  output_path = "${path.module}/.build/page_renderer.zip"

  depends_on = [null_resource.page_renderer_stage]
}

resource "aws_cloudwatch_log_group" "page_renderer" {
  count             = var.enable_incident_page ? 1 : 0
  name              = "/aws/lambda/${var.project_name}-${var.environment}-page-renderer"
  retention_in_days = 30

  tags = {
    Name = "${var.project_name}-page-renderer-logs"
  }
}

resource "aws_iam_role" "page_renderer" {
  count = var.enable_incident_page ? 1 : 0
  name  = "${var.project_name}-${var.environment}-page-renderer"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "page_renderer_basic" {
  count      = var.enable_incident_page ? 1 : 0
  role       = aws_iam_role.page_renderer[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "page_renderer" {
  count = var.enable_incident_page ? 1 : 0
  name  = "incident-page-render"
  role  = aws_iam_role.page_renderer[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadModelAndCharts"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.traces.arn}/*"
      },
      {
        Sid      = "WriteRenderedPages"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.traces.arn}/pages/*"
      },
      {
        # Inputs (page_model.json, charts/*.json) are CMK-encrypted, so the
        # renderer needs Decrypt to read them. Pages are written AES256, so no
        # GenerateDataKey is required.
        Sid    = "ReadCmkInputs"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = aws_kms_key.traces.arn
      },
    ]
  })
}

resource "aws_lambda_function" "page_renderer" {
  count         = var.enable_incident_page ? 1 : 0
  function_name = "${var.project_name}-${var.environment}-page-renderer"
  description   = "Renders pages/<id>.html from page_model.json + charts on S3 ObjectCreated"

  filename         = data.archive_file.page_renderer[0].output_path
  source_code_hash = data.archive_file.page_renderer[0].output_base64sha256

  runtime = "python3.12"
  handler = "page_renderer.handler.lambda_handler"

  role        = aws_iam_role.page_renderer[0].arn
  memory_size = 512
  timeout     = 60

  depends_on = [aws_cloudwatch_log_group.page_renderer]

  tags = {
    Name = "${var.project_name}-page-renderer"
  }
}

resource "aws_lambda_permission" "page_renderer_s3" {
  count         = var.enable_incident_page ? 1 : 0
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.page_renderer[0].function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.traces.arn
}

resource "aws_s3_bucket_notification" "page_renderer" {
  count  = var.enable_incident_page ? 1 : 0
  bucket = aws_s3_bucket.traces.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.page_renderer[0].arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = "page_model.json"
  }

  depends_on = [aws_lambda_permission.page_renderer_s3]
}

resource "aws_cloudwatch_metric_alarm" "page_renderer_errors" {
  count               = var.enable_incident_page ? 1 : 0
  alarm_name          = "${var.project_name}-${var.environment}-page-renderer-errors"
  alarm_description   = "Page renderer Lambda is throwing errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.page_renderer[0].function_name
  }
}

# ── Master: read the private signing key from Secrets Manager ───────────────

resource "aws_iam_role_policy" "master_incident_page_signing" {
  count = var.enable_incident_page ? 1 : 0
  name  = "incident-page-signing-key"
  role  = aws_iam_role.master_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadIncidentPageSigningKey"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.incident_page_private_key[0].arn
      },
    ]
  })
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "incident_page_cloudfront_domain" {
  description = "CloudFront domain serving interactive incident pages (empty when disabled)"
  value       = var.enable_incident_page ? aws_cloudfront_distribution.incident_page[0].domain_name : ""
}

output "incident_page_key_pair_id" {
  description = "CloudFront public key ID the master signs incident-page URLs with (empty when disabled)"
  value       = var.enable_incident_page ? aws_cloudfront_public_key.incident_page[0].id : ""
}
