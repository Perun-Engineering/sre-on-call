"""Investigation death notice — issue #22.

The Lambda fires the master's ``investigate_alert`` tool and discards the
response; the master posts its own "Investigation Started" notice. If the
background investigation task raises *before* posting the Incident Report,
the channel sees "Investigation Started" and then silence. The tool's
done-callback must inspect the task exception and post a short failure
notice to the originating channel/thread.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from agents.master import tools
from shared.models import AlertContext
from shared.report_renderer import FailureNoticeSections, SlackReportRenderer


@pytest.fixture
def alert_context() -> AlertContext:
    return AlertContext(
        investigation_id="inv-test-022",
        platform="slack",
        channel_id="C0B2N09Q23W",
        message_id="1705312320.000100",
        alert_text="High CPU usage on service-api",
        alert_timestamp="2025-01-15T14:32:00Z",
        investigation_window=("2025-01-15T14:27:00Z", "2025-01-15T14:37:00Z"),
    )


class RecordingChatPlatform:
    """Captures every ``deliver`` call so the test can assert what was posted."""

    name = "slack"

    def __init__(self) -> None:
        self._renderer = SlackReportRenderer()
        self.deliveries: list[tuple] = []  # (target, payload, rendered_text)

    def ingest(self, headers, raw_body):  # pragma: no cover - not exercised
        raise NotImplementedError

    def ack(self, command, text):  # pragma: no cover - not exercised
        raise NotImplementedError

    async def deliver(self, target, payload) -> str:
        text = self._renderer.render(payload)
        self.deliveries.append((target, payload, text))
        return text


class _OrchestratorStub:
    """Stand-in whose ``investigate`` raises, simulating an early crash."""

    def __init__(self, *args, **kwargs) -> None:
        # Non-empty so ``investigate_alert`` reaches the background dispatch.
        self.agent_endpoints = {"cloudwatch_logs": "http://localhost:9004"}

    async def investigate(self, alert_context: AlertContext) -> None:
        raise RuntimeError("boom before initial report")


async def _drain_until(predicate, *, ticks: int = 100) -> None:
    """Yield to the loop until *predicate* holds or *ticks* are exhausted."""
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0.01)


async def test_early_crash_posts_failure_notice(alert_context, monkeypatch):
    platform = RecordingChatPlatform()
    monkeypatch.setattr(tools, "InvestigationOrchestrator", _OrchestratorStub)
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.investigate_alert(json.dumps(asdict(alert_context)))
    await _drain_until(lambda: bool(platform.deliveries))

    assert len(platform.deliveries) == 1
    target, payload, text = platform.deliveries[0]
    assert isinstance(payload, FailureNoticeSections)
    assert payload.investigation_id == "inv-test-022"
    # Routed back to the originating channel/thread.
    assert target.channel_id == "C0B2N09Q23W"
    assert target.thread_anchor == "1705312320.000100"
    # The investigation_id is surfaced so the trace archive can be consulted.
    assert "inv-test-022" in text


async def test_successful_investigation_posts_no_failure_notice(
    alert_context, monkeypatch
):
    platform = RecordingChatPlatform()

    class _HealthyOrchestrator(_OrchestratorStub):
        async def investigate(self, alert_context: AlertContext) -> None:
            return None  # posts its own report; no exception

    monkeypatch.setattr(tools, "InvestigationOrchestrator", _HealthyOrchestrator)
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.investigate_alert(json.dumps(asdict(alert_context)))
    # Give any spuriously-scheduled notice a chance to land.
    await asyncio.sleep(0.05)

    assert platform.deliveries == []
