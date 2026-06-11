"""Tests for the Fanout seam and the RoutingHTTPClient transport.

Fanout is generic over the per-agent result type, so these tests drive its
mechanics (dispatch / harvest / cancel) with trivial coroutines — no real
A2A traffic. The reply→domain mapping and trace live in the orchestrators and
are covered by their own suites.
"""

from __future__ import annotations

import asyncio

import pytest

from shared.a2a_client import RoutingHTTPClient
from shared.agents import AgentRegistry
from shared.config import AgentConfig, Defaults, ProjectConfig
from shared.fanout import Fanout


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Make Agent.resolve_endpoint fall back to catalogue localhost defaults."""
    for var in (
        "SLACK_SCANNER_AGENT_RUNTIME_ARN", "SLACK_SCANNER_AGENT_URL",
        "DISCORD_SCANNER_AGENT_RUNTIME_ARN", "DISCORD_SCANNER_AGENT_URL",
        "CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN", "CLOUDWATCH_LOGS_AGENT_URL",
        "EKS_AGENT_RUNTIME_ARN", "EKS_AGENT_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _registry() -> AgentRegistry:
    """slack_scanner + cloudwatch_logs + eks active; discord_scanner disabled."""
    return AgentRegistry(ProjectConfig(
        project="test",
        environment="dev",
        defaults=Defaults(model_id="anthropic.claude-test"),
        agents={
            "master": AgentConfig(skills=["investigate_alert"]),
            "slack_scanner": AgentConfig(enabled=True, skills=["scan_slack_channels"]),
            "discord_scanner": AgentConfig(enabled=False, skills=["scan_discord_channels"]),
            "cloudwatch_logs": AgentConfig(enabled=True, skills=["query_cloudwatch_logs"]),
            "eks": AgentConfig(enabled=True, network_mode="VPC", skills=["gather_eks_state"]),
        },
    ))


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post_json(self, url: str, payload: dict) -> dict:
        self.calls.append(url)
        return {"echo": url}


def _fanout() -> Fanout:
    return Fanout(http_client=_FakeTransport(), registry=_registry())


# ---------------------------------------------------------------------------
# RoutingHTTPClient
# ---------------------------------------------------------------------------


class TestRoutingHTTPClient:
    @pytest.mark.asyncio
    async def test_arn_routes_to_agentcore(self):
        aio, core = _FakeTransport(), _FakeTransport()
        router = RoutingHTTPClient(aiohttp_client=aio, agentcore_client=core)
        await router.post_json("arn:aws:bedrock-agentcore:us-east-1:0:r/x", {})
        assert core.calls == ["arn:aws:bedrock-agentcore:us-east-1:0:r/x"]
        assert aio.calls == []

    @pytest.mark.asyncio
    async def test_url_routes_to_aiohttp(self):
        aio, core = _FakeTransport(), _FakeTransport()
        router = RoutingHTTPClient(aiohttp_client=aio, agentcore_client=core)
        await router.post_json("http://localhost:9001", {})
        assert aio.calls == ["http://localhost:9001"]
        assert core.calls == []

    @pytest.mark.asyncio
    async def test_mixed_deployment_routes_each_endpoint(self):
        aio, core = _FakeTransport(), _FakeTransport()
        router = RoutingHTTPClient(aiohttp_client=aio, agentcore_client=core)
        await router.post_json("arn:aws:x", {})
        await router.post_json("http://localhost:9005", {})
        assert core.calls == ["arn:aws:x"]
        assert aio.calls == ["http://localhost:9005"]


# ---------------------------------------------------------------------------
# Fanout construction + dispatch
# ---------------------------------------------------------------------------


class TestFanoutTargets:
    def test_endpoints_are_active_specialized_in_order(self):
        fan = _fanout()
        assert list(fan.agent_endpoints) == ["slack_scanner", "cloudwatch_logs", "eks"]
        assert [a.id for a in fan.targets] == ["slack_scanner", "cloudwatch_logs", "eks"]

    def test_disabled_excludes_active_and_includes_disabled(self):
        fan = _fanout()
        assert [a.id for a in fan.disabled] == ["discord_scanner"]


class TestFanoutDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_spawns_one_task_per_active_agent(self):
        fan = _fanout()

        async def make_coro(agent_id: str) -> str:
            return f"done-{agent_id}"

        pending = fan.dispatch(make_coro)
        try:
            assert set(pending) == {"slack_scanner", "cloudwatch_logs", "eks"}
        finally:
            settled, _ = await Fanout.harvest(pending, timeout=1.0)
        assert settled["eks"] == "done-eks"

    @pytest.mark.asyncio
    async def test_dispatch_subset_only_spawns_named_agents(self):
        fan = _fanout()

        async def make_coro(agent_id: str) -> str:
            return f"done-{agent_id}"

        pending = fan.dispatch(make_coro, agent_ids=["eks", "slack_scanner"])
        try:
            assert set(pending) == {"eks", "slack_scanner"}
        finally:
            await Fanout.harvest(pending, timeout=1.0)

    @pytest.mark.asyncio
    async def test_dispatch_subset_ignores_unknown_ids(self):
        fan = _fanout()

        async def make_coro(agent_id: str) -> str:
            return f"done-{agent_id}"

        pending = fan.dispatch(make_coro, agent_ids=["eks", "ghost"])
        try:
            assert set(pending) == {"eks"}
        finally:
            await Fanout.harvest(pending, timeout=1.0)


# ---------------------------------------------------------------------------
# Fanout harvest + cancel
# ---------------------------------------------------------------------------


class TestFanoutHarvest:
    @pytest.mark.asyncio
    async def test_empty_pending_returns_empty(self):
        assert await Fanout.harvest({}, timeout=0.0) == ({}, {})

    @pytest.mark.asyncio
    async def test_splits_settled_and_still_pending(self):
        async def fast() -> str:
            return "fast"

        async def slow() -> str:
            await asyncio.sleep(0.2)
            return "slow"

        pending = {
            "a": asyncio.create_task(fast()),
            "b": asyncio.create_task(slow()),
        }
        settled, still = await Fanout.harvest(pending, timeout=0.02)
        assert settled == {"a": "fast"}
        assert set(still) == {"b"}
        await Fanout.cancel(still)

    @pytest.mark.asyncio
    async def test_re_wait_continues_same_in_flight_task(self):
        async def slow() -> str:
            await asyncio.sleep(0.05)
            return "slow"

        task = asyncio.create_task(slow())
        settled, still = await Fanout.harvest({"a": task}, timeout=0.01)
        assert settled == {} and set(still) == {"a"}
        # Re-wait the SAME task — no re-dispatch.
        settled2, still2 = await Fanout.harvest(still, timeout=0.5)
        assert settled2 == {"a": "slow"} and still2 == {}

    @pytest.mark.asyncio
    async def test_raised_exception_returned_as_value(self):
        async def boom() -> str:
            raise ValueError("kaboom")

        settled, still = await Fanout.harvest(
            {"a": asyncio.create_task(boom())}, timeout=1.0
        )
        assert still == {}
        assert isinstance(settled["a"], ValueError)
        assert str(settled["a"]) == "kaboom"

    @pytest.mark.asyncio
    async def test_cancel_drains_pending(self):
        async def forever() -> str:
            await asyncio.sleep(10)
            return "never"

        task = asyncio.create_task(forever())
        await Fanout.cancel({"a": task})
        assert task.cancelled()
