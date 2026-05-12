# Master Agent

The orchestration agent. Receives an `AlertContext` from the Lambda adapter, fans out investigation work to the enabled specialized agents in parallel, enforces deadlines, and posts the Incident Report (plus enrichment updates) back to chat.

## Purpose

When a webhook arrives, the master agent's only job is to call its `investigate_alert` tool with the user message verbatim. The tool deserializes the `AlertContext`, instantiates `InvestigationOrchestrator`, posts an "Investigation Started" announcement, fans out via A2A to the enabled specialized agents, enforces a 60-second initial-report deadline plus a 5-minute hard cutoff, and posts the report and any late enrichment updates back to chat.

The set of downstream agents is configured at deploy time via the `ENABLED_AGENTS` environment variable, so the master never assumes which specialized agents will run.

## Skills

| Name | Description |
|---|---|
| `investigate_alert` | Receive an `AlertContext`, orchestrate parallel investigation across enabled agents, synthesize the Incident Report within 60 seconds, post enrichment updates until the 5-minute cutoff. |

> Skills are declared in [`config.yaml`](../../config.yaml) under `agents.master.skills` and resolved at startup by `shared.a2a_factory` from [`skills/investigate_alert/SKILL.md`](skills/investigate_alert/SKILL.md). The frontmatter's `tool:` field points at `agents.master.tools:investigate_alert`.

## MCPs

None.

## IAM

The `master_agent` IAM role (see `terraform/iam.tf`) carries:

- `bedrock-agentcore:InvokeAgentRuntime` on each specialized runtime ARN — required to fan out via the A2A client.
- `secretsmanager:GetSecretValue` on the Slack and Discord bot-token secrets — required to post chat replies.
- `dynamodb:PutItem` on the experiment-results table — required to record A/B experiment outcomes.

Runtime-execution permissions shared across every agent role (ECR image pull, CloudWatch Logs, X-Ray, Bedrock model invocation, workload tokens) are attached in [`terraform/iam_agentcore.tf`](../../terraform/iam_agentcore.tf).

## Local dev

```bash
AGENT=master python -m shared.a2a_factory agents/master
```

The master agent listens on port 9000 by default. To exercise a full local fan-out, start each enabled specialized agent on its own port and export `<NAME>_AGENT_URL=http://localhost:<port>` for each before starting the master.

See [`docs/testing.md`](../../docs/testing.md) for the synthetic webhook procedure that drives the master end-to-end.
