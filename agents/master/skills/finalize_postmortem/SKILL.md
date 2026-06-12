---
name: finalize_postmortem
description: Assemble and post a Post-Incident Report (PIR) for an incident thread. Used for the operator-driven `/postmortem` command.
tool: agents.master.tools:finalize_postmortem
---
# When to use

When the user message is a JSON object with `task: "pir"`. The Lambda intake dispatches `/postmortem` slash-command invocations this way. Pass the full JSON payload verbatim as `pir_request_json`.

# Inputs

- `pir_request_json` (required): the user message verbatim. Must be a JSON object with:
  - `task`: must equal `"pir"`.
  - `platform`: chat platform name (`"slack"` or `"discord"`).
  - `channel_id`: channel of the incident thread.
  - `thread_ts`: the incident thread's root timestamp — used to recover the original investigation.
  - `user_id`: who invoked `/postmortem` (logged, not posted).
  - `command_text`: the raw command text (logged).

# Output

The tool returns immediately with a short acknowledgement once the PIR assembly is dispatched. It runs in the background: it recovers the original investigation from the trace archive (by channel + thread), rebuilds the report from the archived findings, and posts a `PIRSections` payload as a reply in the incident thread. Do not respond with prose — the tool handles the entire user-visible interaction.

# Behaviour

- **Recovery**: resolves `thread_ts` → the original investigation via the trace index, then reloads the alert context and per-agent results from the archive.
- **Posting**: threaded under `thread_ts` (the PIR belongs to the incident conversation).
- **Fail-open**: if the trace archive is disabled, the investigation can't be found, or its records are incomplete, the tool posts one short notice into the thread instead of a report — never silence.
