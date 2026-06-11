"""Unit tests for the EKS Agent tools.

Tests cover the core gathering logic in ``agents.eks.tools``,
including resource identification, pod status/events/logs/descriptions
gathering, node condition checks, and error handling for unreachable
EKS API server.

Requirements: 7.1, 7.2, 7.3, 7.5, 7.6
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.eks.tools import (
    _collect_pods,
    _container_statuses_summary,
    _execute_capture_snapshot,
    _execute_gather,
    _gather_node_conditions,
    _gather_pod_events,
    _gather_pod_logs,
    _gather_pod_status,
    _get_eks_bearer_token,
    _is_label_selector,
    _load_kube_config_from_eks,
    _pod_phase,
    _severity_from_phase,
)
from shared.models import Finding, SnapshotReport
from shared.tool_result import ToolResult, build_agent_result


# ---------------------------------------------------------------------------
# Helpers — build mock Kubernetes objects using SimpleNamespace
# ---------------------------------------------------------------------------


def _make_pod(
    name: str = "my-pod-abc",
    namespace: str = "default",
    phase: str = "Running",
    node_name: str = "node-1",
    containers: list | None = None,
) -> SimpleNamespace:
    """Build a mock V1Pod-like object."""
    if containers is None:
        containers = [
            _make_container_status("main", running=True, ready=True),
        ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        spec=SimpleNamespace(node_name=node_name),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=containers,
        ),
    )


def _make_container_status(
    name: str = "main",
    running: bool = True,
    ready: bool = True,
    restart_count: int = 0,
    waiting_reason: str | None = None,
    terminated_reason: str | None = None,
) -> SimpleNamespace:
    """Build a mock V1ContainerStatus-like object."""
    state = SimpleNamespace(running=None, waiting=None, terminated=None)
    if running:
        state.running = SimpleNamespace()
    elif waiting_reason:
        state.waiting = SimpleNamespace(reason=waiting_reason)
    elif terminated_reason:
        state.terminated = SimpleNamespace(reason=terminated_reason)

    return SimpleNamespace(
        name=name,
        ready=ready,
        restart_count=restart_count,
        state=state,
    )


def _make_event(
    name: str = "my-pod-abc",
    event_type: str = "Normal",
    reason: str = "Scheduled",
    message: str = "Successfully assigned",
    last_timestamp=None,
    event_time=None,
    count: int = 1,
) -> SimpleNamespace:
    """Build a mock V1Event-like object."""
    return SimpleNamespace(
        type=event_type,
        reason=reason,
        message=message,
        last_timestamp=last_timestamp,
        event_time=event_time,
        count=count,
    )


def _make_node(
    name: str = "node-1",
    conditions: list | None = None,
) -> SimpleNamespace:
    """Build a mock V1Node-like object."""
    if conditions is None:
        conditions = [
            SimpleNamespace(
                type="Ready",
                status="True",
                reason="KubeletReady",
                message="kubelet is posting ready status",
                last_transition_time=None,
            ),
        ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(conditions=conditions),
    )


def _make_deployment(match_labels: dict | None = None) -> SimpleNamespace:
    """Build a mock V1Deployment-like object."""
    if match_labels is None:
        match_labels = {"app": "my-service"}
    return SimpleNamespace(
        spec=SimpleNamespace(
            selector=SimpleNamespace(match_labels=match_labels),
        ),
    )


def _api_exception(status: int = 403, reason: str = "Forbidden"):
    """Create a kubernetes ApiException-like object."""
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=status, reason=reason)


# ---------------------------------------------------------------------------
# _is_label_selector
# ---------------------------------------------------------------------------


class TestIsLabelSelector:
    def test_label_selector(self):
        assert _is_label_selector("app=my-service") is True

    def test_deployment_name(self):
        assert _is_label_selector("my-deployment") is False

    def test_complex_selector(self):
        assert _is_label_selector("app=web,tier=frontend") is True

    def test_not_equal_selector(self):
        assert _is_label_selector("env!=staging") is True


# ---------------------------------------------------------------------------
# _pod_phase / _severity_from_phase
# ---------------------------------------------------------------------------


class TestPodPhase:
    def test_running(self):
        pod = _make_pod(phase="Running")
        assert _pod_phase(pod) == "Running"

    def test_no_status(self):
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name="p"),
            spec=SimpleNamespace(node_name=None),
            status=None,
        )
        assert _pod_phase(pod) == "Unknown"

    def test_no_phase(self):
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name="p"),
            spec=SimpleNamespace(node_name=None),
            status=SimpleNamespace(phase=None, container_statuses=None),
        )
        assert _pod_phase(pod) == "Unknown"


class TestSeverityFromPhase:
    def test_failed(self):
        assert _severity_from_phase("Failed") == "critical"

    def test_unknown(self):
        assert _severity_from_phase("Unknown") == "critical"

    def test_pending(self):
        assert _severity_from_phase("Pending") == "warning"

    def test_running(self):
        assert _severity_from_phase("Running") == "info"

    def test_succeeded(self):
        assert _severity_from_phase("Succeeded") == "info"


# ---------------------------------------------------------------------------
# _container_statuses_summary
# ---------------------------------------------------------------------------


class TestContainerStatusesSummary:
    def test_running_container(self):
        pod = _make_pod(containers=[
            _make_container_status("main", running=True, ready=True),
        ])
        summary = _container_statuses_summary(pod)
        assert "main" in summary
        assert "running" in summary
        assert "ready" in summary

    def test_waiting_container(self):
        pod = _make_pod(containers=[
            _make_container_status(
                "main", running=False, ready=False,
                waiting_reason="CrashLoopBackOff",
            ),
        ])
        summary = _container_statuses_summary(pod)
        assert "CrashLoopBackOff" in summary
        assert "not-ready" in summary

    def test_no_container_statuses(self):
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name="p"),
            spec=SimpleNamespace(node_name=None),
            status=SimpleNamespace(phase="Pending", container_statuses=None),
        )
        assert "no container status" in _container_statuses_summary(pod)

    def test_multiple_containers(self):
        pod = _make_pod(containers=[
            _make_container_status("app", running=True, ready=True),
            _make_container_status("sidecar", running=True, ready=True, restart_count=3),
        ])
        summary = _container_statuses_summary(pod)
        assert "app" in summary
        assert "sidecar" in summary
        assert "restarts=3" in summary


# ---------------------------------------------------------------------------
# _gather_pod_status
# ---------------------------------------------------------------------------


class TestGatherPodStatus:
    def test_adds_finding(self):
        pod = _make_pod(name="web-abc", phase="Running", node_name="node-1")
        result = ToolResult()

        _gather_pod_status(pod, result)

        assert len(result.findings) == 1
        assert result.findings[0].source == "pod/web-abc"
        assert "Running" in result.findings[0].content
        assert "node-1" in result.findings[0].content
        assert "pod/web-abc" in result.scanned_items

    def test_failed_pod_severity(self):
        pod = _make_pod(name="crash-pod", phase="Failed")
        result = ToolResult()

        _gather_pod_status(pod, result)

        assert result.findings[0].severity == "critical"


# ---------------------------------------------------------------------------
# _gather_pod_events
# ---------------------------------------------------------------------------


class TestGatherPodEvents:
    def test_adds_event_findings(self):
        core_v1 = MagicMock()
        core_v1.list_namespaced_event.return_value = SimpleNamespace(
            items=[
                _make_event(event_type="Warning", reason="BackOff", message="Back-off restarting"),
                _make_event(event_type="Normal", reason="Pulled", message="Image pulled"),
            ]
        )
        result = ToolResult()

        _gather_pod_events(core_v1, "default", "my-pod", result)

        assert len(result.findings) == 2
        assert result.findings[0].severity == "warning"
        assert result.findings[1].severity == "info"

    def test_api_error_recorded(self):
        core_v1 = MagicMock()
        core_v1.list_namespaced_event.side_effect = _api_exception(403, "Forbidden")
        result = ToolResult()

        _gather_pod_events(core_v1, "default", "my-pod", result)

        assert len(result.errors) == 1
        assert "Forbidden" in result.errors[0]

    def test_no_events(self):
        core_v1 = MagicMock()
        core_v1.list_namespaced_event.return_value = SimpleNamespace(items=[])
        result = ToolResult()

        _gather_pod_events(core_v1, "default", "my-pod", result)

        assert len(result.findings) == 0


# ---------------------------------------------------------------------------
# _gather_pod_logs
# ---------------------------------------------------------------------------


class TestGatherPodLogs:
    def test_adds_log_finding(self):
        core_v1 = MagicMock()
        core_v1.read_namespaced_pod_log.return_value = "ERROR: connection refused\nRetrying..."
        result = ToolResult()

        _gather_pod_logs(core_v1, "default", "my-pod", result)

        assert len(result.findings) == 1
        assert "connection refused" in result.findings[0].content
        assert result.findings[0].metadata["kind"] == "pod_logs"

    def test_empty_logs(self):
        core_v1 = MagicMock()
        core_v1.read_namespaced_pod_log.return_value = ""
        result = ToolResult()

        _gather_pod_logs(core_v1, "default", "my-pod", result)

        # Empty logs should not produce a finding
        assert len(result.findings) == 0

    def test_api_error_recorded(self):
        core_v1 = MagicMock()
        core_v1.read_namespaced_pod_log.side_effect = _api_exception(404, "Not Found")
        result = ToolResult()

        _gather_pod_logs(core_v1, "default", "my-pod", result)

        assert len(result.errors) == 1
        assert "Not Found" in result.errors[0]


# ---------------------------------------------------------------------------
# _gather_node_conditions
# ---------------------------------------------------------------------------


class TestGatherNodeConditions:
    def test_healthy_node(self):
        core_v1 = MagicMock()
        core_v1.read_node.return_value = _make_node("node-1", conditions=[
            SimpleNamespace(
                type="Ready", status="True",
                reason="KubeletReady", message="kubelet is posting ready status",
                last_transition_time=None,
            ),
        ])
        result = ToolResult()

        _gather_node_conditions(core_v1, {"node-1"}, result)

        assert len(result.findings) == 1
        assert result.findings[0].severity == "info"
        assert "node/node-1" in result.scanned_items

    def test_not_ready_node(self):
        core_v1 = MagicMock()
        core_v1.read_node.return_value = _make_node("node-1", conditions=[
            SimpleNamespace(
                type="Ready", status="False",
                reason="KubeletNotReady", message="container runtime not ready",
                last_transition_time=None,
            ),
        ])
        result = ToolResult()

        _gather_node_conditions(core_v1, {"node-1"}, result)

        assert result.findings[0].severity == "critical"

    def test_memory_pressure(self):
        core_v1 = MagicMock()
        core_v1.read_node.return_value = _make_node("node-1", conditions=[
            SimpleNamespace(
                type="MemoryPressure", status="True",
                reason="KubeletHasInsufficientMemory", message="low memory",
                last_transition_time=None,
            ),
        ])
        result = ToolResult()

        _gather_node_conditions(core_v1, {"node-1"}, result)

        assert result.findings[0].severity == "warning"

    def test_node_api_error(self):
        core_v1 = MagicMock()
        core_v1.read_node.side_effect = _api_exception(404, "Not Found")
        result = ToolResult()

        _gather_node_conditions(core_v1, {"node-1"}, result)

        assert len(result.errors) == 1
        assert "Not Found" in result.errors[0]

    def test_no_conditions(self):
        core_v1 = MagicMock()
        core_v1.read_node.return_value = _make_node("node-1", conditions=None)
        # Override to set conditions to None
        core_v1.read_node.return_value.status.conditions = None
        result = ToolResult()

        _gather_node_conditions(core_v1, {"node-1"}, result)

        assert len(result.findings) == 1
        assert "no conditions" in result.findings[0].content

    def test_empty_node_set(self):
        core_v1 = MagicMock()
        result = ToolResult()

        _gather_node_conditions(core_v1, set(), result)

        assert len(result.findings) == 0
        core_v1.read_node.assert_not_called()


# ---------------------------------------------------------------------------
# _collect_pods
# ---------------------------------------------------------------------------


class TestCollectPods:
    def test_deployment_selector(self):
        apps_v1 = MagicMock()
        core_v1 = MagicMock()

        apps_v1.read_namespaced_deployment.return_value = _make_deployment(
            {"app": "web"}
        )
        core_v1.list_namespaced_pod.return_value = SimpleNamespace(
            items=[_make_pod(name="web-abc")]
        )
        result = ToolResult()

        pods = _collect_pods(apps_v1, core_v1, "default", ["web-deploy"], result)

        assert len(pods) == 1
        assert pods[0].metadata.name == "web-abc"

    def test_label_selector(self):
        apps_v1 = MagicMock()
        core_v1 = MagicMock()

        core_v1.list_namespaced_pod.return_value = SimpleNamespace(
            items=[_make_pod(name="api-xyz")]
        )
        result = ToolResult()

        pods = _collect_pods(
            apps_v1, core_v1, "default", ["app=api"], result,
        )

        assert len(pods) == 1
        core_v1.list_namespaced_pod.assert_called_once_with(
            namespace="default", label_selector="app=api",
        )

    def test_deduplicates_pods(self):
        apps_v1 = MagicMock()
        core_v1 = MagicMock()

        pod = _make_pod(name="shared-pod")
        core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
        apps_v1.read_namespaced_deployment.return_value = _make_deployment(
            {"app": "shared"}
        )
        result = ToolResult()

        # Both selectors resolve to the same pod
        pods = _collect_pods(
            apps_v1, core_v1, "default",
            ["app=shared", "shared-deploy"],
            result,
        )

        assert len(pods) == 1

    def test_api_error_recorded(self):
        apps_v1 = MagicMock()
        core_v1 = MagicMock()

        core_v1.list_namespaced_pod.side_effect = _api_exception(403, "Forbidden")
        result = ToolResult()

        pods = _collect_pods(
            apps_v1, core_v1, "default", ["app=secret"], result,
        )

        assert len(pods) == 0
        assert len(result.errors) == 1


# ---------------------------------------------------------------------------
# _build_agent_result
# ---------------------------------------------------------------------------


class TestBuildAgentResult:
    def test_success_with_findings(self):
        eks = ToolResult(
            findings=[
                Finding(
                    source="pod/web-abc",
                    timestamp="2025-01-15T14:32:00Z",
                    content="Pod web-abc: phase=Running",
                    severity="info",
                ),
            ],
            scanned_items=["pod/web-abc", "node/node-1"],
        )

        result = build_agent_result("eks", eks)

        assert result.status == "success"
        assert result.agent_name == "eks"
        assert "2 item" in result.summary

    def test_error_no_pods(self):
        eks = ToolResult(
            errors=["EKS cluster API server unreachable: Connection refused"],
        )

        result = build_agent_result("eks", eks)

        assert result.status == "error"
        assert result.error_message is not None
        assert "unreachable" in result.error_message

    def test_partial_errors(self):
        eks = ToolResult(
            findings=[
                Finding(
                    source="pod/web-abc",
                    timestamp="",
                    content="Pod web-abc: phase=Running",
                    severity="info",
                ),
            ],
            scanned_items=["pod/web-abc"],
            errors=["Failed to fetch logs for pod web-abc: Not Found"],
        )

        result = build_agent_result("eks", eks)

        assert result.status == "success"
        assert result.error_message is not None


# ---------------------------------------------------------------------------
# _execute_gather — core gathering logic
# ---------------------------------------------------------------------------


class TestExecuteGather:
    """Tests for _execute_gather — clients are passed in directly."""

    def test_successful_gather(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()

        pod = _make_pod(name="web-abc", phase="Running", node_name="node-1")

        apps_v1.read_namespaced_deployment.return_value = _make_deployment({"app": "web"})
        core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
        core_v1.list_namespaced_event.return_value = SimpleNamespace(items=[])
        core_v1.read_namespaced_pod_log.return_value = "OK"
        core_v1.read_node.return_value = _make_node("node-1")

        result = _execute_gather(core_v1, apps_v1, "default", ["web-deploy"])

        assert "pod/web-abc" in result.scanned_items
        assert "node/node-1" in result.scanned_items
        assert len(result.findings) > 0

    def test_empty_selectors(self):
        result = _execute_gather(MagicMock(), MagicMock(), "default", [])

        assert len(result.errors) == 1
        assert "No resource selectors" in result.errors[0]

    def test_no_pods_found(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment.side_effect = _api_exception(404, "Not Found")

        result = _execute_gather(core_v1, apps_v1, "default", ["nonexistent-deploy"])

        assert len(result.errors) >= 1
        assert "No pods found" in result.errors[-1]

        assert len(result.errors) >= 1
        assert "No pods found" in result.errors[-1]


# ---------------------------------------------------------------------------
# Agent card loading
# ---------------------------------------------------------------------------


class TestAgentCard:
    def test_load_agent_card(self):
        import json
        _AGENT_CARD_PATH = pathlib.Path(__file__).resolve().parent.parent / "agent_card.json"

        with open(_AGENT_CARD_PATH) as fh:
            card = json.load(fh)

        assert card["name"] == "EKS Agent"
        assert card["url"] == "http://localhost:9000"
        # The static `skills` array was removed in PR 3 — A2A skill metadata
        # is now generated at runtime from resolved SKILL.md bundles.

    def test_build_skills(self):
        """Skills now resolve from SKILL.md, not agent_card.json."""
        from shared.skill_loader import resolve
        skill = resolve("gather_eks_state", "eks")
        assert skill.name == "gather_eks_state"
        assert skill.tool_symbol == "agents.eks.tools:gather_eks_state"


class TestEksIamAuth:
    """EKS bearer-token generation and kubeconfig loading via IAM auth."""

    def test_bearer_token_format(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        token = _get_eks_bearer_token("eks-uat", "us-east-1")

        assert token.startswith("k8s-aws-v1.")
        # No padding chars in the urlsafe base64 portion
        assert "=" not in token

    def test_load_kube_config_from_eks_configures_client(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")

        # Real "ca-data" base64-encoded — kubernetes Config writes it to a file
        import base64 as _b64
        sample_pem = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        ca_b64 = _b64.b64encode(sample_pem).decode("utf-8")

        fake_eks = MagicMock()
        fake_eks.describe_cluster.return_value = {
            "cluster": {
                "endpoint": "https://example.eks.amazonaws.com",
                "certificateAuthority": {"data": ca_b64},
            }
        }
        with pytest.MonkeyPatch.context() as mp:
            import boto3 as _boto3

            mp.setattr(_boto3, "client", lambda service, **kw: fake_eks if service == "eks" else _boto3.session.Session().client(service, **kw))

            _load_kube_config_from_eks("eks-uat", "us-east-1")

        from kubernetes import client as k8s_client
        cfg = k8s_client.Configuration.get_default_copy()
        assert cfg.host == "https://example.eks.amazonaws.com"
        assert cfg.api_key.get("authorization", "").startswith("Bearer k8s-aws-v1.")
        # CA file was written and path stored
        assert cfg.ssl_ca_cert is not None
        assert pathlib.Path(cfg.ssl_ca_cert).read_bytes() == sample_pem
        fake_eks.describe_cluster.assert_called_once_with(name="eks-uat")


# ---------------------------------------------------------------------------
# capture_snapshot — /sre-snapshot path
# ---------------------------------------------------------------------------


REQUESTED_AT = "2026-05-28T19:00:00+00:00"
NOW = datetime(2026, 5, 28, 19, 0, 0, tzinfo=timezone.utc)


# ---- helpers --------------------------------------------------------------


def _node(name: str, ready: str = "True", *, unschedulable: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(unschedulable=unschedulable),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status=ready)],
        ),
    )


def _snap_pod(
    *,
    namespace: str = "default",
    name: str = "pod-1",
    phase: str = "Running",
    restart_counts: list[int] | None = None,
) -> SimpleNamespace:
    statuses = [
        SimpleNamespace(restart_count=r) for r in (restart_counts or [0])
    ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        status=SimpleNamespace(phase=phase, container_statuses=statuses),
    )


def _event(
    *,
    reason: str,
    message: str,
    kind: str = "Pod",
    name: str = "obj",
    when: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        type="Warning",
        reason=reason,
        message=message,
        last_timestamp=when or NOW,
        event_time=None,
        involved_object=SimpleNamespace(kind=kind, name=name),
    )


def _make_clients(
    *,
    nodes: list | None = None,
    pods: list | None = None,
    events: list | None = None,
    git_version: str = "v1.30.0-eks-abc123",
) -> tuple[MagicMock, MagicMock]:
    core_v1 = MagicMock()
    core_v1.list_node.return_value = SimpleNamespace(items=nodes or [])
    core_v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=pods or [])
    core_v1.list_namespaced_event.return_value = SimpleNamespace(items=events or [])
    version_api = MagicMock()
    version_api.get_code.return_value = SimpleNamespace(git_version=git_version)
    return core_v1, version_api


def _section(report: SnapshotReport, label: str):
    for s in report.sections:
        if s.label == label:
            return s
    raise AssertionError(f"section {label!r} missing; got {[s.label for s in report.sections]}")


# ---- happy path -----------------------------------------------------------


class TestCaptureSnapshotHappyPath:
    def test_no_anomaly_for_clean_cluster(self):
        core_v1, version_api = _make_clients(
            nodes=[_node("node-1"), _node("node-2")],
            pods=[
                _snap_pod(name="api-1", phase="Running"),
                _snap_pod(name="api-2", phase="Running"),
                _snap_pod(name="worker-1", phase="Running"),
            ],
            events=[],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT,
            cluster_name="eks-prod", now=NOW,
        )
        assert report.anomaly is False
        assert report.anomaly_summary is None

    def test_cluster_section_includes_name_and_version(self):
        core_v1, version_api = _make_clients(git_version="v1.31.4-eks-xyz")
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT,
            cluster_name="eks-prod", now=NOW,
        )
        cluster = _section(report, "Cluster")
        joined = "\n".join(cluster.lines)
        assert "name: eks-prod" in joined
        assert "v1.31.4-eks-xyz" in joined

    def test_cluster_name_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("EKS_CLUSTER_NAME", "from-env")
        core_v1, version_api = _make_clients()
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(_section(report, "Cluster").lines)
        assert "from-env" in joined

    def test_cluster_name_unknown_when_no_env(self, monkeypatch):
        monkeypatch.delenv("EKS_CLUSTER_NAME", raising=False)
        core_v1, version_api = _make_clients()
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(_section(report, "Cluster").lines)
        assert "(unknown)" in joined

    def test_node_section_counts_by_status(self):
        core_v1, version_api = _make_clients(
            nodes=[
                _node("a"),
                _node("b"),
                _node("c", ready="False"),
                _node("d", unschedulable=True),
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(_section(report, "Nodes").lines)
        assert "2 Ready" in joined
        assert "1 NotReady" in joined
        assert "1 SchedulingDisabled" in joined

    def test_pod_section_counts_by_phase(self):
        core_v1, version_api = _make_clients(
            pods=[
                _snap_pod(name="r1", phase="Running"),
                _snap_pod(name="r2", phase="Running"),
                _snap_pod(name="p1", phase="Pending"),
                _snap_pod(name="s1", phase="Succeeded"),
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(_section(report, "Pods").lines)
        assert "2 Running" in joined
        assert "1 Pending" in joined
        assert "1 Succeeded" in joined


# ---- anomaly paths --------------------------------------------------------


class TestCaptureSnapshotAnomalyPaths:
    def test_not_ready_node_flags_anomaly(self):
        core_v1, version_api = _make_clients(
            nodes=[_node("a"), _node("b", ready="False")],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        assert report.anomaly is True
        assert "NotReady" in (report.anomaly_summary or "")

    def test_failed_pod_flags_anomaly(self):
        core_v1, version_api = _make_clients(
            pods=[
                _snap_pod(name="api", phase="Running"),
                _snap_pod(name="dead", phase="Failed"),
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        assert report.anomaly is True
        assert "Failed" in (report.anomaly_summary or "")

    def test_high_restart_count_flags_anomaly(self):
        core_v1, version_api = _make_clients(
            pods=[
                _snap_pod(name="crashloop", phase="Pending", restart_counts=[5]),
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        assert report.anomaly is True
        assert "restarts" in (report.anomaly_summary or "")
        assert "≥ 3" in (report.anomaly_summary or "")

    def test_two_restarts_does_not_flag_anomaly(self):
        core_v1, version_api = _make_clients(
            pods=[_snap_pod(name="p", phase="Pending", restart_counts=[2])],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        # Pending is fine on its own; restart count only flips at >= 3
        assert report.anomaly is False

    def test_recent_kube_system_warning_event_flags_anomaly(self):
        core_v1, version_api = _make_clients(
            events=[
                _event(
                    reason="FailedScheduling",
                    message="0/3 nodes available",
                    kind="Pod", name="bad",
                    when=NOW - timedelta(minutes=2),
                )
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        assert report.anomaly is True
        assert "kube-system warning" in (report.anomaly_summary or "")

    def test_old_event_outside_5min_window_does_not_flag(self):
        core_v1, version_api = _make_clients(
            events=[
                _event(
                    reason="OldStuff", message="x",
                    when=NOW - timedelta(minutes=20),
                )
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        assert report.anomaly is False

    def test_multiple_anomalies_concatenated_in_summary(self):
        core_v1, version_api = _make_clients(
            nodes=[_node("a", ready="False")],
            pods=[
                _snap_pod(name="dead", phase="Failed"),
                _snap_pod(name="restart", phase="Pending", restart_counts=[10]),
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        summary = report.anomaly_summary or ""
        assert "NotReady" in summary
        assert "Failed" in summary
        assert "restarts" in summary


# ---- non-Running pods list ------------------------------------------------


class TestCaptureSnapshotNonRunningList:
    def test_lists_non_running_pods_with_namespace_phase_restarts(self):
        core_v1, version_api = _make_clients(
            pods=[
                _snap_pod(namespace="prod", name="api-1", phase="Running"),
                _snap_pod(namespace="prod", name="api-2", phase="Pending", restart_counts=[1]),
                _snap_pod(namespace="data", name="worker", phase="Failed", restart_counts=[2]),
            ],
        )
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        nr = _section(report, "Non-Running pods (top 10)")
        # Failed sorts before Pending; "prod/api-1" (Running) excluded
        joined = "\n".join(nr.lines)
        assert "data/worker · phase=Failed · restarts=2" in joined
        assert "prod/api-2 · phase=Pending · restarts=1" in joined
        assert "prod/api-1" not in joined

    def test_truncates_to_10_with_tail_count(self):
        pods = [
            _snap_pod(namespace="ns", name=f"p-{i:02d}", phase="Pending")
            for i in range(15)
        ]
        core_v1, version_api = _make_clients(pods=pods)
        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        nr_lines = _section(report, "Non-Running pods (top 10)").lines
        # 10 entries + 1 tail line
        assert len(nr_lines) == 11
        assert "5 more not shown" in nr_lines[-1]


# ---- error paths ----------------------------------------------------------


class TestCaptureSnapshotErrorPaths:
    def test_list_node_api_failure_renders_error_in_section(self):
        core_v1, version_api = _make_clients()
        from kubernetes.client.exceptions import ApiException
        core_v1.list_node.side_effect = ApiException(reason="forbidden")

        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(_section(report, "Nodes").lines)
        assert "❌" in joined
        assert "forbidden" in joined
        # API failure on nodes does not flip anomaly on its own
        assert report.anomaly is False

    def test_list_pods_api_failure_returns_partial_report_no_raise(self):
        core_v1, version_api = _make_clients()
        from kubernetes.client.exceptions import ApiException
        core_v1.list_pod_for_all_namespaces.side_effect = ApiException(
            reason="cluster unreachable"
        )

        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(_section(report, "Pods").lines)
        assert "cluster unreachable" in joined
        # Cluster + Nodes + Pods + Non-Running list still present
        labels = [s.label for s in report.sections]
        assert "Cluster" in labels
        assert "Pods" in labels
        assert "Non-Running pods (top 10)" in labels

    def test_version_api_failure_does_not_block_other_sections(self):
        core_v1, version_api = _make_clients()
        version_api.get_code.side_effect = RuntimeError("connection refused")

        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(_section(report, "Cluster").lines)
        assert "connection refused" in joined
        # All five sections still rendered
        assert len(report.sections) == 5

    def test_event_api_failure_renders_in_section(self):
        core_v1, version_api = _make_clients()
        from kubernetes.client.exceptions import ApiException
        core_v1.list_namespaced_event.side_effect = ApiException(reason="rbac denied")

        report = _execute_capture_snapshot(
            core_v1, version_api, requested_at=REQUESTED_AT, now=NOW,
        )
        joined = "\n".join(
            _section(report, "kube-system warning events (last 5 min)").lines
        )
        assert "rbac denied" in joined
        assert report.anomaly is False
