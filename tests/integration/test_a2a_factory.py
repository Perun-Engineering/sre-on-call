"""Tests for shared/a2a_factory.py — model resolution and executor concurrency."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import busy_state
from shared.a2a_factory import (
    DEFAULT_MODEL_ID,
    TelemetryCapturingA2AExecutor,
    _ping_status,
    _resolve_agent_model,
    _resolve_model,
)
from shared.models import AgentResult, Finding
from shared.tool_result import AGENT_RESULT


def test_ping_status_healthy_when_idle():
    assert busy_state.is_busy() is False
    assert _ping_status() == "Healthy"


async def test_ping_status_reflects_busy_idle_transitions():
    """Acceptance: /ping flips to HealthyBusy around a background task."""
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    task = asyncio.create_task(work())
    busy_state.track(task)
    try:
        assert _ping_status() == "HealthyBusy"
    finally:
        release.set()
        await task
        # Let the done-callback discard the task before re-checking.
        await asyncio.sleep(0)

    assert _ping_status() == "Healthy"


async def test_ping_route_returns_busy_status_over_http():
    """End-to-end through the real FastAPI /ping route AgentCore polls.

    Exercises the mounted handler over HTTP (not just _ping_status), proving
    a background task registered in shared.busy_state flips the wire response
    to HealthyBusy and back. This is the locally-observable equivalent of the
    nmi-dev ping probe, which AgentCore consumes internally — the runtime's
    live ping status is not exposed on any AgentCore API.
    """
    import httpx
    from fastapi import FastAPI

    from shared.a2a_factory import _mount_ping

    app = FastAPI()
    _mount_ping(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:
        assert (await client.get("/ping")).json() == {"status": "Healthy"}

        release = asyncio.Event()

        async def work() -> None:
            await release.wait()

        task = asyncio.create_task(work())
        busy_state.track(task)
        try:
            assert (await client.get("/ping")).json() == {"status": "HealthyBusy"}
        finally:
            release.set()
            await task
            await asyncio.sleep(0)

        assert (await client.get("/ping")).json() == {"status": "Healthy"}


def test_default_model_is_claude_haiku_4_5():
    assert DEFAULT_MODEL_ID == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_resolve_model_uses_default_when_env_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    model = _resolve_model()
    assert model.get_config()["model_id"] == DEFAULT_MODEL_ID  # type: ignore[typeddict-item]


def test_resolve_model_honors_env_override(monkeypatch: pytest.MonkeyPatch):
    override = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    monkeypatch.setenv("MODEL_ID", override)
    assert _resolve_model().get_config()["model_id"] == override  # type: ignore[typeddict-item]


def test_resolve_model_treats_empty_env_as_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_ID", "")
    assert _resolve_model().get_config()["model_id"] == DEFAULT_MODEL_ID  # type: ignore[typeddict-item]


def test_resolve_model_no_guardrail_when_env_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
    cfg = _resolve_model().get_config()
    assert cfg.get("guardrail_id") is None  # type: ignore[typeddict-item]


def test_resolve_model_attaches_guardrail_when_env_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc123")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "3")
    cfg = _resolve_model().get_config()
    assert cfg["guardrail_id"] == "gr-abc123"  # type: ignore[typeddict-item]
    assert cfg["guardrail_version"] == "3"  # type: ignore[typeddict-item]


def test_resolve_model_guardrail_version_defaults_to_draft(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc123")
    monkeypatch.delenv("BEDROCK_GUARDRAIL_VERSION", raising=False)
    cfg = _resolve_model().get_config()
    assert cfg["guardrail_id"] == "gr-abc123"  # type: ignore[typeddict-item]
    assert cfg["guardrail_version"] == "DRAFT"  # type: ignore[typeddict-item]


_SONNET_ID = "us.anthropic.claude-sonnet-4-6-20250929-v1:0"


def _project_config(*, master_model_id: str | None):
    """Minimal valid ProjectConfig: master (optionally carrying a per-agent
    model_id) + one scanner without one."""
    from shared.config import ProjectConfig

    return ProjectConfig(
        **{
            "project": "test",
            "environment": "dev",
            "defaults": {"model_id": DEFAULT_MODEL_ID, "network_mode": "PUBLIC"},
            "agents": {
                "master": {"model_id": master_model_id, "skills": [], "mcps": []},
                "slack_scanner": {"enabled": True, "skills": [], "mcps": []},
            },
        }
    )


def test_resolve_model_override_wins_over_env(monkeypatch: pytest.MonkeyPatch):
    """The per-call override (e.g. SYNTHESIS_MODEL_ID) beats the MODEL_ID env."""
    monkeypatch.setenv("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    resolved = _resolve_model(model_id_override=_SONNET_ID).get_config()
    assert resolved["model_id"] == _SONNET_ID  # type: ignore[typeddict-item]


def test_resolve_agent_model_uses_per_agent_config_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("MODEL_ID", raising=False)
    cfg = _project_config(master_model_id=_SONNET_ID)
    assert _resolve_agent_model(cfg, "master").get_config()["model_id"] == _SONNET_ID  # type: ignore[typeddict-item]
    assert (
        _resolve_agent_model(cfg, "slack_scanner").get_config()["model_id"]  # type: ignore[typeddict-item]
        == DEFAULT_MODEL_ID
    )


def test_resolve_agent_model_per_agent_config_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """Deploy path: MODEL_ID env set to Haiku — the master's per-agent config
    still resolves Sonnet, while an agent without one takes the env value."""
    env_haiku = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setenv("MODEL_ID", env_haiku)
    cfg = _project_config(master_model_id=_SONNET_ID)
    assert _resolve_agent_model(cfg, "master").get_config()["model_id"] == _SONNET_ID  # type: ignore[typeddict-item]
    assert (
        _resolve_agent_model(cfg, "slack_scanner").get_config()["model_id"]  # type: ignore[typeddict-item]
        == env_haiku
    )


def test_resolve_agent_model_unknown_agent_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("MODEL_ID", raising=False)
    cfg = _project_config(master_model_id=_SONNET_ID)
    assert (
        _resolve_agent_model(cfg, "not_in_config").get_config()["model_id"]  # type: ignore[typeddict-item]
        == DEFAULT_MODEL_ID
    )


@pytest.mark.asyncio
async def test_executor_serialises_concurrent_invocations(monkeypatch: pytest.MonkeyPatch):
    """Regression: AgentCore's edge can retry an invoke while the first is in
    flight (observed during 9-second cold-starts). Strands' Agent raises
    ConcurrencyException on overlapping calls. The executor's lock must queue
    the duplicate so it succeeds instead of returning a JSON-RPC Internal error.
    """
    from strands.multiagent.a2a.executor import StrandsA2AExecutor

    in_flight = 0
    max_concurrent = 0
    completion_order: list[int] = []

    async def fake_parent_execute(self, ctx, queue):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        completion_order.append(ctx.request_id)

    monkeypatch.setattr(StrandsA2AExecutor, "execute", fake_parent_execute)

    executor = TelemetryCapturingA2AExecutor(agent=MagicMock())

    requests = [MagicMock(request_id=i) for i in range(3)]
    queues = [MagicMock() for _ in range(3)]

    await asyncio.gather(
        *(executor.execute(r, q) for r, q in zip(requests, queues))
    )

    assert max_concurrent == 1, (
        f"lock failed to serialise: saw {max_concurrent} concurrent invocations"
    )
    assert completion_order == [0, 1, 2], "FIFO ordering not preserved"


def test_executor_forwards_streaming_kwarg():
    """Spec-compliant streaming must reach the parent StrandsA2AExecutor —
    Strands warns otherwise and AgentCore's edge retry depends on it.
    """
    executor = TelemetryCapturingA2AExecutor(
        agent=MagicMock(), enable_a2a_compliant_streaming=True,
    )
    assert executor.enable_a2a_compliant_streaming is True


@pytest.mark.asyncio
async def test_executor_appends_structured_tool_result_footer():
    """The A2A response carries structured tool output even if final prose omits it."""
    structured = AgentResult(
        agent_name="eks",
        status="success",
        findings=[
            Finding(
                source="pod/api-123",
                timestamp="2025-01-15T14:32:00Z",
                content="Pod api-123: phase=Failed",
                severity="critical",
                metadata={"kind": "pod_status"},
            )
        ],
        summary="Inspected 1 item(s). Found 1 finding(s).",
    )

    agent = MagicMock()
    agent.messages = [
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "content": [
                            {"text": "Tool text\n\n" + AGENT_RESULT.encode(structured)}
                        ]
                    }
                }
            ],
        }
    ]
    executor = TelemetryCapturingA2AExecutor(agent=agent)

    class FakeMetrics:
        accumulated_usage = {}

    class FakeResult:
        def __init__(self) -> None:
            self.message = {"content": [{"text": "Concise final analysis."}]}
            self.metrics = FakeMetrics()

        def __str__(self) -> str:
            return "\n".join(
                item.get("text", "") for item in self.message.get("content", [])
            )

    result = FakeResult()
    updater = AsyncMock()

    await executor._handle_agent_result(result, updater)

    text_blocks = [
        item.get("text", "") for item in result.message.get("content", [])
    ]
    assert any("<<<AGENT_RESULT " in text for text in text_blocks)


async def test_executor_emits_structured_datapart_artifact():
    """Issue #24: the executor attaches AGENT_RESULT + AGENT_METADATA as DataParts.

    Round-trips the emitted ``agent_data`` artifact back through the reader
    (``extract_response_data``) to prove the structured payloads survive the
    A2A wire without depending on text-footer parsing.
    """
    from shared.a2a_protocol import extract_response_data
    from shared.agent_telemetry import AGENT_METADATA

    structured = AgentResult(
        agent_name="eks",
        status="success",
        findings=[
            Finding(
                source="pod/api-123",
                timestamp="2025-01-15T14:32:00Z",
                content="Pod api-123: phase=Failed",
                severity="critical",
                metadata={"kind": "pod_status"},
            )
        ],
        summary="Inspected 1 item(s). Found 1 finding(s).",
    )

    agent = MagicMock()
    agent.messages = [
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "content": [
                            {"text": "Tool text\n\n" + AGENT_RESULT.encode(structured)}
                        ]
                    }
                }
            ],
        }
    ]
    model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    executor = TelemetryCapturingA2AExecutor(agent=agent, model_id=model_id)

    class FakeMetrics:
        accumulated_usage = {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150}

    class FakeResult:
        def __init__(self) -> None:
            self.message = {"content": [{"text": "Concise final analysis."}]}
            self.metrics = FakeMetrics()

        def __str__(self) -> str:
            return "\n".join(
                item.get("text", "") for item in self.message.get("content", [])
            )

    updater = AsyncMock()
    await executor._handle_agent_result(FakeResult(), updater)

    data_calls = [
        call for call in updater.add_artifact.call_args_list
        if call.kwargs.get("name") == "agent_data"
    ]
    assert len(data_calls) == 1
    parts = data_calls[0].args[0]
    fake_result = {
        "artifacts": [
            {
                "name": "agent_data",
                "parts": [p.model_dump(by_alias=True, exclude_none=True) for p in parts],
            }
        ]
    }

    data = extract_response_data(fake_result)
    assert AGENT_RESULT.decode_data(data["AGENT_RESULT"]) == structured
    metadata = AGENT_METADATA.decode_data(data["AGENT_METADATA"])
    assert metadata is not None
    assert metadata.model_id == model_id
    assert metadata.input_tokens == 100
    assert metadata.cost_usd is not None  # priced model + token counts


class TestComposeSystemPrompt:
    """The composed prompt = base + each skill's markdown body."""

    def test_no_skills_returns_base_unchanged(self):
        from shared.a2a_factory import _compose_system_prompt
        base = "You are X. Do Y."
        assert _compose_system_prompt(base, []) == base

    def test_single_skill_appends_body_under_heading(self):
        from shared.a2a_factory import _compose_system_prompt
        from shared.skill_loader import Skill
        base = "You are X."
        skill = Skill(name="do_thing", description="d", tool_symbol="x:y", body="# When to use\n\nAlways.")
        composed = _compose_system_prompt(base, [skill])
        assert composed.startswith("You are X.")
        assert "# Available skills" in composed
        assert "## do_thing" in composed
        assert "Always." in composed

    def test_multiple_skills_in_order(self):
        from shared.a2a_factory import _compose_system_prompt
        from shared.skill_loader import Skill
        s1 = Skill(name="alpha", description="d", tool_symbol="x:y", body="alpha body")
        s2 = Skill(name="beta", description="d", tool_symbol="x:y", body="beta body")
        composed = _compose_system_prompt("base", [s1, s2])
        assert composed.index("## alpha") < composed.index("## beta")


class TestSkillsFromBundles:
    """A2A skill catalog is generated from resolved SKILL.md bundles."""

    def test_each_skill_becomes_an_agent_skill_entry(self):
        from shared.a2a_factory import _skills_from_bundles
        from shared.skill_loader import Skill
        s1 = Skill(name="alpha", description="d1", tool_symbol="x:y", body="b")
        s2 = Skill(name="beta", description="d2", tool_symbol="x:y", body="b")
        cards = _skills_from_bundles([s1, s2])
        assert [c.id for c in cards] == ["alpha", "beta"]
        assert cards[0].description == "d1"
        assert cards[1].description == "d2"
