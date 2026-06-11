"""Tests for the master's Phase 0.5 LLM routing (agent selection + hints)."""

from __future__ import annotations

import asyncio

import pytest

from agents.master.routing import (
    AgentCandidate,
    AgentRouter,
    RoutingDecision,
    AgentSelection,
    RoutingResult,
    build_routing_prompt,
)
from shared.models import AlertContext


def _alert() -> AlertContext:
    return AlertContext(
        investigation_id="inv-route-1",
        platform="slack",
        channel_id="C1",
        message_id="m1",
        alert_text="PaymentService 5xx rate spiked to 40% in us-east-1",
        alert_timestamp="2026-06-11T10:00:00Z",
        investigation_window=("2026-06-11T09:55:00Z", "2026-06-11T10:05:00Z"),
    )


def _candidates() -> list[AgentCandidate]:
    return [
        AgentCandidate("cloudwatch_logs", "CloudWatch Logs — application & platform logs"),
        AgentCandidate("eks", "EKS — Kubernetes cluster & pod state"),
        AgentCandidate("slack_scanner", "Slack Scanner — recent chatter in ops channels"),
    ]


class TestBuildRoutingPrompt:
    def test_prompt_includes_alert_text(self):
        prompt = build_routing_prompt(_alert(), _candidates())
        assert "PaymentService 5xx rate spiked to 40%" in prompt

    def test_prompt_lists_every_candidate_id_and_description(self):
        prompt = build_routing_prompt(_alert(), _candidates())
        for c in _candidates():
            assert c.agent_id in prompt
            assert c.description in prompt


class _FakeAgent:
    """Stand-in Strands agent for structured_output_async."""

    def __init__(
        self,
        *,
        returns: RoutingDecision | None = None,
        raises: Exception | None = None,
        delay: float = 0.0,
    ):
        self._returns = returns
        self._raises = raises
        self._delay = delay
        self.calls: list[str] = []

    async def structured_output_async(
        self, output_model: type[RoutingDecision], prompt: str
    ) -> RoutingDecision:
        self.calls.append(prompt)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        assert self._returns is not None
        return self._returns


def _decision(*selections: AgentSelection, rationale: str = "r") -> RoutingDecision:
    return RoutingDecision(selections=list(selections), rationale=rationale)


class TestRoute:
    @pytest.mark.asyncio
    async def test_selects_subset_and_carries_hints(self):
        agent = _FakeAgent(
            returns=_decision(
                AgentSelection(
                    agent_id="cloudwatch_logs",
                    dispatch=True,
                    hint="focus on payment log group",
                ),
                AgentSelection(agent_id="eks", dispatch=True, hint="check payment pods"),
                AgentSelection(
                    agent_id="slack_scanner", dispatch=False, reason="no chatter relevance"
                ),
            )
        )
        result = await AgentRouter(agent=agent).route(_alert(), _candidates())

        assert isinstance(result, RoutingResult)
        assert set(result.selected) == {"cloudwatch_logs", "eks"}
        assert result.selected["cloudwatch_logs"] == "focus on payment log group"
        assert result.skipped == {"slack_scanner": "no chatter relevance"}

    @pytest.mark.asyncio
    async def test_unmentioned_candidate_defaults_to_dispatch(self):
        # Model only spoke about slack_scanner; the other two must still dispatch.
        agent = _FakeAgent(
            returns=_decision(
                AgentSelection(agent_id="slack_scanner", dispatch=False, reason="n/a"),
            )
        )
        result = await AgentRouter(agent=agent).route(_alert(), _candidates())

        assert result is not None
        assert set(result.selected) == {"cloudwatch_logs", "eks"}
        assert set(result.skipped) == {"slack_scanner"}

    @pytest.mark.asyncio
    async def test_unknown_agent_ids_are_ignored(self):
        agent = _FakeAgent(
            returns=_decision(
                AgentSelection(agent_id="ghost_agent", dispatch=False, reason="x"),
                AgentSelection(agent_id="eks", dispatch=False, reason="not k8s"),
            )
        )
        result = await AgentRouter(agent=agent).route(_alert(), _candidates())

        assert result is not None
        # ghost_agent never appears; eks is the only real skip.
        assert "ghost_agent" not in result.selected
        assert "ghost_agent" not in result.skipped
        assert set(result.skipped) == {"eks"}

    @pytest.mark.asyncio
    async def test_skipping_every_agent_fails_open_to_none(self):
        agent = _FakeAgent(
            returns=_decision(
                *[
                    AgentSelection(agent_id=c.agent_id, dispatch=False, reason="x")
                    for c in _candidates()
                ]
            )
        )
        assert await AgentRouter(agent=agent).route(_alert(), _candidates()) is None

    @pytest.mark.asyncio
    async def test_fail_open_on_model_error(self):
        agent = _FakeAgent(raises=RuntimeError("bedrock down"))
        assert await AgentRouter(agent=agent).route(_alert(), _candidates()) is None

    @pytest.mark.asyncio
    async def test_fail_open_on_timeout(self):
        agent = _FakeAgent(
            returns=_decision(
                AgentSelection(agent_id="eks", dispatch=True),
            ),
            delay=0.2,
        )
        router = AgentRouter(agent=agent, timeout_seconds=0.01)
        assert await router.route(_alert(), _candidates()) is None

    @pytest.mark.asyncio
    async def test_no_candidates_returns_none(self):
        assert await AgentRouter(agent=_FakeAgent()).route(_alert(), []) is None


class TestFromEnv:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ALERT_ROUTING_ENABLED", raising=False)
        assert AgentRouter.from_env() is None

    def test_enabled_when_flag_truthy(self, monkeypatch):
        monkeypatch.setenv("ALERT_ROUTING_ENABLED", "true")
        router = AgentRouter.from_env()
        assert isinstance(router, AgentRouter)

    def test_timeout_override(self, monkeypatch):
        monkeypatch.setenv("ALERT_ROUTING_ENABLED", "1")
        monkeypatch.setenv("ROUTING_TIMEOUT_SECONDS", "3.5")
        router = AgentRouter.from_env()
        assert router is not None and router.timeout_seconds == 3.5
