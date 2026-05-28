# Deployment

This document covers the full procedure for deploying the sre-on-call stack to an AWS account from a clean state, plus operational notes for re-deploys, secret rotation, and scoping the fan-out for testing.

## Prerequisites

- AWS account with administrator access (the stack creates IAM roles, KMS keys, secrets, runtimes).
- AWS CLI configured for an SSO profile (referenced as `AWS_PROFILE` below) targeting the deploy region (default `us-east-1`).
- Terraform >= 1.5.
- Docker with `buildx` and `linux/arm64` support — AgentCore Runtime requires arm64.
- Python 3.12 + the project venv (`pip install -e ".[dev]"`).
- An existing EKS cluster the EKS agent will inspect. The cluster must use the **EKS Access Entries API** (`authenticationMode=API` or `API_AND_CONFIG_MAP`); the deploy creates an `aws_eks_access_entry` + policy association rather than touching `aws-auth`.
- Bedrock model access for the chosen `model_id` (default `us.anthropic.claude-haiku-4-5-20251001-v1:0`). Verify with a Converse smoke-test before deploying.

## One-time setup

### 1. Required Terraform variables

Create `terraform/terraform.tfvars`:

```hcl
eks_cluster_name         = "eks-uat"
agent_container_registry = "<account-id>.dkr.ecr.<region>.amazonaws.com"
```

Or pass them on the command line via `-var`. The container registry value is used to build agent image URIs.

### 2. Initialize Terraform

```bash
cd terraform
AWS_PROFILE=<profile> terraform init
```

### 3. Create the ECR repos first

The agent containers can't be built until the ECR repos exist, and the rest of the stack can't apply until images exist. Two-step apply:

```bash
AWS_PROFILE=<profile> terraform apply -target=aws_ecr_repository.agents
```

This creates the 5 repos (`sre-on-call-master`, `-slack-scanner`, `-discord-scanner`, `-cloudwatch-logs`, `-eks`) and nothing else.

### 4. Build and push the agent images

```bash
AWS_PROFILE=<profile> ./scripts/build_and_push_agents.sh <tag>
```

The script:

- Logs into ECR.
- Builds the multi-stage `Dockerfile` once per agent with `--build-arg AGENT=<name>`, targeting `linux/arm64`.
- Tags each image as both `<tag>` and `latest`.
- Pushes both tags.

`<tag>` defaults to the short git SHA when omitted. Use a meaningful tag (e.g. `tools-wired-v3`) when iterating so you can roll forward/back precisely.

### 5. Apply the rest of the stack

```bash
AWS_PROFILE=<profile> terraform plan \
    -var 'agent_image_tag=<tag>' \
    -out=/tmp/plan
AWS_PROFILE=<profile> terraform apply /tmp/plan
```

This creates: networking SG, DynamoDB tables, Secrets Manager containers, Lambda function + URL, IAM roles, and the 5 AgentCore runtimes wired to one another via env-var ARN passing.

### 6. Hydrate secrets

The Terraform `secrets.tf` only creates empty secret containers. Fill them in:

```bash
AWS_PROFILE=<profile> \
SLACK_BOT_TOKEN=xoxb-… \
SLACK_SIGNING_SECRET=… \
  ./scripts/hydrate_secrets.sh
```

The script also stubs the Discord secrets with placeholder strings so the Discord adapter doesn't crash on cold-start when only Slack is in use. Re-run with real Discord credentials when needed.

### 7. Verify

```bash
# Master should be READY at the latest version
AWS_PROFILE=<profile> aws bedrock-agentcore-control get-agent-runtime \
    --region us-east-1 \
    --agent-runtime-id <master-runtime-id> \
    --query '{status:status, version:agentRuntimeVersion}'
```

Then run the synthetic webhook (see `docs/testing.md`).

## Re-deploy after a code change

For agent code changes:

```bash
AWS_PROFILE=<profile> ./scripts/build_and_push_agents.sh <new-tag>

cd terraform
AWS_PROFILE=<profile> terraform plan \
    -var 'agent_image_tag=<new-tag>' \
    -out=/tmp/plan
AWS_PROFILE=<profile> terraform apply /tmp/plan
```

Each AgentCore runtime updates in place; AWS provisions a new version and shifts traffic to it once it's `READY`. Wait ~30–60s after apply before invoking, especially for VPC-attached runtimes.

For Lambda-only code changes, the Lambda zip is rebuilt by Terraform automatically — `terraform plan` picks up changes under `lambda_adapter/` and `shared/`.

## Scoping the fan-out

The set of specialized agents the master fans out to is read from
`config.yaml`'s `agents:` block via the `AgentRegistry`
(`shared/agents.py`). There are two levers, each with a different
operational meaning:

- **Add / remove an agent from the deployment** — edit the `agents:` block
  in `config.yaml`. An agent listed there is built, pushed to ECR, and has
  its terraform resources created. An agent absent from the block is
  treated as not-deployed (no ECR repo, no runtime, no IAM role).
- **Toggle an agent active without re-running terraform** — set
  `enabled: false` on the agent's entry in `config.yaml`. The runtime is
  still built and deployed; the orchestrator skips it on fan-out and
  surfaces a 🚫 disabled evidence block in the Incident Report. Flip the
  flag back to `true` and re-run terraform to re-activate without a
  rebuild.

Examples:

```yaml
# config.yaml — deploy EKS only, leave Discord built but disabled
agents:
  master:
    skills: [investigate_alert]
  eks:
    enabled: true
    network_mode: VPC
    skills: [gather_eks_state]
  discord_scanner:
    enabled: false   # built and pushed, but orchestrator skips it
    skills: [scan_discord_channels]
```

There is no `ENABLED_AGENTS` env var or `enabled_agents` Terraform
variable — `config.yaml` is the only switch. Validate before applying:

```bash
python -m shared.config validate
```

## Secret rotation

```bash
AWS_PROFILE=<profile> aws secretsmanager put-secret-value \
    --region us-east-1 \
    --secret-id sre-on-call-dev-slack-bot-token \
    --secret-string "<new-token>"
```

Lambda + agent containers resolve secrets via `shared.secrets.resolve_secret` on every invocation, so rotation takes effect without redeploy. AgentCore container env vars hold the secret **ARN**, not the value, so there's nothing to update on the runtime.

If a Lambda environment variable held the value directly (legacy path), bump `SECRET_REFRESH` in `terraform/lambda.tf` to force a new Lambda version.

## Tear-down

```bash
cd terraform
AWS_PROFILE=<profile> terraform destroy
```

Caveats:

- ECR repos with images are deleted by Terraform if `force_delete = true` is set on the repo resource (it currently is). Otherwise `terraform destroy` fails until images are removed manually.
- Secrets Manager defaults to a 30-day recovery window. To force-delete in dev, pass `--force-delete-without-recovery`:

  ```bash
  AWS_PROFILE=<profile> aws secretsmanager delete-secret \
      --region us-east-1 --force-delete-without-recovery \
      --secret-id sre-on-call-dev-slack-bot-token
  ```

- DynamoDB tables go away cleanly.

## Lessons-learned reference

The current stack incorporates fixes uncovered during the first real-AWS apply:

- **EKS subnet AZ filter** — AgentCore VPC mode rejects `use1-az6`. `data.aws_subnets.eks_private` filters on `availability-zone-id` to keep only `use1-az1/2/4`.
- **EKS subnet tier** — the cluster registers its API ENIs on `intra` subnets that have no NAT route. Placing the agent there leaves it unable to reach Bedrock or the AgentCore data plane and invokes hang at the AgentCore edge. `data.aws_subnets.eks_private` filters on `tag:purpose=private` (NAT-routed) instead of the cluster's own subnet list. Same-VPC routing + the cluster SG ingress rule keep the cluster's API reachable from the new subnets.
- **Secret resolution** — Lambda + agent env vars hold Secrets Manager ARNs; `shared/secrets.py:resolve_secret` resolves at runtime. Don't read these env vars as plaintext.
- **A2A protocol declaration** — every `aws_bedrockagentcore_agent_runtime` declares `protocol_configuration { server_protocol = "A2A" }`. Default HTTP/8080 won't work because Strands' A2AServer listens on 9000 with JSON-RPC.
- **`/ping` health probe** — `shared/a2a_factory.py` mounts a manual `GET /ping` on the FastAPI app from `server.to_fastapi_app()` and runs it with uvicorn. Without this, AgentCore declares the runtime unhealthy and 502s every invoke.
- **IAM action drift** — Lambda + master roles use `bedrock-agentcore:InvokeAgentRuntime`, scoped to specific runtime ARNs. The legacy `bedrock:InvokeAgent` does not authorise AgentCore.
- **JSON-RPC envelope** — Lambda wraps the AlertContext as a JSON-RPC `message/send` payload via `lambda_adapter/intake.py:_wrap_a2a_message`. Raw JSON is rejected by the A2A server with 502.
- **Master tool wiring** — the master agent's only skill (per `config.yaml` → `agents/master/skills/investigate_alert/SKILL.md`) is the async `investigate_alert` tool. It fires the orchestrator as a background asyncio task and returns immediately. Without fire-and-forget, Lambda blocks for the full deadline window and times out at 30s.
- **Task-envelope response parsing** — Strands' A2AServer returns a completed `Task` (with `artifacts[*].parts[*].text` under `agent_response`) for tool-driven agents, not an inline `Message`. `agents/master/orchestrator.py:_parse_agent_result` extracts text from all three shapes (inline `Message`, wrapped `Message`, `Task`) and never falls back to `str(result_data)` — that fallback once produced a Python dict-repr dump of the entire envelope inside the Incident Report's Summary section.
- **Concurrent-invoke serialization** — Strands' `Agent` raises `ConcurrencyException` on overlapping `stream_async` calls, and AgentCore's edge can retry an invocation it considers stalled (e.g., during cold-start). `TelemetryCapturingA2AExecutor` in `shared/a2a_factory.py` wraps `execute()` in an `asyncio.Lock` so the duplicate queues instead of erroring. The same factory passes `enable_a2a_compliant_streaming=True` per Strands' deprecation warning.
- **Lambda alias `routing_config` drift** — if the alias has `additional_version_weights` set externally (e.g., from a canary deploy), `aws_lambda_provisioned_concurrency_config` fails with `Alias with weights can not be used with Provisioned Concurrency`. Clear the weights with `aws lambda update-alias … --routing-config '{}'` and re-apply. Terraform doesn't manage the routing config field.
