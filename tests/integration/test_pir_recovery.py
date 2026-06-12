"""End-to-end PIR recovery — issue #56. Archive -> /postmortem -> threaded PIR."""

from __future__ import annotations

import asyncio

import boto3
from moto import mock_aws

from agents.master import tools
from shared.models import AgentResult, AgentMetadata, Finding
from shared.report_renderer import PIRSections, SlackReportRenderer
from shared.trace_store import ResultSummary, TraceManifest, TraceStore

BUCKET = "sre-on-call-pir-traces"
TABLE = "sre-on-call-pir-traces"


class _Recorder:
    name = "slack"

    def __init__(self):
        self._r = SlackReportRenderer()
        self.deliveries = []

    async def deliver(self, target, payload):
        self.deliveries.append((target, payload))
        return self._r.render(payload)

    def notice(self, target, text):  # pragma: no cover - happy path
        self.deliveries.append(("NOTICE", text))


async def _drain_until(pred, ticks=100):
    for _ in range(ticks):
        if pred():
            return
        await asyncio.sleep(0.01)


async def test_postmortem_recovers_and_posts(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "channel_id", "AttributeType": "S"},
                {"AttributeName": "message_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "channel_id-message_id-index",
                "KeySchema": [
                    {"AttributeName": "channel_id", "KeyType": "HASH"},
                    {"AttributeName": "message_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.Table(TABLE).meta.client.get_waiter("table_exists").wait(
            TableName=TABLE)

        store = TraceStore(bucket=BUCKET, table_name=TABLE,
                           s3_client=s3, dynamodb_resource=ddb)
        store.put_manifest(TraceManifest(
            investigation_id="inv-e2e",
            alert_context={"investigation_id": "inv-e2e", "platform": "slack",
                           "channel_id": "C9", "message_id": "1800.5",
                           "alert_text": "OOMKilled",
                           "alert_timestamp": "2025-01-15T14:00:00Z",
                           "investigation_window": ["2025-01-15T13:55:00Z",
                                                    "2025-01-15T14:05:00Z"]},
            started_at="2025-01-15T14:00:00Z", ended_at="2025-01-15T14:01:00Z",
            total_duration_seconds=60.0, dispatched_agents=["eks"],
            results_summary={"eks": ResultSummary("success", 1, 4.0)},
            status="completed", error_count=0,
        ))
        store.put_results(
            investigation_id="inv-e2e", dt="dt=2025-01-15",
            results={"eks": AgentResult(
                agent_name="eks", status="success",
                findings=[Finding(source="pod/api", timestamp="t",
                                  content="OOMKilled", severity="critical")],
                summary="OOM", metadata=AgentMetadata())},
        )

        platform = _Recorder()
        monkeypatch.setattr(tools.TraceStore, "from_env",
                            classmethod(lambda cls: store))
        monkeypatch.setattr(tools, "for_platform", lambda name: platform)

        await tools.finalize_postmortem(
            '{"task":"pir","platform":"slack","channel_id":"C9",'
            '"thread_ts":"1800.5","user_id":"U1","command_text":"/postmortem"}'
        )
        await _drain_until(lambda: bool(platform.deliveries))

    assert len(platform.deliveries) == 1
    target, payload = platform.deliveries[0]
    assert isinstance(payload, PIRSections)
    assert target.thread_anchor == "1800.5"
