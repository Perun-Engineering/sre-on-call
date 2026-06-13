"""Tests for the StructuredModelCall seam (issue #65)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from shared.model_call import (
    StructuredModelCall,
    drain_usage,
    record_usage,
    reset_usage,
)


class _Out(BaseModel):
    value: str


class _Metrics:
    def __init__(self, usage: dict | None) -> None:
        self.accumulated_usage = usage


class _Result:
    def __init__(self, structured_output, usage: dict | None = None) -> None:
        self.structured_output = structured_output
        self.metrics = _Metrics(usage)


class _FakeStrandsAgent:
    """Stands in for a Strands ``Agent`` exposing the non-deprecated API."""

    def __init__(self, *, result=None, raises=None, delay=0.0) -> None:
        self._result = result
        self._raises = raises
        self._delay = delay
        self.calls: list[str] = []

    async def invoke_async(self, prompt, *, structured_output_model, **kwargs):
        self.calls.append(prompt)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._result


def _mc(agent, **kw) -> StructuredModelCall:
    kw.setdefault("system_prompt", "sys")
    kw.setdefault("timeout_seconds", 5.0)
    return StructuredModelCall(agent=agent, **kw)


class TestCall:
    @pytest.mark.asyncio
    async def test_returns_structured_output(self):
        agent = _FakeStrandsAgent(result=_Result(_Out(value="ok")))
        out = await _mc(agent).call(_Out, "prompt")
        assert isinstance(out, _Out) and out.value == "ok"
        assert agent.calls == ["prompt"]

    @pytest.mark.asyncio
    async def test_fail_open_on_model_error(self):
        agent = _FakeStrandsAgent(raises=RuntimeError("bedrock down"))
        assert await _mc(agent).call(_Out, "p") is None

    @pytest.mark.asyncio
    async def test_fail_open_on_timeout(self):
        agent = _FakeStrandsAgent(result=_Result(_Out(value="late")), delay=0.2)
        assert await _mc(agent, timeout_seconds=0.01).call(_Out, "p") is None

    @pytest.mark.asyncio
    async def test_fail_open_on_build_failure(self, monkeypatch):
        mc = _mc(None, model_id="m")

        def _boom():
            raise RuntimeError("no strands")

        monkeypatch.setattr(mc, "_build_agent", _boom)
        assert await mc.call(_Out, "p") is None


class TestCallOrRaise:
    @pytest.mark.asyncio
    async def test_propagates_model_error(self):
        agent = _FakeStrandsAgent(raises=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await _mc(agent).call_or_raise(_Out, "p")

    @pytest.mark.asyncio
    async def test_propagates_timeout(self):
        agent = _FakeStrandsAgent(result=_Result(_Out(value="x")), delay=0.2)
        with pytest.raises(asyncio.TimeoutError):
            await _mc(agent, timeout_seconds=0.01).call_or_raise(_Out, "p")

    @pytest.mark.asyncio
    async def test_propagates_build_failure(self, monkeypatch):
        mc = _mc(None, model_id="m")
        monkeypatch.setattr(mc, "_build_agent", lambda: (_ for _ in ()).throw(RuntimeError()))
        with pytest.raises(RuntimeError):
            await mc.call_or_raise(_Out, "p")


class TestFromEnv:
    def test_gate_off_returns_none(self, monkeypatch):
        monkeypatch.delenv("MY_GATE", raising=False)
        assert StructuredModelCall.from_env(
            system_prompt="s", gate_env="MY_GATE",
            model_env="MY_MODEL", timeout_env="MY_TIMEOUT", default_timeout=8.0,
        ) is None

    def test_gate_on_returns_instance(self, monkeypatch):
        monkeypatch.setenv("MY_GATE", "true")
        mc = StructuredModelCall.from_env(
            system_prompt="s", gate_env="MY_GATE",
            model_env="MY_MODEL", timeout_env="MY_TIMEOUT", default_timeout=8.0,
        )
        assert isinstance(mc, StructuredModelCall)

    def test_no_gate_always_builds(self, monkeypatch):
        monkeypatch.delenv("MY_MODEL", raising=False)
        mc = StructuredModelCall.from_env(
            system_prompt="s", model_env="MY_MODEL", timeout_env="MY_TIMEOUT",
            default_timeout=60.0, default_model_id="opus-default", temperature=0.0,
        )
        assert mc is not None
        assert mc.model_id == "opus-default"
        assert mc.timeout_seconds == 60.0

    def test_model_env_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("MY_MODEL", "env-model")
        mc = StructuredModelCall.from_env(
            system_prompt="s", model_env="MY_MODEL", timeout_env="MY_TIMEOUT",
            default_timeout=60.0, default_model_id="opus-default",
        )
        assert mc is not None and mc.model_id == "env-model"

    def test_timeout_override_and_bad_value(self, monkeypatch):
        monkeypatch.setenv("MY_TIMEOUT", "3.5")
        mc = StructuredModelCall.from_env(
            system_prompt="s", model_env="MY_MODEL", timeout_env="MY_TIMEOUT",
            default_timeout=8.0,
        )
        assert mc is not None and mc.timeout_seconds == 3.5
        monkeypatch.setenv("MY_TIMEOUT", "not-a-number")
        mc2 = StructuredModelCall.from_env(
            system_prompt="s", model_env="MY_MODEL", timeout_env="MY_TIMEOUT",
            default_timeout=8.0,
        )
        assert mc2 is not None and mc2.timeout_seconds == 8.0


class TestUsageAccumulator:
    def test_drain_blank_when_no_calls(self):
        reset_usage()
        assert drain_usage() == (None, None, None)

    def test_records_tokens_and_cost(self):
        reset_usage()
        record_usage(10, 2, 0.001)
        record_usage(5, 1, 0.0005)
        assert drain_usage() == (15, 3, pytest.approx(0.0015))

    def test_cost_none_when_unpriced_but_tokens_count(self):
        reset_usage()
        record_usage(10, 2, None)  # a call happened, but its model is unpriced
        in_tok, out_tok, cost = drain_usage()
        assert (in_tok, out_tok) == (10, 2)
        assert cost is None

    @pytest.mark.asyncio
    async def test_call_records_usage(self):
        reset_usage()
        agent = _FakeStrandsAgent(
            result=_Result(_Out(value="ok"), usage={"inputTokens": 100, "outputTokens": 20})
        )
        # Price the effective model so cost is non-None.
        mc = _mc(agent, model_id="us.anthropic.claude-opus-4-5")
        await mc.call(_Out, "p")
        in_tok, out_tok, cost = drain_usage()
        assert (in_tok, out_tok) == (100, 20)
        assert cost is not None and cost > 0
