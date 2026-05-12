#!/usr/bin/env bash
# Build and push the 5 agent images to ECR.
#
# Usage:
#   AWS_PROFILE=<profile> ./scripts/build_and_push_agents.sh [TAG]
#
# Defaults to TAG=$(git rev-parse --short HEAD) when not provided.
# Requires the ECR repos to exist (terraform apply -target=aws_ecr_repository.agents).

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
PROFILE="${AWS_PROFILE:?AWS_PROFILE must be set}"

ACCOUNT_ID="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> Logging into ECR ${REGISTRY}"
aws ecr get-login-password --profile "$PROFILE" --region "$REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"

cd "$(dirname "$0")/.."

# Master is always built. Specialized agents are read from config.yaml and
# filtered by their `enabled` flag (defaults true if omitted) so a disabled
# agent (e.g. discord_scanner) is neither built nor pushed.
mapfile -t SPECIALIZED < <(
    python3 -c '
import sys, yaml
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)
for name, spec in cfg.get("agents", {}).items():
    if name == "master":
        continue
    if spec.get("enabled", True):
        print(name)
'
)
AGENTS=(master "${SPECIALIZED[@]}")

REPO_DASH() { echo "${PROJECT_NAME:-sre-on-call}-${1//_/-}"; }

echo "==> Building agents: ${AGENTS[*]}"

for agent in "${AGENTS[@]}"; do
    repo="$(REPO_DASH "$agent")"
    image="${REGISTRY}/${repo}:${TAG}"
    echo "==> Building ${agent} -> ${image}"

    # AgentCore runtime requires linux/arm64
    docker buildx build \
        --platform linux/arm64 \
        --build-arg "AGENT=${agent}" \
        --tag "${image}" \
        --tag "${REGISTRY}/${repo}:latest" \
        --pull \
        --no-cache-filter=deps \
        --push \
        .
done

echo
echo "All 5 images pushed at tag: ${TAG}"
echo "Pass to terraform: -var 'agent_image_tag=${TAG}'"
