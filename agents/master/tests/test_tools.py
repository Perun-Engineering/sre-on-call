"""Unit tests for the Master Agent's investigate_alert tool wrapper."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from agents.master.tools import (
    _alert_context_from_payload,
    capture_status_snapshot,
    investigate_alert,
)
from shared.models import AlertContext


@pytest.fixture
def alert_context() -> AlertContext:
    return AlertContext(
        investigation_id="inv-tool-001",
        platform="slack",
        channel_id="C12345",
        message_id="1705312320.000100",
        alert_text="High error rate on payments",
        alert_timestamp="2025-01-15T14:32:00Z",
        investigation_window=("2025-01-15T14:27:00Z", "2025-01-15T14:37:00Z"),
        platform_metadata={"thread_ts": "1705312320.000100"},
    )


def _serialize_via_lambda_path(ctx: AlertContext) -> str:
    """Mimic the Lambda intake's serialization (asdict + json.dumps)."""
    return json.dumps(asdict(ctx))


class TestAlertContextRoundTrip:
    def test_window_tuple_survives_json_roundtrip(self, alert_context):
        payload = json.loads(_serialize_via_lambda_path(alert_context))
        ctx = _alert_context_from_payload(payload)
        assert ctx.investigation_window == (
            "2025-01-15T14:27:00Z",
            "2025-01-15T14:37:00Z",
        )
        assert isinstance(ctx.investigation_window, tuple)

    def test_optional_fields_default(self, alert_context):
        payload = json.loads(_serialize_via_lambda_path(alert_context))
        ctx = _alert_context_from_payload(payload)
        assert ctx.experiment_id is None
        assert ctx.variant_id is None


class TestInvestigateAlertTool:
    @pytest.mark.asyncio
    async def test_kicks_off_orchestrator_in_background(
        self, alert_context, monkeypatch
    ):
        captured: dict = {}
        started = asyncio.Event()
        finish = asyncio.Event()

        class StubOrchestrator:
            def __init__(self):
                self.agent_endpoints = {"eks": "arn:eks"}

            async def investigate(self, ctx: AlertContext) -> None:
                captured["ctx"] = ctx
                started.set()
                # Block until the test releases us — proves the tool returned
                # before the orchestrator finished.
                await finish.wait()

        monkeypatch.setattr(
            "agents.master.tools.InvestigationOrchestrator", StubOrchestrator,
        )

        payload = _serialize_via_lambda_path(alert_context)
        result = await investigate_alert(payload)

        assert "started" in result
        assert "eks" in result
        # The tool must have returned without waiting for investigate() to finish.
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert captured["ctx"].investigation_id == "inv-tool-001"
        finish.set()
        # Drain the background task to keep pytest's loop clean.
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_aborts_when_no_agents_enabled(
        self, alert_context, monkeypatch
    ):
        class EmptyOrchestrator:
            def __init__(self):
                self.agent_endpoints = {}

            async def investigate(self, ctx: AlertContext) -> None:
                raise AssertionError("Should not be called when no agents enabled")

        monkeypatch.setattr(
            "agents.master.tools.InvestigationOrchestrator", EmptyOrchestrator,
        )

        payload = _serialize_via_lambda_path(alert_context)
        result = await investigate_alert(payload)

        assert "aborted" in result.lower()
        assert "config.yaml" in result


# ---------------------------------------------------------------------------
# capture_status_snapshot
# ---------------------------------------------------------------------------


class TestCaptureStatusSnapshotTool:
    @pytest.mark.asyncio
    async def test_kicks_off_orchestrator_in_background(self, monkeypatch):
        captured: dict = {}
        started = asyncio.Event()
        finish = asyncio.Event()

        class StubOrchestrator:
            def __init__(self):
                self.agent_endpoints = {"slack_scanner": "http://localhost:9001"}

            async def capture(self, request: dict) -> None:
                captured["request"] = request
                started.set()
                # Block to prove the tool returns before capture completes
                await finish.wait()

        monkeypatch.setattr(
            "agents.master.tools.StatusSnapshotOrchestrator", StubOrchestrator,
        )

        request = {
            "task": "snapshot",
            "platform": "slack",
            "channel_id": "C1",
            "user_id": "U1",
            "requested_at": "2026-05-28T19:00:00+00:00",
        }
        result = await capture_status_snapshot(json.dumps(request))

        assert "started" in result.lower()
        assert "slack_scanner" in result
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert captured["request"] == request
        finish.set()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_rejects_unexpected_task_field(self, monkeypatch):
        called = False

        class ShouldNotConstruct:
            def __init__(self):
                nonlocal called
                called = True

        monkeypatch.setattr(
            "agents.master.tools.StatusSnapshotOrchestrator", ShouldNotConstruct,
        )

        request = {"task": "investigation", "platform": "slack", "channel_id": "C1"}
        result = await capture_status_snapshot(json.dumps(request))

        assert "unexpected task" in result.lower()
        assert "'investigation'" in result
        assert called is False, "orchestrator must not be constructed for non-snapshot tasks"

    @pytest.mark.asyncio
    async def test_no_active_agents_still_completes_dispatch(self, monkeypatch):
        finish = asyncio.Event()

        class EmptyOrchestrator:
            def __init__(self):
                self.agent_endpoints: dict[str, str] = {}

            async def capture(self, request: dict) -> None:
                # /status with no active agents still posts a master-only
                # snapshot — capture() is allowed to run.
                await finish.wait()

        monkeypatch.setattr(
            "agents.master.tools.StatusSnapshotOrchestrator", EmptyOrchestrator,
        )

        request = {
            "task": "snapshot",
            "platform": "slack",
            "channel_id": "C1",
            "user_id": "U1",
            "requested_at": "2026-05-28T19:00:00+00:00",
        }
        result = await capture_status_snapshot(json.dumps(request))

        assert "started" in result.lower()
        assert "no active specialized agents" in result.lower()
        finish.set()
        await asyncio.sleep(0)
