"""Tests for the bounded agentic loop control (issue #58).

These exercise the *structural* bound directly at the loop level: the hook
counts tool-use cycles and vetoes any tool call that would exceed the hard
ceiling or start a new pass past the deadline. The model's cooperation is
irrelevant — these assertions hold regardless of what the model emits.
"""

from __future__ import annotations

from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent, HookRegistry

from shared.bounded_loop import (
    CYCLE_CAP_MESSAGE,
    DEADLINE_MESSAGE,
    BoundedLoopHook,
)


class _Clock:
    """A hand-cranked monotonic clock."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _before_model() -> BeforeModelCallEvent:
    return BeforeModelCallEvent(agent=object())  # type: ignore[arg-type]


def _before_tool(tool_id: str = "t") -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=object(),  # type: ignore[arg-type]
        selected_tool=None,
        tool_use={"toolUseId": tool_id, "name": "gather", "input": {}},  # type: ignore[arg-type]
        invocation_state={},
    )


def _run_cycle(hook: BoundedLoopHook, *, tools: int = 1) -> list[BeforeToolCallEvent]:
    """Simulate one model turn that calls *tools* tools; return the tool events."""
    hook._on_before_model(_before_model())
    events = []
    for i in range(tools):
        ev = _before_tool(f"t{i}")
        hook._on_before_tool(ev)
        events.append(ev)
    return events


def test_first_four_cycles_allowed_fifth_cancelled() -> None:
    hook = BoundedLoopHook(max_cycles=4)
    hook.arm(deadline_seconds=None)

    for n in range(1, 5):
        events = _run_cycle(hook)
        assert not events[0].cancel_tool, f"cycle {n} must run"
        assert hook.cycles == n

    fifth = _run_cycle(hook)
    assert fifth[0].cancel_tool == CYCLE_CAP_MESSAGE.format(max_cycles=4)


def test_every_pass_past_the_cap_is_vetoed() -> None:
    # The model cannot talk its way past the ceiling: once the cap is hit,
    # every subsequent pass is cancelled, no matter how many it attempts.
    hook = BoundedLoopHook(max_cycles=2)
    hook.arm(deadline_seconds=None)
    allowed = [not _run_cycle(hook)[0].cancel_tool for _ in range(6)]
    assert allowed == [True, True, False, False, False, False]


def test_parallel_tools_in_one_turn_count_as_one_cycle() -> None:
    hook = BoundedLoopHook(max_cycles=4)
    hook.arm(deadline_seconds=None)
    _run_cycle(hook, tools=3)
    assert hook.cycles == 1


def test_short_deadline_runs_first_pass_then_stops() -> None:
    clock = _Clock()
    hook = BoundedLoopHook(max_cycles=4, emit_reserve_seconds=5.0, clock=clock)
    hook.arm(deadline_seconds=10.0)

    # Pass 1 always runs — it is not a "new" pass.
    first = _run_cycle(hook)
    assert not first[0].cancel_tool

    # Budget is now spent past the emit reserve; pass 2 must be vetoed.
    clock.advance(6.0)  # remaining 4s < 5s emit reserve
    second = _run_cycle(hook)
    assert second[0].cancel_tool == DEADLINE_MESSAGE
    assert hook.cycles == 2  # counted, but its tools were cancelled


def test_deadline_does_not_block_first_pass_even_when_already_exhausted() -> None:
    clock = _Clock()
    hook = BoundedLoopHook(max_cycles=4, emit_reserve_seconds=5.0, clock=clock)
    hook.arm(deadline_seconds=1.0)  # tiny budget, already inside emit reserve
    first = _run_cycle(hook)
    assert not first[0].cancel_tool, "iteration-1 must run regardless of deadline"


def test_deadline_allows_second_pass_when_budget_remains() -> None:
    clock = _Clock()
    hook = BoundedLoopHook(max_cycles=4, emit_reserve_seconds=5.0, clock=clock)
    hook.arm(deadline_seconds=60.0)
    _run_cycle(hook)
    clock.advance(1.0)
    second = _run_cycle(hook)
    assert not second[0].cancel_tool


def test_arm_resets_state_between_requests() -> None:
    hook = BoundedLoopHook(max_cycles=2)
    hook.arm(deadline_seconds=None)
    _run_cycle(hook)
    _run_cycle(hook)
    assert _run_cycle(hook)[0].cancel_tool  # 3rd cycle vetoed
    hook.arm(deadline_seconds=None)
    assert hook.cycles == 0
    assert not _run_cycle(hook)[0].cancel_tool  # fresh budget


def test_register_hooks_wires_callbacks() -> None:
    hook = BoundedLoopHook(max_cycles=4)
    hook.arm(deadline_seconds=None)
    registry = HookRegistry()
    registry.add_hook(hook)

    registry.invoke_callbacks(_before_model())
    ev = _before_tool()
    registry.invoke_callbacks(ev)
    assert not ev.cancel_tool
    assert hook.cycles == 1


def test_max_cycles_must_be_positive() -> None:
    try:
        BoundedLoopHook(max_cycles=0)
    except ValueError:
        return
    raise AssertionError("max_cycles=0 must raise ValueError")
