"""Exercise a real securityContext permission incident through public Flawless APIs.

The test harness uses kubectl only to inject the broken Deployment and to make
the final independent assertion. Diagnosis, Skill routing, approvals, patches,
rollout checks and retry/escalation all travel through the running product.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx


BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:18080").rstrip("/")
KUBECONFIG = Path(os.environ["E2E_KUBECONFIG"]).resolve()
KUBECTL = os.getenv("E2E_KUBECTL", "kubectl")
CLUSTER_ID = os.getenv("E2E_CLUSTER_ID", "kube-full-aiops")
CLUSTER_NAME = os.getenv("E2E_CLUSTER_NAME", "isolated-full-aiops")
NAMESPACE = os.getenv("E2E_NAMESPACE", f"flawless-full-{uuid.uuid4().hex[:8]}")
WORKLOAD = "security-context-file-writer"
IMAGE = os.getenv("E2E_WORKLOAD_IMAGE", "flawless-local:latest")
TIMEOUT_SECONDS = int(os.getenv("E2E_TIMEOUT_SECONDS", "300"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("E2E_HTTP_TIMEOUT_SECONDS", "130"))
REQUIRE_LLM = os.getenv("E2E_REQUIRE_LLM", "false").lower() in {"1", "true", "yes", "on"}


def kubectl(*args: str, stdin: dict | None = None) -> str:
    result = subprocess.run(
        [KUBECTL, "--kubeconfig", str(KUBECONFIG), *args],
        input=json.dumps(stdin, ensure_ascii=False) if stdin is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"kubectl {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def broken_deployment() -> dict:
    program = """
import sqlite3
import time

path = "/root/flawless-e2e-probe.txt"
try:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("ready")
except Exception as exc:
    print(f"ERROR cannot create {path}: {type(exc).__name__}: {exc}", flush=True)
    try:
        sqlite3.connect("/root/flawless-e2e-runtime.db")
    except Exception as database_exc:
        print(
            f"ERROR unable to open database file /root/flawless-e2e-runtime.db: "
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
        "metadata": {"name": WORKLOAD, "namespace": NAMESPACE},
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
                        "image": IMAGE,
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


def wait_for_fault() -> tuple[str, str]:
    deadline = time.monotonic() + 90
    last_logs = ""
    while time.monotonic() < deadline:
        pods = json.loads(
            kubectl(
                "get",
                "pods",
                "-n",
                NAMESPACE,
                "-l",
                f"app={WORKLOAD}",
                "-o",
                "json",
            )
        ).get("items") or []
        if pods:
            pod_name = (pods[-1].get("metadata") or {}).get("name") or ""
            logs = subprocess.run(
                [
                    KUBECTL,
                    "--kubeconfig",
                    str(KUBECONFIG),
                    "logs",
                    "-n",
                    NAMESPACE,
                    pod_name,
                    "-c",
                    "writer",
                    "--tail=100",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            last_logs = f"{logs.stdout}\n{logs.stderr}"
            if "cannot create /root/flawless-e2e-probe.txt" in last_logs and "PermissionError" in last_logs:
                return pod_name, last_logs
        time.sleep(1)
    raise AssertionError(f"real file-creation permission failure was not observed: {last_logs}")


def wait_for_recovery() -> tuple[str, str]:
    deadline = time.monotonic() + 120
    last = ""
    while time.monotonic() < deadline:
        pods = json.loads(
            kubectl(
                "get",
                "pods",
                "-n",
                NAMESPACE,
                "-l",
                f"app={WORKLOAD}",
                "-o",
                "json",
            )
        ).get("items") or []
        for pod in pods:
            statuses = ((pod.get("status") or {}).get("containerStatuses") or [])
            if statuses and all(item.get("ready") is True for item in statuses):
                pod_name = (pod.get("metadata") or {}).get("name") or ""
                logs = kubectl("logs", "-n", NAMESPACE, pod_name, "-c", "writer", "--tail=100")
                if "FILE_CREATE_OK database-ready" in logs:
                    return pod_name, logs
                last = logs
        time.sleep(1)
    raise AssertionError(f"independent Kubernetes recovery proof was not observed: {last}")


def main() -> int:
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=5.0)
    with httpx.Client(base_url=BASE_URL, timeout=timeout) as client:
        build = client.get("/api/build").json()
        kubeconfig_text = KUBECONFIG.read_text(encoding="utf-8")
        cluster = client.post(
            "/api/clusters",
            json={
                "kubeconfig": kubeconfig_text,
                "name": CLUSTER_NAME,
                "cluster_id": CLUSTER_ID,
            },
        )
        cluster.raise_for_status()
        assert cluster.json().get("status") == "connected", cluster.json()

        kubectl("apply", "-f", "-", stdin={
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": NAMESPACE},
        })
        kubectl("apply", "-f", "-", stdin=broken_deployment())
        failed_pod, failed_logs = wait_for_fault()
        assert "unable to open database file" in failed_logs

        chat = client.post(
            "/api/chat",
            json={
                "message": (
                    f"请深度分析并持续修复 {NAMESPACE} 命名空间 Deployment/{WORKLOAD}。"
                    "先读 ERROR/WARNING、current/previous logs、Pod 状态和 Workload YAML；"
                    "每个变更都等待我审批，变更后验证新 Pod，未恢复就继续下一策略。"
                ),
                "original_message": "securityContext 文件创建权限故障",
                "cluster": CLUSTER_NAME,
                "cluster_id": CLUSTER_ID,
                "namespace": NAMESPACE,
                "deployment": WORKLOAD,
                "workload_type": "Deployment",
                "pod": failed_pod,
                "severity": "P1",
                "auto_healing_enabled": False,
            },
        )
        chat.raise_for_status()
        chat_data = chat.json()
        diagnosis = ((chat_data.get("raw") or {}).get("diagnosis") or {})
        if REQUIRE_LLM:
            assert (diagnosis.get("diagnosis_metadata") or {}).get("source") == "llm", (
                diagnosis.get("llm_error") or diagnosis.get("diagnosis_metadata")
            )
        chat_plan = diagnosis.get("remediation_plan") or {}
        matched_ids = [
            str(item.get("id") or "")
            for item in (chat_plan.get("operator_skills") or diagnosis.get("operator_skills") or [])
        ]
        assert "skill-volume-permission-recovery" in matched_ids, {
            "root_cause": diagnosis.get("root_cause"),
            "matched_skills": matched_ids,
        }

        # Reproduce the user's "deep analysis/replan" entry point: begin with
        # read-only diagnosis and let the running worker collect live evidence,
        # materialize the Skill, request approval, execute and verify.
        plan = {
            "id": f"full-aiops-{uuid.uuid4().hex[:8]}",
            "title": "深度分析：securityContext 文件创建失败",
            "cluster": CLUSTER_NAME,
            "cluster_id": CLUSTER_ID,
            "source": "kubeconfig",
            "source_surface": "sre_chat",
            "namespace": NAMESPACE,
            "target": f"Deployment/{WORKLOAD}",
            "pod_name": failed_pod,
            "summary": diagnosis.get("root_cause") or "容器无法在 /work 创建文件和数据库。",
            "root_cause": diagnosis.get("root_cause") or "",
            "steps": [{
                "id": "current_logs",
                "probe": "current_logs",
                "title": "优先检查 ERROR/WARNING 与文件创建错误",
                "description": "读取当前和 previous 日志后与 securityContext、volumeMount 交叉验证。",
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

        deadline = time.monotonic() + TIMEOUT_SECONDS
        approvals: list[dict] = []
        seen_approval_ids: set[str] = set()
        job = created.json()
        while time.monotonic() < deadline:
            job_response = client.get(f"/api/ops/jobs/{job_id}")
            job_response.raise_for_status()
            job = job_response.json()
            pending = job.get("pending_approval") or {}
            approval_id = str(pending.get("approval_id") or "")
            if job.get("status") == "awaiting_approval" and approval_id and approval_id not in seen_approval_ids:
                seen_approval_ids.add(approval_id)
                approvals.append({
                    "action": pending.get("action"),
                    "risk": pending.get("risk"),
                    "patch": pending.get("patch"),
                })
                approved = client.post(
                    f"/api/ops/jobs/{job_id}/approve-step",
                    json={
                        "change_index": pending["change_index"],
                        "approval_id": approval_id,
                        "change_fingerprint": pending["change_fingerprint"],
                        "confirm": True,
                        "comment": "真实 K3s E2E：已核对目标、securityContext 差异、风险和回滚方式",
                    },
                )
                approved.raise_for_status()
            if job.get("status") in {"completed", "failed", "cancelled", "unresolved", "blocked"}:
                break
            time.sleep(0.5)

        assert job.get("status") == "completed", job
        verification = (job.get("result") or {}).get("verification") or {}
        assert verification.get("recovered") is True, verification
        stages = [str(item.get("stage") or "") for item in (job.get("events") or [])]
        for required in (
            "collecting_evidence_done",
            "diagnosing",
            "root_cause_diagnosing",
            "root_cause_diagnosed",
            "diagnosis_done",
            "awaiting_change_approval",
            "change_start",
            "change_done",
            "verifying",
            "verification_done",
            "recovered",
        ):
            assert required in stages, {"missing_stage": required, "stages": stages}
        assert len(approvals) >= 2, approvals
        first_patch = approvals[0].get("patch") or {}
        final_patch = approvals[-1].get("patch") or {}
        first_spec = (((first_patch.get("spec") or {}).get("template") or {}).get("spec") or {})
        final_spec = (((final_patch.get("spec") or {}).get("template") or {}).get("spec") or {})
        assert (first_spec.get("securityContext") or {}).get("runAsUser") == 10001, approvals
        assert (final_spec.get("securityContext") or {}).get("runAsUser") == 0, approvals

        recovered_pod, recovered_logs = wait_for_recovery()
        live = json.loads(kubectl("get", "deployment", WORKLOAD, "-n", NAMESPACE, "-o", "json"))
        live_spec = (((live.get("spec") or {}).get("template") or {}).get("spec") or {})
        assert (live_spec.get("securityContext") or {}).get("runAsUser") == 0, live_spec
        assert "PermissionError" not in recovered_logs

        print(json.dumps({
            "status": "passed",
            "build": build,
            "job_id": job_id,
            "namespace": NAMESPACE,
            "failed_pod": failed_pod,
            "recovered_pod": recovered_pod,
            "diagnosis_source": (diagnosis.get("diagnosis_metadata") or {}).get("source"),
            "matched_skill": "skill-volume-permission-recovery",
            "approvals": len(approvals),
            "approval_actions": [item.get("action") for item in approvals],
            "stages": stages,
            "verification": verification,
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
