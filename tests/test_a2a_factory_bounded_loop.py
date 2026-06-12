"""Bounded-loop wiring in the A2A factory (issue #58).

Covers the request-side seam: the specialist reads its master-granted
``deadline_seconds`` off the inbound serialized ``AlertContext`` and arms the
bounded loop before the Strands agent runs.
"""

from __future__ import annotations

import json
from typing import cast

import pytest

from a2a.server.agent_execution import RequestContext
from agents.master.orchestrator import _serialize_alert_context
from shared.a2a_factory import (
    TelemetryCapturingA2AExecutor,
    _extract_deadline_seconds,
)
from shared.bounded_loop import BoundedLoopHook
from shared.models import AlertContext


class _FakeContext:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_user_input(self, delimiter: str = "\n") -> str:
        return self._text


def _ctx(text: str) -> RequestContext:
    return cast(RequestContext, _FakeContext(text))


def _alert(deadline_seconds: float | None = None) -> AlertContext:
    return AlertContext(
        investigation_id="inv-1",
        platform="slack",
        channel_id="C1",
        message_id="m1",
        alert_text="CrashLoopBackOff payment-api",
        alert_timestamp="2026-06-12T00:00:00Z",
        investigation_window=("2026-06-12T00:00:00Z", "2026-06-12T00:10:00Z"),
        deadline_seconds=deadline_seconds,
    )


def test_extract_deadline_from_serialized_alert_roundtrip() -> None:
    payload = _serialize_alert_context(_alert(deadline_seconds=42.5))
    assert _extract_deadline_seconds(_ctx(payload)) == 42.5


def test_extract_deadline_absent_field_is_none() -> None:
    payload = _serialize_alert_context(_alert())  # deadline_seconds defaults None
    assert _extract_deadline_seconds(_ctx(payload)) is None


@pytest.mark.parametrize(
    "text",
    ["not json", "", "[1, 2, 3]", json.dumps({"deadline_seconds": "soon"}),
     json.dumps({"deadline_seconds": True})],
)
def test_extract_deadline_defensive_on_bad_input(text: str) -> None:
    assert _extract_deadline_seconds(_ctx(text)) is None


def test_extract_deadline_accepts_integer() -> None:
    assert _extract_deadline_seconds(_ctx('{"deadline_seconds": 30}')) == 30.0


@pytest.mark.asyncio
async def test_executor_arms_hook_before_invoking(monkeypatch) -> None:
    """execute() arms the bounded loop with the request's budget, then delegates."""
    armed: list[float | None] = []
    hook = BoundedLoopHook(max_cycles=4)
    monkeypatch.setattr(hook, "arm", lambda d: armed.append(d))

    # Stub the Strands parent so no real agent runs.
    async def _noop(self, context, event_queue):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "strands.multiagent.a2a.executor.StrandsA2AExecutor.execute", _noop
    )

    executor = TelemetryCapturingA2AExecutor(
        agent=object(), model_id="m", bounded_loop=hook
    )
    ctx = _FakeContext(_serialize_alert_context(_alert(deadline_seconds=12.0)))
    await executor.execute(ctx, event_queue=object())  # type: ignore[arg-type]

    assert armed == [12.0]


# ---------------------------------------------------------------------------
# Summarizer telemetry wiring (issue #49)
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402


@pytest.mark.asyncio
async def test_execute_resets_summarizer_usage_per_request(monkeypatch) -> None:
    """execute() zeroes the summarizer accumulator before the agent runs."""
    from shared import log_summarizer

    log_summarizer.record_usage(50, 50)  # leftover state from a prior request

    async def _noop(self, context, event_queue):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "strands.multiagent.a2a.executor.StrandsA2AExecutor.execute", _noop
    )
    executor = TelemetryCapturingA2AExecutor(agent=object(), model_id="m")
    await executor.execute(_FakeContext("{}"), event_queue=object())  # type: ignore[arg-type]

    assert log_summarizer.drain_usage() == (None, None)


def test_build_metadata_folds_in_summarizer_cost() -> None:
    """_build_metadata surfaces the summarizer's tokens + Haiku-priced cost."""
    from shared import log_summarizer

    log_summarizer.reset_usage()
    log_summarizer.record_usage(1_000_000, 1_000_000)  # $1/M in + $5/M out = $6

    executor = TelemetryCapturingA2AExecutor(agent=object(), model_id="m")
    result = SimpleNamespace(
        metrics=SimpleNamespace(
            accumulated_usage={"inputTokens": 5, "outputTokens": 2, "totalTokens": 7}
        )
    )
    metadata = executor._build_metadata(result)

    assert metadata.summarizer_input_tokens == 1_000_000
    assert metadata.summarizer_output_tokens == 1_000_000
    assert metadata.summarizer_cost_usd == 6.0
    # planner-side telemetry is untouched by the summarizer fold-in
    assert metadata.input_tokens == 5
