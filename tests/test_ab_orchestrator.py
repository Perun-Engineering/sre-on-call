"""Tests for A/B experiment support in the orchestrator and report formatter."""

from __future__ import annotations

from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from agents.master.orchestrator import InvestigationOrchestrator
from agents.master.report_formatter import ReportFormatter
from shared.experiment import ExperimentResult
from shared.experiment_results_store import (
    DEFAULT_TABLE_NAME as RESULTS_TABLE,
    ExperimentResultsStore,
)
from shared.models import AgentFailure, AgentResult, AlertContext
from shared.report_renderer import SlackReportRenderer, DiscordReportRenderer


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


class FakePoster:
    def __init__(self):
        self.messages = []

    async def post_reply(self, alert_context, text):
        self.messages.append(text)


# ---------------------------------------------------------------------------
# Report formatter variant label tests
# ---------------------------------------------------------------------------


class TestReportFormatterVariantLabel:
    def test_report_includes_variant_label_slack(self) -> None:
        fmt = ReportFormatter(renderer=SlackReportRenderer())
        ctx = _alert(variant_label="A: Claude Sonnet")
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = fmt.format_incident_report(ctx, results)
        assert "[A: Claude Sonnet]" in report
        assert "Incident Report" in report

    def test_report_no_variant_label_when_none(self) -> None:
        fmt = ReportFormatter(renderer=SlackReportRenderer())
        ctx = _alert()
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = fmt.format_incident_report(ctx, results)
        assert "📊 *[" not in report
        assert "Incident Report" in report

    def test_report_includes_variant_label_discord(self) -> None:
        fmt = ReportFormatter(renderer=DiscordReportRenderer())
        ctx = _alert(variant_label="B: Nova Pro")
        results: dict[str, AgentResult | AgentFailure] = {k: _success_result(k) for k in ["slack_scanner", "prometheus", "cloudwatch_logs", "eks"]}
        report = fmt.format_incident_report(ctx, results)
        assert "[B: Nova Pro]" in report

    def test_enrichment_includes_variant_label(self) -> None:
        fmt = ReportFormatter(renderer=SlackReportRenderer())
        update = fmt.format_enrichment_update(
            source_agent="eks",
            new_findings=_success_result("eks"),
            initial_report_summary="...",
            variant_label="A: Claude Sonnet",
        )
        assert "[A: Claude Sonnet]" in update
        assert "Enrichment Update" in update

    def test_enrichment_no_variant_label_when_none(self) -> None:
        fmt = ReportFormatter(renderer=SlackReportRenderer())
        update = fmt.format_enrichment_update(
            source_agent="eks",
            new_findings=_success_result("eks"),
            initial_report_summary="...",
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
        poster = FakePoster()
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=poster,
            agent_endpoints={"eks": "http://localhost:9004"},
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
        poster = FakePoster()
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=poster,
            agent_endpoints={"eks": "http://localhost:9004"},
            results_store=store,
        )
        ctx = _alert()
        await orch.investigate(ctx)
        results = store.get_results("", "inv-001")
        assert results == []

    async def test_report_posted_with_variant_label(self, results_table) -> None:
        poster = FakePoster()
        orch = InvestigationOrchestrator(
            http_client=FakeHTTPClient(),
            chat_poster=poster,
            agent_endpoints={"eks": "http://localhost:9004"},
        )
        ctx = _alert(variant_id="b", variant_label="B: Nova Pro")
        await orch.investigate(ctx)
        assert any("[B: Nova Pro]" in m for m in poster.messages)
