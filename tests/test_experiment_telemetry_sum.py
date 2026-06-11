"""Tests for per-agent cost/token summing into the experiment result (issue #26)."""

from __future__ import annotations

from agents.master.orchestrator import _sum_agent_telemetry
from shared.models import AgentFailure, AgentMetadata, AgentResult


def _result(name: str, *, cost: float | None, tokens: int | None) -> AgentResult:
    return AgentResult(
        agent_name=name,
        status="success",
        findings=[],
        summary="",
        metadata=AgentMetadata(cost_usd=cost, total_tokens=tokens),
    )


def test_sums_cost_and_tokens_across_agents():
    results = {
        "eks": _result("eks", cost=0.01, tokens=1000),
        "cloudwatch": _result("cloudwatch", cost=0.02, tokens=2500),
    }
    cost, tokens = _sum_agent_telemetry(results)
    assert cost == 0.03
    assert tokens == 3500


def test_none_when_no_agent_reports_telemetry():
    results = {"eks": _result("eks", cost=None, tokens=None)}
    cost, tokens = _sum_agent_telemetry(results)
    assert cost is None
    assert tokens is None


def test_partial_telemetry_sums_what_exists():
    results = {
        "eks": _result("eks", cost=0.01, tokens=None),
        "cloudwatch": _result("cloudwatch", cost=None, tokens=2500),
    }
    cost, tokens = _sum_agent_telemetry(results)
    assert cost == 0.01
    assert tokens == 2500


def test_includes_failures_with_metadata():
    results = {
        "eks": _result("eks", cost=0.01, tokens=1000),
        "down": AgentFailure(
            agent_name="down",
            error_message="timeout",
            timestamp="2026-05-01T00:00:00Z",
            metadata=AgentMetadata(cost_usd=0.005, total_tokens=500),
        ),
    }
    cost, tokens = _sum_agent_telemetry(results)
    assert cost == 0.015
    assert tokens == 1500
