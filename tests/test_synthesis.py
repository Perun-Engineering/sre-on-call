"""Tests for the master's post-harvest LLM synthesis (Analysis section)."""

from __future__ import annotations

import pytest

from agents.master.synthesis import (
    AnalysisSynthesizer,
    IncidentAnalysis,
    build_synthesis_prompt,
)
from shared.models import AgentFailure, AgentResult, AlertContext, Finding
from tests.fakes import FakeModelCall


def _alert() -> AlertContext:
    return AlertContext(
        investigation_id="inv-synth-1",
        platform="slack",
        channel_id="C1",
        message_id="m1",
        alert_text="PaymentService 5xx rate spiked to 40% in us-east-1",
        alert_timestamp="2026-06-11T10:00:00Z",
        investigation_window=("2026-06-11T09:55:00Z", "2026-06-11T10:05:00Z"),
    )


def _result(agent: str, summary: str, finding: str) -> AgentResult:
    return AgentResult(
        agent_name=agent,
        status="success",
        findings=[
            Finding(
                source="log-group/payment",
                timestamp="2026-06-11T10:01:00Z",
                content=finding,
                severity="critical",
            )
        ],
        summary=summary,
    )


class TestBuildSynthesisPrompt:
    def test_prompt_includes_alert_text(self):
        prompt = build_synthesis_prompt(_alert(), {})
        assert "PaymentService 5xx rate spiked to 40%" in prompt

    def test_prompt_includes_each_agent_summary_and_findings(self):
        results: dict[str, AgentResult | AgentFailure] = {
            "cloudwatch_logs": _result(
                "cloudwatch_logs",
                "OOMKilled containers in payment deployment",
                "payment-7c9 exited 137 (OOMKilled)",
            ),
        }
        prompt = build_synthesis_prompt(_alert(), results)
        assert "OOMKilled containers in payment deployment" in prompt
        assert "payment-7c9 exited 137 (OOMKilled)" in prompt

    def test_prompt_includes_agent_failures(self):
        results: dict[str, AgentResult | AgentFailure] = {
            "eks": AgentFailure(
                agent_name="eks",
                error_message="timed out after 60s",
                timestamp="2026-06-11T10:01:00Z",
            ),
        }
        prompt = build_synthesis_prompt(_alert(), results)
        assert "eks" in prompt
        assert "timed out after 60s" in prompt




def _analysis() -> IncidentAnalysis:
    return IncidentAnalysis(
        root_cause_hypothesis="Payment pods OOMKilled under load",
        correlation="5xx spike aligns with exit-137 container restarts",
        confidence="high",
        suggested_next_action="Raise the payment deployment memory limit",
    )


class TestSynthesize:
    @pytest.mark.asyncio
    async def test_returns_structured_analysis(self):
        model_call = FakeModelCall(returns=_analysis())
        synth = AnalysisSynthesizer(model_call=model_call)

        result = await synth.synthesize(_alert(), {})

        assert isinstance(result, IncidentAnalysis)
        assert result.confidence == "high"
        assert model_call.prompts, "the model call should have been invoked"

    @pytest.mark.asyncio
    async def test_fail_open_on_model_error(self):
        # The seam swallows errors/timeouts to None; synthesis posts no Analysis.
        synth = AnalysisSynthesizer(model_call=FakeModelCall(raises=RuntimeError("bedrock down")))

        assert await synth.synthesize(_alert(), {}) is None


class TestFromEnv:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("SYNTHESIS_ENABLED", raising=False)
        assert AnalysisSynthesizer.from_env() is None

    def test_enabled_when_flag_truthy(self, monkeypatch):
        monkeypatch.setenv("SYNTHESIS_ENABLED", "true")
        synth = AnalysisSynthesizer.from_env()
        assert isinstance(synth, AnalysisSynthesizer)
