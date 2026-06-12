# Scorecard runbook — Sonnet bounded loop A/B (#58 → #29)

Produces the before/after comparison the **#29 keep/revert gate** reads: does
the Sonnet bounded-loop variant of the EKS and CloudWatch agents find more /
better-severity incidents, and is the cost + latency it adds worth it?

The scoring machinery already exists (issue #26): the offline LLM judge
(`scripts/judge_experiments.py`) renders per-investigation winners across the
coverage / severity / actionability / noise rubric plus an aggregate with cost
ratio and latency delta. This runbook is the **deploy-and-measure** procedure
that feeds it. Everything here runs in the dev AWS account; nothing touches the
investigation hot path.

## The two variants

Issue #58 ships the bounded loop as the *only* behaviour for `eks` and
`cloudwatch_logs` — there is no runtime flag. So the A/B is between two
**deployments**, not a toggle:

| Variant | id | Behaviour | Source |
|---|---|---|---|
| Control | `a` | Haiku, single-pass | a **pre-#58** master+specialist runtime (last release tag before #58 merged) |
| Treatment | `b` | Sonnet, ≤4 tool-use passes, deadline-aware | the **#58** runtime |

The intake Lambda already fans every alert out to both `master_endpoint` ARNs
of the single `active` experiment and tags each run's `variant_id`
(`lambda_adapter/intake.py`); the results store pairs them by
`investigation_id`.

## Procedure

### 1. Deploy both runtimes

Build/push and apply twice, into two AgentCore runtime stacks:

- **Control**: check out the pre-#58 tag, build its image, deploy as the
  control master + its specialists. (Follow `docs/deployment.md` apply order.)
- **Treatment**: from this branch, build + deploy the #58 master + specialists.
  `config.yaml` already carries `model_id: us.anthropic.claude-sonnet-4-6` and
  `max_tool_cycles: 4` on `eks`/`cloudwatch_logs`.

> ⚠️ Confirm the Sonnet inference profile `us.anthropic.claude-sonnet-4-6` is
> enabled in the dev account (it is, per the project log) — the bounded agents
> resolve to it. The specialists also need `bedrock:InvokeModel` for that id
> (covered by the existing agent exec role).

Record the two master runtime ARNs.

### 2. Define + activate the experiment

```bash
AWS_PROFILE=<dev> python scripts/define_experiment.py \
    --experiment-id sonnet-loop-58 \
    --name "haiku-oneshot-vs-sonnet-loop" \
    --control-arn   arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/<control-master> \
    --treatment-arn arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/<treatment-master> \
    --table sre-on-call-experiments
```

`--dry-run` first to eyeball the record. Only one experiment may be `active` at
a time — pause any other.

### 3. Replay alerts

Drive a representative set so the judge has signal. Both variants run per alert
automatically once the experiment is active.

- **Synthetic baseline** — the EKS/CloudWatch-relevant alert shapes:

  ```bash
  SLACK_SIGNING_SECRET=... ./scripts/synthetic_slack_webhook.py \
      --url https://<fn-id>.lambda-url.us-east-1.on.aws/ \
      --channel <channel-id> --team <team-id> \
      --text "ALERT: CrashLoopBackOff payment-api in ns prod (5xx spike)"
  ```

- **Archived real alerts** — re-post the alert text of *N* incidents from the
  trace archive (the `alert_text` of each archived investigation) through the
  same synthetic webhook. Aim for N ≥ 8–10 spanning EKS crashloop/OOM, node
  pressure, and CloudWatch error-spike cases — the situations where a second
  drill-down pass should pay off.

Give both variants time to land their results (initial report + any
enrichment) before scoring.

### 4. Score

```bash
AWS_PROFILE=<dev> JUDGE_MODEL_ID=us.anthropic.claude-opus-4-5 \
    python scripts/judge_experiments.py --experiment-id sonnet-loop-58
#   ... --json   # machine-readable, to attach verbatim
```

The judge runs both presentation orderings per pair (cancels position bias),
persists each verdict, and prints per-investigation rows + the aggregate
headline, cost ratio (B/A), and latency delta.

### 5. Attach to #29

Paste the rendered scorecard (and the `--json` blob) into issue **#29** as the
input to the keep/revert decision. Note alongside it the per-agent telemetry
already captured on both arms — `AgentMetadata` tokens / `cost_usd` /
`duration_seconds` / finding count — so the gate weighs coverage and severity
gains against the real cost + latency the Sonnet loop adds.

## What "good" looks like

Treatment (b) should win or tie **coverage** and **severity** on the
drill-down cases (a crashloop whose root cause only shows in the pod's logs;
an error spike that only a refined query quantifies) without inflating
**noise**, at a cost ratio and added latency the team judges acceptable inside
the 60-second initial-report budget. If it doesn't, #29 reverts — the bounded
loop is contained to two agents behind their `config.yaml` entries, so backing
it out is a config change plus a redeploy of the prior image.
