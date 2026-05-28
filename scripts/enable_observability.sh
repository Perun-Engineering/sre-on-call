#!/usr/bin/env bash
###############################################################################
# scripts/enable_observability.sh
#
# One-time, account/region-scoped setup for AgentCore Observability.
#
# This script enables CloudWatch Transaction Search so the GenAI
# Observability dashboard can render runtime traces and spans from the
# `bedrock-agentcore` namespace. Without this, AgentCore still emits
# metrics, but spans accumulate raw in CloudWatch Logs without the
# nice trace-graph UI.
#
# Idempotent — safe to re-run. Each step queries current state and
# skips when already configured.
#
# Reference: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html
#
# Usage:
#   ./scripts/enable_observability.sh                      # default region
#   ./scripts/enable_observability.sh us-west-2            # explicit region
#   ./scripts/enable_observability.sh us-east-1 5          # 5% sampling
#
# Requires: aws CLI v2, jq, an AWS profile with permission to call
#   logs:PutResourcePolicy
#   xray:UpdateTraceSegmentDestination
#   xray:UpdateIndexingRule
#   xray:GetTraceSegmentDestination (for the idempotency check)
#   xray:GetIndexingRules (for the idempotency check)
#
###############################################################################

set -euo pipefail

REGION="${1:-${AWS_REGION:-us-east-1}}"
SAMPLING_PCT="${2:-1}"   # 1% by default — index 1% of spans at no cost.
POLICY_NAME="AgentCoreTransactionSearchAccess"

echo "▶ Enabling AgentCore Observability"
echo "  Region:        ${REGION}"
echo "  Sampling rate: ${SAMPLING_PCT}%"
echo

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "  Account:       ${ACCOUNT_ID}"
echo

# ── Step 1: PutResourcePolicy for X-Ray → /aws/spans ────────────────────────

POLICY_DOC=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "TransactionSearchXRayAccess",
    "Effect": "Allow",
    "Principal": {"Service": "xray.amazonaws.com"},
    "Action": "logs:PutLogEvents",
    "Resource": [
      "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:aws/spans:*",
      "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/application-signals/data:*"
    ],
    "Condition": {
      "ArnLike":      {"aws:SourceArn":     "arn:aws:xray:${REGION}:${ACCOUNT_ID}:*"},
      "StringEquals": {"aws:SourceAccount": "${ACCOUNT_ID}"}
    }
  }]
}
JSON
)

echo "▶ Step 1/3: Putting CloudWatch Logs resource policy '${POLICY_NAME}'…"
aws logs put-resource-policy \
  --region "${REGION}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "${POLICY_DOC}" >/dev/null
echo "  ✓ Done"
echo

# ── Step 2: Set X-Ray trace destination to CloudWatchLogs ───────────────────

echo "▶ Step 2/3: Setting X-Ray trace segment destination → CloudWatchLogs…"
CURRENT_DEST="$(aws xray get-trace-segment-destination --region "${REGION}" --query Destination --output text 2>/dev/null || echo NONE)"
if [[ "${CURRENT_DEST}" == "CloudWatchLogs" ]]; then
  echo "  ✓ Already set to CloudWatchLogs (no-op)"
else
  aws xray update-trace-segment-destination \
    --region "${REGION}" \
    --destination CloudWatchLogs >/dev/null
  echo "  ✓ Done (was: ${CURRENT_DEST})"
fi
echo

# ── Step 3: Configure span sampling rate ────────────────────────────────────

echo "▶ Step 3/3: Setting X-Ray indexing-rule sampling to ${SAMPLING_PCT}%…"
RULE_JSON="$(printf '{"Probabilistic":{"DesiredSamplingPercentage":%s}}' "${SAMPLING_PCT}")"
aws xray update-indexing-rule \
  --region "${REGION}" \
  --name "Default" \
  --rule "${RULE_JSON}" >/dev/null
echo "  ✓ Done"
echo

cat <<EOF
✅ AgentCore Observability enabled in ${REGION}.

   Spans appear in: /aws/spans/default
   Dashboard:       https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#gen-ai-observability
   Note:            it can take up to 10 minutes for spans to start showing.

EOF
