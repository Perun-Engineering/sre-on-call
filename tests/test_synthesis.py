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


class TestCausalChainSchema:
    """Rec #3 — causal chain, competing hypotheses, and ruled-out fields."""

    def test_new_list_fields_default_to_empty(self):
        # A partial structured-output parse (only the original four fields) must
        # still yield a usable Analysis with empty causal-chain extensions.
        analysis = IncidentAnalysis(
            root_cause_hypothesis="rc",
            correlation="co",
            confidence="low",
            suggested_next_action="na",
        )
        assert analysis.causal_chain == []
        assert analysis.competing_hypotheses == []
        assert analysis.ruled_out == []

    def test_new_list_fields_parse_when_supplied(self):
        analysis = IncidentAnalysis(
            root_cause_hypothesis="rc",
            correlation="co",
            confidence="high",
            suggested_next_action="na",
            causal_chain=["deploy v2", "memory leak", "OOMKilled", "5xx spike"],
            competing_hypotheses=["upstream DB latency (lower: no slow queries)"],
            ruled_out=["network partition (checked: no SG changes)"],
        )
        assert analysis.causal_chain[0] == "deploy v2"
        assert analysis.causal_chain[-1] == "5xx spike"
        assert len(analysis.competing_hypotheses) == 1
        assert "network partition" in analysis.ruled_out[0]

    def test_schema_caps_chain_and_competing_in_descriptions(self):
        fields = IncidentAnalysis.model_fields
        # The schema descriptions bound the list lengths so the structured
        # output stays within the ~10s synthesis budget.
        assert "4" in (fields["causal_chain"].description or "")
        assert "3" in (fields["competing_hypotheses"].description or "")


class TestSystemPromptCausalDiscipline:
    """The synthesis prompt must demand backward tracing + falsification."""

    def test_prompt_demands_backward_tracing_and_falsification(self):
        from agents.master.synthesis import _SYSTEM_PROMPT

        lowered = _SYSTEM_PROMPT.lower()
        assert "upstream" in lowered or "backward" in lowered
        # Re-states the never-invent rule specifically for chain links.
        assert "chain" in lowered
        # Instructs falsification / ruling out.
        assert "ruled out" in lowered or "ruling out" in lowered or "falsif" in lowered
