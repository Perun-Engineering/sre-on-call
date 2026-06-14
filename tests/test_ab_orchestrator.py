"""Tests for A/B experiment support in the orchestrator and report formatter."""

from __future__ import annotations


import boto3
import pytest
from moto import mock_aws

from agents.master.orchestrator import InvestigationOrchestrator
from agents.master.report_formatter import ReportFormatter
from shared.agents import AgentRegistry
from shared.config import AgentConfig, Defaults, ProjectConfig
from shared.experiment_results_store import (
    DEFAULT_TABLE_NAME as RESULTS_TABLE,
    ExperimentResultsStore,
)
from shared.models import AgentFailure, AgentResult, AlertContext
from shared.report_renderer import SlackReportRenderer, DiscordReportRenderer


def _eks_only_registry() -> AgentRegistry:
    """Registry with only EKS active — matches the legacy single-agent fan-out."""
    return AgentRegistry(
        ProjectConfig(
            project="test",
            environment="dev",
            defaults=Defaults(model_id="anthropic.claude-test"),
            agents={
                "master": AgentConfig(skills=["investigate_alert"]),
                "eks": AgentConfig(enabled=True, network_mode="VPC"),
            },
        )
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _alert(variant_id=None, variant_label=None, experiment_id=None):
    return AlertContext(
        investigation_id="inv-001",
        platform="slack",
        channel_id="C123",
        message_id="1700000000.000100",
        alert_text="High CPU",
        alert_timestamp="2026-04-30T10:00:00Z",
        investigation_window=("2026-04-30T09:55:00Z", "2026-04-30T10:05:00Z"),
        platform_metadata={"thread_ts": "1700000000.000100"},
        experiment_id=experiment_id,
        variant_id=variant_id,
        variant_label=variant_label,
    )


def _success_result(agent_name="eks"):
    return AgentResult(
        agent_name=agent_name, status="success", findings=[], summary="All good", duration_seconds=1.5,
    )


class FakeHTTPClient:
    async def post_json(self, url, payload):
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id", "x"),
            "result": {"message": {"role": "agent", "parts": [{"kind": "text", "text": "OK"}], "messageId": "r1"}},
        }


class FakeChatPlatform:
    """Fake :class:`ChatPlatform` for orchestrator tests.

    Renders payloads via the Slack mrkdwn renderer (matching the legacy
    ``FakePoster`` semantics) and exposes ``messages`` as a list of
    rendered strings — preserving existing assertions like
    ``any("[B: Nova Pro]" in m for m in poster.messages)``.
    """

    name = "slack"

    def __init__(self) -> None:
        self._renderer = SlackReportRenderer()
        self.deliveries: list = []

    def ingest(self, headers, raw_body):
        raise NotImplementedError

    def ack(self, command, text):
        raise NotImplementedError

    def notice(self, target, text) -> None:
        raise NotImplementedError

    async def deliver(self, target, payload) -> str:
        text = self._renderer.render(payload)
        self.deliveries.append((target, payload, text))
        return text

    @property
    def messages(self) -> list[str]:
        return [text for _, _, text in self.deliveries]


# ---------------------------------------------------------------------------
# Report formatter variant label tests
# ---------------------------------------------------------------------------


class TestReportFormatterVariantLabel:
    def test_report_includes_variant_label_slack(self) -> None:
        fmt = ReportFormatter()
        ctx = _alert(variant_label="A: Claude Sonnet")
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = SlackReportRenderer().render_report(
            fmt.build_incident_sections(
                fmt.derive_facts(ctx, results), variant_label=ctx.variant_label
            )
        )
        assert "[A: Claude Sonnet]" in report
        assert "Incident Report" in report

    def test_report_no_variant_label_when_none(self) -> None:
        fmt = ReportFormatter()
        ctx = _alert()
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = SlackReportRenderer().render_report(
            fmt.build_incident_sections(
                fmt.derive_facts(ctx, results), variant_label=ctx.variant_label
            )
        )
        assert "📊 *[" not in report
        assert "Incident Report" in report

    def test_report_includes_variant_label_discord(self) -> None:
        fmt = ReportFormatter()
        ctx = _alert(variant_label="B: Nova Pro")
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = DiscordReportRenderer().render_report(
            fmt.build_incident_sections(
                fmt.derive_facts(ctx, results), variant_label=ctx.variant_label
            )
        )
        assert "[B: Nova Pro]" in report

    def test_enrichment_includes_variant_label(self) -> None:
        fmt = ReportFormatter()
        update = SlackReportRenderer().render_enrichment(
            fmt.build_enrichment_sections(
                source_agent="eks",
                new_findings=_success_result("eks"),
                initial_report_summary="...",
                variant_label="A: Claude Sonnet",
            )
        )
        assert "[A: Claude Sonnet]" in update
        assert "Enrichment Update" in update

    def test_enrichment_no_variant_label_when_none(self) -> None:
        fmt = ReportFormatter()
        update = SlackReportRenderer().render_enrichment(
            fmt.build_enrichment_sections(
                source_agent="eks",
                new_findings=_success_result("eks"),
                initial_report_summary="...",
            )
        )
        assert "📊 *[" not in update


# ---------------------------------------------------------------------------
# Orchestrator experiment result storage tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def results_table():
    from typing import Any
    with mock_aws():
        dynamodb: Any = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=RESULTS_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb


class TestOrchestratorExperimentStorage:
    async def test_stores_result_when_variant_set(self, results_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=results_table)
        platform = FakeChatPlatform()
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=platform,
            registry=_eks_only_registry(),
            results_store=store,
        )
        ctx = _alert(variant_id="a", variant_label="A: Claude", experiment_id="exp-001")
        await orch.investigate(ctx)
        results = store.get_results("exp-001", "inv-001")
        assert len(results) == 1
        assert results[0].variant_id == "a"
        assert results[0].experiment_id == "exp-001"
        assert len(results[0].report) > 0

    async def test_no_result_stored_without_experiment(self, results_table) -> None:
        store = ExperimentResultsStore(dynamodb_resource=results_table)
        platform = FakeChatPlatform()
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=platform,
            registry=_eks_only_registry(),
            results_store=store,
        )
        ctx = _alert()
        await orch.investigate(ctx)
        results = store.get_results("", "inv-001")
        assert results == []

    async def test_report_posted_with_variant_label(self, results_table) -> None:
        platform = FakeChatPlatform()
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=platform,
            registry=_eks_only_registry(),
        )
        ctx = _alert(variant_id="b", variant_label="B: Nova Pro")
        await orch.investigate(ctx)
        assert any("[B: Nova Pro]" in m for m in platform.messages)


class TestMasterSideDecisionTelemetry:
    """Master-side decision cost (routing/synthesis/follow-up) joins the
    scorecard total alongside specialized-agent cost (issue #65)."""

    def _orch(self, store) -> InvestigationOrchestrator:
        return InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_platform=FakeChatPlatform(),
            registry=_eks_only_registry(),
            results_store=store,
        )

    async def test_decision_tokens_and_cost_folded_into_totals(self, results_table) -> None:
        import asyncio

        from shared import model_call

        store = ExperimentResultsStore(dynamodb_resource=results_table)
        orch = self._orch(store)
        ctx = _alert(variant_id="a", experiment_id="exp-tele")

        # Simulate the master's routing/synthesis/follow-up decision calls
        # recording their usage during the investigation, then store the result.
        model_call.reset_usage()
        model_call.record_usage(100, 20, 0.0007)
        orch._store_experiment_result(
            ctx, {}, "report", asyncio.get_event_loop().time()
        )

        res = store.get_results("exp-tele", "inv-001")[0]
        assert res.total_tokens == 120
        assert res.total_cost_usd == pytest.approx(0.0007)

    async def test_blank_when_no_decision_calls(self, results_table) -> None:
        import asyncio

        from shared import model_call

        store = ExperimentResultsStore(dynamodb_resource=results_table)
        orch = self._orch(store)
        ctx = _alert(variant_id="a", experiment_id="exp-blank")

        model_call.reset_usage()  # no decision calls recorded
        orch._store_experiment_result(
            ctx, {}, "report", asyncio.get_event_loop().time()
        )

        res = store.get_results("exp-blank", "inv-001")[0]
        # No agents, no decisions → blank totals, not a misleading zero.
        assert res.total_tokens is None
        assert res.total_cost_usd is None
