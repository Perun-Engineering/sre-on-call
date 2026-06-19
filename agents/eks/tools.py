"""EKS Agent tools — gather Kubernetes cluster state.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 9.6
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from kubernetes.client.exceptions import ApiException
from strands import tool

from shared.digest_tier import execute_digest
from shared.log_summarizer import (
    BedrockLogSummarizer,
    Digest,
    SummarizeChunk,
    Summarizer,
)
from shared.models import Finding, SnapshotReport, SnapshotSection
from shared.tool_result import (
    SEVERITY_RANK,
    ToolResult,
    build_agent_result,
    format_result,
    format_snapshot_result,
    pick_top_by_severity,
)

logger = logging.getLogger(__name__)

_LOG_TAIL_LINES: int = 50

# How many top-severity raw events survive a pod digest as exemplars (issue #49).
_SUMMARIZER_POD_EXEMPLARS: int = 1


def _get_eks_bearer_token(cluster_name: str, region: str) -> str:
    """Generate an EKS IAM bearer token (``k8s-aws-v1.<base64>``).

    Implements the same SigV4-presigned ``sts:GetCallerIdentity`` flow as
    ``aws eks get-token`` and ``aws-iam-authenticator`` so we don't need
    the AWS CLI in the runtime container.
    """
    import boto3
    from botocore.signers import RequestSigner

    session = boto3.Session()
    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError("No AWS credentials available for EKS token generation.")

    sts = session.client("sts", region_name=region)
    signer = RequestSigner(
        sts.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        creds,
        session.events,
    )
    signed_url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        },
        region_name=region,
        expires_in=60,
        operation_name="",
    )
    if signed_url is None:
        raise RuntimeError("Failed to presign STS URL for EKS token generation.")
    encoded = base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode("utf-8")
    return "k8s-aws-v1." + encoded.rstrip("=")


def _load_kube_config_from_eks(cluster_name: str, region: str) -> None:
    """Configure the global kubernetes client to talk to an EKS cluster via IAM auth."""
    import boto3
    from kubernetes import client as k8s_client

    eks = boto3.client("eks", region_name=region)
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]

    ca_data = base64.b64decode(cluster["certificateAuthority"]["data"])
    ca_file = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".crt")
    ca_file.write(ca_data)
    ca_file.close()

    cfg = k8s_client.Configuration()
    cfg.host = cluster["endpoint"]
    cfg.ssl_ca_cert = ca_file.name  # type: ignore[misc]
    cfg.api_key["authorization"] = "Bearer " + _get_eks_bearer_token(cluster_name, region)
    k8s_client.Configuration.set_default(cfg)


def _load_kube_config():
    """Load Kubernetes configuration.

    Lookup order:
        1. EKS API auth when ``EKS_CLUSTER_NAME`` env is set (deployed AgentCore)
        2. In-cluster config (when running as a pod)
        3. Default kubeconfig file (local dev)
    """
    cluster_name = os.environ.get("EKS_CLUSTER_NAME")
    if cluster_name:
        region = os.environ.get("AWS_REGION", "us-east-1")
        _load_kube_config_from_eks(cluster_name, region)
        logger.info("Loaded EKS config for cluster %s in %s.", cluster_name, region)
        return

    from kubernetes import config

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config.")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig from default location.")
        except config.ConfigException as exc:
            raise RuntimeError(
                "Unable to load Kubernetes configuration. Set EKS_CLUSTER_NAME for "
                "EKS IAM auth, run in-cluster, or provide a kubeconfig."
            ) from exc


def _is_label_selector(selector: str) -> bool:
    """Return True if *selector* looks like a Kubernetes label selector."""
    return "=" in selector


def _pods_from_match_labels(core_v1, namespace: str, match_labels: dict | None) -> list:
    """List pods matching a controller's ``spec.selector.matchLabels``."""
    if not match_labels:
        return []
    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    return core_v1.list_namespaced_pod(
        namespace=namespace, label_selector=label_selector,
    ).items


def _get_pods_for_workload(apps_v1, core_v1, namespace: str, name: str) -> list:
    """Resolve a bare workload *name* to its pods.

    Tries Deployment, then DaemonSet, then StatefulSet — all three expose
    ``spec.selector.matchLabels``, so the pod lookup is identical once the
    controller is found. Covering DaemonSets/StatefulSets matters because the
    workloads behind node-level ``TargetDown`` alerts (``aws-node``,
    ``kube-proxy``, ``node-exporter``, kubelet, log shippers) are DaemonSets,
    which a Deployment-only resolver can never find. Returns ``[]`` when no
    controller of any kind matches the name; a non-404 API error propagates so
    the caller can record it.
    """
    readers = (
        apps_v1.read_namespaced_deployment,
        apps_v1.read_namespaced_daemon_set,
        apps_v1.read_namespaced_stateful_set,
    )
    for read in readers:
        try:
            workload = read(name=name, namespace=namespace)
        except ApiException as exc:
            if exc.status == 404:
                continue
            raise
        return _pods_from_match_labels(core_v1, namespace, workload.spec.selector.match_labels)

    logger.warning(
        "No Deployment/DaemonSet/StatefulSet %s found in namespace %s", name, namespace
    )
    return []


def _get_pods_by_label(core_v1, namespace: str, label_selector: str) -> list:
    """List pods matching a label selector in the given namespace."""
    return core_v1.list_namespaced_pod(
        namespace=namespace, label_selector=label_selector,
    ).items


def _collect_pods(apps_v1, core_v1, namespace: str, resource_selectors: list[str], result: ToolResult) -> list:
    """Resolve *resource_selectors* to a deduplicated list of pods."""
    seen: set[str] = set()
    pods: list = []

    for selector in resource_selectors:
        try:
            if _is_label_selector(selector):
                matched = _get_pods_by_label(core_v1, namespace, selector)
            else:
                matched = _get_pods_for_workload(apps_v1, core_v1, namespace, selector)
        except ApiException as exc:
            msg = f"Error resolving selector '{selector}': {exc.reason}"
            logger.warning(msg)
            result.errors.append(msg)
            continue

        for pod in matched:
            name = pod.metadata.name
            if name not in seen:
                seen.add(name)
                pods.append(pod)

    return pods


def _pod_phase(pod) -> str:
    if pod.status and pod.status.phase:
        return pod.status.phase
    return "Unknown"


def _container_statuses_summary(pod) -> str:
    if not pod.status or not pod.status.container_statuses:
        return "no container status available"

    parts: list[str] = []
    for cs in pod.status.container_statuses:
        ready = "ready" if cs.ready else "not-ready"
        restarts = cs.restart_count or 0
        state = "unknown"
        if cs.state:
            if cs.state.running:
                state = "running"
            elif cs.state.waiting:
                state = f"waiting ({cs.state.waiting.reason or 'unknown'})"
            elif cs.state.terminated:
                state = f"terminated ({cs.state.terminated.reason or 'unknown'})"
        parts.append(f"{cs.name}: {state}, {ready}, restarts={restarts}")
    return "; ".join(parts)


def _severity_from_phase(phase: str) -> str:
    """Map a pod phase to a finding severity."""
    if phase in ("Failed", "Unknown"):
        return "critical"
    if phase == "Pending":
        return "warning"
    return "info"


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _gather_pod_status(pod, result: ToolResult) -> None:
    name = pod.metadata.name
    phase = _pod_phase(pod)
    containers = _container_statuses_summary(pod)
    node_name = pod.spec.node_name or "unscheduled"

    result.findings.append(
        Finding(
            source=f"pod/{name}",
            timestamp=_iso_now(),
            content=f"Pod {name}: phase={phase}, node={node_name}, containers=[{containers}]",
            severity=_severity_from_phase(phase),
            metadata={"kind": "pod_status", "pod": name, "phase": phase, "node": node_name},
        )
    )
    result.scanned_items.append(f"pod/{name}")


def _gather_pod_events(core_v1, namespace: str, pod_name: str, result: ToolResult) -> None:
    try:
        events = core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod",
        )
    except ApiException as exc:
        msg = f"Failed to retrieve events for pod {pod_name}: {exc.reason}"
        logger.warning(msg)
        result.errors.append(msg)
        return

    for event in events.items:
        ts = ""
        if event.last_timestamp:
            ts = event.last_timestamp.isoformat()
        elif event.event_time:
            ts = event.event_time.isoformat()

        severity = "warning" if event.type == "Warning" else "info"

        result.findings.append(
            Finding(
                source=f"pod/{pod_name}",
                timestamp=ts,
                content=f"[{event.type}] {event.reason}: {event.message}",
                severity=severity,
                metadata={
                    "kind": "event",
                    "pod": pod_name,
                    "event_type": event.type or "",
                    "reason": event.reason or "",
                    "count": event.count or 0,
                },
            )
        )


def _gather_pod_logs(core_v1, namespace: str, pod_name: str, result: ToolResult) -> None:
    try:
        logs = core_v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, tail_lines=_LOG_TAIL_LINES,
        )
    except ApiException as exc:
        msg = f"Failed to fetch logs for pod {pod_name}: {exc.reason}"
        logger.warning(msg)
        result.errors.append(msg)
        return

    if logs:
        result.findings.append(
            Finding(
                source=f"pod/{pod_name}",
                timestamp=_iso_now(),
                content=f"Tail logs ({_LOG_TAIL_LINES} lines):\n{logs}",
                severity="info",
                metadata={"kind": "pod_logs", "pod": pod_name, "tail_lines": _LOG_TAIL_LINES},
            )
        )


def _gather_node_conditions(core_v1, node_names: set[str], result: ToolResult) -> None:
    for node_name in sorted(node_names):
        try:
            node = core_v1.read_node(name=node_name)
        except ApiException as exc:
            msg = f"Failed to read node {node_name}: {exc.reason}"
            logger.warning(msg)
            result.errors.append(msg)
            continue

        result.scanned_items.append(f"node/{node_name}")

        if not node.status or not node.status.conditions:
            result.findings.append(
                Finding(
                    source=f"node/{node_name}",
                    timestamp=_iso_now(),
                    content=f"Node {node_name}: no conditions available",
                    severity="warning",
                    metadata={"kind": "node_condition", "node": node_name},
                )
            )
            continue

        for condition in node.status.conditions:
            severity = "info"
            if condition.type == "Ready" and condition.status != "True":
                severity = "critical"
            elif condition.type != "Ready" and condition.status == "True":
                severity = "warning"

            ts = ""
            if condition.last_transition_time:
                ts = condition.last_transition_time.isoformat()

            result.findings.append(
                Finding(
                    source=f"node/{node_name}",
                    timestamp=ts,
                    content=(
                        f"Node {node_name}: {condition.type}={condition.status} "
                        f"(reason={condition.reason}, message={condition.message})"
                    ),
                    severity=severity,
                    metadata={
                        "kind": "node_condition",
                        "node": node_name,
                        "condition_type": condition.type,
                        "condition_status": condition.status,
                    },
                )
            )


def _pod_bulk_volume(findings: list[Finding]) -> int:
    """Approximate line volume of a pod's bulk findings (events + log tail)."""
    total = 0
    for f in findings:
        if f.metadata.get("kind") == "event":
            total += 1
        else:  # a log-tail blob — count its lines
            total += f.content.count("\n") + 1
    return total


def _max_finding_severity(findings: list[Finding]) -> str:
    """Highest severity (critical > warning > info) across a set of findings."""
    best = "info"
    for f in findings:
        if SEVERITY_RANK.get(f.severity, 2) < SEVERITY_RANK.get(best, 2):
            best = f.severity
    return best


class EksPodDigestSource:
    """:class:`shared.digest_tier.DigestSource` over one pod's events + log tail.

    A single chunk per pod (events + log-tail findings joined); the digest
    finding carries no deep link / chart. Exemplars are the top-severity raw
    *events* kept beside the digest when the chunk was digested away.
    """

    def __init__(self, pod_name: str, bulk_findings: list[Finding]) -> None:
        self._pod_name = pod_name
        self._bulk = bulk_findings
        self._key = f"pod/{pod_name}"

    def volume(self) -> int:
        return _pod_bulk_volume(self._bulk)

    def raw_findings(self) -> list[Finding]:
        return list(self._bulk)

    def chunks(self) -> list[SummarizeChunk]:
        return [
            SummarizeChunk(
                key=self._key,
                text="\n".join(f.content for f in self._bulk),
                severity=_max_finding_severity(self._bulk),
            )
        ]

    def chunk_raw_findings(self, chunk_key: str) -> list[Finding]:
        return list(self._bulk)

    def digest_finding(self, digest: Digest, chunk: SummarizeChunk) -> Finding:
        return Finding(
            source=self._key,
            timestamp=_iso_now(),
            content=digest.text or "",
            severity=chunk.severity,
            metadata={
                "kind": "pod_digest",
                "pod": self._pod_name,
                "covered": len(self._bulk),
            },
        )

    def exemplars(self, kept_raw: set[str]) -> list[Finding]:
        if self._key in kept_raw:
            return []  # chunk fell back to raw — events already shown literally
        events = [f for f in self._bulk if f.metadata.get("kind") == "event"]
        picked = pick_top_by_severity(events, lambda f: f.severity, _SUMMARIZER_POD_EXEMPLARS)
        for f in picked:
            f.metadata["exemplar"] = True
        return picked


def _gather_pod_bulk(
    core_v1,
    namespace: str,
    pod_name: str,
    summarizer: Summarizer | None,
    result: ToolResult,
) -> None:
    """Gather a pod's events + log tail, digesting them when volume warrants it.

    With a summarizer present and the pod's combined event/log volume over the
    min-volume gate, the raw event + log findings are replaced by a single
    per-pod digest finding (issue #49), keeping the top-severity event as an
    exemplar. A failed digest, sub-gate volume, or no summarizer leaves the raw
    findings in place — byte-identical to the pre-#49 path.
    """
    bulk = ToolResult()
    _gather_pod_events(core_v1, namespace, pod_name, bulk)
    _gather_pod_logs(core_v1, namespace, pod_name, bulk)
    result.errors.extend(bulk.errors)

    source = EksPodDigestSource(pod_name, bulk.findings)
    result.findings.extend(execute_digest(source, summarizer))


@tool
def gather_eks_state(
    namespace: str,
    resource_selectors: list[str],
) -> str:
    """Gather Kubernetes cluster state for specified resources.

    Args:
        namespace: Kubernetes namespace to inspect.
        resource_selectors: Deployment names or label selectors.

    Returns:
        A human-readable summary string for the LLM to consume.
    """
    _load_kube_config()
    from kubernetes import client as k8s_client
    core_v1 = k8s_client.CoreV1Api()
    apps_v1 = k8s_client.AppsV1Api()
    result = _execute_gather(
        core_v1, apps_v1, namespace, resource_selectors,
        summarizer=BedrockLogSummarizer.from_env(),
    )
    return format_result(build_agent_result("eks", result))


def _execute_gather(
    core_v1,
    apps_v1,
    namespace: str,
    resource_selectors: list[str],
    *,
    summarizer: Summarizer | None = None,
) -> ToolResult:
    """Core gathering logic — all I/O goes through *core_v1* and *apps_v1*.

    Args:
        core_v1: A Kubernetes CoreV1Api client.
        apps_v1: A Kubernetes AppsV1Api client.
        namespace: Kubernetes namespace to inspect.
        resource_selectors: Deployment names or label selectors.
        summarizer: Optional Haiku map-reduce summarizer (issue #49). When
            supplied, each pod's bulky events + log tail are digested into a
            single finding; ``None`` keeps the raw findings (test default).
    """
    result = ToolResult()

    if not resource_selectors:
        result.errors.append("No resource selectors provided.")
        return result

    try:
        pods = _collect_pods(apps_v1, core_v1, namespace, resource_selectors, result)
    except ApiException as exc:
        result.errors.append(f"EKS cluster API server unreachable: {exc.reason}")
        return result

    if not pods and not result.errors:
        result.errors.append(
            f"No pods found matching selectors {resource_selectors} "
            f"in namespace '{namespace}'."
        )
        return result

    node_names: set[str] = set()

    for pod in pods:
        pod_name = pod.metadata.name
        _gather_pod_status(pod, result)
        _gather_pod_bulk(core_v1, namespace, pod_name, summarizer, result)
        if pod.spec.node_name:
            node_names.add(pod.spec.node_name)

    _gather_node_conditions(core_v1, node_names, result)

    return result


# ---------------------------------------------------------------------------
# /sre-snapshot snapshot — read-only cluster overview
# ---------------------------------------------------------------------------


_SNAPSHOT_PHASE_ORDER = ("Failed", "Unknown", "Pending", "Succeeded", "Running")
_SNAPSHOT_NON_RUNNING_LIMIT: int = 10
_SNAPSHOT_KUBE_SYSTEM_EVENT_LIMIT: int = 5
_SNAPSHOT_KUBE_SYSTEM_EVENT_WINDOW = timedelta(minutes=5)
_SNAPSHOT_RESTART_ANOMALY_THRESHOLD: int = 3


@tool
def capture_snapshot(requested_at: str) -> str:
    """Capture a read-only snapshot of EKS cluster state.

    Probes nodes, pods across all namespaces, and recent kube-system
    warning events. Returns a human-readable summary with an embedded
    :class:`SnapshotReport` footer that the master orchestrator extracts
    via :data:`shared.tool_result.SNAPSHOT_RESULT`.

    The tool never raises: if the cluster config cannot be loaded or the
    API is unreachable, the failure is folded into a single anomaly
    section.

    Args:
        requested_at: ISO 8601 timestamp from the master, used as the
            ``captured_at`` field of the returned report.

    Returns:
        A short human-readable string ending with a
        ``<<<SNAPSHOT_RESULT ... SNAPSHOT_RESULT>>>`` footer.
    """
    try:
        _load_kube_config()
        from kubernetes import client as k8s_client

        core_v1 = k8s_client.CoreV1Api()
        version_api = k8s_client.VersionApi()
    except Exception as exc:
        report = SnapshotReport(
            agent_name="eks",
            captured_at=requested_at,
            sections=[
                SnapshotSection(
                    label="Cluster",
                    lines=[f"❌ failed to load cluster config: {exc}"],
                )
            ],
            anomaly=True,
            anomaly_summary=f"EKS cluster config could not be loaded: {exc}",
        )
        return format_snapshot_result(report)

    report = _execute_capture_snapshot(
        core_v1, version_api, requested_at=requested_at,
    )
    return format_snapshot_result(report)


def _execute_capture_snapshot(
    core_v1,
    version_api,
    *,
    requested_at: str,
    cluster_name: str | None = None,
    now: datetime | None = None,
) -> SnapshotReport:
    """Pure snapshot builder. All I/O goes through *core_v1* and *version_api*.

    Tests pass mock K8s clients to drive every branch: happy path,
    NotReady node, Failed pod, restart-count anomaly, kube-system events,
    and per-probe API failures (which surface as ❌ section lines but
    only flip ``anomaly=True`` if they're an anomaly-criteria probe).
    """
    sections: list[SnapshotSection] = []
    anomalies: list[str] = []

    cluster_name = cluster_name or os.environ.get("EKS_CLUSTER_NAME") or "(unknown)"
    now = now or datetime.now(tz=timezone.utc)
    cutoff = now - _SNAPSHOT_KUBE_SYSTEM_EVENT_WINDOW

    # ----------------------------------------------------------------------
    # Section 1: Cluster identity (cluster name + server version)
    # ----------------------------------------------------------------------
    cluster_lines: list[str] = [f"name: {cluster_name}"]
    try:
        version = version_api.get_code()
        git_version = getattr(version, "git_version", None) or "(unknown)"
        cluster_lines.append(f"server version: {git_version}")
    except Exception as exc:
        cluster_lines.append(f"❌ failed to read server version: {exc}")
    sections.append(SnapshotSection(label="Cluster", lines=cluster_lines))

    # ----------------------------------------------------------------------
    # Section 2: Nodes
    # ----------------------------------------------------------------------
    node_lines: list[str] = []
    try:
        nodes = core_v1.list_node().items
        ready = sum(1 for n in nodes if _node_is_ready(n))
        not_ready = sum(1 for n in nodes if _node_is_not_ready(n))
        sched_disabled = sum(1 for n in nodes if _node_is_scheduling_disabled(n))
        node_lines.append(
            f"{ready} Ready · {not_ready} NotReady · {sched_disabled} SchedulingDisabled"
        )
        if not_ready > 0:
            anomalies.append(f"{not_ready} node(s) NotReady")
    except ApiException as exc:
        node_lines.append(f"❌ list_node failed: {_api_error(exc)}")
    except Exception as exc:
        node_lines.append(f"❌ list_node failed: {exc}")
    sections.append(SnapshotSection(label="Nodes", lines=node_lines))

    # ----------------------------------------------------------------------
    # Section 3 + 4: Pod phase counts + non-Running list
    # ----------------------------------------------------------------------
    try:
        pods = core_v1.list_pod_for_all_namespaces().items
    except ApiException as exc:
        sections.append(
            SnapshotSection(
                label="Pods",
                lines=[f"❌ list_pod_for_all_namespaces failed: {_api_error(exc)}"],
            )
        )
        sections.append(
            SnapshotSection(label="Non-Running pods (top 10)", lines=[])
        )
        return _finalise_report(requested_at, sections, anomalies)
    except Exception as exc:
        sections.append(
            SnapshotSection(
                label="Pods",
                lines=[f"❌ list_pod_for_all_namespaces failed: {exc}"],
            )
        )
        sections.append(
            SnapshotSection(label="Non-Running pods (top 10)", lines=[])
        )
        return _finalise_report(requested_at, sections, anomalies)

    phase_counts: dict[str, int] = {p: 0 for p in _SNAPSHOT_PHASE_ORDER}
    non_running: list[tuple] = []  # (phase_rank, -restart_count, ns, name, phase, restarts)
    for pod in pods:
        phase = _pod_phase(pod)
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if phase != "Running":
            ns = pod.metadata.namespace if pod.metadata else "?"
            name = pod.metadata.name if pod.metadata else "?"
            restarts = _pod_total_restarts(pod)
            phase_rank = (
                _SNAPSHOT_PHASE_ORDER.index(phase)
                if phase in _SNAPSHOT_PHASE_ORDER
                else len(_SNAPSHOT_PHASE_ORDER)
            )
            non_running.append((phase_rank, -restarts, ns, name, phase, restarts))

    pod_summary = " · ".join(
        f"{phase_counts.get(p, 0)} {p}"
        for p in ("Running", "Pending", "Failed", "Unknown", "Succeeded")
    )
    sections.append(SnapshotSection(label="Pods", lines=[pod_summary]))

    if phase_counts.get("Failed", 0) > 0:
        anomalies.append(f"{phase_counts['Failed']} pod(s) Failed")

    non_running.sort()
    high_restart = sum(1 for _, _, _, _, _, r in non_running if r >= _SNAPSHOT_RESTART_ANOMALY_THRESHOLD)
    if high_restart > 0:
        anomalies.append(
            f"{high_restart} non-Running pod(s) with restarts ≥ {_SNAPSHOT_RESTART_ANOMALY_THRESHOLD}"
        )

    nr_lines: list[str] = []
    for _, _, ns, name, phase, restarts in non_running[:_SNAPSHOT_NON_RUNNING_LIMIT]:
        nr_lines.append(f"{ns}/{name} · phase={phase} · restarts={restarts}")
    if len(non_running) > _SNAPSHOT_NON_RUNNING_LIMIT:
        nr_lines.append(
            f"… {len(non_running) - _SNAPSHOT_NON_RUNNING_LIMIT} more not shown"
        )
    sections.append(
        SnapshotSection(label="Non-Running pods (top 10)", lines=nr_lines)
    )

    # ----------------------------------------------------------------------
    # Section 5: kube-system warning events (last 5 min)
    # ----------------------------------------------------------------------
    event_lines: list[str] = []
    try:
        events = core_v1.list_namespaced_event(
            namespace="kube-system", field_selector="type=Warning",
        ).items
        recent_warnings = []
        for event in events:
            ts = event.last_timestamp or event.event_time
            if ts is None:
                continue
            ts_dt = ts if isinstance(ts, datetime) else None
            if ts_dt is None:
                continue
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            if ts_dt < cutoff:
                continue
            recent_warnings.append((ts_dt, event))

        recent_warnings.sort(key=lambda t: t[0], reverse=True)
        for _, event in recent_warnings[:_SNAPSHOT_KUBE_SYSTEM_EVENT_LIMIT]:
            reason = getattr(event, "reason", None) or "(no reason)"
            message = getattr(event, "message", None) or "(no message)"
            obj = getattr(event, "involved_object", None)
            kind = getattr(obj, "kind", None) or "?"
            name = getattr(obj, "name", None) or "?"
            event_lines.append(f"{reason}: {message} ({kind}/{name})")
        if len(recent_warnings) > _SNAPSHOT_KUBE_SYSTEM_EVENT_LIMIT:
            event_lines.append(
                f"… {len(recent_warnings) - _SNAPSHOT_KUBE_SYSTEM_EVENT_LIMIT} more not shown"
            )
        if recent_warnings:
            anomalies.append(
                f"{len(recent_warnings)} kube-system warning event(s) in last 5 min"
            )
    except ApiException as exc:
        event_lines.append(
            f"❌ list_namespaced_event(kube-system) failed: {_api_error(exc)}"
        )
    except Exception as exc:
        event_lines.append(f"❌ list_namespaced_event(kube-system) failed: {exc}")
    sections.append(
        SnapshotSection(
            label="kube-system warning events (last 5 min)", lines=event_lines
        )
    )

    return _finalise_report(requested_at, sections, anomalies)


def _finalise_report(
    requested_at: str,
    sections: list[SnapshotSection],
    anomalies: list[str],
) -> SnapshotReport:
    """Build the final SnapshotReport from accumulated sections + anomaly list."""
    return SnapshotReport(
        agent_name="eks",
        captured_at=requested_at,
        sections=sections,
        anomaly=bool(anomalies),
        anomaly_summary="; ".join(anomalies) if anomalies else None,
    )


def _node_is_ready(node) -> bool:
    cond = _node_condition(node, "Ready")
    return cond is not None and cond.status == "True" and not _node_is_scheduling_disabled(node)


def _node_is_not_ready(node) -> bool:
    cond = _node_condition(node, "Ready")
    if cond is None:
        return True
    return cond.status != "True"


def _node_is_scheduling_disabled(node) -> bool:
    spec = getattr(node, "spec", None)
    return bool(getattr(spec, "unschedulable", False)) if spec else False


def _node_condition(node, condition_type: str):
    status = getattr(node, "status", None)
    if not status:
        return None
    for cond in getattr(status, "conditions", None) or []:
        if getattr(cond, "type", None) == condition_type:
            return cond
    return None


def _pod_total_restarts(pod) -> int:
    status = getattr(pod, "status", None)
    if not status:
        return 0
    statuses = getattr(status, "container_statuses", None) or []
    return sum((getattr(cs, "restart_count", 0) or 0) for cs in statuses)


def _api_error(exc: ApiException) -> str:
    reason = getattr(exc, "reason", None)
    return reason or str(exc)
