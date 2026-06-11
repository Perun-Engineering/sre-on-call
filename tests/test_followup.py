"""Tests for the master's Stage 2 bounded follow-up planner (issue #28)."""

from __future__ import annotations

import asyncio

import pytest

from agents.master.followup import (
    DEFAULT_MAX_FOLLOWUP_AGENTS,
    FollowupCandidate,
    FollowupDecision,
    FollowupDispatch,
    FollowupPlanner,
    build_followup_prompt,
)
from shared.models import AgentResult, AlertContext, Finding


def _alert() -> AlertContext:
    return AlertContext(
        investigation_id="inv-followup-1",
        platform="slack",
        channel_id="C1",
        message_id="m1",
        alert_text="PaymentService 5xx rate spiked to 40% in us-east-1",
        alert_timestamp="2026-06-11T10:00:00Z",
        investigation_window=("2026-06-11T09:55:00Z", "2026-06-11T10:05:00Z"),
    )


def _result(agent: str, summary: str) -> AgentResult:
    return AgentResult(
        agent_name=agent,
        status="success",
        findings=[
            Finding(
                source="log-group/payment",
                timestamp="2026-06-11T10:01:00Z",
                content="exit 137 (OOMKilled)",
                severity="critical",
            )
        ],
        summary=summary,
    )


def _candidates() -> list[FollowupCandidate]:
    return [
        FollowupCandidate("cloudwatch_logs", "CloudWatch Logs"),
        FollowupCandidate("eks", "EKS cluster state"),
    ]


class TestBuildFollowupPrompt:
    def test_prompt_includes_alert_and_current_results(self):
        results = {"cloudwatch_logs": _result("cloudwatch_logs", "saw OOM restarts")}
        prompt = build_followup_prompt(_alert(), results, _candidates())
        assert "PaymentService 5xx rate spiked to 40%" in prompt
        assert "saw OOM restarts" in prompt
        assert "eks" in prompt


class _FakeAgent:
    def __init__(
        self,
        *,
        returns: FollowupDecision | None = None,
        raises: Exception | None = None,
        delay: float = 0.0,
    ):
        self._returns = returns
        self._raises = raises
        self._delay = delay
        self.calls: list[str] = []

    async def structured_output_async(
        self, output_model: type[FollowupDecision], prompt: str
    ) -> FollowupDecision:
        self.calls.append(prompt)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        assert self._returns is not None
        return self._returns


class TestPlan:
    @pytest.mark.asyncio
    async def test_returns_refined_dispatches(self):
        agent = _FakeAgent(
            returns=FollowupDecision(
                should_followup=True,
                dispatches=[
                    FollowupDispatch(agent_id="eks", hint="describe payment pods now")
                ],
                rationale="logs point at OOM; confirm in k8s",
            )
        )
        plan = await FollowupPlanner(agent=agent).plan(_alert(), {}, _candidates())
        assert plan == [("eks", "describe payment pods now")]

    @pytest.mark.asyncio
    async def test_caps_at_max_agents(self):
        many = [FollowupCandidate(f"a{i}", f"agent {i}") for i in range(5)]
        agent = _FakeAgent(
            returns=FollowupDecision(
                should_followup=True,
                dispatches=[FollowupDispatch(agent_id=f"a{i}") for i in range(5)],
                rationale="all of them",
            )
        )
        plan = await FollowupPlanner(agent=agent, max_agents=2).plan(_alert(), {}, many)
        assert len(plan) == 2
        assert [aid for aid, _ in plan] == ["a0", "a1"]

    @pytest.mark.asyncio
    async def test_should_followup_false_returns_empty(self):
        agent = _FakeAgent(
            returns=FollowupDecision(
                should_followup=False, dispatches=[], rationale="enough evidence"
            )
        )
        assert await FollowupPlanner(agent=agent).plan(_alert(), {}, _candidates()) == []

    @pytest.mark.asyncio
    async def test_unknown_ids_dropped(self):
        agent = _FakeAgent(
            returns=FollowupDecision(
                should_followup=True,
                dispatches=[
                    FollowupDispatch(agent_id="ghost", hint="x"),
                    FollowupDispatch(agent_id="eks", hint="real"),
                ],
                rationale="r",
            )
        )
        plan = await FollowupPlanner(agent=agent).plan(_alert(), {}, _candidates())
        assert plan == [("eks", "real")]

    @pytest.mark.asyncio
    async def test_no_candidates_returns_empty(self):
        agent = _FakeAgent(returns=FollowupDecision(should_followup=True, dispatches=[], rationale="r"))
        assert await FollowupPlanner(agent=agent).plan(_alert(), {}, []) == []

    @pytest.mark.asyncio
    async def test_fail_open_on_model_error(self):
        agent = _FakeAgent(raises=RuntimeError("bedrock down"))
        assert await FollowupPlanner(agent=agent).plan(_alert(), {}, _candidates()) == []

    @pytest.mark.asyncio
    async def test_fail_open_on_timeout(self):
        agent = _FakeAgent(
            returns=FollowupDecision(
                should_followup=True,
                dispatches=[FollowupDispatch(agent_id="eks")],
                rationale="r",
            ),
            delay=0.2,
        )
        planner = FollowupPlanner(agent=agent, timeout_seconds=0.01)
        assert await planner.plan(_alert(), {}, _candidates()) == []


class TestFromEnv:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("FOLLOWUP_ROUND_ENABLED", raising=False)
        assert FollowupPlanner.from_env() is None

    def test_enabled_when_flag_truthy(self, monkeypatch):
        monkeypatch.setenv("FOLLOWUP_ROUND_ENABLED", "true")
        planner = FollowupPlanner.from_env()
        assert isinstance(planner, FollowupPlanner)
        assert planner.max_agents == DEFAULT_MAX_FOLLOWUP_AGENTS

    def test_max_agents_override(self, monkeypatch):
        monkeypatch.setenv("FOLLOWUP_ROUND_ENABLED", "1")
        monkeypatch.setenv("FOLLOWUP_MAX_AGENTS", "3")
        planner = FollowupPlanner.from_env()
        assert planner is not None and planner.max_agents == 3
