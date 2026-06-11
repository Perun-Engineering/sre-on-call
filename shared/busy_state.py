"""Process-wide busy-state seam for AgentCore ``/ping`` health reporting.

The master runs investigations and snapshots as fire-and-forget background
``asyncio`` tasks *after* the A2A response has returned. AgentCore uses the
``/ping`` status to decide instance health and may reclaim an idle-looking
instance mid-run. This seam lets the ``/ping`` handler in
:mod:`shared.a2a_factory` report ``HealthyBusy`` while such background work is
in flight, so the instance is not reaped before the investigation finishes.

Specialized agents respond synchronously and register no work here, so their
``/ping`` stays ``Healthy``.
"""

from __future__ import annotations

import asyncio

# Strong references to in-flight background tasks. Holding the reference both
# keeps the event loop from GC'ing a task mid-flight and doubles as the
# busy-state count. The done-callback registered in :func:`track` removes the
# task on completion.
_TRACKED_TASKS: set[asyncio.Task] = set()


def track(task: asyncio.Task) -> None:
    """Register *task* as in-flight background work.

    Holds a strong reference (so the loop won't GC the task before it
    finishes) and auto-discards it on completion. Callers need not keep their
    own reference.
    """
    _TRACKED_TASKS.add(task)
    task.add_done_callback(_TRACKED_TASKS.discard)


def busy_count() -> int:
    """Number of background tasks currently in flight."""
    return len(_TRACKED_TASKS)


def is_busy() -> bool:
    """``True`` when at least one background task is in flight."""
    return bool(_TRACKED_TASKS)
