"""Run the complete permission recovery loop with Flawless inside Kubernetes.

Unlike the kubeconfig-driven E2E, this scenario forces every read and mutation
through the in-cluster MCP server and its real ServiceAccount. It first proves
that the application namespace guard and Kubernetes RBAC can independently
block a repair, then applies the minimum test authorization and requires the
product to patch the Deployment, roll out a new Pod, and verify recovery.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid

import httpx


KUBECONFIG = os.environ["E2E_KUBECONFIG"]
KUBECTL = os.getenv("E2E_KUBECTL", "kubectl")
PLATFORM_IMAGE = os.getenv("E2E_PLATFORM_IMAGE", "flawless-local:latest")
WORKLOAD_IMAGE = os.getenv("E2E_WORKLOAD_IMAGE", PLATFORM_IMAGE)
LLM_API_BASE = os.environ["E2E_LLM_API_BASE"]
LLM_API_KEY = os.environ["E2E_LLM_API_KEY"]
LLM_MODEL = os.getenv("E2E_LLM_MODEL", "deepseek-v4-pro")
BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:18081").rstrip("/")
PLATFORM_NAMESPACE = "k8s-agent"
TARGET_NAMESPACE = os.getenv(
    "E2E_TARGET_NAMESPACE",
    f"flawless-rbac-{uuid.uuid4().hex[:8]}",
)
WORKLOAD = "security-context-file-writer"
TIMEOUT_SECONDS = int(os.getenv("E2E_TIMEOUT_SECONDS", "360"))


def kubectl(*args: str, stdin: dict | None = None, check: bool = True) -> str:
    result = subprocess.run(
        [KUBECTL, "--kubeconfig", KUBECONFIG, *args],
        input=json.dumps(stdin, ensure_ascii=False) if stdin is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(
            f"kubectl {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def apply(item: dict) -> None:
    kubectl("apply", "-f", "-", stdin=item)


def base_metadata(name: str, namespace: str | None = None) -> dict:
    metadata = {"name": name}
    if namespace:
        metadata["namespace"] = namespace
    return metadata


def platform_config(allowed_namespaces: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": base_metadata("k8s-agent-config", PLATFORM_NAMESPACE),
        "data": {
            "APP_BUILD_VERSION": "incluster-e2e",
            "ADAPTER_URL": (
                "http://flawless.k8s-agent.svc.cluster.local:8200"
            ),
            "MCP_SERVER_URL": (
                "http://flawless.k8s-agent.svc.cluster.local:8105/mcp"
            ),
            "OBSERVABILITY_URL": (
                "http://flawless.k8s-agent.svc.cluster.local:8100"
            ),
            "OBSERVABILITY_AGENT_URL": (
                "http://flawless.k8s-agent.svc.cluster.local:8100"
            ),
            "LLM_API_BASE": LLM_API_BASE,
            "LLM_MODEL": LLM_MODEL,
            "LLM_PROVIDER": "deepseek",
            "LLM_PROFILE_ID": "e2e-deepseek",
            "LLM_AUTH_TYPE": "api_key",
            "LLM_MAX_TOKENS": "8192",
            "LLM_DIAGNOSIS_MAX_TOKENS": "4096",
            "SRE_DIAGNOSIS_TIMEOUT_SECONDS": "120",
            "LLM_STRUCTURED_THINKING_PROTOCOL": "auto",
            "LLM_STRUCTURED_THINKING_MODE": "disabled",
            "LLM_VERIFY_SSL": "true",
            "OUTBOUND_VERIFY_SSL": "true",
            "OPS_MUTATION_ENABLED": "true",
            "AUTONOMOUS_OPS_ENABLED": "true",
            "ALLOWED_NAMESPACES": allowed_namespaces,
            "CMDB_ALLOWED_NAMESPACES": "all",
            "OPS_EVIDENCE_TIMEOUT_SECONDS": "35",
            "OPS_ROOT_CAUSE_TIMEOUT_SECONDS": "90",
            "OPS_CHANGE_TIMEOUT_SECONDS": "60",
            "OPS_VERIFY_TIMEOUT_SECONDS": "45",
            "OPS_VERIFY_INTERVAL_SECONDS": "2",
            "OPS_HEARTBEAT_SECONDS": "2",
            "OPS_CONTINUATION_INTERVAL_SECONDS": "10",
            "AUTO_OPS_MAX_ATTEMPTS": "4",
            "NODE_EXEC_IMAGE": "",
            "CLUSTER_REGISTRY_PATH": "/var/lib/flawless/clusters.db",
            "OPS_JOB_STORE_PATH": "/var/lib/flawless/ops-jobs.json",
            "OPS_SKILL_ROOT": "/var/lib/flawless/ops-skills",
            "RELIABILITY_STORE_PATH": "/var/lib/flawless/reliability.json",
            "KNOWLEDGE_STORE_PATH": "/var/lib/flawless/knowledge.json",
            "MODEL_PROFILES_STORE": "/var/lib/flawless/models.json",
            "CONSOLE_AUTH_REQUIRED": "false",
            "DISABLE_OPENAPI_DOCS": "true",
            "LOCAL_CLUSTER_NAME": "local-cluster",
            "PLATFORM_NAMESPACE": PLATFORM_NAMESPACE,
            "PLATFORM_WORKLOAD_NAME": "flawless",
        },
    }


def reader_role() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": base_metadata("flawless-e2e-reader"),
        "rules": [
            {
                "apiGroups": [""],
                "resources": [
                    "namespaces", "nodes", "pods", "pods/status", "pods/log",
                    "events", "services", "endpoints", "persistentvolumeclaims",
                    "persistentvolumes", "configmaps",
                ],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": ["apps"],
                "resources": [
                    "deployments", "statefulsets", "daemonsets", "replicasets",
                ],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": ["storage.k8s.io"],
                "resources": ["storageclasses"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": ["authorization.k8s.io"],
                "resources": ["selfsubjectaccessreviews"],
                "verbs": ["create"],
            },
        ],
    }


def remediator_role() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": base_metadata("flawless-e2e-remediator"),
        "rules": [
            {
                "apiGroups": ["apps"],
                "resources": ["deployments", "statefulsets", "daemonsets"],
                "verbs": ["get", "patch"],
            },
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["get", "delete"],
            },
        ],
    }


def binding(name: str, role: str) -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": base_metadata(name),
        "subjects": [{
            "kind": "ServiceAccount",
            "name": "k8s-agent-sa",
            "namespace": PLATFORM_NAMESPACE,
        }],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": role,
        },
    }


def container(name: str, module: str, port: int) -> dict:
    return {
        "name": name,
        "image": PLATFORM_IMAGE,
        "imagePullPolicy": "Never",
        "command": [
            "python", "-m", "uvicorn", module,
            "--host", "0.0.0.0", "--port", str(port),
        ],
        "envFrom": [
            {"configMapRef": {"name": "k8s-agent-config"}},
            {"secretRef": {"name": "k8s-agent-model"}},
        ],
        "ports": [{"name": name[:15], "containerPort": port}],
        "volumeMounts": [
            {"name": "runtime", "mountPath": "/var/lib/flawless"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
    }


def platform_deployment() -> dict:
    mcp = container(
        "mcp-server",
        "mcp_servers.mcp_http_server:app",
        8105,
    )
    mcp["readinessProbe"] = {
        "httpGet": {"path": "/health", "port": 8105},
        "periodSeconds": 1,
        "failureThreshold": 60,
    }
    observability = container(
        "observability-agent",
        "agents.observability_agent:app",
        8100,
    )
    observability["readinessProbe"] = {
        "httpGet": {"path": "/health", "port": 8100},
        "periodSeconds": 1,
        "failureThreshold": 60,
    }
    adapter = container(
        "openwebui-adapter",
        "openwebui.openwebui_adapter:app",
        8200,
    )
    adapter["readinessProbe"] = {
        "httpGet": {"path": "/health", "port": 8200},
        "periodSeconds": 1,
        "failureThreshold": 120,
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": base_metadata("flawless", PLATFORM_NAMESPACE),
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "flawless"}},
            "template": {
                "metadata": {"labels": {"app": "flawless"}},
                "spec": {
                    "serviceAccountName": "k8s-agent-sa",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                    },
                    "containers": [
                        mcp,
                        observability,
                        adapter,
                    ],
                    "volumes": [
                        {"name": "runtime", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def api_deployment() -> dict:
    api = container("api", "backend.app.main:app", 8080)
    api["readinessProbe"] = {
        "httpGet": {"path": "/api/build", "port": 8080},
        "periodSeconds": 1,
        "failureThreshold": 120,
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": base_metadata("k8s-agent-api", PLATFORM_NAMESPACE),
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": "k8s-agent-api"}},
            "template": {
                "metadata": {"labels": {"app": "k8s-agent-api"}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                    },
                    "containers": [api],
                    "volumes": [
                        {"name": "runtime", "emptyDir": {}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def service(
    name: str,
    selector: dict,
    ports: list[tuple[str, int]],
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": base_metadata(name, PLATFORM_NAMESPACE),
        "spec": {
            "selector": selector,
            "ports": [
                {"name": port_name, "port": port, "targetPort": port}
                for port_name, port in ports
            ],
        },
    }


def broken_deployment() -> dict:
    program = """
import sqlite3
import time

path = "/root/flawless-incluster-probe.txt"
try:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("ready")
except Exception as exc:
    print(f"ERROR cannot create {path}: {type(exc).__name__}: {exc}", flush=True)
    try:
        sqlite3.connect("/root/flawless-incluster-runtime.db")
    except Exception as database_exc:
        print(
            "ERROR unable to open database file "
            f"/root/flawless-incluster-runtime.db: "
            f"{type(database_exc).__name__}: {database_exc}",
            flush=True,
        )
    raise

print("FILE_CREATE_OK database-ready", flush=True)
time.sleep(3600)
""".strip()
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": base_metadata(WORKLOAD, TARGET_NAMESPACE),
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": WORKLOAD}},
            "template": {
                "metadata": {"labels": {"app": WORKLOAD}},
                "spec": {
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                    },
                    "containers": [{
                        "name": "writer",
                        "image": WORKLOAD_IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["python", "-c", program],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "allowPrivilegeEscalation": False,
                        },
                    }],
                },
            },
        },
    }


def wait_rollout(name: str, namespace: str = PLATFORM_NAMESPACE) -> None:
    kubectl(
        "rollout", "status", f"deployment/{name}",
        "-n", namespace, "--timeout=180s",
    )


def wait_fault() -> tuple[str, str]:
    deadline = time.monotonic() + 90
    latest = ""
    while time.monotonic() < deadline:
        payload = json.loads(kubectl(
            "get", "pods", "-n", TARGET_NAMESPACE,
            "-l", f"app={WORKLOAD}", "-o", "json",
        ))
        for pod in payload.get("items") or []:
            pod_name = (pod.get("metadata") or {}).get("name") or ""
            logs = kubectl(
                "logs", "-n", TARGET_NAMESPACE, pod_name,
                "-c", "writer", "--tail=100", check=False,
            )
            latest = logs
            if "PermissionError" in logs and "unable to open database file" in logs:
                return pod_name, logs
        time.sleep(1)
    raise AssertionError(f"permission fault was not observed: {latest}")


def wait_http(client: httpx.Client) -> dict:
    deadline = time.monotonic() + 120
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/build")
            if response.status_code == 200:
                return response.json()
            last_error = response.text
        except Exception as exc:
            last_error = str(exc)
        time.sleep(1)
    raise AssertionError(f"Flawless API did not become ready: {last_error}")


def start_port_forward() -> subprocess.Popen:
    last_error = ""
    for _attempt in range(5):
        process = subprocess.Popen(
            [
                KUBECTL, "--kubeconfig", KUBECONFIG,
                "-n", PLATFORM_NAMESPACE, "port-forward",
                "service/k8s-agent-api", "18081:8080",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                last_error = (
                    process.stderr.read() if process.stderr else ""
                ).strip()
                break
            try:
                with socket.create_connection(("127.0.0.1", 18081), timeout=1):
                    return process
            except OSError as exc:
                last_error = str(exc)
                time.sleep(0.2)
        if process.poll() is None:
            stop_process(process)
        time.sleep(0.5)
    raise AssertionError(f"kubectl port-forward failed: {last_error}")


def stop_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def install_platform() -> None:
    # The disposable cluster may be reused while iterating on this E2E. Always
    # restore the intended initial condition: read access exists, mutation
    # access does not.
    kubectl(
        "delete",
        "clusterrolebinding",
        "flawless-e2e-remediator-binding",
        "--ignore-not-found=true",
    )
    apply({
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": base_metadata(PLATFORM_NAMESPACE),
    })
    apply({
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": base_metadata(TARGET_NAMESPACE),
    })
    apply({
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": base_metadata("k8s-agent-sa", PLATFORM_NAMESPACE),
    })
    apply(platform_config(PLATFORM_NAMESPACE))
    apply({
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": base_metadata("k8s-agent-model", PLATFORM_NAMESPACE),
        "type": "Opaque",
        "stringData": {"LLM_API_KEY": LLM_API_KEY},
    })
    apply(reader_role())
    apply(remediator_role())
    apply(binding("flawless-e2e-reader-binding", "flawless-e2e-reader"))
    apply(platform_deployment())
    apply(service(
        "flawless",
        {"app": "flawless"},
        [("observability", 8100), ("mcp", 8105), ("adapter", 8200)],
    ))
    apply(api_deployment())
    apply(service("k8s-agent-api", {"app": "k8s-agent-api"}, [("http", 8080)]))
    apply(broken_deployment())
    kubectl(
        "rollout", "restart", "deployment/flawless",
        "deployment/k8s-agent-api", "-n", PLATFORM_NAMESPACE,
    )
    wait_rollout("flawless")
    wait_rollout("k8s-agent-api")


def access_check(client: httpx.Client) -> dict:
    response = client.post("/api/mcp/call", json={
        "tool": "check_access",
        "arguments": {
            "namespace": TARGET_NAMESPACE,
            "verb": "patch",
            "resource": "deployments",
            "group": "apps",
            "name": WORKLOAD,
        },
    })
    response.raise_for_status()
    return response.json()


def fix_execution_permissions() -> None:
    apply(platform_config("all"))
    apply(binding(
        "flawless-e2e-remediator-binding",
        "flawless-e2e-remediator",
    ))
    kubectl(
        "rollout", "restart", "deployment/flawless",
        "deployment/k8s-agent-api", "-n", PLATFORM_NAMESPACE,
    )
    wait_rollout("flawless")
    wait_rollout("k8s-agent-api")


def run_aiops(client: httpx.Client, failed_pod: str) -> dict:
    chat = client.post(
        "/api/chat",
        json={
            "message": (
                f"持续修复 {TARGET_NAMESPACE} namespace 中 "
                f"Deployment/{WORKLOAD} 的文件创建和数据库打开错误。"
                "优先读取 ERROR/WARNING、Pod 状态和 Deployment YAML；"
                "每次修改必须等待人工审批，修改后验证新 Pod，未恢复继续。"
            ),
            "original_message": "securityContext 文件权限故障",
            "cluster": "local-cluster",
            "cluster_id": "local",
            "namespace": TARGET_NAMESPACE,
            "deployment": WORKLOAD,
            "workload_type": "Deployment",
            "pod": failed_pod,
            "severity": "P1",
            "auto_healing_enabled": False,
        },
        timeout=150,
    )
    chat.raise_for_status()
    diagnosis = ((chat.json().get("raw") or {}).get("diagnosis") or {})
    assert (diagnosis.get("diagnosis_metadata") or {}).get("source") == "llm", (
        diagnosis.get("llm_error") or diagnosis
    )
    matched = [
        str(item.get("id") or "")
        for item in (
            (diagnosis.get("remediation_plan") or {}).get("operator_skills")
            or diagnosis.get("operator_skills")
            or []
        )
    ]
    assert "skill-volume-permission-recovery" in matched, matched

    plan = {
        "id": f"incluster-aiops-{uuid.uuid4().hex[:8]}",
        "title": "深度分析：in-cluster securityContext 文件创建失败",
        "cluster": "local-cluster",
        "cluster_id": "local",
        "source": "mcp",
        "source_surface": "sre_chat",
        "namespace": TARGET_NAMESPACE,
        "target": f"Deployment/{WORKLOAD}",
        "pod_name": failed_pod,
        "summary": diagnosis.get("root_cause") or "容器文件创建失败",
        "root_cause": diagnosis.get("root_cause") or "",
        "steps": [{
            "id": "current_logs",
            "probe": "current_logs",
            "title": "读取 ERROR/WARNING 和文件创建错误",
            "description": (
                "将日志与 Deployment securityContext 交叉验证。"
            ),
        }],
        "changes": [],
        "requires_confirmation": False,
        "requires_high_risk_confirmation": False,
        "success_criteria": [
            "new_pod_ready",
            "file_create_succeeds",
            "permission_error_absent",
            "restart_count_stable",
        ],
    }
    created = client.post(
        "/api/ops/jobs",
        json={"plan": plan, "confirm": True, "autonomous": False},
    )
    created.raise_for_status()
    job_id = created.json()["id"]
    approvals: list[dict] = []
    seen: set[str] = set()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    job = created.json()
    while time.monotonic() < deadline:
        response = client.get(f"/api/ops/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        pending = job.get("pending_approval") or {}
        approval_id = str(pending.get("approval_id") or "")
        if (
            job.get("status") == "awaiting_approval"
            and approval_id
            and approval_id not in seen
        ):
            seen.add(approval_id)
            approvals.append(pending)
            approved = client.post(
                f"/api/ops/jobs/{job_id}/approve-step",
                json={
                    "change_index": pending["change_index"],
                    "approval_id": approval_id,
                    "change_fingerprint": pending["change_fingerprint"],
                    "confirm": True,
                    "comment": (
                        "in-cluster RBAC E2E：已核对 securityContext 差异和回滚"
                    ),
                },
            )
            approved.raise_for_status()
        if job.get("status") in {
            "completed", "failed", "cancelled", "unresolved", "blocked",
        }:
            break
        time.sleep(0.5)
    assert job.get("status") == "completed", job
    assert ((job.get("result") or {}).get("verification") or {}).get(
        "recovered"
    ) is True, job
    assert len(approvals) >= 2, approvals
    stages = [str(item.get("stage") or "") for item in job.get("events") or []]
    for required in (
        "collecting_evidence_done",
        "diagnosing",
        "root_cause_diagnosing",
        "root_cause_diagnosed",
        "execution_preflight",
        "execution_preflight_done",
        "awaiting_change_approval",
        "change_start",
        "change_done",
        "verifying",
        "verification_done",
        "recovered",
    ):
        assert required in stages, {"missing": required, "stages": stages}
    return {
        "job": job,
        "approvals": approvals,
        "stages": stages,
        "diagnosis_source": (
            diagnosis.get("diagnosis_metadata") or {}
        ).get("source"),
    }


def verify_recovery() -> tuple[str, dict]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        payload = json.loads(kubectl(
            "get", "pods", "-n", TARGET_NAMESPACE,
            "-l", f"app={WORKLOAD}", "-o", "json",
        ))
        for pod in payload.get("items") or []:
            statuses = (pod.get("status") or {}).get("containerStatuses") or []
            if statuses and all(item.get("ready") is True for item in statuses):
                pod_name = (pod.get("metadata") or {}).get("name") or ""
                logs = kubectl(
                    "logs", "-n", TARGET_NAMESPACE,
                    pod_name, "-c", "writer", "--tail=100",
                )
                if "FILE_CREATE_OK database-ready" in logs:
                    deployment = json.loads(kubectl(
                        "get", "deployment", WORKLOAD,
                        "-n", TARGET_NAMESPACE, "-o", "json",
                    ))
                    return pod_name, deployment
        time.sleep(1)
    raise AssertionError("new Pod never produced independent recovery proof")


def main() -> int:
    print("E2E stage: deploy platform and faulty workload", flush=True)
    install_platform()
    print("E2E stage: observe permission/database fault", flush=True)
    failed_pod, failed_logs = wait_fault()
    assert "PermissionError" in failed_logs
    print(f"E2E evidence: failed pod={failed_pod}", flush=True)
    port_forward = start_port_forward()
    try:
        with httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(30, connect=5),
        ) as client:
            build = wait_http(client)
            print(f"E2E build: {build}", flush=True)
            denied = access_check(client)
            assert denied.get("allowed") is False, denied
            print(f"E2E preflight denied as expected: {denied}", flush=True)
            readable = client.post("/api/mcp/call", json={
                "tool": "list_pods",
                "arguments": {"namespace": TARGET_NAMESPACE},
            })
            readable.raise_for_status()
            assert (readable.json().get("pods") or []), readable.json()
            guarded_preview = client.post("/api/mcp/call", json={
                "tool": "patch_workload",
                "arguments": {
                    "namespace": TARGET_NAMESPACE,
                    "workload_type": "Deployment",
                    "workload_name": WORKLOAD,
                    "patch": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "securityContext": {
                                        "runAsUser": 10001,
                                        "runAsGroup": 10001,
                                        "runAsNonRoot": True,
                                    },
                                },
                            },
                        },
                    },
                    "dry_run": True,
                },
            })
            guarded_preview.raise_for_status()
            blocked_payload = guarded_preview.json()
            assert "not allowed by app guard" in json.dumps(
                blocked_payload,
                ensure_ascii=False,
            ), blocked_payload
            before = json.loads(kubectl(
                "get", "deployment", WORKLOAD,
                "-n", TARGET_NAMESPACE, "-o", "json",
            ))
            before_generation = (before.get("metadata") or {}).get("generation")

    finally:
        stop_process(port_forward)

    print("E2E stage: grant target-namespace remediation permission", flush=True)
    fix_execution_permissions()
    port_forward = start_port_forward()
    try:
        with httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(30, connect=5),
        ) as client:
            wait_http(client)
            allowed = access_check(client)
            assert allowed.get("allowed") is True, allowed
            print(f"E2E preflight allowed: {allowed}", flush=True)
            after_permission_fix = json.loads(kubectl(
                "get", "deployment", WORKLOAD,
                "-n", TARGET_NAMESPACE, "-o", "json",
            ))
            assert (
                (after_permission_fix.get("metadata") or {}).get("generation")
                == before_generation
            ), "authorization repair must not mutate the business Deployment"

            print("E2E stage: submit AI operation and approve changes", flush=True)
            outcome = run_aiops(client, failed_pod)
            print("E2E stage: independently verify new Pod", flush=True)
            recovered_pod, deployment = verify_recovery()
            pod_spec = (
                (((deployment.get("spec") or {}).get("template") or {}).get(
                    "spec"
                ) or {})
            )
            assert (pod_spec.get("securityContext") or {}).get(
                "runAsUser"
            ) == 0, pod_spec
            containers = pod_spec.get("containers") or []
            writer = next(
                item for item in containers if item.get("name") == "writer"
            )
            assert (writer.get("securityContext") or {}).get(
                "runAsUser"
            ) == 0, writer
            print(json.dumps({
                "status": "passed",
                "build": build,
                "target_namespace": TARGET_NAMESPACE,
                "failed_pod": failed_pod,
                "recovered_pod": recovered_pod,
                "rbac_before": denied,
                "rbac_after": allowed,
                "job_id": outcome["job"]["id"],
                "diagnosis_source": outcome["diagnosis_source"],
                "approvals": len(outcome["approvals"]),
                "stages": outcome["stages"],
                "deployment_generation_before": before_generation,
                "deployment_generation_after": (
                    deployment.get("metadata") or {}
                ).get("generation"),
                "final_run_as_user": 0,
                "verification": (
                    outcome["job"].get("result") or {}
                ).get("verification"),
            }, ensure_ascii=False))
    finally:
        stop_process(port_forward)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
