---
name: investigate_alert
description: Receive an AlertContext, orchestrate parallel investigation across enabled agents, synthesize the Incident Report within 60 seconds, and post enrichment updates until the 5-minute cutoff.
tool: agents.master.tools:investigate_alert
---
# When to use

Always. The user message you receive is a JSON-serialized `AlertContext` produced upstream by the webhook intake. Pass it verbatim as `alert_context_json`.

# Inputs

- `alert_context_json` (required): the user message verbatim — a JSON-serialized `AlertContext`.

# Output

The tool returns immediately with an acknowledgement. The orchestrator runs in the background, posts the Incident Report within 60 seconds of receiving the alert, and posts enrichment updates until 5 minutes have elapsed. Do not respond with prose — the tool handles the entire user-visible interaction.
