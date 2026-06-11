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
