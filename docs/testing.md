# Testing

Two ways to exercise a deployed sre-on-call stack:

- **[Synthetic webhook](#synthetic-webhook)** — fast smoke test against the Lambda function URL, no Slack involvement. Covers the alert path AND the `/status` slash-command path.
- **[Real Slack alert](#real-slack-alert)** — end-to-end via a real Slack workspace and bot mention.
- **[/status command](#status-command)** — same script, slash-command mode; or run from a real Slack workspace.

Both paths post the Incident Report to whatever chat platform the AlertContext targets, so the chat poster needs working credentials.

## Synthetic webhook

`scripts/synthetic_slack_webhook.py` builds a correctly-signed `app_mention` event and POSTs it to the Lambda function URL. The Lambda's signature verifier accepts it as if Slack sent it.

### Prerequisites

- Lambda function URL (from `terraform output lambda_function_url`)
- AWS profile with read access to `sre-on-call-<env>-slack-signing-secret`

### Run

```bash
SLACK_SIGNING_SECRET="$(aws secretsmanager get-secret-value \
    --profile "$AWS_PROFILE" --region us-east-1 \
    --secret-id sre-on-call-dev-slack-signing-secret \
    --query SecretString --output text)" \
  ./scripts/synthetic_slack_webhook.py \
    --url 'https://<lambda-url-id>.lambda-url.us-east-1.on.aws/' \
    --channel <channel-id> \
    --team <team-id> \
    --text 'ALERT: high CPU on api-server'
```

Expected: `HTTP 200 {"ok": true}` within ~3 seconds. The investigation continues in the master container in the background.

### Watch the investigation run

In one terminal:

```bash
aws logs tail /aws/lambda/sre-on-call-dev-lambda-adapter \
    --profile "$AWS_PROFILE" --follow
```

In another:

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/sre_on_call_dev_master-<id>-DEFAULT \
    --profile "$AWS_PROFILE" --follow | grep -v "GET /ping"
```

(Replace `<id>` with the actual suffix; find it via `terraform output master_agent_runtime_arn`.)

You should see this sequence in the master's log:

```
Tool #1: investigate_alert
INFO:agents.master.tools:Starting investigation <uuid>, fan-out=[<enabled agents>]
INFO:     127.0.0.1:NNNN - "POST / HTTP/1.1" 200 OK     # Lambda gets its 200
INFO:agents.master.orchestrator:Investigation <uuid> terminated. Agents responded: [...]
```

### Caveats

- If `ENABLED_AGENTS` is set to a subset, only those agents fan out — the Incident Report reflects that subset.
- The synthetic test uses a fake message timestamp. If the chat poster hits Slack, it will try to reply in thread to a non-existent message and fail (logged, non-fatal). The investigation still completes; only the chat post is missing.
- The dedup table will swallow repeat synthetic calls with the same channel/timestamp pair. Vary the script's clock or use distinct channels if you want repeated runs.

## Real Slack alert

This drives the system from a real Slack workspace, exercising signature verification, the Slack chat poster, and end-user-visible thread replies.

### 1. Configure the Slack App

At <https://api.slack.com/apps> for your app:

**Event Subscriptions** (left nav):

- Enable Events: **on**
- Request URL: the Lambda function URL (from `terraform output lambda_function_url`)
  - Slack POSTs a `url_verification` challenge on save; the Lambda's adapter responds automatically. Should turn green within a few seconds.
- Subscribe to bot events: **`app_mention` only**.

> **Important.** The intake pipeline does not filter by event type — every signed `event_callback` triggers an investigation. Subscribing to `message.channels` will fire an investigation for every message in every channel the bot is in.

**OAuth & Permissions**:

- Bot Token Scopes: `app_mentions:read`, `chat:write`, `channels:history`
- Click *Install to Workspace* (or *Reinstall*) and copy the **Bot User OAuth Token** (`xoxb-…`)

**Basic Information**:

- Copy the **Signing Secret**

### 2. Hydrate Secrets Manager

Replace the synthetic-test placeholder values with real ones:

```bash
AWS_PROFILE=<profile> \
SLACK_BOT_TOKEN=xoxb-… \
SLACK_SIGNING_SECRET=… \
  ./scripts/hydrate_secrets.sh
```

If you've already done this once and you're rotating credentials, the same script overwrites the existing secret values. Lambda picks up the new values on the next cold start; bump `SECRET_REFRESH` env var (or redeploy) to force one.

### 3. Invite the bot to a channel

In Slack:

```
/invite @YourBotName
```

Use the `--channel <channel-id>` and `--team <team-id>` of any channel where the bot is a member.

### 4. Trigger an alert

Post a message in that channel that mentions the bot:

```
@YourBotName high CPU on api-server in namespace default
```

What to expect:

| Time | Event |
|---|---|
| t=0s | You post the message |
| ~t=1–3s | Orchestrator pre-post: `🔎 Investigation Started — Querying agents: <list>` |
| up to t=60s | Master fans out to enabled agents |
| ~t=60s | Bot posts the Incident Report in-thread (⏳ markers for any still-pending agents; report-level Investigation Cost line + per-agent metadata footer) |
| up to t=5min | Bot posts enrichment updates as late agent results arrive: `📬 Enrichment Update` for successes, `⚠️ Late Result (failed)` for failures |

### What the messages look like now

The Incident Report distinguishes three states per agent: completed (full section with findings), pending (⏳ "still investigating" marker, retained until the 5-minute hard cutoff), and failed (⚠️). Agents not dispatched at all (e.g. excluded by `ENABLED_AGENTS`) are omitted from the report rather than shown as "data unavailable".

Every agent's evidence block — and every enrichment update — ends with a metadata line of the form `model=<model_id> · analysis time=mm:ss · tokens=<in>in/<out>out · cost=$<amount>`. The Incident Report header also shows an aggregate `Investigation Cost: tokens=<in>in/<out>out · cost=$<amount>` line summing every agent that reported telemetry. Token counts come from the Strands `AgentResult.metrics.accumulated_usage`; cost is computed from a built-in price table for Haiku 4.5 / Sonnet 4. The per-agent footer rides through A2A as an appended artifact chunk so it survives spec-compliant streaming, where `result.message.content` is flushed as chunks before `_handle_agent_result` runs.

### 5. Watch in CloudWatch (optional)

Same `aws logs tail` commands as the synthetic case.

### Caveats specific to a real-alert test

- **`ENABLED_AGENTS` scope:** if the deploy uses `enabled_agents=eks`, only the EKS agent reports. The Incident Report shows just that one section. Redeploy with `-var 'enabled_agents='` (empty) to fan out to all agents.
- **EKS targeting:** the EKS agent's tool inspects a namespace + resource list. The LLM picks targets from the alert text, so put the namespace/service name in the message for meaningful output.
- **Cold starts:** the first invocation after a quiet period takes longer (~5–10s init for Lambda, similar for AgentCore containers). Slack will retry the webhook after 3s if the ack response hasn't returned, but the dedup table absorbs the retry.
- **Concurrency:** Strands' `Agent` rejects parallel `stream_async` calls on the same container, but `TelemetryCapturingA2AExecutor` wraps `execute()` in an `asyncio.Lock` so duplicate invokes (e.g., AgentCore edge retries during cold-start) queue instead of returning `Internal error`. Throughput is still one-investigation-per-container; if you need parallelism, scale out the runtime rather than relying on serialization.
- **Postmortem command:** `/postmortem` from a thread invokes the master with a `task=pir` payload instead of a fan-out. That path is implemented but not currently part of the master's tool surface and is not exercised by either testing path here.

## /status command

`/status` is the operator-driven snapshot command. The Lambda intake routes it to `_process_status_command`, which acks immediately, then invokes the Master Agent with `{"task": "snapshot", platform, channel_id, user_id, requested_at}`. The master's `capture_status_snapshot` tool runs `StatusSnapshotOrchestrator`, fans out a snapshot request to every active specialized agent under a 30-second hard cutoff, and posts a `SnapshotSections` payload at top-level (not as a thread reply) when collection completes.

Test it via the synthetic webhook (no Slack workspace required) or by running `/status` in a real channel.

### Synthetic /status

Same script as the alert path, with `--command /status`:

```bash
SLACK_SIGNING_SECRET="$(aws secretsmanager get-secret-value \
    --profile "$AWS_PROFILE" --region us-east-1 \
    --secret-id sre-on-call-dev-slack-signing-secret \
    --query SecretString --output text)" \
  ./scripts/synthetic_slack_webhook.py \
    --url 'https://<lambda-url-id>.lambda-url.us-east-1.on.aws/' \
    --channel <channel-id> \
    --team <team-id> \
    --command /status
```

Expected: `HTTP 200` within ~1 second (the synchronous ack). The master agent runs the snapshot in the background and posts the result to `<channel-id>` at top-level within 30 seconds.

> **Note**: the synthetic `/status` ack is sent to a fake `response_url` in the script's body, so the user-visible "🩺 Capturing status snapshot..." ack is never seen — Lambda still POSTs it, just into the void. The actual snapshot still posts via the bot token to `<channel-id>` and is visible if the bot is a member of that channel.

### What appears in chat

When the orchestrator finishes (≤ 30s), Slack receives a top-level message in `<channel-id>`:

```
🩺 *Status Snapshot*
━━━━━━━━━━━━━━━━━━━
*Captured at:* 2026-05-28T19:00:00+00:00
🩺 4/4 agents responded

🎯 *Master Agent*
_model=… · network=PUBLIC · skills=investigate_alert, capture_status_snapshot_

*Agent registry*
- 📡 Slack Scanner: 🟢 active
- 🎮 Discord Scanner: 🟫 disabled in config.yaml
- 📋 CloudWatch Logs: 🟢 active
- ☸️ EKS Cluster State: 🟢 active
- 📈 Prometheus: ⚪ not deployed

📡 *Slack Scanner*
_model=… · network=PUBLIC · skills=scan_slack_channels, capture_snapshot_

*Authentication*
- workspace: Acme (T123)
- bot user: sre-on-call (U456)

*Channel access*
- bot is a member of 12 channel(s)

📋 *CloudWatch Logs*
…

☸️ *EKS Cluster State*
…
```

Each per-agent block opens with a registry-derived header line (model · network · skills) and ends with the agent's own snapshot sections. Anomalies render with a ⚠️ marker plus an italic `anomaly_summary` line under the header. Failures render as ❌ with the error message. Disabled-in-config agents render as 🚫.

### Real /status

Type `/status` in any channel. The bot posts the snapshot at top-level in that same channel — no thread context required.

### Caveats specific to /status

- **Non-thread post**: unlike `/postmortem`, the snapshot is posted at the top of the channel. The Slack platform's `_post_reply` drops the empty `thread_ts` for this path so Slack accepts the message.
- **30-second hard cutoff**: agents that don't respond within 30s render as ❌ "no response within 30s". There is no late-enrichment phase — `/status` is single-shot.
- **No deduplication**: re-running `/status` always produces a fresh snapshot. Operators may legitimately want to retry after a fix.
- **Master section is registry-only today**: the IAM grants `bedrock-agentcore:GetAgentRuntime` and `dynamodb:DescribeTable` (slice 6) so future versions of `MasterSnapshotBuilder` can probe runtime status and table reachability, but the slice 3 baseline only renders the registry view.
- **CloudWatch Logs cost**: `/status` runs one `GetMetricData` (cheap) plus one bounded Logs Insights query against the top 10 log groups (~$0.005 per invocation). If `/status` ends up running on a tight loop, this adds up; rate-limit at the operator level if needed.
