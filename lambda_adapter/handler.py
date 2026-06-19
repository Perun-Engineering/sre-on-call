"""Single Lambda entry point for all webhook platforms.

Handles two invocation shapes:

* **Webhook (function URL):** an event with ``headers`` and ``body`` — the
  platform is detected from the headers and the request runs through the shared
  intake pipeline.
* **Self-dispatch (async):** an event carrying :data:`DISPATCH_EVENT_KEY`,
  delivered by :class:`AsyncMasterDispatch` so the blocking master invoke runs
  off Slack's 3-second slash-command deadline. It is replayed through
  :func:`run_dispatched_task`.
"""

from __future__ import annotations

import logging

from lambda_adapter.intake import process_webhook
from lambda_adapter.master_dispatch import DISPATCH_EVENT_KEY, run_dispatched_task
from shared.platforms import detect_platform

# The python3.12 managed runtime pins the root logger to WARNING when the
# function uses the legacy Text log format, which drops every INFO-level
# decision log in the intake pipeline (dispatch, dedup, ignore reason). Force
# INFO at import so those traces reach CloudWatch without a log-format change.
logging.getLogger().setLevel(logging.INFO)


def lambda_handler(event: dict, context: object) -> dict:
    """Unified Lambda entry point — webhook ingestion or async self-dispatch."""
    if DISPATCH_EVENT_KEY in event:
        run_dispatched_task(event)
        return {"ok": True}

    headers = event.get("headers", {})
    platform = detect_platform(headers)
    return process_webhook(event, platform)
