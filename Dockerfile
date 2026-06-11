# syntax=docker/dockerfile:1.7
#
# Single multi-stage Dockerfile for all sre-on-call agents.
# Build per agent with: docker build --build-arg AGENT=<name> -t <repo>:<tag> .
#   AGENT in: master, slack_scanner, discord_scanner, cloudwatch_logs, eks, incident_history
#

# ── Stage 1: install Python deps ─────────────────────────────────────────────
FROM python:3.12-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY pyproject.toml ./

RUN pip install --upgrade pip \
    && pip install --prefix=/install \
        "strands-agents[a2a]==1.36.0" \
        "slack_sdk>=3.27.0" \
        "boto3>=1.34.0" \
        "kubernetes>=29.0.0" \
        "aiohttp>=3.9.0" \
        "cryptography>=42.0.0"

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ARG AGENT
RUN test -n "$AGENT" \
    || (echo "ERROR: build-arg AGENT is required (master|slack_scanner|discord_scanner|cloudwatch_logs|eks|incident_history)" && exit 1)

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    A2A_HOST=0.0.0.0 \
    A2A_PORT=9000 \
    AGENT=${AGENT}

WORKDIR /app

COPY --from=deps /install /usr/local

COPY shared/ ./shared/
COPY agents/ ./agents/
# config.yaml is NOT baked in — runtimes read it from SSM (CONFIG_SSM_PARAMETER,
# set by Terraform). Local dev/tests read the repo file via shared.config.

EXPOSE 9000

# shared.a2a_factory's __main__ takes the agent dir as argv[1]
CMD ["sh", "-c", "exec python -m shared.a2a_factory agents/${AGENT}"]
