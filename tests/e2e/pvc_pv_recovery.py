"""Real Kubernetes Pending PVC -> static PV -> Ready Pod recovery check."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from kubernetes import client, config


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

KUBECONFIG_PATH = Path(os.environ["E2E_KUBECONFIG"]).resolve()
RUN_ID = str(os.getpid())
NAMESPACE = f"flawless-pv-e2e-{RUN_ID}"
WORKLOAD = "pvc-check"
PVC_NAME = "pvc-check-data"
STORAGE_CLASS = f"flawless-static-{RUN_ID}"
IMAGE = os.getenv("E2E_WORKLOAD_IMAGE", "flawless-local:latest")
LOCAL_PATH = f"/tmp/flawless-pv-e2e/{RUN_ID}"
STATE_DIR = Path(tempfile.mkdtemp(prefix="flawless-pv-e2e-state-"))

api_client = config.new_client_from_config(config_file=str(KUBECONFIG_PATH))
core = client.CoreV1Api(api_client)
NODE_NAME = core.list_node(limit=10).items[0].metadata.name

os.environ.update({
    "CLUSTER_REGISTRY_PATH": str(STATE_DIR / "clusters.db"),
    "OPS_SKILL_ROOT": str(STATE_DIR / "ops-skills"),
    "OPS_SKILL_STORE_PATH": str(STATE_DIR / "ops-skills.json"),
    "OPS_MUTATION_ENABLED": "true",
    "SKILL_EXECUTION_REQUIRED": "true",
    "AUTO_OPS_ALLOW_LOCAL_STATIC_PV": "true",
    "OPS_VERIFY_INITIAL_GRACE_SECONDS": "5",
    "OPS_VERIFY_TIMEOUT_SECONDS": "70",
    "OPS_VERIFY_INTERVAL_SECONDS": "2",
    "AUTO_OPS_STATIC_PV_TEMPLATE_JSON": json.dumps({
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "spec": {
            "capacity": {"storage": "1Gi"},
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Filesystem",
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": STORAGE_CLASS,
            "local": {"path": LOCAL_PATH},
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [{
                        "matchExpressions": [{
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": [NODE_NAME],
                        }],
                    }],
                },
            },
        },
    }, ensure_ascii=False),
})

from backend.app import main as application  # noqa: E402


def namespace_manifest() -> dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}


def storage_class_manifest() -> dict:
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": STORAGE_CLASS},
        "provisioner": "kubernetes.io/no-provisioner",
        "volumeBindingMode": "Immediate",
        "reclaimPolicy": "Retain",
    }


def path_preparer_manifest() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "prepare-local-path", "namespace": NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "nodeName": NODE_NAME,
            "containers": [{
                "name": "prepare",
                "image": IMAGE,
                "imagePullPolicy": "Never",
                "command": ["/bin/sh", "-c", "chmod 0777 /data"],
                "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            }],
            "volumes": [{
                "name": "data",
                "hostPath": {"path": LOCAL_PATH, "type": "DirectoryOrCreate"},
            }],
        },
    }


def pvc_manifest() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": PVC_NAME, "namespace": NAMESPACE},
        "spec": {
            "storageClassName": STORAGE_CLASS,
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Filesystem",
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }


def deployment_manifest() -> dict:
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
                    "containers": [{
                        "name": "app",
                        "image": IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["/bin/sh", "-c", "echo mounted-ready > /data/ready && echo mounted-ready && sleep 3600"],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    }],
                    "volumes": [{
                        "name": "data",
                        "persistentVolumeClaim": {"claimName": PVC_NAME},
                    }],
                },
            },
        },
    }


def wait_for_preparer(registry, cluster_id: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pods = registry.inventory(cluster_id).get("pods") or []
        pod = next(
            (
                item for item in pods
                if (item.get("metadata") or {}).get("namespace") == NAMESPACE
                and (item.get("metadata") or {}).get("name") == "prepare-local-path"
            ),
            None,
        )
        if pod and (pod.get("status") or {}).get("phase") == "Succeeded":
            return
        time.sleep(1)
    raise AssertionError("local path preparer did not complete")


def wait_for_pending_fault(registry, cluster_id: str) -> tuple[str, dict]:
    deadline = time.monotonic() + 80
    last: dict = {}
    while time.monotonic() < deadline:
        pods = [
            item for item in (registry.inventory(cluster_id).get("pods") or [])
            if (item.get("metadata") or {}).get("namespace") == NAMESPACE
            and ((item.get("metadata") or {}).get("labels") or {}).get("app") == WORKLOAD
        ]
        if pods:
            pod_name = str((pods[0].get("metadata") or {}).get("name") or "")
            last = registry.pod_diagnostics(
                cluster_id,
                namespace=NAMESPACE,
                pod_name=pod_name,
                tail_lines=80,
            )
            storage = last.get("storage") or []
            if any(item.get("pvc") == PVC_NAME and item.get("pvc_phase") == "Pending" for item in storage):
                return pod_name, last
        time.sleep(1)
    raise AssertionError(f"Pending PVC evidence not observed: {last}")


async def run() -> None:
    cluster_id = "kube-pv-e2e"
    registry = application.CLUSTER_REGISTRY
    saved = registry.save_verified(
        content=KUBECONFIG_PATH.read_text(encoding="utf-8"),
        name="isolated-pv-e2e",
        cluster_id=cluster_id,
    )
    assert saved["status"] == "connected", saved
    pv_name = ""
    registry.apply_manifest(cluster_id, namespace_manifest())
    registry.apply_manifest(cluster_id, storage_class_manifest())
    registry.apply_manifest(cluster_id, path_preparer_manifest())
    try:
        await asyncio.to_thread(wait_for_preparer, registry, cluster_id)
        registry.apply_manifest(cluster_id, pvc_manifest())
        registry.apply_manifest(cluster_id, deployment_manifest())
        pod_name, _diagnostics = await asyncio.to_thread(wait_for_pending_fault, registry, cluster_id)
        plan = {
            "id": "pvc-pv-recovery-e2e",
            "cluster": "isolated-pv-e2e",
            "cluster_id": cluster_id,
            "source": "kubeconfig",
            "namespace": NAMESPACE,
            "target": f"Deployment/{WORKLOAD}",
            "pod_name": pod_name,
            "summary": f"Pod Pending: PVC {PVC_NAME} has no matching PersistentVolume.",
            "root_cause": "Static StorageClass has no matching PV, so the workload cannot schedule.",
            "steps": [{"id": "storage_chain", "title": "读取 Pod/PVC/PV/StorageClass 状态"}],
            "changes": [],
            "success_criteria": ["pvc_bound", "mount_events_absent", "pod_ready"],
            "requires_confirmation": True,
        }
        evidence = await application._collect_plan_deep_evidence(plan)
        assert not evidence.get("error"), evidence
        plan["evidence"] = evidence
        plan = application._attach_operator_skills_to_plan(
            plan,
            {
                "question": plan["summary"],
                "diagnosis": {"root_cause": plan["root_cause"]},
                "evidence": evidence,
                "plan": plan,
            },
            preferred_skill_ids=["skill-storage-pvc-pv"],
        )
        assert plan.get("storage_recovery_stage") == "create_approved_static_pv", plan
        assert (plan.get("changes") or [{}])[0].get("type") == "create_pv", plan
        pv_name = str(((plan["changes"][0]["manifest"].get("metadata") or {}).get("name")) or "")

        blocked = await application._execute_change(plan["changes"][0], plan)
        assert blocked["status"] == "blocked", blocked

        plan["high_risk_confirmed"] = True
        plan["operator_force_execute"] = True
        approvals: list[str] = []

        async def approve(_index: int, _total: int, _change: dict, target: str) -> bool:
            approvals.append(target)
            return True

        result = await application._execute_ops_plan_once(
            plan,
            summarize=False,
            change_approval=approve,
        )
        assert approvals, {"result": result}
        assert result["status"] == "completed", result
        assert (result.get("verification") or {}).get("recovered") is True, result
        pvc = core.read_namespaced_persistent_volume_claim(PVC_NAME, NAMESPACE)
        assert pvc.status.phase == "Bound", pvc
        ready_pods = [
            item for item in (registry.inventory(cluster_id).get("pods") or [])
            if (item.get("metadata") or {}).get("namespace") == NAMESPACE
            and ((item.get("metadata") or {}).get("labels") or {}).get("app") == WORKLOAD
            and any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in ((item.get("status") or {}).get("conditions") or [])
            )
        ]
        assert ready_pods, {"result": result}
        ready_name = str((ready_pods[0].get("metadata") or {}).get("name") or "")
        log = core.read_namespaced_pod_log(ready_name, NAMESPACE, container="app", tail_lines=20)
        assert "mounted-ready" in log, log
        print({
            "status": result["status"],
            "fault": "PVC Pending without matching PV",
            "skill": plan["selected_skill_id"],
            "action": plan["changes"][0]["type"],
            "approval_blocked_before_confirm": True,
            "pvc_phase": pvc.status.phase,
            "pod_ready": True,
            "mount_write_verified": True,
            "recovered": result["verification"]["recovered"],
        })
    finally:
        try:
            registry.delete_resource(cluster_id, api_version="v1", kind="Namespace", name=NAMESPACE, namespace="")
        except Exception:
            pass
        if pv_name:
            try:
                registry.delete_resource(cluster_id, api_version="v1", kind="PersistentVolume", name=pv_name, namespace="")
            except Exception:
                pass
        try:
            registry.delete_resource(cluster_id, api_version="storage.k8s.io/v1", kind="StorageClass", name=STORAGE_CLASS, namespace="")
        except Exception:
            pass
        registry.delete(cluster_id)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
