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

    async def deliver(self, alert_context, payload) -> str:
        from shared.report_renderer import (
            EnrichmentSections,
            InvestigationStartedSections,
            PIRSections,
            ReportSections,
        )
        if isinstance(payload, ReportSections):
            text = self._renderer.render_report(payload)
        elif isinstance(payload, EnrichmentSections):
            text = self._renderer.render_enrichment(payload)
        elif isinstance(payload, InvestigationStartedSections):
            text = self._renderer.render_investigation_started(payload)
        elif isinstance(payload, PIRSections):
            text = self._renderer.render_pir(payload)
        else:
            raise TypeError(f"Unsupported deliver payload: {type(payload).__name__}")
        self.deliveries.append((alert_context, payload, text))
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
            fmt.build_incident_sections(ctx, results)
        )
        assert "[A: Claude Sonnet]" in report
        assert "Incident Report" in report

    def test_report_no_variant_label_when_none(self) -> None:
        fmt = ReportFormatter()
        ctx = _alert()
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = SlackReportRenderer().render_report(
            fmt.build_incident_sections(ctx, results)
        )
        assert "📊 *[" not in report
        assert "Incident Report" in report

    def test_report_includes_variant_label_discord(self) -> None:
        fmt = ReportFormatter()
        ctx = _alert(variant_label="B: Nova Pro")
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = DiscordReportRenderer().render_report(
            fmt.build_incident_sections(ctx, results)
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
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
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
