# Incident History Agent

Surfaces past investigations whose alert text is similar to the current alert — "this fired before on `<date>`, root cause was X, here's the thread" — by ranking stored incident-outcome records against the current alert's embedding.

## Purpose

The system has a write side and a read side (issue #30):

- **Write** — at the end of every investigation, the master orchestrator (Phase 8) embeds the alert text with Amazon Titan and stores a compact *outcome record* (alert text, embedding, one-line summary, the synthesized root cause from the Analysis section, and a back-reference to the originating thread) in the existing trace archive table. See [`shared/incident_history_store.py`](../../shared/incident_history_store.py) and the `_record_incident_outcome` hook in [`agents/master/orchestrator.py`](../master/orchestrator.py).
- **Read** — when the master fans out an investigation, this agent embeds the current alert, scans the stored outcome records, ranks them by cosine similarity, and returns the closest matches as `AgentResult` findings.

No matches is a clean, successful "no similar past incidents" result — never an error. The agent reports **unhealthy** only when the deployment isn't wired for history (no traces table, or embeddings disabled).

## Skills

| Name | Description |
|---|---|
| `find_similar_incidents` | Embed the current alert and return the most cosine-similar past incidents, with their recorded root cause and a link back to the original thread. |
| `capture_snapshot` | Report how many incident-outcome records were written in the last 30 days (for `/sre-snapshot`). |

> Skills are declared in [`config.yaml`](../../config.yaml) under `agents.incident_history.skills` and resolved at startup by `shared.a2a_factory`.

## Storage & the replaceable seam

Outcome records live in the **existing traces DynamoDB table** (zero new infra) under a `history#<investigation_id>` partition key, so they never collide with the trace manifest item the [`shared.trace_store`](../../shared/trace_store.py) writes for the same investigation. Embeddings are packed as little-endian `float32` bytes; cosine ranking is pure Python.

The read path is intentionally a small seam — `SimilarIncidentSearch` in [`shared/incident_history_store.py`](../../shared/incident_history_store.py). The brute-force DDB scan is trivial at current volume; it can be swapped for S3 Vectors or a Bedrock Knowledge Base later without touching this agent.

## MCPs

None.

## IAM

The `incident_history_agent` IAM role (see [`modules/sre-on-call/iam.tf`](../../modules/sre-on-call/iam.tf), plus the shared runtime-execution attachments in [`modules/sre-on-call/iam_agentcore.tf`](../../modules/sre-on-call/iam_agentcore.tf)) carries:

- `dynamodb:Scan` / `dynamodb:GetItem` on the traces table — read outcome records.
- `kms:Decrypt` / `kms:DescribeKey` on the traces CMK — the table is SSE-KMS encrypted.
- `bedrock:InvokeModel` (from the shared `agentcore_runtime_exec` policy) — Titan embeddings.

## Network requirements

`network_mode: PUBLIC`. Retrieval + formatting only — Haiku is sufficient; no VPC attachment.

## Configuration

| Env var | Purpose |
|---|---|
| `INCIDENT_HISTORY_ENABLED` | Master gate for embedding (read + write). `from_env` is inert unless truthy. |
| `EMBEDDING_MODEL_ID` | Titan model (default `amazon.titan-embed-text-v2:0`). |
| `EMBEDDING_DIMENSIONS` | Titan V2 output dim (256/512/1024; default 1024). |
| `TRACES_TABLE_NAME` | Shared trace archive table the outcome records live in. |
