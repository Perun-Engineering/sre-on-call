###############################################################################
# sre-on-call — AgentCore Observability Alarms
#
# This file provisions CloudWatch alarms over the metrics AgentCore
# Runtime emits to the `bedrock-agentcore` namespace. The one-time
# Transaction Search enablement (which makes spans visible in the GenAI
# dashboard) is account/region-scoped and lives in
# `scripts/enable_observability.sh` — Terraform doesn't manage it
# because there's no first-class resource for the trace-segment
# destination or indexing rule.
#
# Reference:
#   - Metric definitions:
#     https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html
#   - Best practices:
#     https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html
###############################################################################

# ── Variables ────────────────────────────────────────────────────────────────

variable "alarm_email_subscriptions" {
  description = "List of email addresses to subscribe to the AgentCore alarm SNS topic. Set to [] to skip subscriptions (the topic is always created)."
  type        = list(string)
  default     = []
}

variable "agentcore_latency_p95_threshold_ms" {
  description = "Threshold (milliseconds) for the p95 Latency alarm on AgentCore runtimes. Investigations themselves can take minutes; this guards the per-invocation transport latency, not the investigation duration."
  type        = number
  default     = 30000 # 30s — generous default; tune after first week of data.
}

variable "agentcore_error_rate_threshold" {
  description = "Threshold for TotalErrors over a 5-minute window (count). Above this, the alarm fires."
  type        = number
  default     = 5
}

variable "agentcore_throttle_threshold" {
  description = "Threshold for Throttles over a 5-minute window (count). Above this, the alarm fires."
  type        = number
  default     = 1
}

# ── Metric namespace ────────────────────────────────────────────────────────
#
# AgentCore vends runtime metrics to the `bedrock-agentcore` namespace
# (matching the IAM PutMetricData condition in `iam_agentcore.tf`). We
# alarm at the account/region level rather than per-runtime — this gives
# a single signal across all 5 runtimes; per-runtime drilldown lives in
# the GenAI dashboard. Per-runtime alarms can be added later once the
# operator has confirmed the exact dimension shape from the console.

locals {
  agentcore_metric_namespace = "bedrock-agentcore"
}

# ── SNS topic for alarm fan-out ─────────────────────────────────────────────

resource "aws_sns_topic" "agentcore_alarms" {
  name = "${var.project_name}-${var.environment}-agentcore-alarms"

  tags = {
    Name = "${var.project_name}-agentcore-alarms"
  }
}

resource "aws_sns_topic_subscription" "agentcore_alarms_email" {
  for_each = toset(var.alarm_email_subscriptions)

  topic_arn = aws_sns_topic.agentcore_alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

# ── Alarm 1: Total error count over 5 min ───────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "agentcore_errors" {
  alarm_name          = "${var.project_name}-${var.environment}-agentcore-errors"
  alarm_description   = "AgentCore runtime errors (system + user) exceeded the threshold over a 5-minute window."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = var.agentcore_error_rate_threshold
  treat_missing_data  = "notBreaching"

  metric_name = "TotalErrors"
  namespace   = local.agentcore_metric_namespace
  period      = 300
  statistic   = "Sum"

  alarm_actions = [aws_sns_topic.agentcore_alarms.arn]
  ok_actions    = [aws_sns_topic.agentcore_alarms.arn]

  tags = {
    Name = "${var.project_name}-agentcore-errors"
  }
}

# ── Alarm 2: p95 invocation latency ─────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "agentcore_latency_p95" {
  alarm_name          = "${var.project_name}-${var.environment}-agentcore-latency-p95"
  alarm_description   = "AgentCore runtime p95 invocation latency exceeded the threshold (ms) over a 5-minute window. This measures the AgentCore transport, not the end-to-end investigation duration."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2 # require two consecutive breaches to reduce flapping
  threshold           = var.agentcore_latency_p95_threshold_ms
  treat_missing_data  = "notBreaching"

  metric_name        = "Latency"
  namespace          = local.agentcore_metric_namespace
  period             = 300
  extended_statistic = "p95"

  alarm_actions = [aws_sns_topic.agentcore_alarms.arn]
  ok_actions    = [aws_sns_topic.agentcore_alarms.arn]

  tags = {
    Name = "${var.project_name}-agentcore-latency-p95"
  }
}

# ── Alarm 3: Throttles ──────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "agentcore_throttles" {
  alarm_name          = "${var.project_name}-${var.environment}-agentcore-throttles"
  alarm_description   = "AgentCore runtime returned 429 Throttling responses. Indicates we're hitting service quotas — investigate or request a quota increase."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = var.agentcore_throttle_threshold
  treat_missing_data  = "notBreaching"

  metric_name = "Throttles"
  namespace   = local.agentcore_metric_namespace
  period      = 300
  statistic   = "Sum"

  alarm_actions = [aws_sns_topic.agentcore_alarms.arn]
  ok_actions    = [aws_sns_topic.agentcore_alarms.arn]

  tags = {
    Name = "${var.project_name}-agentcore-throttles"
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "agentcore_alarms_topic_arn" {
  description = "SNS topic ARN that AgentCore alarms publish to. Subscribe additional channels (Slack webhook via Lambda, PagerDuty, etc.) here."
  value       = aws_sns_topic.agentcore_alarms.arn
}

output "agentcore_alarm_names" {
  description = "Names of the CloudWatch alarms created for AgentCore observability."
  value = {
    errors      = aws_cloudwatch_metric_alarm.agentcore_errors.alarm_name
    latency_p95 = aws_cloudwatch_metric_alarm.agentcore_latency_p95.alarm_name
    throttles   = aws_cloudwatch_metric_alarm.agentcore_throttles.alarm_name
  }
}
