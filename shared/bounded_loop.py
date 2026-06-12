"""Bounded agentic loop control for iterative specialized agents (issue #58).

EKS and cloudwatch_logs run a Sonnet-class model over a small number of
purposeful tool-use passes that drill on what the prior pass surfaced. This
module supplies the *structural* bound the model cannot talk its way past:

- a hard ceiling of ``max_cycles`` tool-use cycles — one cycle is one model
  turn that calls tools — enforced by cancelling any tool call that would
  start a cycle beyond the cap; and
- a deadline gate that, once the remaining wall-clock budget minus an emit
  reserve is exhausted, stops *starting new passes* (pass 2+) and lets the
  model finalize from what it already gathered — never cancelling a pass
  mid-flight, so the master never harvests a half-built result.

The first pass always runs regardless of the deadline (it is not a "new"
pass), so a short budget yields iteration-1 findings rather than nothing.

Implemented as a Strands :class:`~strands.hooks.HookProvider`. It counts a
cycle on the first tool call after each model turn (independent of model
stop-reason strings) and writes ``cancel_tool`` on
:class:`~strands.hooks.BeforeToolCallEvent` to veto work past the bound.
``cancel_tool`` lands an error tool-result the model then reads and
summarizes from — turning the structural stop into a clean finalization.

The hook is *armed* per request: the A2A executor reads ``deadline_seconds``
off the inbound :class:`~shared.models.AlertContext` and calls :meth:`arm`
before invoking the agent. Invocations are serialized by the executor's
lock, so one shared hook instance per agent is safe.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from strands.hooks import (
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookRegistry,
)

logger = logging.getLogger(__name__)

DEFAULT_EMIT_RESERVE_SECONDS = 5.0

CYCLE_CAP_MESSAGE = (
    "Tool-use budget exhausted ({max_cycles} investigative passes used). "
    "Do not call any more tools — summarize the findings gathered so far now."
)
DEADLINE_MESSAGE = (
    "Investigation deadline reached. Do not start another pass — summarize "
    "the findings gathered so far now."
)


class BoundedLoopHook:
    """Enforce a hard tool-cycle ceiling and a deadline gate on an agent loop.

    A :class:`~strands.hooks.HookProvider`. Register it on the
    :class:`strands.Agent` once and :meth:`arm` it per request.
    """

    def __init__(
        self,
        max_cycles: int,
        *,
        emit_reserve_seconds: float = DEFAULT_EMIT_RESERVE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_cycles < 1:
            raise ValueError("max_cycles must be >= 1")
        self._max_cycles = max_cycles
        self._emit_reserve = emit_reserve_seconds
        self._clock = clock
        self._deadline: float | None = None
        self._cycles = 0
        self._turn_open = False

    @property
    def cycles(self) -> int:
        """Number of tool-use cycles opened so far in the current request."""
        return self._cycles

    @property
    def max_cycles(self) -> int:
        return self._max_cycles

    def arm(self, deadline_seconds: float | None) -> None:
        """Reset per-request state.

        ``deadline_seconds`` is the wall-clock budget the specialist was
        granted by the master; ``None`` disables the deadline gate (the cycle
        cap still applies).
        """
        self._cycles = 0
        self._turn_open = False
        self._deadline = (
            self._clock() + deadline_seconds if deadline_seconds is not None else None
        )

    def _cancel_reason(self) -> str | None:
        """Pure decision: why (if at all) the current tool call must be vetoed."""
        if self._cycles > self._max_cycles:
            return CYCLE_CAP_MESSAGE.format(max_cycles=self._max_cycles)
        # The deadline only blocks *new* passes (pass 2+); pass 1 always runs
        # so a short budget still yields iteration-1 findings.
        if (
            self._cycles >= 2
            and self._deadline is not None
            and self._clock() > self._deadline - self._emit_reserve
        ):
            return DEADLINE_MESSAGE
        return None

    # ------------------------------------------------------------------
    # HookProvider plumbing
    # ------------------------------------------------------------------

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeModelCallEvent, self._on_before_model)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_before_model(self, event: BeforeModelCallEvent) -> None:
        # A model turn has begun; the next tool call opens a new cycle.
        self._turn_open = True

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        if self._turn_open:
            self._cycles += 1
            self._turn_open = False
        reason = self._cancel_reason()
        if reason and not event.cancel_tool:
            logger.info(
                "BoundedLoopHook vetoing tool call (cycle %d/%d): %s",
                self._cycles,
                self._max_cycles,
                reason,
            )
            event.cancel_tool = reason
