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

# Master is always built. Specialized agents are read from config.yaml's
# `agents:` block — *all* listed agents are built, regardless of the
# `enabled` flag. Disabled-in-config means "operator turned it off in this
# deployment," not "remove its image" — keeping the image lets the operator
# flip `enabled: true` and skip the build cycle on re-enablement. Agents
# absent from config.yaml entirely (e.g. prometheus today) are skipped.
mapfile -t SPECIALIZED < <(
    python3 -c '
import sys, yaml
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)
for name in cfg.get("agents", {}):
    if name == "master":
        continue
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
echo "All ${#AGENTS[@]} images pushed at tag: ${TAG}"
echo "Pass to terraform: -var 'agent_image_tag=${TAG}'"
