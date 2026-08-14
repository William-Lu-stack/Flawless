"""发布治理到运维任务的转换服务。

本模块只负责把已审批发布转换成统一 OpsJob 计划，不直接访问 Kubernetes。
真实变更仍由运维状态机和 MCP/Rancher 执行层完成，因此发布、AI 巡检和
SRE Run 共用同一套门禁、进度、取消与恢复验证能力。
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException


EnqueueOpsJob = Callable[..., Awaitable[dict[str, Any]]]


async def submit_release_job(
    release: dict[str, Any],
    actor: str,
    enqueue_ops_job: EnqueueOpsJob,
) -> dict[str, Any]:
    """将人工批准的发布申请转换成受控运维计划。"""
    patch = copy.deepcopy(release.get("patch") or {})
    if release.get("image") and not release.get("manifest"):
        patch = {
            "spec": {"template": {"spec": {"containers": [{
                "name": release.get("container_name"),
                "image": release.get("image"),
            }]}}},
        }

    cluster_id = str(release.get("cluster") or "local")
    source = "rancher" if cluster_id not in {"local", "local-cluster", ""} else "release-api"
    is_new_workload = release.get("release_mode") == "new"
    is_emergency = release.get("change_channel") == "emergency_recovery"
    emergency_action = str(release.get("emergency_action") or "")
    progressive_command = str(release.get("progressive_command") or "start")
    progressive_release = bool(
        not is_new_workload
        and not is_emergency
        and str(release.get("workload_kind") or "Deployment") == "Deployment"
    )

    if progressive_command in {"promote", "abort"}:
        release_change = {
            "type": (
                "promote_progressive_rollout"
                if progressive_command == "promote"
                else "abort_progressive_rollout"
            ),
            "namespace": release.get("namespace") or "default",
            "workload_type": "Deployment",
            "workload_name": release.get("workload_name"),
            "rollout_name": release.get("rollout_name"),
            "release_id": release.get("id"),
            "reason": (
                f"release={release.get('id')} progressive_command={progressive_command} "
                f"approved_by={release.get('approved_by', 'unknown')}"
            ),
        }
    elif is_new_workload:
        release_change = {
            "type": "create_workload",
            "namespace": release.get("namespace") or "default",
            "workload_type": release.get("workload_kind") or "Deployment",
            "workload_name": release.get("workload_name"),
            "manifest": release.get("manifest") or {},
            "reason": f"release={release.get('id')} approved_by={release.get('approved_by', 'unknown')}",
        }
    elif is_emergency and emergency_action == "restart_component":
        release_change = {
            "type": "restart",
            "namespace": release.get("namespace") or "default",
            "workload_type": release.get("workload_kind") or "Deployment",
            "workload_name": release.get("workload_name"),
            "patch": {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}},
            "reason": f"emergency restart release={release.get('id')} approved_by={release.get('approved_by', 'unknown')}",
        }
    elif progressive_release:
        release_change = {
            "type": "progressive_rollout",
            "namespace": release.get("namespace") or "default",
            "workload_type": "Deployment",
            "workload_name": release.get("workload_name"),
            "patch": patch,
            "release": {
                key: copy.deepcopy(release.get(key))
                for key in (
                    "id", "service", "cluster", "namespace", "workload_kind",
                    "workload_name", "container_name", "image", "patch", "gate",
                    "error_budget", "analysis_interval_seconds", "analysis_count",
                    "error_rate_promql", "latency_p99_promql",
                    "max_error_rate", "max_p99_latency_ms",
                )
            },
            "reason": (
                f"release={release.get('id')} approved_by={release.get('approved_by', 'unknown')} "
                "engine=ArgoRolloutsCanary/v1"
            ),
        }
    else:
        release_change = {
            "type": "patch_workload",
            "namespace": release.get("namespace") or "default",
            "workload_type": release.get("workload_kind") or "Deployment",
            "workload_name": release.get("workload_name"),
            "patch": patch,
            "reason": f"release={release.get('id')} approved_by={release.get('approved_by', 'unknown')}",
        }

    if (
        progressive_command not in {"promote", "abort"}
        and not is_new_workload
        and not patch
        and not (is_emergency and emergency_action == "restart_component")
    ):
        raise HTTPException(status_code=422, detail="发布申请缺少 image 或 patch，无法生成 Kubernetes 变更")
    if is_new_workload and not release.get("manifest"):
        raise HTTPException(status_code=422, detail="新建发布缺少经过校验的 manifest")

    plan = {
        "id": f"release-{release.get('id')}",
        "title": f"紧急修复 {release.get('service')}" if is_emergency else f"受控发布 {release.get('service')}",
        "service": release.get("service"),
        "change_class": "emergency_recovery" if is_emergency else "application_release",
        "cluster": cluster_id,
        "cluster_id": cluster_id,
        "namespace": release.get("namespace") or "default",
        "source": source,
        "target": f"{release.get('workload_kind')}/{release.get('workload_name')}",
        "summary": (
            release.get("emergency_reason") or release.get("change_summary") or "恢复稳定性的紧急修复变更。"
            if is_emergency else
            release.get("change_summary") or "通过 SLO 错误预算和变更风险门禁的应用发布。"
        ),
        "steps": [
            {"id": "workload_spec", "title": "读取发布前状态", "description": "记录当前镜像、Workload generation、Pod Ready 和 Events。"},
            {
                "id": "release_gate",
                "title": "复核 SLO 与紧急通道",
                "description": "确认该动作只用于恢复稳定性，并保存错误预算豁免与审批证据。" if is_emergency else "执行前重新计算预算，防止审批后稳定性恶化。",
            },
            {"id": "dependency_topology", "title": "核对拓扑影响", "description": "保存关键依赖和爆炸半径证据。"},
        ],
        "changes": [release_change],
        "success_criteria": (
            ["上一稳定 ReplicaSet Ready", "新版本流量已回收", "Workload 可用副本恢复"]
            if progressive_command == "abort" else
            ["新版本成为 stableRS", "全部新 Pod Ready", "错误率和延迟未突破 SLO 门槛"]
            if progressive_command == "promote" else
            [
                "每个灰度批次 AnalysisRun 成功",
                "Ready/Available 副本保持收敛",
                "到达算法批准的最大灰度比例后停在人工晋级点",
            ]
            if progressive_release else
            ["新 Pod Ready", "Workload rollout 完成", "错误率和延迟未突破 SLO 门槛"]
        ),
        "requires_confirmation": True,
        "high_risk_confirmed": bool(release.get("approved_by")),
        "release_id": release.get("id"),
        "release_gate_snapshot": release.get("gate"),
        "emergency_audit": release.get("emergency_audit") or {},
        "skill_execution_exempt": bool(progressive_release or progressive_command in {"promote", "abort"}),
        "skill_execution_exempt_reason": (
            "应用发布由 SLO/错误预算/爆炸半径门禁和 Argo Rollouts AnalysisRun 约束，"
            "不属于故障修复 Skill 路由。"
            if progressive_release or progressive_command in {"promote", "abort"} else ""
        ),
        "progressive_delivery": {
            "enabled": bool(progressive_release or progressive_command in {"promote", "abort"}),
            "engine": "ArgoRolloutsCanary/v1",
            "command": progressive_command,
            "manual_full_promotion": True,
        },
    }
    if progressive_command == "promote":
        plan["title"] = f"批准全量晋级 {release.get('service')}"
        plan["summary"] = "灰度指标已通过；重新核对错误预算后，由操作员批准新版本全量晋级。"
    elif progressive_command == "abort":
        plan["title"] = f"中止灰度发布 {release.get('service')}"
        plan["summary"] = "停止新版本扩散并恢复上一稳定 ReplicaSet。"
    return await enqueue_ops_job(plan, actor, autonomous=False, confirmed=True)
