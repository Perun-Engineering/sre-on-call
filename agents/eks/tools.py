"""EKS Agent tools — gather Kubernetes cluster state.

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 9.6
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from datetime import datetime, timezone

from kubernetes.client.exceptions import ApiException
from strands import tool

from shared.models import Finding
from shared.tool_result import ToolResult, build_agent_result, format_result

logger = logging.getLogger(__name__)

_LOG_TAIL_LINES: int = 50


def _get_eks_bearer_token(cluster_name: str, region: str) -> str:
    """Generate an EKS IAM bearer token (``k8s-aws-v1.<base64>``).

    Implements the same SigV4-presigned ``sts:GetCallerIdentity`` flow as
    ``aws eks get-token`` and ``aws-iam-authenticator`` so we don't need
    the AWS CLI in the runtime container.
    """
    import boto3
    from botocore.signers import RequestSigner

    session = boto3.session.Session()
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


def _get_pods_for_deployment(apps_v1, core_v1, namespace: str, deployment_name: str) -> list:
    """Find pods belonging to a deployment by tracing the ReplicaSet chain."""
    try:
        deployment = apps_v1.read_namespaced_deployment(
            name=deployment_name, namespace=namespace,
        )
    except ApiException:
        logger.warning("Deployment %s not found in namespace %s", deployment_name, namespace)
        return []

    match_labels = deployment.spec.selector.match_labels or {}
    if not match_labels:
        return []

    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    return core_v1.list_namespaced_pod(
        namespace=namespace, label_selector=label_selector,
    ).items


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
                matched = _get_pods_for_deployment(apps_v1, core_v1, namespace, selector)
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
    result = _execute_gather(core_v1, apps_v1, namespace, resource_selectors)
    return format_result(build_agent_result("eks", result))


def _execute_gather(
    core_v1,
    apps_v1,
    namespace: str,
    resource_selectors: list[str],
) -> ToolResult:
    """Core gathering logic — all I/O goes through *core_v1* and *apps_v1*.

    Args:
        core_v1: A Kubernetes CoreV1Api client.
        apps_v1: A Kubernetes AppsV1Api client.
        namespace: Kubernetes namespace to inspect.
        resource_selectors: Deployment names or label selectors.
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
        _gather_pod_events(core_v1, namespace, pod_name, result)
        _gather_pod_logs(core_v1, namespace, pod_name, result)
        if pod.spec.node_name:
            node_names.add(pod.spec.node_name)

    _gather_node_conditions(core_v1, node_names, result)

    return result
