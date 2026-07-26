"""Real Kubernetes permission-failure recovery check.

Run with E2E_KUBECONFIG pointing at an isolated cluster. The script injects a
bad initContainer chmod, proves that an unapproved mutation is blocked, then
executes a Skill-bound patch with explicit approval and verifies rollout health.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REQUIRE_LLM = os.getenv("E2E_REQUIRE_LLM", "false").lower() in {"1", "true", "yes", "on"}
if REQUIRE_LLM:
    # A caller can pipe a trusted provider profile into this isolated process.
    # The API key never appears in argv, repository files, logs or test output.
    model_config = json.load(sys.stdin)
    os.environ.update({
        "LLM_API_BASE": str(model_config["base_url"]),
        "LLM_API_KEY": str(model_config["api_key"]),
        "LLM_MODEL": str(model_config["model"]),
        "LLM_PROFILE_ID": "e2e-openclaw",
        "LLM_AUTH_TYPE": "api_key",
        "LLM_MAX_TOKENS": str(model_config.get("max_tokens") or 4096),
        "LLM_READ_TIMEOUT_SECONDS": str(model_config.get("read_timeout_seconds") or 90),
        "MODEL_PROFILES_JSON": "",
        "OAUTH_TOKEN_URL": "",
    })
    override_ip = str(model_config.get("dns_override_ip") or "").strip()
    override_host = urlparse(str(model_config["base_url"])).hostname or ""
    if override_ip and override_host:
        original_getaddrinfo = socket.getaddrinfo

        def e2e_getaddrinfo(host, port, *args, **kwargs):
            return original_getaddrinfo(override_ip if host == override_host else host, port, *args, **kwargs)

        socket.getaddrinfo = e2e_getaddrinfo

KUBECONFIG_PATH = Path(os.environ["E2E_KUBECONFIG"]).resolve()
STATE_DIR = Path(tempfile.mkdtemp(prefix="flawless-e2e-state-"))
os.environ.update({
    "CLUSTER_REGISTRY_PATH": str(STATE_DIR / "clusters.db"),
    "OPS_SKILL_ROOT": str(STATE_DIR / "ops-skills"),
    "OPS_SKILL_STORE_PATH": str(STATE_DIR / "ops-skills.json"),
    "MODEL_PROFILES_STORE": str(STATE_DIR / "model-profiles.json"),
    "OPS_MUTATION_ENABLED": "true",
    "SKILL_EXECUTION_REQUIRED": "true",
    "OPS_VERIFY_INITIAL_GRACE_SECONDS": "8",
    "OPS_VERIFY_TIMEOUT_SECONDS": "80",
    "OPS_VERIFY_INTERVAL_SECONDS": "2",
    "OPS_LLM_PLANNER_TIMEOUT_SECONDS": "90",
    "OPS_LLM_PLANNER_MAX_TOKENS": "6000",
})

from backend.app import main as application  # noqa: E402


SCENARIO = os.getenv("E2E_PERMISSION_SCENARIO", "mkdir").strip().lower()
NAMESPACE = f"flawless-e2e-{SCENARIO[:8]}-{os.getpid()}"
WORKLOAD = "permission-check"
IMAGE = os.getenv("E2E_WORKLOAD_IMAGE", "flawless-local:latest")


def deployment(init_command: str) -> dict:
    app_command = (
        "python -c \"import sqlite3,time; "
        "db=sqlite3.connect('/work/runtime.db'); "
        "db.execute('create table if not exists health(id integer)'); "
        "db.commit(); print('database-ready', flush=True); time.sleep(3600)\""
        if SCENARIO == "database" else
        "mkdir -p /work/runtime && echo ready && sleep 3600"
    )
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
                    "restartPolicy": "Always",
                    "initContainers": [{
                        "name": "prepare-volume",
                        "image": IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["/bin/sh", "-c", init_command],
                        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                    }],
                    "containers": [{
                        "name": "app",
                        "image": IMAGE,
                        "imagePullPolicy": "Never",
                        "command": ["/bin/sh", "-c", app_command],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "allowPrivilegeEscalation": False,
                        },
                        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                    }],
                    "volumes": [{"name": "work", "emptyDir": {}}],
                },
            },
        },
    }


def wait_for_fault(cluster_id: str) -> tuple[str, dict]:
    deadline = time.monotonic() + 90
    last = {}
    while time.monotonic() < deadline:
        inventory = application.CLUSTER_REGISTRY.inventory(cluster_id)
        pods = [
            pod for pod in inventory.get("pods") or []
            if (pod.get("metadata") or {}).get("namespace") == NAMESPACE
            and ((pod.get("metadata") or {}).get("labels") or {}).get("app") == WORKLOAD
        ]
        if pods:
            pod_name = (pods[0].get("metadata") or {}).get("name")
            last = application.CLUSTER_REGISTRY.pod_diagnostics(
                cluster_id,
                namespace=NAMESPACE,
                pod_name=pod_name,
                tail_lines=100,
            )
            text = str(last.get("logs") or {}).lower()
            fault_observed = (
                "unable to open database file" in text
                if SCENARIO == "database" else
                "permission denied" in text and "mkdir" in text
            )
            if fault_observed:
                return pod_name, last
        time.sleep(2)
    raise AssertionError(f"fault evidence not observed: {last}")


async def run() -> None:
    cluster_id = "kube-e2e"
    registry = application.CLUSTER_REGISTRY
    saved = registry.save_verified(
        content=KUBECONFIG_PATH.read_text(encoding="utf-8"),
        name="isolated-e2e",
        cluster_id=cluster_id,
    )
    assert saved["status"] == "connected", saved
    registry.apply_manifest(cluster_id, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
    init_command = (
        "mkdir -p /work && chown 0:20002 /work && chmod 0770 /work"
        if SCENARIO == "gid" else
        "mkdir -p /work && chmod 0500 /work"
    )
    registry.apply_manifest(cluster_id, deployment(init_command))
    try:
        pod_name, diagnostics = await asyncio.to_thread(wait_for_fault, cluster_id)
        summary = (
            "sqlite3.OperationalError: unable to open database file；失败路径位于 /work 挂载卷，业务 UID/GID=10001。"
            if SCENARIO == "database" else
            "mkdir /work/runtime permission denied；目录属组 20002 与业务 runAsGroup 10001 不一致。"
            if SCENARIO == "gid" else
            "mkdir /work/runtime permission denied；initContainer chmod 0500 与业务 UID 10001 冲突。"
        )
        plan = {
            "id": "permission-recovery-e2e",
            "cluster": "isolated-e2e",
            "cluster_id": cluster_id,
            "source": "kubeconfig",
            "namespace": NAMESPACE,
            "target": f"Deployment/{WORKLOAD}",
            "pod_name": pod_name,
            "summary": summary,
            "root_cause": "错误 initContainer 权限配置导致业务容器无法写 emptyDir，应用将底层权限错误包装为文件打开失败。",
            "evidence": {"state_text": summary, "pod": diagnostics.get("raw_pod") or {}},
            "steps": [{"id": "previous_logs", "title": "复核失败日志"}],
            "changes": [],
            "success_criteria": ["rollout_complete", "pod_ready", "restart_count_stable"],
            "requires_confirmation": True,
        }
        if REQUIRE_LLM:
            from agents.llm_client import get_llm

            deep_evidence = await application._collect_plan_deep_evidence(plan)
            assert not deep_evidence.get("error"), deep_evidence
            deep_evidence["log_triage"] = application.triage_kubernetes_logs(
                deep_evidence.get("logs") or {}
            )
            plan["evidence"] = deep_evidence
            signal = application._skill_signal_payload(
                question=plan["summary"],
                diagnosis={"root_cause": plan["root_cause"]},
                evidence=deep_evidence,
                plan=plan,
            )
            available_skills = application.OPS_SKILL_REGISTRY.agent_context(signal, top_k=3)
            prompt = (
                "你是 Kubernetes SRE Skill 路由器。优先阅读 log_triage.priority 的 ERROR/WARNING，"
                "把应用包装错误还原为底层根因，并用 Pod/Workload securityContext、volumeMount、"
                "PVC/PV、Events 交叉验证。unable to open database file 不能只做关键词匹配。"
                "通常只选一个最高匹配 Skill。写路径权限新故障的 first_stage 必须是 nonroot_group；"
                "只有它执行后验证失败才可升级 root。只返回 JSON："
                "{root_cause,root_cause_candidates:[{hypothesis,confidence,supporting_evidence,"
                "contradicting_evidence}],skill_routing:{primary_skill_id,secondary_skill_ids,"
                "strategy_id,confidence,rationale},first_stage}。\n"
                f"Skills={json.dumps(available_skills, ensure_ascii=False)[:9000]}\n"
                f"Evidence={json.dumps(application._redact_sensitive(deep_evidence), ensure_ascii=False)[:12000]}"
            )
            # Reasoning-capable DeepSeek profiles may spend a substantial part
            # of the completion budget before emitting the final JSON.
            llm = get_llm(temperature=0.0, max_tokens=4096, profile_id="e2e-openclaw")
            response = await asyncio.to_thread(llm.invoke, prompt)
            diagnosis = application._extract_json_object(
                getattr(response, "content", str(response))
            )
            routing = diagnosis.get("skill_routing") or {}
            assert routing.get("primary_skill_id") == "skill-volume-permission-recovery", diagnosis
            first_stage = str(diagnosis.get("first_stage") or "").lower()
            assert (
                "nonroot_group" in first_stage
                or ("fsgroup" in first_stage and "root" not in first_stage.replace("non-root", ""))
            ), diagnosis
            plan = application._attach_operator_skills_to_plan(
                plan,
                {
                    "question": plan["summary"],
                    "diagnosis": diagnosis,
                    "evidence": deep_evidence,
                    "plan": plan,
                },
                preferred_skill_ids=[routing["primary_skill_id"]],
                routing={
                    "source": "deepseek-e2e",
                    "selected_skill_ids": [routing["primary_skill_id"]],
                    "strategy_id": routing.get("strategy_id") or "",
                    "confidence": routing.get("confidence"),
                    "rationale": routing.get("rationale") or "",
                },
            )
            plan["planning"] = {
                "source": "llm+DynamicSkillRouterE2E",
                "model": str(model_config["model"]),
                "first_stage": first_stage,
            }
        else:
            # Dynamic Skills authorize mutations only after their complete
            # evidence_required set has been collected from the live cluster.
            deep_evidence = await application._collect_plan_deep_evidence(plan)
            assert not deep_evidence.get("error"), deep_evidence
            plan["evidence"] = deep_evidence
            plan["changes"] = []
            plan = application._attach_operator_skills_to_plan(plan, {
                "question": plan["summary"],
                "diagnosis": {"root_cause": plan["root_cause"]},
                "evidence": deep_evidence,
                "plan": plan,
            }, preferred_skill_ids=["skill-volume-permission-recovery"])
        assert plan["changes"][0]["skill_id"] == "skill-volume-permission-recovery", plan

        blocked = await application._execute_change(copy.deepcopy(plan["changes"][0]), copy.deepcopy(plan))
        assert blocked["status"] == "blocked", blocked

        plan["high_risk_confirmed"] = True
        plan["operator_force_execute"] = True
        approvals: list[dict] = []

        async def approve(index: int, total: int, approved_change: dict, target: str) -> bool:
            approvals.append({"index": index, "total": total, "target": target, "command": approved_change.get("command")})
            return True

        stages: list[str] = []
        result: dict = {}
        for _attempt in range(4):
            stages.append(str(plan.get("permission_recovery_stage") or "initial"))
            result = await application._execute_ops_plan_once(
                plan,
                summarize=False,
                change_approval=approve,
            )
            if (result.get("verification") or {}).get("recovered") is True:
                break
            candidates = [
                item
                for item in (result.get("alternative_plans") or [])
                if isinstance(item, dict)
                and item.get("changes")
                and item.get("selected_skill_id") == "skill-volume-permission-recovery"
            ]
            assert candidates, {"error": "no next permission strategy", "result": result}
            plan = candidates[0]
            plan["high_risk_confirmed"] = True
            plan["operator_force_execute"] = True
        assert approvals and approvals[0]["index"] == 1, {"approvals": approvals, "result": result, "plan": plan}
        assert result["status"] == "completed", result
        assert not (result.get("results") or [{}])[0].get("permission_guidance"), result
        assert (result.get("verification") or {}).get("recovered") is True, result
        candidate = result.get("candidate_skill") or {}
        assert candidate.get("lifecycle") == "candidate" and candidate.get("enabled") is False, candidate
        print({
            "status": result["status"],
            "fault": (
                "unable to open database file"
                if SCENARIO == "database" else
                "directory GID mismatch"
                if SCENARIO == "gid" else
                "mkdir permission denied"
            ),
            "planner": (plan.get("planning") or {}).get("source") or "deterministic-test-plan",
            "action": plan["changes"][0].get("type"),
            "skill": plan["changes"][0]["skill_id"],
            "approval_blocked_before_confirm": True,
            "approved_steps": len(approvals),
            "permission_stages": stages,
            "recovered": result["verification"]["recovered"],
            "candidate_skill": candidate.get("id"),
        })
    finally:
        try:
            registry.delete_resource(cluster_id, api_version="v1", kind="Namespace", name=NAMESPACE, namespace="")
        finally:
            registry.delete(cluster_id)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
