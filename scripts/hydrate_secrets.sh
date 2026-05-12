#!/usr/bin/env bash
# Hydrate Slack/Discord secrets in Secrets Manager.
#
# Usage:
#   AWS_PROFILE=<profile> \
#   SLACK_BOT_TOKEN=xoxb-... \
#   SLACK_SIGNING_SECRET=... \
#   ./scripts/hydrate_secrets.sh
#
# Discord secrets are populated with placeholder strings (Slack-only test).
# Run after `terraform apply` has created the secret containers.

set -euo pipefail

PROFILE="${AWS_PROFILE:?AWS_PROFILE must be set}"
REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT_NAME:-sre-on-call}"
ENV="${ENVIRONMENT:-dev}"

require() {
    if [ -z "${!1:-}" ]; then
        echo "ERROR: $1 must be set" >&2
        exit 1
    fi
}

require SLACK_BOT_TOKEN
require SLACK_SIGNING_SECRET

put() {
    local name="$1" value="$2"
    aws secretsmanager put-secret-value \
        --profile "$PROFILE" --region "$REGION" \
        --secret-id "${PROJECT}-${ENV}-${name}" \
        --secret-string "$value" >/dev/null
    echo "  -> ${PROJECT}-${ENV}-${name}"
}

echo "==> Hydrating secrets in ${REGION}"
put slack-bot-token "$SLACK_BOT_TOKEN"
put slack-signing-secret "$SLACK_SIGNING_SECRET"
put discord-bot-token "stub-discord-bot-token-rotate-before-use"
put discord-public-key "stub-discord-public-key-rotate-before-use"

echo
echo "Done. Verify with:"
echo "  aws secretsmanager describe-secret --profile $PROFILE --region $REGION \\"
echo "      --secret-id ${PROJECT}-${ENV}-slack-bot-token --query 'LastChangedDate'"
