"""Unit tests for shared.busy_state — the /ping HealthyBusy seam."""

from __future__ import annotations

import asyncio

from shared import busy_state


async def test_idle_by_default():
    assert busy_state.busy_count() == 0
    assert busy_state.is_busy() is False


async def test_busy_while_task_in_flight_then_idle():
    started = asyncio.Event()
    release = asyncio.Event()

    async def work() -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(work())
    busy_state.track(task)
    await started.wait()

    assert busy_state.is_busy() is True
    assert busy_state.busy_count() == 1

    release.set()
    await task
    # The done-callback that discards the task runs on the next loop tick.
    await asyncio.sleep(0)

    assert busy_state.is_busy() is False
    assert busy_state.busy_count() == 0


async def test_busy_count_tracks_concurrent_tasks():
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    tasks = [asyncio.create_task(work()) for _ in range(3)]
    for task in tasks:
        busy_state.track(task)

    assert busy_state.busy_count() == 3

    release.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)

    assert busy_state.busy_count() == 0


async def test_track_holds_strong_reference():
    # The caller drops its reference immediately; the seam must keep the task
    # alive until it completes.
    ran = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0)
        ran.set()

    busy_state.track(asyncio.create_task(work()))
    await asyncio.wait_for(ran.wait(), timeout=1)
    await asyncio.sleep(0)
    assert busy_state.busy_count() == 0
