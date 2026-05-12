"""Unit tests for the Master Agent's investigate_alert tool wrapper."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from agents.master.tools import _alert_context_from_payload, investigate_alert
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
        assert "ENABLED_AGENTS" in result
