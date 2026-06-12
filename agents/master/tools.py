"""Master Agent tools — bridge the LLM to the deterministic orchestrators.

Three async tools wrap the master's orchestration paths:

* :func:`investigate_alert` — full incident investigation lifecycle, fanning
  out to active specialized agents and posting an Incident Report.
* :func:`capture_status_snapshot` — read-only ``/sre-snapshot`` snapshot, fanning
  out a snapshot request and posting a :class:`SnapshotSections` payload at
  top-level.
* :func:`finalize_postmortem` — handles the ``/postmortem`` ``task: "pir"``
  dispatch: recovers the original investigation from the trace archive,
  rebuilds the PIR via :meth:`ReportFormatter.build_pir_sections`, and posts
  it threaded.

Both tools kick off their orchestration as background asyncio tasks and
return immediately so the upstream Lambda invoker isn't held open for the
full lifecycle. Strands A2AServer is a long-running uvicorn process, so
background tasks keep running across requests until AgentCore tears the
container down.

The fan-out target lists are derived from the
:class:`shared.agents.AgentRegistry`'s ``active(kind="specialized")`` view —
i.e. specialized agents listed in ``config.yaml`` with ``enabled: true``.
There is no ``ENABLED_AGENTS`` allowlist; operators control fan-out by
editing ``config.yaml``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from strands import tool

from agents.master.orchestrator import InvestigationOrchestrator
from agents.master.report_formatter import ReportFormatter
from agents.master.snapshot_orchestrator import StatusSnapshotOrchestrator
from shared import busy_state
from shared.models import AlertContext
from shared.platforms import DeliveryTarget, deliver_with_retry, for_platform
from shared.report_renderer import FailureNoticeSections
from shared.trace_store import TraceStore

logger = logging.getLogger(__name__)


def _alert_context_from_payload(payload: dict) -> AlertContext:
    """Reconstruct an :class:`AlertContext` from a JSON-decoded dict.

    ``asdict()`` on the Lambda side converts the ``investigation_window``
    tuple to a list, which JSON round-trips back as a list. The
    dataclass tolerates either, but normalising here keeps downstream
    code that pattern-matches on the tuple shape happy.
    """
    window = payload.get("investigation_window")
    if isinstance(window, list) and len(window) == 2:
        payload = {**payload, "investigation_window": (window[0], window[1])}
    return AlertContext(**payload)


def _notify_on_investigation_failure(
    task: asyncio.Task, alert_context: AlertContext
) -> None:
    """Done-callback: post a failure notice when an investigation crashes.

    The orchestrator owns posting on the happy path; this only fires when the
    background task raises before its Incident Report lands, leaving the
    channel with a lone "Investigation Started" message. Fail-open — the
    callback runs in the event loop's exception-unfriendly context, so any
    error here is logged and swallowed, never re-raised. A cancelled task
    (instance teardown) is not treated as a failure.
    """
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:  # pragma: no cover - defensive
        return
    if exc is None:
        return

    logger.error(
        "Investigation %s task died before reporting: %s",
        alert_context.investigation_id,
        exc,
        exc_info=exc,
    )
    try:
        asyncio.create_task(
            _post_failure_notice(alert_context),
            name=f"failure-notice-{alert_context.investigation_id}",
        )
    except Exception:
        logger.exception(
            "Failed to schedule failure notice for investigation %s",
            alert_context.investigation_id,
        )


async def _post_failure_notice(alert_context: AlertContext) -> None:
    """Deliver a short "investigation died" notice to the originating thread.

    Fail-open: a failed notice is logged and never raised.
    """
    sections = FailureNoticeSections(
        investigation_id=alert_context.investigation_id,
        detail=(
            "The investigation stopped unexpectedly before posting its report. "
            "Reference the investigation ID above to consult the trace archive."
        ),
    )
    try:
        platform = for_platform(alert_context.platform)
        target = DeliveryTarget.for_alert(alert_context)
        await deliver_with_retry(platform, target, sections)
    except Exception:
        logger.exception(
            "Failed to deliver failure notice for investigation %s",
            alert_context.investigation_id,
        )


@tool
async def investigate_alert(alert_context_json: str) -> str:
    """Run the full incident investigation lifecycle for an alert.

    Fans out to every active specialized agent (per the registry's view of
    ``config.yaml``), enforces the 60-second initial deadline and 5-minute
    hard cutoff, and posts the Incident Report plus enrichment updates to
    the originating chat platform directly. The return string is a status
    line for the LLM and is not user-visible.

    Args:
        alert_context_json: The verbatim JSON payload received in the A2A
            ``message/send`` text part. Must deserialize into a
            :class:`shared.models.AlertContext`.

    Returns:
        A short status string describing how the investigation concluded.
    """
    payload = json.loads(alert_context_json)
    alert_context = _alert_context_from_payload(payload)

    orchestrator = InvestigationOrchestrator()
    enabled_agents = sorted(orchestrator.agent_endpoints.keys())

    if not enabled_agents:
        msg = (
            f"Investigation {alert_context.investigation_id} aborted: "
            f"no active specialized agents in config.yaml "
            f"(check the `enabled: true` flag on each agent)."
        )
        logger.error(msg)
        return msg

    logger.info(
        "Starting investigation %s, fan-out=%s",
        alert_context.investigation_id,
        enabled_agents,
    )
    task = asyncio.create_task(
        orchestrator.investigate(alert_context),
        name=f"investigate-{alert_context.investigation_id}",
    )
    # Track as in-flight work: holds a strong reference so the loop doesn't GC
    # the task mid-flight, and flips /ping to HealthyBusy so AgentCore won't
    # reclaim the instance before the investigation finishes.
    busy_state.track(task)
    # Surface a crash that kills the investigation before it posts a report,
    # so the channel doesn't see "Investigation Started" then silence (#22).
    task.add_done_callback(
        lambda t: _notify_on_investigation_failure(t, alert_context)
    )

    return (
        f"Investigation {alert_context.investigation_id} started "
        f"(fan-out: {', '.join(enabled_agents)}). "
        f"Results will be posted to chat as agents respond."
    )


@tool
async def capture_status_snapshot(snapshot_request_json: str) -> str:
    """Run the ``/sre-snapshot`` snapshot lifecycle for an operator.

    Fans out a snapshot A2A request to every active specialized agent (per
    the registry's view of ``config.yaml``), aggregates the
    :class:`shared.models.SnapshotReport` results under a 30-second hard
    cutoff, and posts a :class:`shared.report_renderer.SnapshotSections`
    payload to the originating chat platform at top-level (not as a thread
    reply). The return string is a status line for the LLM and is not
    user-visible.

    Args:
        snapshot_request_json: The verbatim JSON payload received in the A2A
            ``message/send`` text part. Must be an object with ``task =
            "snapshot"``, plus ``platform``, ``channel_id``, ``user_id``,
            and ``requested_at``.

    Returns:
        A short status string describing how the snapshot was dispatched.
    """
    payload = json.loads(snapshot_request_json)
    task_kind = payload.get("task")
    if task_kind != "snapshot":
        msg = (
            f"capture_status_snapshot received unexpected task={task_kind!r}; "
            f"expected 'snapshot'."
        )
        logger.error(msg)
        return msg

    requested_at = payload.get("requested_at", "")
    orchestrator = StatusSnapshotOrchestrator()
    enabled_agents = sorted(orchestrator.agent_endpoints.keys())

    logger.info(
        "Starting status snapshot at requested_at=%s, fan-out=%s",
        requested_at,
        enabled_agents,
    )
    bg_task = asyncio.create_task(
        orchestrator.capture(payload),
        name=f"snapshot-{requested_at or 'now'}",
    )
    # Track as in-flight work: holds a strong reference so the loop doesn't GC
    # the task mid-flight, and flips /ping to HealthyBusy so AgentCore won't
    # reclaim the instance before the snapshot is collected.
    busy_state.track(bg_task)

    return (
        f"Status snapshot started "
        f"(fan-out: {', '.join(enabled_agents) or 'no active specialized agents'}). "
        f"Result will be posted to chat once collected."
    )


def _post_pir_notice(
    platform: str, channel_id: str, thread_ts: str, text: str
) -> None:
    """Post a short fail-open notice into the incident thread (#56).

    Uses the sync ``ChatPlatform.notice`` seam; swallows delivery errors.
    """
    try:
        target = DeliveryTarget(
            platform=platform, channel_id=channel_id, thread_anchor=thread_ts,
        )
        for_platform(platform).notice(target, text)
    except Exception:
        logger.exception(
            "Failed to post PIR notice to %s/%s", channel_id, thread_ts,
        )


async def _run_postmortem(
    *, platform: str, channel_id: str, thread_ts: str
) -> None:
    """Recover the investigation, build the PIR, and post it to the thread.

    Fail-open at every recovery miss — the thread gets one short notice,
    never silence.
    """
    try:
        store = TraceStore.from_env()
        if store is None:
            _post_pir_notice(
                platform, channel_id, thread_ts,
                "Post-incident review is unavailable: the trace archive is "
                "disabled in this deployment.",
            )
            return

        ref = store.find_investigation(channel_id, thread_ts)
        if ref is None:
            _post_pir_notice(
                platform, channel_id, thread_ts,
                "Couldn't locate the original investigation for this thread "
                "— it may have aged out of the trace archive.",
            )
            return

        manifest = store.get_manifest(ref.investigation_id, dt=ref.dt)
        results = store.get_results(ref.investigation_id, dt=ref.dt)
        if not manifest or results is None or "alert_context" not in manifest:
            _post_pir_notice(
                platform, channel_id, thread_ts,
                "The original investigation's records are incomplete; cannot "
                "assemble a post-incident review.",
            )
            return

        alert_context = _alert_context_from_payload(manifest["alert_context"])
        sections = ReportFormatter().build_pir_sections(alert_context, results)

        chat = for_platform(platform)
        target = DeliveryTarget(
            platform=platform, channel_id=channel_id, thread_anchor=thread_ts,
        )
        await deliver_with_retry(chat, target, sections)
    except Exception:
        logger.exception(
            "Postmortem assembly failed for thread %s/%s",
            channel_id, thread_ts,
        )
        _post_pir_notice(
            platform, channel_id, thread_ts,
            "The post-incident review failed to assemble. Consult the trace "
            "archive directly using the investigation reference.",
        )


@tool
async def finalize_postmortem(pir_request_json: str) -> str:
    """Build and post a Post-Incident Report for an incident thread.

    Handles the ``task: "pir"`` master dispatch fired by ``/postmortem``.
    Recovers the original investigation from the trace archive (by
    channel + thread), rebuilds the report, and posts it as a thread reply.
    Kicks off in the background and returns a status line (not user-visible).

    Args:
        pir_request_json: The verbatim A2A text payload — a JSON object with
            ``task = "pir"`` plus ``platform``, ``channel_id``, ``thread_ts``,
            ``user_id``, ``command_text``.

    Returns:
        A short status string describing how the PIR was dispatched.
    """
    payload = json.loads(pir_request_json)
    task_kind = payload.get("task")
    if task_kind != "pir":
        msg = (
            f"finalize_postmortem received unexpected task={task_kind!r}; "
            f"expected 'pir'."
        )
        logger.error(msg)
        return msg

    platform = str(payload.get("platform", ""))
    channel_id = str(payload.get("channel_id", ""))
    thread_ts = str(payload.get("thread_ts", ""))

    bg_task = asyncio.create_task(
        _run_postmortem(
            platform=platform, channel_id=channel_id, thread_ts=thread_ts,
        ),
        name=f"pir-{channel_id}-{thread_ts}",
    )
    # Hold a strong ref + flip /ping to HealthyBusy while the PIR assembles.
    busy_state.track(bg_task)

    return (
        f"Post-incident review started for thread {thread_ts} in "
        f"{channel_id}. The report will be posted to the thread once assembled."
    )
