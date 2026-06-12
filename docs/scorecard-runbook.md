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
| Control | `a` | eks/cloudwatch: Haiku, single-pass | commit **`05ee0e6`** (`28e654c^`, the merge parent of #58) |
| Treatment | `b` | eks/cloudwatch: Sonnet, ≤4 tool-use passes, deadline-aware | this branch (**#58** merged) |

The repo has **no release tags** — check out the control by commit, not a tag.

The two arms differ in **one place only**: the `eks` and `cloudwatch_logs`
specialists. The master already runs Sonnet in *both* arms (per-agent
`model_id` landed in #46–#48, before #58), so this A/B isolates the bounded
specialist loop and nothing else — exactly the variable #29 gates on.

The intake Lambda already fans every alert out to both `master_endpoint` ARNs
of the single `active` experiment and tags each run's `variant_id`
(`lambda_adapter/intake.py`); the results store pairs them by
`investigation_id`. Pairing needs **both masters writing to one results
table** — see the shared-table note in step 1.

## Procedure

### 1. Deploy both arms

The treatment is your normal stack. The control is a **second, independent
stack** under its *own* `project_name` (so no resource collides), pointed at
the treatment's results table so the judge can pair both variants. Three
module seams (added for #29) make this work without sharing the data plane:

- `experiment_results_table_name` — redirects the control master's writes to
  the treatment's results table *and* grants it `PutItem` on it.
- `additional_master_runtime_arns` — lets the treatment's intake Lambda invoke
  the control master ARN (its invoke policy is otherwise scoped to its own).
- `EXPERIMENT_RESULTS_TABLE_NAME` is now always set from the resolved table, so
  a non-default `project_name` writes to the right table.

Deploy **control first** — it needs only the treatment's results table *name*,
a fixed string (`sre-on-call-experiment-results`), not the treatment to exist
yet. Then re-apply the treatment with the control master's ARN.

1. **Control** (variant `a`): `git checkout 05ee0e6` (the merge parent of #58 —
   there is no tag), build its image, then deploy a *separate* stack:

   ```bash
   terraform apply \
       -var 'project_name=sre-on-call-ctl' \
       -var 'agent_image_tag=<control-tag>' \
       -var 'experiment_results_table_name=sre-on-call-experiment-results'
   ```

   A distinct `project_name` keeps the control's own tables, roles, runtimes,
   and (unused) Lambda from colliding with the treatment's. Its own
   `sre-on-call-ctl-experiment-results` table stays empty — expected; both arms
   write to the treatment's table. Note the output `master_runtime_arn`
   (`sre_on_call_ctl_master`). Return to your working branch afterwards.

2. **Treatment** (variant `b`): from this branch, build + deploy the #58 master
   + specialists into the existing `project_name=sre-on-call` stack, granting
   its Lambda the control master ARN:

   ```bash
   terraform apply \
       -var 'agent_image_tag=<treatment-tag>' \
       -var 'additional_master_runtime_arns=["arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/sre_on_call_ctl_master-..."]'
   ```

   `config.yaml` already carries `model_id: us.anthropic.claude-sonnet-4-6` and
   `max_tool_cycles: 4` on `eks`/`cloudwatch_logs`.

> ⚠️ Confirm the Sonnet inference profile `us.anthropic.claude-sonnet-4-6` is
> enabled in the dev account (it is, per the project log) — the bounded agents
> resolve to it. The specialists also need `bedrock:InvokeModel` for that id
> (covered by the existing agent exec role).

Record the two master runtime ARNs (`sre_on_call_dev_master` and
`sre_on_call_ctl_master`).

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

> The judge's built-in "judge model == variant model" bias warning reads the
> per-agent `model_id`s off the *experiment record*, which is empty for a
> deployment-shaped A/B (the model lives in each runtime's config, not the
> record). So the warning stays silent here — confirm by eye that
> `JUDGE_MODEL_ID` (Opus-4.5) differs from both deployed models (Haiku control
> specialists / Sonnet treatment specialists). It does, so the judge is
> unbiased; just don't expect the automatic warning to vouch for it.

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
