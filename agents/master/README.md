# Master Agent

The orchestration agent. Receives an `AlertContext` from the Lambda adapter, fans out investigation work to the enabled specialized agents in parallel, enforces deadlines, and posts the Incident Report (plus enrichment updates) back to chat.

## Purpose

When a webhook arrives, the master agent's only job is to call its `investigate_alert` tool with the user message verbatim. The tool deserializes the `AlertContext`, instantiates `InvestigationOrchestrator`, posts an "Investigation Started" announcement, fans out via A2A to the enabled specialized agents, enforces a 60-second initial-report deadline plus a 5-minute hard cutoff, and posts the report and any late enrichment updates back to chat.

The set of downstream agents is configured at deploy time via the `ENABLED_AGENTS` environment variable, so the master never assumes which specialized agents will run.

### Analysis synthesis

After harvesting agent results and before building the deterministic report, the master makes one LLM call (`agents/master/synthesis.py`) that reasons over the alert plus every agent's findings, summaries, and failures to produce a structured root-cause **Analysis** — hypothesis, cross-source correlation, confidence, and suggested next action — rendered above the verbatim Evidence blocks. Late results re-synthesize over everything gathered so far and carry an updated Analysis in their enrichment post.

It is **fail-open**: any error or timeout posts the report exactly as it would without synthesis. Controlled by env vars:

- `SYNTHESIS_ENABLED` — `true` to enable (Terraform `enable_analysis_synthesis`, default on).
- `SYNTHESIS_MODEL_ID` — model for the synthesis call; falls back to `MODEL_ID`. Point at a Sonnet-class model for stronger reasoning while dispatch stays cheap.
- `SYNTHESIS_TIMEOUT_SECONDS` — time budget (default 10s), reserved out of the 60s initial-report deadline.

### Pre-dispatch routing (Phase 0.5)

Before fanning out, the master makes one LLM call (`agents/master/routing.py`) that maps the alert onto (a) which of the active agents are worth dispatching and (b) a focused per-agent investigation hint (suspected pods, candidate log groups, time-window emphasis), injected onto each dispatch's `AlertContext.investigation_hints`. Agents the router skips render as a distinct **➖ "not investigated"** state in the Incident Report — never a failure. The decision (selected + hints + skip reasons + rationale) is written to the trace archive (`routing_decision` event + the manifest's `routing` block).

It is **fail-open**: routing disabled, any error, or a decision that would skip *every* agent dispatches the full active roster — exactly today's behavior. Routing runs before the kick-off notice so it lists only the agents actually queried; its latency is spent out of the 60s window. Controlled by env vars:

- `ALERT_ROUTING_ENABLED` — `true` to enable (Terraform `enable_alert_routing`, default on).
- `ROUTING_MODEL_ID` — model for the routing call; falls back to `MODEL_ID`. Point at a Sonnet-class model for better triage judgment while dispatch stays cheap.
- `ROUTING_TIMEOUT_SECONDS` — time budget (default 8s).

### Bounded follow-up round (Stage 2)

After the initial harvest and report, the master optionally makes one LLM call (`agents/master/followup.py`) asking whether a single additional targeted dispatch is worth it. If so it re-dispatches **at most N** agents (default 2) with refined hints; the results land through the existing late-result enrichment path. The round is **hard-capped** and only runs when the remaining cutoff budget can absorb it — and the dispatched tasks are bounded by the same Phase 4 cutoff loop — so the 5-minute deadline always holds.

It is **fail-open**: disabled or any error means no follow-up. Controlled by env vars:

- `FOLLOWUP_ROUND_ENABLED` — `true` to enable (Terraform `enable_followup_round`, default **off** until validated).
- `FOLLOWUP_MODEL_ID` — model for the follow-up planning call; falls back to `MODEL_ID`.
- `FOLLOWUP_TIMEOUT_SECONDS` — planning-call budget (default 6s).
- `FOLLOWUP_MAX_AGENTS` — hard cap on the round (default 2, Terraform `followup_max_agents`).

## Skills

| Name | Description |
|---|---|
| `investigate_alert` | Receive an `AlertContext`, orchestrate parallel investigation across enabled agents, synthesize the Incident Report within 60 seconds, post enrichment updates until the 5-minute cutoff. |

> Skills are declared in [`config.yaml`](../../config.yaml) under `agents.master.skills` and resolved at startup by `shared.a2a_factory` from [`skills/investigate_alert/SKILL.md`](skills/investigate_alert/SKILL.md). The frontmatter's `tool:` field points at `agents.master.tools:investigate_alert`.

## MCPs

None.

## IAM

The `master_agent` IAM role (see `modules/sre-on-call/iam.tf`) carries:

- `bedrock-agentcore:InvokeAgentRuntime` on each specialized runtime ARN — required to fan out via the A2A client.
- `secretsmanager:GetSecretValue` on the Slack and Discord bot-token secrets — required to post chat replies.
- `dynamodb:PutItem` on the experiment-results table — required to record A/B experiment outcomes.

Runtime-execution permissions shared across every agent role (ECR image pull, CloudWatch Logs, X-Ray, Bedrock model invocation, workload tokens) are attached in [`modules/sre-on-call/iam_agentcore.tf`](../../modules/sre-on-call/iam_agentcore.tf).

## Local dev

```bash
AGENT=master python -m shared.a2a_factory agents/master
```

The master agent listens on port 9000 by default. To exercise a full local fan-out, start each enabled specialized agent on its own port and export `<NAME>_AGENT_URL=http://localhost:<port>` for each before starting the master.

See [`docs/testing.md`](../../docs/testing.md) for the synthetic webhook procedure that drives the master end-to-end.
