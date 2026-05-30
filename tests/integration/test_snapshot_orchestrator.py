"""Integration tests for the master agent's StatusSnapshotOrchestrator.

Slice 3 of the ``/status`` command. Exercises the orchestrator end-to-end
with a controllable :class:`FakeHTTPClient` and a fake :class:`ChatPlatform`
that records the rendered :class:`SnapshotSections` payloads. No real
A2A traffic, no real chat platform.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.master.snapshot_orchestrator import (
    DEFAULT_MODEL_ID,
    MasterSnapshotBuilder,
    StatusSnapshotOrchestrator,
)
from shared.agents import AgentRegistry
from shared.config import AgentConfig, Defaults, ProjectConfig
from shared.models import AgentMetadata, SnapshotReport, SnapshotSection
from shared.report_renderer import (
    EnrichmentSections,
    InvestigationStartedSections,
    PIRSections,
    ReportSections,
    SlackReportRenderer,
    SnapshotBlock,
    SnapshotSections,
)
from shared.tool_result import format_snapshot_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REQUESTED_AT = "2026-05-28T19:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure :meth:`Agent.resolve_endpoint` falls back to catalogue defaults."""
    for var in (
        "SLACK_SCANNER_AGENT_RUNTIME_ARN",
        "SLACK_SCANNER_AGENT_URL",
        "DISCORD_SCANNER_AGENT_RUNTIME_ARN",
        "DISCORD_SCANNER_AGENT_URL",
        "CLOUDWATCH_LOGS_AGENT_RUNTIME_ARN",
        "CLOUDWATCH_LOGS_AGENT_URL",
        "EKS_AGENT_RUNTIME_ARN",
        "EKS_AGENT_URL",
        "MODEL_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def _build_registry(
    *,
    active_specialized: list[str] | None = None,
    disabled_specialized: list[str] | None = None,
) -> AgentRegistry:
    """Build a test registry with a custom deployment manifest."""
    if active_specialized is None:
        active_specialized = ["slack_scanner"]
    if disabled_specialized is None:
        disabled_specialized = []

    agents: dict[str, AgentConfig] = {
        "master": AgentConfig(skills=["investigate_alert", "capture_status_snapshot"]),
    }
    for aid in active_specialized:
        kwargs: dict = {"enabled": True, "skills": ["capture_snapshot"]}
        if aid == "eks":
            kwargs["network_mode"] = "VPC"
        agents[aid] = AgentConfig(**kwargs)
    for aid in disabled_specialized:
        kwargs = {"enabled": False, "skills": ["capture_snapshot"]}
        if aid == "eks":
            kwargs["network_mode"] = "VPC"
        agents[aid] = AgentConfig(**kwargs)

    return AgentRegistry(
        ProjectConfig(
            project="test",
            environment="dev",
            defaults=Defaults(model_id="anthropic.claude-test"),
            agents=agents,
        )
    )


def _a2a_response_for(report: SnapshotReport) -> dict:
    """Build a JSON-RPC ``message/send`` success response carrying the
    formatted ``SnapshotReport`` (footer + human text) as the artifact."""
    text = format_snapshot_result(report)
    return {
        "jsonrpc": "2.0",
        "id": "test-id",
        "result": {
            "artifacts": [
                {
                    "name": "agent_response",
                    "parts": [{"kind": "text", "text": text}],
                }
            ]
        },
    }


def _a2a_error_response(message: str = "boom") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "test-id",
        "error": {"code": -32000, "message": message},
    }


def _a2a_no_footer_response(text: str = "tool returned plain text without footer") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "test-id",
        "result": {
            "artifacts": [
                {
                    "name": "agent_response",
                    "parts": [{"kind": "text", "text": text}],
                }
            ]
        },
    }


def _make_report(*, anomaly: bool = False, agent_name: str = "slack_scanner") -> SnapshotReport:
    return SnapshotReport(
        agent_name=agent_name,
        captured_at=REQUESTED_AT,
        sections=[
            SnapshotSection(
                label="Authentication",
                lines=["workspace: Acme (T123)", "bot user: sre-bot (U456)"],
            ),
            SnapshotSection(
                label="Channel access",
                lines=["bot is a member of 12 channel(s)"],
            ),
        ],
        anomaly=anomaly,
        anomaly_summary="Slack auth.test failed: invalid_auth" if anomaly else None,
    )


class FakeHTTPClient:
    """Controllable :class:`AsyncHTTPClient`: per-URL responses, optional delay."""

    def __init__(
        self,
        responses: dict[str, dict] | None = None,
        delay_per_url: dict[str, float] | None = None,
    ):
        self.responses = responses or {}
        self.delay_per_url = delay_per_url or {}
        self.calls: list[tuple[str, dict]] = []

    async def post_json(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        delay = self.delay_per_url.get(url, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        if url in self.responses:
            return self.responses[url]
        raise AssertionError(f"FakeHTTPClient: no canned response for {url!r}")


class FakeChatPlatform:
    """Fake ChatPlatform that records every deliver() call. Renders via Slack
    mrkdwn so tests can inspect the actual rendered text without sending it."""

    name = "slack"

    def __init__(self) -> None:
        self._renderer = SlackReportRenderer()
        self.deliveries: list[tuple] = []  # (ctx, payload, rendered_text)

    def ingest(self, headers, raw_body):  # not exercised
        raise NotImplementedError

    def ack(self, command, text):  # not exercised
        raise NotImplementedError

    async def deliver(self, target, payload) -> str:
        text = self._render(payload)
        self.deliveries.append((target, payload, text))
        return text

    def _render(self, payload) -> str:
        if isinstance(payload, ReportSections):
            return self._renderer.render_report(payload)
        if isinstance(payload, EnrichmentSections):
            return self._renderer.render_enrichment(payload)
        if isinstance(payload, InvestigationStartedSections):
            return self._renderer.render_investigation_started(payload)
        if isinstance(payload, PIRSections):
            return self._renderer.render_pir(payload)
        if isinstance(payload, SnapshotSections):
            return self._renderer.render_snapshot(payload)
        raise TypeError(f"Unsupported deliver payload: {type(payload).__name__}")


def _last_snapshot_sections(platform: FakeChatPlatform) -> SnapshotSections:
    """Find the SnapshotSections payload posted by the orchestrator."""
    for _, payload, _ in platform.deliveries:
        if isinstance(payload, SnapshotSections):
            return payload
    raise AssertionError("No SnapshotSections payload was delivered")


def _block_for(sections: SnapshotSections, display_name: str) -> SnapshotBlock:
    for block in sections.blocks:
        if block.display_name == display_name:
            return block
    raise AssertionError(f"No block for {display_name!r} in {[b.display_name for b in sections.blocks]}")


def _make_request() -> dict:
    return {
        "task": "snapshot",
        "platform": "slack",
        "channel_id": "C12345",
        "user_id": "U67890",
        "requested_at": REQUESTED_AT,
    }


def _make_orchestrator(
    *,
    registry: AgentRegistry | None = None,
    http_client: FakeHTTPClient | None = None,
    chat_platform: FakeChatPlatform | None = None,
    hard_cutoff: float | None = None,
) -> tuple[StatusSnapshotOrchestrator, FakeHTTPClient, FakeChatPlatform]:
    registry = registry or _build_registry()
    http_client = http_client or FakeHTTPClient()
    chat_platform = chat_platform or FakeChatPlatform()
    orch = StatusSnapshotOrchestrator(
        http_client=http_client,
        chat_platform=chat_platform,
        registry=registry,
    )
    if hard_cutoff is not None:
        orch.HARD_CUTOFF_SECONDS = hard_cutoff
    return orch, http_client, chat_platform


# ---------------------------------------------------------------------------
# MasterSnapshotBuilder
# ---------------------------------------------------------------------------


class TestMasterSnapshotBuilder:
    def test_master_block_present_with_registry_section(self):
        builder = MasterSnapshotBuilder(_build_registry())
        block = builder.build()
        assert block.display_name == "Master Agent"
        assert block.status == "ok"
        assert any(s.label == "Agent registry" for s in block.sections)

    def test_registry_section_lists_active_disabled_and_not_deployed(self):
        registry = _build_registry(
            active_specialized=["slack_scanner"],
            disabled_specialized=["eks"],
        )
        builder = MasterSnapshotBuilder(registry)
        block = builder.build()
        registry_section = next(s for s in block.sections if s.label == "Agent registry")
        joined = "\n".join(registry_section.lines)
        assert "Slack Scanner: 🟢 active" in joined
        assert "EKS Cluster State: 🟫 disabled in config.yaml" in joined
        # cloudwatch_logs is in the catalogue but absent from this test's manifest
        assert "CloudWatch Logs: ⚪ not deployed" in joined

    def test_master_excluded_from_registry_listing(self):
        block = MasterSnapshotBuilder(_build_registry()).build()
        registry_section = next(s for s in block.sections if s.label == "Agent registry")
        for line in registry_section.lines:
            assert "Master Agent" not in line

    def test_header_line_includes_model_and_skills(self):
        block = MasterSnapshotBuilder(_build_registry()).build()
        assert "model=" in block.header_line
        assert DEFAULT_MODEL_ID in block.header_line
        assert "skills=" in block.header_line

    def test_header_line_uses_model_id_env_when_set(self, monkeypatch):
        monkeypatch.setenv("MODEL_ID", "anthropic.claude-prod-override")
        block = MasterSnapshotBuilder(_build_registry()).build()
        assert "anthropic.claude-prod-override" in block.header_line


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSnapshotOrchestratorHappyPath:
    @pytest.mark.asyncio
    async def test_single_agent_responds_with_ok_block(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        report = _make_report()
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(report)},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        sections = _last_snapshot_sections(platform)
        # Master block + slack_scanner block, in registry.order (master first)
        assert [b.display_name for b in sections.blocks] == ["Master Agent", "Slack Scanner"]
        slack_block = _block_for(sections, "Slack Scanner")
        assert slack_block.status == "ok"
        # Pre-rendered section lines from the agent flow through
        labels = [s.label for s in slack_block.sections]
        assert labels == ["Authentication", "Channel access"]

    @pytest.mark.asyncio
    async def test_summary_line_counts_responders(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(_make_report())},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        sections = _last_snapshot_sections(platform)
        # 1 master ok + 1 slack ok, both responded
        assert "2/2 agents responded" in sections.summary_line
        assert "anomalies" not in sections.summary_line
        assert "errors" not in sections.summary_line

    @pytest.mark.asyncio
    async def test_a2a_request_carries_snapshot_task_and_requested_at(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(_make_report())},
        )
        orch, http_client, _ = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        assert len(http_client.calls) == 1
        _, payload = http_client.calls[0]
        text = payload["params"]["message"]["parts"][0]["text"]
        decoded = json.loads(text)
        assert decoded == {"task": "snapshot", "requested_at": REQUESTED_AT}


# ---------------------------------------------------------------------------
# Anomaly path
# ---------------------------------------------------------------------------


class TestSnapshotOrchestratorAnomalyPath:
    @pytest.mark.asyncio
    async def test_anomaly_report_renders_as_anomaly_block(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={
                "http://localhost:9001": _a2a_response_for(_make_report(anomaly=True)),
            },
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        slack_block = _block_for(_last_snapshot_sections(platform), "Slack Scanner")
        assert slack_block.status == "anomaly"
        assert slack_block.anomaly_summary == "Slack auth.test failed: invalid_auth"

    @pytest.mark.asyncio
    async def test_anomaly_summary_appears_in_top_line_summary(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={
                "http://localhost:9001": _a2a_response_for(_make_report(anomaly=True)),
            },
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        line = _last_snapshot_sections(platform).summary_line
        assert "anomalies:" in line
        assert "Slack auth.test failed: invalid_auth" in line


# ---------------------------------------------------------------------------
# Timeout / error / malformed paths
# ---------------------------------------------------------------------------


class TestSnapshotOrchestratorErrorPaths:
    @pytest.mark.asyncio
    async def test_timeout_renders_error_block(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        # Agent's response takes longer than the cutoff
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(_make_report())},
            delay_per_url={"http://localhost:9001": 1.0},
        )
        orch, _, platform = _make_orchestrator(
            registry=registry, http_client=http, hard_cutoff=0.1,
        )

        await orch.capture(_make_request())

        slack_block = _block_for(_last_snapshot_sections(platform), "Slack Scanner")
        assert slack_block.status == "error"
        assert "no response within" in (slack_block.error_message or "")

    @pytest.mark.asyncio
    async def test_a2a_error_response_renders_error_block(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_error_response("agent crashed")},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        slack_block = _block_for(_last_snapshot_sections(platform), "Slack Scanner")
        assert slack_block.status == "error"
        assert "agent crashed" in (slack_block.error_message or "")

    @pytest.mark.asyncio
    async def test_response_without_footer_renders_error_block(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_no_footer_response()},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        slack_block = _block_for(_last_snapshot_sections(platform), "Slack Scanner")
        assert slack_block.status == "error"
        assert "footer" in (slack_block.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_errors_appear_in_top_line_summary(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_error_response()},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        line = _last_snapshot_sections(platform).summary_line
        # Master ok (1) + slack error (1); 1/2 responders
        assert "1/2 agents responded" in line
        assert "errors:" in line
        assert "Slack Scanner" in line


# ---------------------------------------------------------------------------
# Disabled-in-config agents
# ---------------------------------------------------------------------------


class TestSnapshotOrchestratorDisabled:
    @pytest.mark.asyncio
    async def test_disabled_agent_renders_disabled_block_no_fanout(self):
        registry = _build_registry(
            active_specialized=["slack_scanner"],
            disabled_specialized=["eks"],
        )
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(_make_report())},
        )
        orch, http_client, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        # No fan-out attempted for disabled EKS
        called_urls = [url for url, _ in http_client.calls]
        assert "http://localhost:9005" not in called_urls

        eks_block = _block_for(_last_snapshot_sections(platform), "EKS Cluster State")
        assert eks_block.status == "disabled"

    @pytest.mark.asyncio
    async def test_disabled_blocks_excluded_from_responder_count(self):
        registry = _build_registry(
            active_specialized=["slack_scanner"],
            disabled_specialized=["eks"],
        )
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(_make_report())},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        # Master ok + slack ok = 2 responders out of 2 attempted; EKS disabled
        # is not counted in either numerator or denominator.
        line = _last_snapshot_sections(platform).summary_line
        assert "2/2 agents responded" in line


# ---------------------------------------------------------------------------
# Routing / shape invariants
# ---------------------------------------------------------------------------


class TestSnapshotOrchestratorRouting:
    @pytest.mark.asyncio
    async def test_delivery_target_is_top_level_for_status(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(_make_report())},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        target, payload, _ = next(
            d for d in platform.deliveries if isinstance(d[1], SnapshotSections)
        )
        # /status delivers at top-level — no thread anchor.
        assert target.thread_anchor is None
        assert target.channel_id == "C12345"
        assert target.platform == "slack"

    @pytest.mark.asyncio
    async def test_master_block_is_first(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(_make_report())},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        sections = _last_snapshot_sections(platform)
        assert sections.blocks[0].display_name == "Master Agent"

    @pytest.mark.asyncio
    async def test_no_active_agents_still_posts_master_only_snapshot(self):
        # Empty active set
        registry = _build_registry(active_specialized=[])
        orch, http_client, platform = _make_orchestrator(registry=registry)

        await orch.capture(_make_request())

        # No fan-out calls
        assert http_client.calls == []
        sections = _last_snapshot_sections(platform)
        assert [b.display_name for b in sections.blocks] == ["Master Agent"]
        assert "1/1 agents responded" in sections.summary_line

    @pytest.mark.asyncio
    async def test_specialized_blocks_ordered_by_registry_order(self):
        registry = _build_registry(
            active_specialized=["eks", "slack_scanner", "cloudwatch_logs"],
        )
        http = FakeHTTPClient(
            responses={
                "http://localhost:9001": _a2a_response_for(_make_report(agent_name="slack_scanner")),
                "http://localhost:9004": _a2a_response_for(_make_report(agent_name="cloudwatch_logs")),
                "http://localhost:9005": _a2a_response_for(_make_report(agent_name="eks")),
            },
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        sections = _last_snapshot_sections(platform)
        # Registry catalogue order: master, slack_scanner, discord_scanner,
        # cloudwatch_logs, eks. discord_scanner is not deployed in this test.
        assert [b.display_name for b in sections.blocks] == [
            "Master Agent",
            "Slack Scanner",
            "CloudWatch Logs",
            "EKS Cluster State",
        ]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestSnapshotOrchestratorMetadata:
    @pytest.mark.asyncio
    async def test_agent_block_header_uses_report_metadata_model_id_when_set(self):
        registry = _build_registry(active_specialized=["slack_scanner"])
        report = SnapshotReport(
            agent_name="slack_scanner",
            captured_at=REQUESTED_AT,
            sections=[],
            metadata=AgentMetadata(model_id="anthropic.claude-3-haiku-20241022"),
        )
        http = FakeHTTPClient(
            responses={"http://localhost:9001": _a2a_response_for(report)},
        )
        orch, _, platform = _make_orchestrator(registry=registry, http_client=http)

        await orch.capture(_make_request())

        slack_block = _block_for(_last_snapshot_sections(platform), "Slack Scanner")
        assert "anthropic.claude-3-haiku-20241022" in slack_block.header_line
