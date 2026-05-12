"""Master Agent tool — bridges the LLM to the deterministic orchestrator.

A single async tool wraps :class:`InvestigationOrchestrator`. The tool's
fan-out target list is derived from the runtime's ``ENABLED_AGENTS`` env
var (see :func:`_load_agent_endpoints`), so deploys can scope the master
to a subset of specialized agents (e.g. only ``eks``) without code
changes.

The tool kicks off the investigation as a background asyncio task and
returns immediately so the upstream Lambda invoker isn't held open for
the full 5-minute orchestrator lifecycle. Strands A2AServer is a
long-running uvicorn process, so background tasks keep running across
requests until AgentCore tears the container down.
"""

from __future__ import annotations

import asyncio
import json
import logging

from strands import tool

from agents.master.orchestrator import InvestigationOrchestrator
from shared.models import AlertContext

logger = logging.getLogger(__name__)

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _alert_context_from_payload(payload: dict) -> AlertContext:
    """Reconstruct an :class:`AlertContext` from a JSON-decoded dict.

    ``asdict()`` on the Lambda side converts the ``investigation_window``
    tuple to a list, which JSON round-trips back as a list. The
    dataclass tolerates either, but normalising here keeps downstream
    code that pattern-matches on the tuple shape happy.
    """
    window = payload.get("investigation_window")
    if isinstance(window, list) and len(window) == 2:
        payload = {**payload, "investigation_window": (window[0], window[1])}
    return AlertContext(**payload)


@tool
async def investigate_alert(alert_context_json: str) -> str:
    """Run the full incident investigation lifecycle for an alert.

    Fans out to every agent in the runtime's ``ENABLED_AGENTS`` allowlist
    (or all known agents when unset), enforces the 60-second initial
    deadline and 5-minute hard cutoff, and posts the Incident Report plus
    enrichment updates to the originating chat platform directly. The
    return string is a status line for the LLM and is not user-visible.

    Args:
        alert_context_json: The verbatim JSON payload received in the A2A
            ``message/send`` text part. Must deserialize into a
            :class:`shared.models.AlertContext`.

    Returns:
        A short status string describing how the investigation concluded.
    """
    payload = json.loads(alert_context_json)
    alert_context = _alert_context_from_payload(payload)

    orchestrator = InvestigationOrchestrator()
    enabled_agents = sorted(orchestrator.agent_endpoints.keys())

    if not enabled_agents:
        msg = (
            f"Investigation {alert_context.investigation_id} aborted: "
            f"no agents enabled (check ENABLED_AGENTS and *_AGENT_RUNTIME_ARN env vars)."
        )
        logger.error(msg)
        return msg

    logger.info(
        "Starting investigation %s, fan-out=%s",
        alert_context.investigation_id,
        enabled_agents,
    )
    task = asyncio.create_task(
        orchestrator.investigate(alert_context),
        name=f"investigate-{alert_context.investigation_id}",
    )
    # Hold a strong reference so the loop doesn't GC the task mid-flight.
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return (
        f"Investigation {alert_context.investigation_id} started "
        f"(fan-out: {', '.join(enabled_agents)}). "
        f"Results will be posted to chat as agents respond."
    )
