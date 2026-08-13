"""Persistent execution harness for long-running SRE remediation jobs.

The harness is deliberately model-agnostic. LLMs may propose and rank a
strategy, but this state machine owns phase progress, resumability, repeated
trajectory detection, tool/change receipts and the final recovery decision.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any


HARNESS_VERSION = "ResumableSREHarness/v1"
PHASES = ("evidence", "root_cause", "change", "verification")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _phase_for_stage(stage: str) -> str:
    name = str(stage or "").lower()
    if any(token in name for token in ("verif", "recover", "rollout_wait", "health_check")):
        return "verification"
    if any(token in name for token in ("change", "approval", "execution", "rollback", "mutation")):
        return "change"
    if any(token in name for token in ("root", "diagnos", "replan", "skill", "step_")):
        return "root_cause"
    return "evidence"


def new_ops_harness(plan: dict, *, created_at: str = "") -> dict:
    created = created_at or _now()
    return {
        "version": HARNESS_VERSION,
        "mode": "diagnose_execute_verify",
        "objective": str(
            plan.get("title")
            or plan.get("summary")
            or plan.get("reason")
            or plan.get("target")
            or "恢复目标资源"
        ),
        "target": {
            "cluster": str(plan.get("cluster_id") or plan.get("cluster") or "local"),
            "namespace": str(plan.get("namespace") or "default"),
            "resource": str(plan.get("target") or ""),
        },
        "created_at": created,
        "updated_at": created,
        "checkpoint_seq": 0,
        "current_phase": "evidence",
        "todos": [
            {"id": phase, "status": "pending", "updated_at": created}
            for phase in PHASES
        ],
        "attempts": [],
        "tool_receipts": [],
        "model_calls": [],
        "last_trajectory_digest": "",
        "no_progress_count": 0,
        "stuck_detected": False,
        "completion": {
            "status": "pending",
            "recovered": False,
            "reason": "尚未完成恢复验证。",
        },
        "capabilities": {
            "checkpoint_after_transition": True,
            "human_approval": True,
            "trajectory_stuck_detection": True,
            "deterministic_completion_evaluator": True,
            "model_independent_execution": True,
        },
    }


def checkpoint_event(harness: dict, stage: str, message: str, values: dict | None = None) -> dict:
    state = copy.deepcopy(harness or {})
    if not state:
        state = new_ops_harness({})
    values = values or {}
    phase = _phase_for_stage(stage)
    now = _now()
    state["checkpoint_seq"] = int(state.get("checkpoint_seq") or 0) + 1
    state["current_phase"] = phase
    state["updated_at"] = now
    state["last_checkpoint"] = {
        "seq": state["checkpoint_seq"],
        "stage": stage,
        "phase": phase,
        "message": str(message or "")[:500],
        "timestamp": now,
    }

    terminal_status = str(values.get("status") or "")
    phase_done = (
        stage.endswith("_done")
        or stage.endswith("_completed")
        or stage in {
            "evidence_complete", "root_cause_complete", "change_done",
            "verification_complete", "recovered", "completed",
        }
    )
    for todo in state.get("todos") or []:
        if todo.get("id") == phase:
            todo["status"] = "completed" if phase_done else "in_progress"
            todo["updated_at"] = now
        elif PHASES.index(str(todo.get("id"))) < PHASES.index(phase) and todo.get("status") != "completed":
            todo["status"] = "completed"
            todo["updated_at"] = now

    if any(token in stage for token in ("change_start", "change_done", "rollback")):
        receipts = state.setdefault("tool_receipts", [])
        receipts.append({
            "timestamp": now,
            "stage": stage,
            "action": values.get("action") or values.get("change_type") or "kubernetes_change",
            "target": values.get("target") or values.get("change_target") or "",
            "status": values.get("change_status") or terminal_status or "running",
        })
        del receipts[:-80]

    if "llm" in stage.lower() or values.get("model_profile_id") or values.get("planner_source") == "llm":
        calls = state.setdefault("model_calls", [])
        model_status = (
            "failed" if "failed" in stage.lower() else
            "timeout" if "timeout" in stage.lower() else
            (terminal_status or "running") if stage.lower().endswith(("planning", "start")) else
            terminal_status or "completed"
        )
        calls.append({
            "timestamp": now,
            "stage": stage,
            "status": model_status,
            "model_profile_id": values.get("model_profile_id") or "",
        })
        del calls[:-40]
    return state


def evaluate_completion(result: dict) -> dict:
    result = result if isinstance(result, dict) else {}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    recovered = bool(result.get("status") == "completed" and verification.get("recovered") is True)
    if recovered:
        criteria = verification.get("criteria") or verification.get("checks") or []
        return {
            "status": "recovered",
            "recovered": True,
            "reason": str(verification.get("message") or "恢复判据已通过。"),
            "criteria": copy.deepcopy(criteria),
            "evaluated_at": _now(),
        }
    if result.get("status") in {"cancelled", "failed", "blocked"}:
        status = str(result.get("status"))
    else:
        status = "ongoing"
    return {
        "status": status,
        "recovered": False,
        "reason": str(
            verification.get("message")
            or result.get("message")
            or "尚未取得可验证的恢复证据。"
        ),
        "evaluated_at": _now(),
    }


def record_attempt(harness: dict, plan: dict, result: dict) -> dict:
    state = copy.deepcopy(harness or new_ops_harness(plan))
    completion = evaluate_completion(result)
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    trajectory = {
        "skill": plan.get("selected_skill_id") or "",
        "changes": plan.get("changes") or [],
        "result_status": result.get("status") or "",
        "verification_recovered": verification.get("recovered"),
        "verification_reason": verification.get("reason") or verification.get("message") or "",
        "failure_class": verification.get("failure_class") or result.get("blocked_reason") or "",
    }
    digest = _stable_digest(trajectory)
    previous = str(state.get("last_trajectory_digest") or "")
    if digest == previous and not completion["recovered"]:
        state["no_progress_count"] = int(state.get("no_progress_count") or 0) + 1
    else:
        state["no_progress_count"] = 0
    state["last_trajectory_digest"] = digest
    state["stuck_detected"] = bool(state["no_progress_count"] >= 2)
    state["completion"] = completion
    state["updated_at"] = _now()
    attempts = state.setdefault("attempts", [])
    attempts.append({
        "timestamp": state["updated_at"],
        "trajectory_digest": digest,
        "skill_id": str(plan.get("selected_skill_id") or ""),
        "status": str(result.get("status") or "unknown"),
        "recovered": completion["recovered"],
        "progress": not (digest == previous),
    })
    del attempts[:-40]
    if completion["recovered"]:
        for todo in state.get("todos") or []:
            todo["status"] = "completed"
            todo["updated_at"] = state["updated_at"]
        state["current_phase"] = "verification"
    return state
