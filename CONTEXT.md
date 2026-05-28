# CONTEXT — sre-on-call

Domain vocabulary for the project. Use these terms consistently in code, docs, and conversation.

## Domain Terms

**AlertContext** — The structured context extracted from a chat alert message (platform, channel, timestamp, investigation window). Passed through the entire investigation pipeline. Defined in `shared/models.py`.

**Finding** — A single piece of evidence discovered by an agent during investigation. Has source, timestamp, content, severity, and metadata. Defined in `shared/models.py`.

**AgentResult** — The canonical result returned by a specialized agent to the Master Agent via A2A protocol. Contains agent name, status (`success` | `error` | `unhealthy`), findings, and summary. `error` is request-level failure (transient — recommended action is "retry / manually check"). `unhealthy` indicates the agent fundamentally cannot do work in this deployment (operator-actionable — recommended action is "investigate agent configuration"). Defined in `shared/models.py`.

**ToolResult** — The generic intermediate result produced by any agent tool's `_execute_*()` function. Contains `findings`, `scanned_items` (flat list, prefixed for multi-category agents e.g. `pod/name`, `node/name`), and `errors`. Converted to `AgentResult` via `build_agent_result()`. Defined in `shared/tool_result.py`.

**WebhookEvent** — A tagged union returned by `ChatPlatform.ingest()`. One of `InvalidWebhook(status_code, reason)`, `ChallengeWebhook(response)`, `AlertWebhook(context)`, or `CommandWebhook(command)`. The intake pipeline pattern-matches on the variant. Defined in `shared/platforms/__init__.py`.

**ChatPlatform** — The seam between the investigation pipeline and a chat platform. Protocol with 3 methods: `ingest(headers, raw_body) -> WebhookEvent` (verify signature, classify the request), `ack(command, text)` (synchronous slash-command callback), and `async deliver(alert_context, payload) -> str` (render `ReportSections` / `EnrichmentSections` / `InvestigationStartedSections` / `PIRSections` to platform-native markup, post it as a thread reply, return the rendered text). Slack and Discord each provide one implementation in `shared/platforms/`. Subsumes the legacy `WebhookAdapter` + `ChatPoster` + `ReportRenderer` seams.

**ChannelMessageSource** — The seam between the shared channel-scanning algorithm and a chat platform. Protocol with 3 methods (`fetch_messages`, `is_alert`, `extract_finding`). Slack and Discord each provide an adapter (`SlackMessageSource`, `DiscordMessageSource`). The scanning algorithm (`execute_channel_scan`) lives in `shared/channel_scan.py` and is parameterized by a source. Defined in `shared/channel_scan.py`.

**Agent** — A runtime in the project (the master orchestrator or one of the specialized agents). Each Agent has a stable identity (id, display name, emoji, render order), a kind (`orchestrator` | `specialized`), and wiring (runtime-ARN env-var key, local-URL env-var key, default local URL). Resolved through the **Agent registry**. The `agents:` block in `config.yaml` is the *deployment manifest* — its keys must be known agent ids; an `enabled: false` flag marks an agent as deployed-but-inactive (built and pushed to ECR, but the orchestrator skips it on fan-out and surfaces it as a 🚫 disabled evidence block).

**Agent registry** — The single source of truth for "what agents exist and what's their state in this deployment." Code-side records (`shared/agents.py`) are folded with the `agents:` block from `config.yaml` at load time. Exposes `all()` (catalogue, used by config validation), `deployed()` (listed in `config.yaml`, used by terraform / build), `active()` (deployed AND `enabled=True`, used by orchestrator fan-out), `disabled_in_config()` (deployed AND `enabled=False`, used by the formatter to render 🚫 disabled evidence blocks), and `lookup(id)`. Each method takes an optional `kind` filter. Replaces the constants `KNOWN_AGENTS`, `DEFAULT_AGENT_ENDPOINTS`, `_ENV_KEYS`, `_RUNTIME_ARN_ENV_KEYS`, `AGENT_DISPLAY`, `AGENT_ORDER`, and the `ENABLED_AGENTS` env var.

**SnapshotReport** — A specialized agent's read-only snapshot of its observed infrastructure, returned by the agent's `capture_snapshot` tool in response to the `/status` slash command. Carries the agent's name, the wall-clock instant the snapshot was taken, a list of labelled `SnapshotSection`s (each a pre-rendered bullet list), and an `anomaly` flag with an optional one-line `anomaly_summary` the master uses for the deterministic top-line summary. Contrasts with `AgentResult` (alert-correlation findings); a snapshot is descriptive, not investigative. Defined in `shared/models.py`.

**SnapshotSections** — The platform-agnostic deliver payload for `/status` output. Built by the master's `StatusSnapshotOrchestrator` from per-agent `SnapshotReport`s plus a registry view, and rendered by each platform's `MarkupReportRenderer.render_snapshot()` into native markup. Contains a `requested_at` timestamp, the deterministic `summary_line`, and a list of `SnapshotBlock`s (one per agent — each with emoji, display name, registry-derived header line, content sections, and a four-state status marker `ok` / `anomaly` / `error` / `disabled`). Sibling to `ReportSections`, `EnrichmentSections`, `InvestigationStartedSections`, `PIRSections` in the `DeliverPayload` union. Defined in `shared/report_renderer.py`.

**StatusSnapshotOrchestrator** — The master agent's engine for the `/status` slash command. Distinct from `InvestigationOrchestrator` (alert path): single 30-second hard cutoff with no late-enrichment phase, builds the master's own block synchronously via `MasterSnapshotBuilder` (no fan-out for itself), fans out a snapshot-shaped A2A request `{"task": "snapshot", "requested_at": <ISO>}` to every active specialized agent, extracts each agent's `SnapshotReport` from the `<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>` footer, and posts a `SnapshotSections` payload at top-level (not as a thread reply) via a synthetic `AlertContext` with `message_id=""`. Defined in `agents/master/snapshot_orchestrator.py`.

## Conventions

- Agent tool files follow the pattern: `@tool` entry point → `_execute_*()` → `build_agent_result()` → `format_result()`.
- `@tool` entry points own I/O client creation (boto3 clients, K8s API clients, SDK clients) and pass them into `_execute_*()`.
- `_execute_*()` receives its I/O clients as required parameters and contains no client construction. This is the test surface — tests call `_execute_*()` directly with mock clients, no patching needed.
- `_execute_*()` returns a `ToolResult`. Platform-specific investigation logic lives here.
- `build_agent_result()` and `format_result()` are shared — do not duplicate in tool files.
- `severity_from_text()` is the shared text-based severity heuristic (critical/fatal → critical, error/exception/failure → warning, else info). Domain-specific severity functions (e.g. `_severity_from_phase` for Kubernetes pod phases) stay local to their agent.
- `scanned_items` uses prefixed strings when an agent inspects multiple resource types (e.g. `pod/nginx-abc`, `node/ip-10-0-1-5`).
