---
name: find_similar_incidents
description: Find past investigations whose alert text is similar to the current alert, ranked by embedding cosine similarity, with their recorded root cause and a link back to the original incident thread.
tool: agents.incident_history.tools:find_similar_incidents
---
# When to use

Call this skill once per investigation, when the user message is a serialized `AlertContext` (the default investigation request). Pass the `alert_text` field from the alert verbatim.

# Inputs

- `alert_text` (required): the full text of the current alert. The tool embeds it with Amazon Titan and ranks stored incident-outcome records by cosine similarity.

# Output

A human-readable summary plus an embedded `AGENT_RESULT` footer. Each finding is one similar past incident:

- `Similar alert (~NN% match) fired YYYY-MM-DD: "<alert preview>". Root cause: <recorded root cause>. <summary>`
- The finding's link points back to the original incident thread when one was recorded.

No matches is a normal, successful outcome — the tool returns "No similar past incidents found." Do not invent incidents; relay only what the tool ranked.
