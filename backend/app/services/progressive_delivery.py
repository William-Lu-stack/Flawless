"""Argo Rollouts manifests and status policy for SRE progressive delivery.

The release risk algorithm decides the maximum automatically observed canary
scope.  Argo Rollouts enforces that scope in Kubernetes.  Reaching the final
pause proves only that the canary is healthy; a separate human-approved
promotion is still required before the release may reach 100%.
"""

from __future__ import annotations

import math
import re
from typing import Any


ARGO_ROLLOUTS_API_VERSION = "argoproj.io/v1alpha1"


def kubernetes_name(value: Any, *, suffix: str = "", limit: int = 63) -> str:
    raw = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    raw = re.sub(r"-+", "-", raw) or "release"
    suffix = re.sub(r"[^a-z0-9-]+", "-", suffix.lower()).strip("-")
    if suffix:
        raw = f"{raw[: max(1, limit - len(suffix) - 1)].rstrip('-')}-{suffix}"
    return raw[:limit].rstrip("-")


def rollout_name(workload_name: str) -> str:
    return kubernetes_name(workload_name, suffix="flawless")


def analysis_template_name(release_id: str) -> str:
    return kubernetes_name(release_id, suffix="sre-gate")


def _ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.01, min(1.0, parsed))


def _positive_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def derive_canary_policy(release: dict[str, Any], replicas: int) -> dict[str, Any]:
    """Translate the algorithm envelope into exact Argo canary steps.

    A replica-weighted canary cannot represent an arbitrary percentage.  The
    smallest non-zero batch is one replica, so a 3-replica Deployment has a
    34% effective floor.  The policy blocks execution when that floor exceeds
    the algorithm's maximum blast radius instead of silently running a larger
    canary than the operator approved.
    """

    gate = release.get("gate") if isinstance(release.get("gate"), dict) else {}
    strategy = gate.get("selected_strategy") if isinstance(gate.get("selected_strategy"), dict) else {}
    first_ratio = _ratio(strategy.get("first_ratio"), 0.01)
    max_ratio = max(first_ratio, _ratio(strategy.get("max_ratio"), 0.10))
    step_ratio = _ratio(strategy.get("step_ratio"), 0.02)
    replicas = _positive_int(replicas, 1, 1, 10000)
    requested_first = max(1, int(math.ceil(first_ratio * 100)))
    requested_max = max(requested_first, int(math.floor(max_ratio * 100)))
    requested_step = max(1, int(math.ceil(step_ratio * 100)))
    replica_floor = int(math.ceil(100 / replicas))
    effective_first = max(requested_first, replica_floor)
    unsupported = effective_first > requested_max

    weights: list[int] = []
    if not unsupported:
        current = effective_first
        while current < requested_max:
            weights.append(current)
            current += requested_step
        weights.append(requested_max)
        weights = sorted(set(max(1, min(99, value)) for value in weights))

    observation_window_min = _positive_int(
        strategy.get("observation_window_min"),
        20,
        1,
        1440,
    )
    analysis_interval_seconds = _positive_int(
        release.get("analysis_interval_seconds"),
        min(300, observation_window_min * 60),
        10,
        3600,
    )
    analysis_count = _positive_int(release.get("analysis_count"), 3, 1, 20)
    required_replicas = int(math.ceil(100 / requested_max))
    return {
        "engine": "ArgoRolloutsCanary/v1",
        "traffic_routing": "replica_weighted",
        "replicas": replicas,
        "requested_first_weight": requested_first,
        "requested_max_weight": requested_max,
        "requested_step_weight": requested_step,
        "effective_first_weight": effective_first,
        "replica_weight_floor": replica_floor,
        "required_replicas_for_envelope": required_replicas,
        "weights": weights,
        "analysis_interval_seconds": analysis_interval_seconds,
        "analysis_count": analysis_count,
        "observation_window_min": observation_window_min,
        "manual_full_promotion": True,
        "unsupported": unsupported,
        "blocked_reason": (
            f"当前 {replicas} 个副本的最小可执行灰度为 {replica_floor}%，"
            f"超过算法批准的最大爆炸半径 {requested_max}%。"
            f"请先将副本扩到至少 {required_replicas}，或接入 Istio/NGINX 流量路由后再发布。"
            if unsupported else ""
        ),
    }


def build_analysis_template(
    release: dict[str, Any],
    policy: dict[str, Any],
    analysis_url: str,
) -> dict[str, Any]:
    release_id = str(release.get("id") or "")
    namespace = str(release.get("namespace") or "default")
    name = analysis_template_name(release_id)
    url = analysis_url.rstrip("/") + f"/api/releases/{release_id}/analysis"
    return {
        "apiVersion": ARGO_ROLLOUTS_API_VERSION,
        "kind": "AnalysisTemplate",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "flawless",
                "flawless.io/release-id": release_id,
            },
        },
        "spec": {
            "metrics": [{
                "name": "flawless-sre-release-gate",
                "interval": f"{int(policy['analysis_interval_seconds'])}s",
                "count": int(policy["analysis_count"]),
                # Argo limits are tolerated counts, not trigger counts.  Zero
                # means the first hard failure aborts and the first
                # inconclusive measurement pauses instead of being counted as
                # a successful batch.
                "failureLimit": 0,
                "inconclusiveLimit": 0,
                "successCondition": "result.safe == true",
                "failureCondition": "result.abort == true",
                "provider": {
                    "web": {
                        "url": url,
                        "timeoutSeconds": 10,
                        "jsonPath": "{$.data}",
                    },
                },
            }],
        },
    }


def build_rollout(
    release: dict[str, Any],
    live_deployment: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    metadata = live_deployment.get("metadata") or {}
    spec = live_deployment.get("spec") or {}
    workload_name = str(metadata.get("name") or release.get("workload_name") or "")
    namespace = str(metadata.get("namespace") or release.get("namespace") or "default")
    selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
    if not selector:
        raise ValueError("Deployment spec.selector 为空，无法创建安全的 Argo Rollout")
    if policy.get("unsupported"):
        raise ValueError(str(policy.get("blocked_reason") or "当前副本数无法满足灰度爆炸半径"))

    template_name = analysis_template_name(str(release.get("id") or ""))
    steps: list[dict[str, Any]] = []
    for weight in policy.get("weights") or []:
        steps.extend([
            {"setWeight": int(weight)},
            {
                "analysis": {
                    "templates": [{"templateName": template_name}],
                },
            },
        ])
    # No duration is intentional.  The algorithm-approved canary ceiling is a
    # hard stop; full promotion is a separate, audited human action.
    steps.append({"pause": {}})
    return {
        "apiVersion": ARGO_ROLLOUTS_API_VERSION,
        "kind": "Rollout",
        "metadata": {
            "name": rollout_name(workload_name),
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "flawless",
                "flawless.io/release-id": str(release.get("id") or ""),
                "flawless.io/source-workload": workload_name,
            },
        },
        "spec": {
            "replicas": int(policy["replicas"]),
            "selector": selector,
            "workloadRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": workload_name,
                "scaleDown": "progressively",
            },
            "progressDeadlineSeconds": int(
                max(
                    600,
                    len(policy.get("weights") or [])
                    * int(policy["analysis_interval_seconds"])
                    * int(policy["analysis_count"])
                    + 300,
                )
            ),
            "progressDeadlineAbort": True,
            "strategy": {
                "canary": {
                    "maxUnavailable": 0,
                    "maxSurge": 1,
                    "abortScaleDownDelaySeconds": 30,
                    "antiAffinity": {
                        "preferredDuringSchedulingIgnoredDuringExecution": {
                            "weight": 100,
                        },
                    },
                    "steps": steps,
                },
            },
        },
    }


def progressive_status(
    rollout: dict[str, Any],
    *,
    command: str,
    expected_steps: int = 0,
) -> dict[str, Any]:
    metadata = rollout.get("metadata") or {}
    spec = rollout.get("spec") or {}
    status = rollout.get("status") or {}
    desired = int(spec.get("replicas") or 0)
    phase = str(status.get("phase") or "Unknown")
    ready = int(status.get("readyReplicas") or 0)
    available = int(status.get("availableReplicas") or 0)
    current_hash = str(status.get("currentPodHash") or "")
    stable_hash = str(status.get("stableRS") or "")
    step_index = int(status.get("currentStepIndex") or 0)
    pause_conditions = status.get("pauseConditions") or []
    conditions = status.get("conditions") or []
    degraded = phase == "Degraded" or any(
        str(item.get("reason") or "") in {
            "ProgressDeadlineExceeded",
            "RolloutAborted",
            "AnalysisRunFailed",
        }
        for item in conditions
        if isinstance(item, dict)
    )
    aborted = bool(status.get("abort") or status.get("abortedAt"))

    common = {
        "engine": "ArgoRolloutsCanary/v1",
        "rollout": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "phase": phase,
        "message": status.get("message") or "",
        "desired_replicas": desired,
        "ready_replicas": ready,
        "available_replicas": available,
        "current_step_index": step_index,
        "current_pod_hash": current_hash,
        "stable_rs": stable_hash,
        "pause_conditions": pause_conditions,
        "conditions": conditions[-8:],
        "aborted": aborted,
    }
    if command == "abort":
        recovered = aborted and ready >= desired and available >= desired
        return {
            **common,
            "status": "verified" if recovered else "progressing",
            "recovered": recovered if recovered else None,
            "progressive_phase": "rolled_back" if recovered else "aborting",
            "message": (
                "Argo Rollouts 已中止新版本并恢复上一稳定 ReplicaSet。"
                if recovered else "正在中止灰度并恢复上一稳定 ReplicaSet。"
            ),
        }
    if aborted:
        rollback_completed = ready >= desired and available >= desired
        return {
            **common,
            "status": "verified" if rollback_completed else "progressing",
            "recovered": rollback_completed if rollback_completed else None,
            "progressive_phase": "rolled_back" if rollback_completed else "aborting",
            "release_succeeded": False,
            "message": (
                "灰度指标未通过；Argo Rollouts 已自动恢复上一 stableRS，Ready/Available 副本已收敛。"
                if rollback_completed else
                "灰度指标未通过；Argo Rollouts 已停止扩散，正在恢复上一 stableRS。"
            ),
        }
    if degraded:
        return {
            **common,
            "status": "needs_followup",
            "recovered": False,
            "progressive_phase": "degraded",
            "terminal_unresolved": [{
                "name": metadata.get("name"),
                "category": "progressive_delivery",
                "reason": status.get("message") or phase,
            }],
            "message": (
                "灰度指标或 Workload 健康检查未通过，Argo Rollouts 已停止扩大影响面。"
            ),
        }
    if command == "promote":
        completed = (
            phase == "Healthy"
            and bool(current_hash)
            and current_hash == stable_hash
            and ready >= desired
            and available >= desired
        )
        return {
            **common,
            "status": "verified" if completed else "progressing",
            "recovered": completed if completed else None,
            "progressive_phase": "fully_promoted" if completed else "promoting",
            "message": (
                "新版本已全量晋级，稳定 ReplicaSet、Ready/Available 副本和期望副本全部收敛。"
                if completed else "已批准全量晋级，Argo Rollouts 正在收敛新稳定版本。"
            ),
        }

    canary_validated = (
        phase == "Paused"
        and bool(pause_conditions)
        and ready >= desired
        and available >= desired
        and (expected_steps <= 0 or step_index >= expected_steps - 1)
    )
    paused_before_gate = phase == "Paused" and not canary_validated
    if paused_before_gate:
        return {
            **common,
            "status": "needs_followup",
            "recovered": False,
            "progressive_phase": "analysis_inconclusive",
            "terminal_unresolved": [{
                "name": metadata.get("name"),
                "category": "progressive_delivery",
                "reason": (
                    status.get("message")
                    or "AnalysisRun 证据不足或结果不确定，灰度已在当前批次暂停"
                ),
            }],
            "message": (
                "SRE 指标证据不足或 AnalysisRun 结果不确定；灰度已停止扩大，等待人工中止或补齐指标。"
            ),
        }
    return {
        **common,
        "status": "verified" if canary_validated else "progressing",
        "recovered": canary_validated if canary_validated else None,
        "progressive_phase": "canary_validated" if canary_validated else "canary_running",
        "canary_validated": canary_validated,
        "message": (
            "算法批准范围内的灰度批次与 SRE 指标门禁全部通过；已停在人工全量晋级点。"
            if canary_validated else "Argo Rollouts 正在按批扩大灰度，并在每一批执行 SRE 指标门禁。"
        ),
    }
