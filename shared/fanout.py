"""Fan-out seam — dispatch an A2A request to every active specialized agent
and harvest the replies under a caller-controlled deadline.

Owns endpoint resolution (from the :class:`~shared.agents.AgentRegistry`),
per-endpoint transport selection (via :class:`~shared.a2a_client.RoutingHTTPClient`),
and the one :class:`~shared.a2a_client.A2AClient`. Generic over the per-agent
result type: the caller supplies the per-agent coroutine, so ``send``, the
``AgentFooter`` choice, the reply→domain mapping, and any trace events stay in
the orchestrators. The fan-out's whole vocabulary is "active agents, tasks,
deadlines".

A second :meth:`Fanout.harvest` re-waits the *same* in-flight requests — there
is no re-invocation. The :class:`InvestigationOrchestrator` harvests at the
initial deadline then in late-enrichment windows to the hard cutoff; the
:class:`StatusSnapshotOrchestrator` harvests once.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, TypeVar

from shared.a2a_client import A2AClient, AsyncHTTPClient, RoutingHTTPClient
from shared.agents import Agent, AgentRegistry, get_registry
from shared.models import AgentFailure, AgentResult

R = TypeVar("R")


def merge_settled(
    settled: dict[str, AgentResult | BaseException],
) -> dict[str, AgentResult | AgentFailure]:
    """Map a :meth:`Fanout.harvest` ``settled`` dict onto the domain union.

    Companion to ``harvest``: a task result is already an
    :class:`~shared.models.AgentResult` (the orchestrator's
    ``_invoke_agent_safe`` maps agent-level errors to ``status="error"``),
    while an exception handed back *as a value* — e.g. a task cancellation —
    becomes an :class:`~shared.models.AgentFailure`.

    Pure: the caller folds the returned entries into its running results map
    (``results.update(merge_settled(settled))``).
    """
    return {
        agent_id: (
            AgentFailure(agent_name=agent_id, error_message=str(value), timestamp="")
            if isinstance(value, BaseException)
            else value
        )
        for agent_id, value in settled.items()
    }


class Fanout:
    """Construct-and-dispatch across the active specialized agents."""

    def __init__(
        self,
        http_client: AsyncHTTPClient | None = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        self._registry = registry or get_registry()
        self._targets: list[Agent] = sorted(
            self._registry.active(kind="specialized"), key=lambda a: a.order
        )
        self.agent_endpoints: dict[str, str] = {
            a.id: a.resolve_endpoint() for a in self._targets
        }
        self.http_client: AsyncHTTPClient = http_client or RoutingHTTPClient()
        self.client = A2AClient(self.http_client)

    @property
    def targets(self) -> list[Agent]:
        """Active specialized agents in render order."""
        return self._targets

    @property
    def disabled(self) -> list[Agent]:
        """Deployed-but-disabled specialized agents (rendered as 🚫 blocks)."""
        return self._registry.disabled_in_config(kind="specialized")

    def dispatch(
        self,
        make_coro: Callable[[str], Coroutine[Any, Any, R]],
        agent_ids: list[str] | None = None,
    ) -> dict[str, asyncio.Task[R]]:
        """Spawn one task per dispatched agent from the caller-supplied coroutine.

        ``agent_ids`` selects a subset of the active agents (issue #28's
        router-chosen targets), preserving the registry render order and
        silently dropping ids that aren't active endpoints. ``None`` dispatches
        every active agent — today's behaviour.
        """
        if agent_ids is None:
            selected = list(self.agent_endpoints)
        else:
            wanted = set(agent_ids)
            selected = [aid for aid in self.agent_endpoints if aid in wanted]
        return {
            agent_id: asyncio.create_task(make_coro(agent_id), name=f"fanout-{agent_id}")
            for agent_id in selected
        }

    @staticmethod
    async def harvest(
        pending: dict[str, asyncio.Task[R]], timeout: float
    ) -> tuple[dict[str, R | BaseException], dict[str, asyncio.Task[R]]]:
        """Wait up to *timeout* for *pending* tasks.

        Returns ``(settled, still_pending)``: ``settled`` maps agent id to the
        task result, or to the raised exception *as a value* (never
        propagated) so the caller maps it to its own domain type. Re-waiting
        the returned ``still_pending`` in a later call continues the same
        in-flight requests.
        """
        if not pending:
            return {}, {}
        done, _ = await asyncio.wait(set(pending.values()), timeout=timeout)
        settled: dict[str, R | BaseException] = {}
        still: dict[str, asyncio.Task[R]] = {}
        for agent_id, task in pending.items():
            if task in done:
                try:
                    settled[agent_id] = task.result()
                except Exception as exc:
                    settled[agent_id] = exc
            else:
                still[agent_id] = task
        return settled, still

    @staticmethod
    async def cancel(pending: dict[str, asyncio.Task[R]]) -> None:
        """Cancel and drain any still-pending tasks."""
        for task in pending.values():
            task.cancel()
        for task in pending.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
