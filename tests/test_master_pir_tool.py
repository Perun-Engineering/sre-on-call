"""Master PIR tool — issue #56. /postmortem -> task:"pir" -> finalize_postmortem."""

from __future__ import annotations

import asyncio
import json

from agents.master import tools
from shared.models import AgentResult, AgentMetadata, Finding
from shared.report_renderer import PIRSections, SlackReportRenderer


class RecordingChatPlatform:
    name = "slack"

    def __init__(self) -> None:
        self._renderer = SlackReportRenderer()
        self.deliveries: list[tuple] = []
        self.notices: list[tuple] = []

    async def deliver(self, target, payload) -> str:
        text = self._renderer.render(payload)
        self.deliveries.append((target, payload, text))
        return text

    def notice(self, target, text) -> None:
        self.notices.append((target, text))


class _StubStore:
    """Stands in for TraceStore — returns canned recovery objects."""

    def __init__(self, *, ref=None, manifest=None, results=None,
                 page_model=None, put_page_raises=False):
        self._ref, self._manifest, self._results = ref, manifest, results
        self._page_model = page_model
        self._put_page_raises = put_page_raises
        self.page_writes: list[dict] = []

    def find_investigation(self, channel_id, thread_ts):
        return self._ref

    def get_manifest(self, investigation_id, *, dt=None):
        return self._manifest

    def get_results(self, investigation_id, *, dt=None):
        return self._results

    def get_page_model(self, investigation_id, *, dt=None):
        return self._page_model

    def put_page_model(self, *, investigation_id, payload, dt=None):
        if self._put_page_raises:
            raise RuntimeError("boom")
        self.page_writes.append(
            {"investigation_id": investigation_id, "payload": payload, "dt": dt}
        )


async def _drain_until(predicate, *, ticks: int = 100) -> None:
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0.01)


def _pir_payload(**over):
    base = {"task": "pir", "platform": "slack", "channel_id": "C1",
            "thread_ts": "1700.1", "user_id": "U1", "command_text": "/postmortem"}
    base.update(over)
    return json.dumps(base)


async def test_rejects_non_pir_task():
    msg = await tools.finalize_postmortem(json.dumps({"task": "snapshot"}))
    assert "expected 'pir'" in msg


async def test_happy_path_posts_threaded_pir(monkeypatch):
    from shared.trace_store import InvestigationRef
    platform = RecordingChatPlatform()
    store = _StubStore(
        ref=InvestigationRef(investigation_id="inv-1", dt="dt=2025-01-15"),
        manifest={"alert_context": {
            "investigation_id": "inv-1", "platform": "slack",
            "channel_id": "C1", "message_id": "1700.1",
            "alert_text": "High CPU", "alert_timestamp": "2025-01-15T14:00:00Z",
            "investigation_window": ["2025-01-15T13:55:00Z",
                                     "2025-01-15T14:05:00Z"]}},
        results={"eks": AgentResult(
            agent_name="eks", status="success",
            findings=[Finding(source="pod", timestamp="t",
                              content="CrashLoop", severity="critical")],
            summary="bad", metadata=AgentMetadata())},
    )
    monkeypatch.setattr(tools.TraceStore, "from_env",
                        classmethod(lambda cls: store))
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.finalize_postmortem(_pir_payload())
    await _drain_until(lambda: bool(platform.deliveries))

    assert len(platform.deliveries) == 1
    target, payload, _ = platform.deliveries[0]
    assert isinstance(payload, PIRSections)
    assert target.thread_anchor == "1700.1"
    assert target.channel_id == "C1"


async def test_pir_carries_manifest_analysis_root_cause(monkeypatch):
    # Rec #5 — the #27 analysis archived on the manifest reaches the chat PIR.
    from shared.trace_store import InvestigationRef
    platform = RecordingChatPlatform()
    store = _StubStore(
        ref=InvestigationRef(investigation_id="inv-1", dt="dt=2025-01-15"),
        manifest={
            "alert_context": {
                "investigation_id": "inv-1", "platform": "slack",
                "channel_id": "C1", "message_id": "1700.1",
                "alert_text": "High CPU", "alert_timestamp": "2025-01-15T14:00:00Z",
                "investigation_window": ["2025-01-15T13:55:00Z",
                                         "2025-01-15T14:05:00Z"]},
            "analysis": {
                "root_cause_hypothesis": "Payment pods OOMKilled under load",
                "correlation": "5xx aligns with restarts",
                "confidence": "high",
                "suggested_next_action": "Raise memory limit",
                "causal_chain": ["traffic surge", "OOMKilled"],
                "competing_hypotheses": [],
                "ruled_out": ["network partition"],
            },
        },
        results={"eks": AgentResult(
            agent_name="eks", status="success",
            findings=[Finding(source="pod", timestamp="t",
                              content="CrashLoop", severity="critical")],
            summary="bad", metadata=AgentMetadata())},
    )
    monkeypatch.setattr(tools.TraceStore, "from_env",
                        classmethod(lambda cls: store))
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.finalize_postmortem(_pir_payload())
    await _drain_until(lambda: bool(platform.deliveries))

    _, payload, text = platform.deliveries[0]
    assert isinstance(payload, PIRSections)
    assert "Payment pods OOMKilled under load" in payload.root_cause
    assert "traffic surge" in payload.root_cause
    assert "Payment pods OOMKilled under load" in text


async def test_no_trace_store_posts_notice(monkeypatch):
    platform = RecordingChatPlatform()
    monkeypatch.setattr(tools.TraceStore, "from_env",
                        classmethod(lambda cls: None))
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.finalize_postmortem(_pir_payload())
    await _drain_until(lambda: bool(platform.notices))

    assert platform.deliveries == []
    assert "archive is disabled" in platform.notices[0][1]


async def test_no_match_posts_notice(monkeypatch):
    platform = RecordingChatPlatform()
    store = _StubStore(ref=None)
    monkeypatch.setattr(tools.TraceStore, "from_env",
                        classmethod(lambda cls: store))
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.finalize_postmortem(_pir_payload())
    await _drain_until(lambda: bool(platform.notices))

    assert platform.deliveries == []
    assert "couldn't locate" in platform.notices[0][1].lower()


# ---------------------------------------------------------------------------
# Incident-page finalization (issue #55)
# ---------------------------------------------------------------------------

from shared.trace_store import InvestigationRef  # noqa: E402


def _recovery_store(**over) -> _StubStore:
    base: dict = dict(
        ref=InvestigationRef(investigation_id="inv-1", dt="dt=2025-01-15"),
        manifest={"alert_context": {
            "investigation_id": "inv-1", "platform": "slack",
            "channel_id": "C1", "message_id": "1700.1",
            "alert_text": "High CPU", "alert_timestamp": "2025-01-15T14:00:00Z",
            "investigation_window": ["2025-01-15T13:55:00Z",
                                     "2025-01-15T14:05:00Z"]}},
        results={"eks": AgentResult(
            agent_name="eks", status="success",
            findings=[Finding(source="pod", timestamp="t",
                              content="CrashLoop", severity="critical")],
            summary="bad", metadata=AgentMetadata())},
    )
    base.update(over)
    return _StubStore(**base)


async def test_finalizes_page_when_page_exists(monkeypatch):
    platform = RecordingChatPlatform()
    page = {"schema_version": 1, "investigation_id": "inv-1",
            "status": "completed", "analysis": {"root_cause_hypothesis": "rc"},
            "timeline": [{"timestamp": "2025-01-15T14:00:00Z", "source": "alert",
                          "kind": "alert", "label": "High CPU",
                          "severity": None, "chart_id": None}]}
    store = _recovery_store(page_model=page)
    monkeypatch.setattr(tools.TraceStore, "from_env",
                        classmethod(lambda cls: store))
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.finalize_postmortem(
        _pir_payload(command_text="/postmortem db failover done")
    )
    await _drain_until(lambda: bool(store.page_writes))

    assert len(platform.deliveries) == 1  # PIR still posted
    assert len(store.page_writes) == 1
    write = store.page_writes[0]
    assert write["investigation_id"] == "inv-1"
    assert write["dt"] == "dt=2025-01-15"  # same key as the original page
    payload = write["payload"]
    assert payload["status"] == "resolved"
    assert payload["analysis"] == {"root_cause_hypothesis": "rc"}  # preserved
    last = payload["timeline"][-1]
    assert last["kind"] == "resolution"
    assert last["label"] == "db failover done"


async def test_no_page_model_is_noop(monkeypatch):
    platform = RecordingChatPlatform()
    store = _recovery_store(page_model=None)  # pages disabled / none archived
    monkeypatch.setattr(tools.TraceStore, "from_env",
                        classmethod(lambda cls: store))
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.finalize_postmortem(_pir_payload())
    await _drain_until(lambda: bool(platform.deliveries))

    assert len(platform.deliveries) == 1  # PIR posts regardless
    assert store.page_writes == []


async def test_page_finalize_failure_does_not_block_pir(monkeypatch):
    platform = RecordingChatPlatform()
    page = {"schema_version": 1, "investigation_id": "inv-1",
            "status": "completed", "timeline": []}
    store = _recovery_store(page_model=page, put_page_raises=True)
    monkeypatch.setattr(tools.TraceStore, "from_env",
                        classmethod(lambda cls: store))
    monkeypatch.setattr(tools, "for_platform", lambda name: platform)

    await tools.finalize_postmortem(_pir_payload())
    await _drain_until(lambda: bool(platform.deliveries))

    assert len(platform.deliveries) == 1  # PIR posted despite page write failure
