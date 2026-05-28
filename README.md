# sre-on-call

A multi-agent bot that automatically investigates infrastructure alerts across Slack and Discord. When an alert is posted to a channel, the system acknowledges it, fans out investigation work to specialized agents in parallel, and posts a structured incident analysis as a thread reply.

## Architecture

```
Slack/Discord webhook
        │
        ▼
   Lambda Adapter ── (signature verify, dedup, ack) ──▶ chat platform
        │
        │ bedrock-agentcore.invoke_agent_runtime (JSON-RPC 2.0 / A2A)
        ▼
   Master Agent  ── investigate_alert tool ──▶ Orchestrator
                                                   │
                            ┌──────────────────────┼──────────────────────┐
                            ▼                      ▼                      ▼
                     Slack Scanner   Discord Scanner
                     CloudWatch Logs  EKS              (4 specialized agents,
                                                        configured via config.yaml)
```

- **Lambda Adapter** — receives Slack/Discord webhooks, verifies signatures, deduplicates via DynamoDB, posts ack, then invokes the Master Agent runtime.
- **Master Agent** — orchestrates the investigation: fans out to specialized agents, enforces deadlines, posts the Incident Report. See [`agents/master/README.md`](agents/master/README.md).
- **Specialized agents** — one per data source. Each has its own README:
  - [Slack Scanner](agents/slack_scanner/README.md) — Slack channel history correlation.
  - [Discord Scanner](agents/discord_scanner/README.md) — Discord channel history correlation.
  - [CloudWatch Logs](agents/cloudwatch_logs/README.md) — Logs Insights queries.
  - [EKS](agents/eks/README.md) — Kubernetes cluster state.

A Prometheus agent (`agents/prometheus/`) is checked-in but **not deployed and not in the orchestrator fan-out** — it isn't listed in `config.yaml` and has no terraform plumbing.

For the full docs index (deployment, testing, architecture, design specs), see [`docs/README.md`](docs/README.md).

All agents run on AWS Bedrock AgentCore Runtime, communicate via the A2A protocol (JSON-RPC 2.0 over the runtime's `/invocations` endpoint), and use the Strands Agents SDK with Claude Haiku 4.5. Each agent's tool surface is described declaratively: [`config.yaml`](config.yaml) lists the agent's enabled skills and MCP servers, every skill is a `SKILL.md` bundle under `agents/<name>/skills/<skill>/`, and `shared.a2a_factory` is the single entry point that loads the config, resolves skills, opens MCP connections, and starts the A2A server.

## Project Structure

```
├── config.yaml                     # Per-agent skills + MCP servers (single source of truth)
├── lambda_adapter/                 # Lambda webhook ingestion
│   ├── handler.py                  # Lambda entry point
│   ├── intake.py                   # Dedup + master agent invocation
│   └── dedup.py                    # DynamoDB deduplication store
├── agents/
│   ├── master/                     # Master orchestration agent
│   │   ├── tools.py                # investigate_alert (single tool, fire-and-forget)
│   │   ├── orchestrator.py         # InvestigationOrchestrator: fan-out + deadlines
│   │   ├── report_formatter.py     # Incident report assembly
│   │   ├── skills/<skill>/SKILL.md # Skill bundles (frontmatter -> tool symbol)
│   │   ├── tests/test_tools.py     # Per-agent unit tests
│   │   └── agent_card.json
│   ├── slack_scanner/              # tools.py + skills/ + tests/ + agent_card.json
│   ├── discord_scanner/            # same layout
│   ├── cloudwatch_logs/            # same layout (also wires the aws_docs MCP)
│   ├── eks/                        # same layout (network_mode: VPC)
│   └── prometheus/                 # Not deployed; not in config.yaml
├── shared/                         # Cross-agent utilities
│   ├── models.py                   # AlertContext, AgentResult, Finding, AgentFailure, AgentMetadata, CommandRequest
│   ├── constants.py
│   ├── a2a_factory.py              # Loads config + skills + MCPs; A2AServer + uvicorn + /ping
│   ├── a2a_protocol.py             # JSON-RPC envelope build/extract helpers
│   ├── agent_telemetry.py          # Per-agent metadata footer (model, tokens, cost)
│   ├── config.py                   # ProjectConfig (Pydantic) + loader for config.yaml
│   ├── skill_loader.py             # SKILL.md parser + tool-symbol resolver
│   ├── mcp_loader.py               # Context-managed MCPConnections handle
│   ├── platforms/                  # ChatPlatform per chat platform (Slack, Discord)
│   │   ├── __init__.py             # Protocol, WebhookEvent tagged union, deliver_with_retry, registry
│   │   ├── slack.py                # SlackChatPlatform: signature, parse, ack, deliver
│   │   └── discord.py              # DiscordChatPlatform: signature, parse, ack, deliver
│   ├── channel_scan.py             # Shared channel-scanning algorithm
│   ├── channel_utils.py
│   ├── report_renderer.py          # MarkupDialect-driven section renderer (Slack mrkdwn, Discord MD)
│   ├── secrets.py                  # Secrets Manager ARN -> plaintext resolver (cached)
│   ├── time_utils.py               # Investigation window + ISO timestamp helpers
│   ├── tool_result.py
│   ├── experiment.py
│   ├── experiment_store.py
│   └── experiment_results_store.py
├── tests/                          # Cross-cutting / shared unit tests
│   ├── integration/                # Handler, orchestrator, A2A factory, synthetic webhook
│   └── property/                   # Hypothesis property-based tests
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── networking.tf               # EKS-VPC reference + agent SG
│   ├── ecr.tf                      # ECR repos for the 5 agent images
│   ├── dynamodb.tf                 # Dedup table
│   ├── dynamodb_experiments.tf     # A/B experiment tables
│   ├── secrets.tf                  # Slack/Discord secret containers
│   ├── lambda.tf                   # Lambda function + URL
│   ├── iam.tf                      # Lambda + agent IAM roles
│   ├── iam_agentcore.tf            # AgentCore-specific IAM
│   └── agentcore.tf                # 5 aws_bedrockagentcore_agent_runtime resources
├── scripts/
│   ├── build_and_push_agents.sh    # Build 5 linux/arm64 images and push to ECR
│   ├── hydrate_secrets.sh          # Push Slack/Discord secret values
│   └── synthetic_slack_webhook.py  # Send a signed synthetic alert to the Lambda URL
├── docs/
│   ├── README.md                   # Docs index
│   ├── deployment.md               # Build, deploy, scoped testing
│   ├── testing.md                  # Synthetic + real Slack alert procedures
│   ├── architecture.d2             # Source for architecture.svg
│   ├── architecture.svg
│   ├── icons/                      # AWS + vendor icons used by the diagram
│   └── superpowers/                # Living design specs and implementation plans
├── CONTEXT.md                      # Domain vocabulary
└── pyproject.toml
```

## Prerequisites

- **Python 3.12+**
- **Terraform >= 1.5** (for infrastructure deployment only)
- **Docker buildx** with `linux/arm64` support (AgentCore runtime requires arm64)
- **AWS CLI** with SSO or static credentials for the target account

## Installation

```bash
git clone <repository-url>
cd sre-on-call
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -c "from shared.models import AlertContext; print('OK')"
```

## Running tests

```bash
pytest                                              # full suite
pytest -v                                           # verbose
pytest agents/eks/tests/test_tools.py               # one agent's unit tests
pytest tests/integration/test_orchestrator.py       # one integration test
pytest tests/property/                              # property-based tests only
```

Current count: **400 collected**, 400 passing. (Prometheus tests run and pass even though the agent isn't deployed.)

### Test layout

- `agents/<name>/tests/test_tools.py` — per-agent unit tests for the tool surface.
- `tests/` — cross-cutting unit tests for shared modules (config, skill loader, MCP loader, channel utils, telemetry, dedup, parser, signature, time utils, report formatter, A/B experiments, postmortem command).
- `tests/integration/` — the master orchestrator, the Lambda handler, the A2A factory, and the synthetic-webhook signing round-trip.
- `tests/property/` — Hypothesis property-based tests for the parser, signature verifier, dedup, time utils, channel utils, report formatter, and CloudWatch Logs query helpers.

## Infrastructure deployment

See **[docs/deployment.md](docs/deployment.md)** for the full procedure (build images → ECR repos → terraform apply → secret hydration). High level:

1. Configure the AWS profile and required Terraform variables.
2. `terraform apply -target=aws_ecr_repository.agents` to create the repos.
3. `./scripts/build_and_push_agents.sh <tag>` to build + push the 5 agent images.
4. `terraform apply -var "agent_image_tag=<tag>"` for the rest.
5. `./scripts/hydrate_secrets.sh` to populate Slack/Discord secret values.

### Required Terraform variables

```hcl
# terraform/terraform.tfvars
eks_cluster_name         = "eks-uat"                           # existing cluster the EKS agent inspects
agent_container_registry = "<account-id>.dkr.ecr.<region>.amazonaws.com"
```

Optional:

| Variable | Default | Purpose |
|----------|---------|---------|
| `aws_region` | `us-east-1` | |
| `environment` | `dev` | Resource-name prefix |
| `project_name` | `sre-on-call` | Resource-name prefix |
| `agent_image_tag` | `latest` | Pin image tag at apply time |
| `model_id` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock model used by all agents |
| `lambda_memory_size` | `256` | |
| `lambda_timeout` | `30` | |

### Configure Slack

See **[docs/testing.md](docs/testing.md)** for the full Slack App + bot setup. Quick version:

1. Set the Event Subscriptions URL to the deployed Lambda function URL.
2. Subscribe to the **`app_mention`** bot event only (the adapter has no event-type filter; subscribing to broader events will trigger an investigation per message).
3. Bot scopes: `app_mentions:read`, `chat:write`, `channels:history`.
4. Hydrate secrets with the real Bot Token (`xoxb-…`) and Signing Secret.

## Testing

See **[docs/testing.md](docs/testing.md)**. Two paths:

- **Synthetic** — `scripts/synthetic_slack_webhook.py` builds a correctly-signed `app_mention` payload and POSTs to the Lambda URL. Useful for fast smoke tests.
- **Real Slack alert** — invite the bot to a channel and `@bot …` to trigger an investigation end-to-end.

## Environment variables

These are set on the Lambda function and the AgentCore runtimes by Terraform; you typically do not need to set them by hand.

| Variable | Component | Description |
|----------|-----------|-------------|
| `SLACK_SIGNING_SECRET` | Lambda | Secrets Manager ARN holding the Slack signing secret |
| `SLACK_BOT_TOKEN` | Lambda, Master, Slack Scanner | Secrets Manager ARN holding the Slack bot OAuth token |
| `DISCORD_PUBLIC_KEY` | Lambda | Secrets Manager ARN holding the Discord application public key |
| `DISCORD_BOT_TOKEN` | Lambda, Master, Discord Scanner | Secrets Manager ARN holding the Discord bot token |
| `DEDUP_TABLE_NAME` | Lambda | DynamoDB deduplication table name |
| `EXPERIMENTS_TABLE_NAME` | Lambda | DynamoDB A/B experiment config table name |
| `MASTER_AGENT_RUNTIME_ARN` | Lambda | AgentCore runtime ARN of the master agent |
| `SLACK_SCANNER_AGENT_RUNTIME_ARN` | Master | AgentCore runtime ARN of the Slack Scanner |
| `DISCORD_SCANNER_AGENT_RUNTIME_ARN` | Master | AgentCore runtime ARN of the Discord Scanner |
| `CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN` | Master | AgentCore runtime ARN of CloudWatch Logs |
| `EKS_AGENT_RUNTIME_ARN` | Master | AgentCore runtime ARN of EKS |
| `MODEL_ID` | All agents | Bedrock model ID or cross-region inference profile |
| `EKS_CLUSTER_NAME` | EKS agent | Cluster the EKS agent inspects |
| `A2A_PORT` / `A2A_HOST` | All agents | A2A server bind port (9000) / host (0.0.0.0) |

For local-dev work where agents run as plain HTTP A2A servers (not on AgentCore), each `*_AGENT_RUNTIME_ARN` falls back to a corresponding `*_AGENT_URL` (e.g. `EKS_AGENT_URL=http://localhost:9005`). When unset, the orchestrator uses `localhost` defaults.
