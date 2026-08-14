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


from backend.app.services.harness_plugins import CISRE_HARNESS_RUNTIME


HARNESS_VERSION = "CISREDurableHarness/v3"
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


def _event_type(stage: str, phase: str) -> str:
    name = str(stage or "").strip().lower().replace("_", "/")
    if stage in {"recovered", "completed"}:
        return "recovery/complete"
    if "approval" in name:
        return "tools/approval"
    if "llm" in name:
        return f"llm/{'response' if any(token in name for token in ('done', 'complete')) else 'request'}"
    if "skill" in name:
        return "skills/selected" if any(token in name for token in ("done", "complete")) else "skills/discover"
    if phase == "change":
        return "tools/result" if any(token in name for token in ("done", "complete")) else "tools/execute"
    if phase == "verification":
        return "recovery/check"
    return f"ops/{name or 'event'}"


def new_ops_harness(plan: dict, *, created_at: str = "") -> dict:
    created = created_at or _now()
    plugin_runtime = CISRE_HARNESS_RUNTIME.diagnostics()
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
        "resume_token": "",
        "current_phase": "evidence",
        "todos": [
            {"id": phase, "status": "pending", "updated_at": created}
            for phase in PHASES
        ],
        "attempts": [],
        "tool_receipts": [],
        "model_calls": [],
        "events": [],
        "plugin_runtime": {
            "runtime": plugin_runtime.get("runtime"),
            "active_plugins": [
                item.get("id")
                for item in plugin_runtime.get("plugins") or []
                if item.get("status") == "active"
            ],
            "plugin_count": (plugin_runtime.get("summary") or {}).get("active", 0),
        },
        "approval_ledger": [],
        "idempotency_ledger": {},
        "last_trajectory_digest": "",
        "last_progress_digest": "",
        "no_progress_count": 0,
        "stuck_detected": False,
        "completion": {
            "status": "pending",
            "recovered": False,
            "reason": "尚未完成恢复验证。",
        },
        "budgets": {
            "max_strategy_attempts": max(1, int(plan.get("max_attempts") or 12)),
            "max_model_calls": max(1, int(plan.get("max_model_calls") or 24)),
            "max_mutations": max(1, int(plan.get("max_mutations") or 16)),
            "strategy_attempts_used": 0,
            "model_calls_used": 0,
            "mutations_used": 0,
        },
        "phase_contracts": {
            "evidence": "fresh logs/events/workload or typed infrastructure evidence",
            "root_cause": "ranked hypothesis + one primary Skill + bounded recovery criteria",
            "change": "human approval + API receipt + exact live read-after-write postcondition",
            "verification": "fresh rollout/health evidence proves recovery and stability",
        },
        "capabilities": {
            "checkpoint_after_transition": True,
            "resume_from_checkpoint": True,
            "human_approval": True,
            "approval_pause_without_worker_slot": True,
            "idempotent_tool_execution": True,
            "read_after_write_postconditions": True,
            "trajectory_stuck_detection": True,
            "deterministic_completion_evaluator": True,
            "bounded_context_compaction": True,
            "strategy_budgeting": True,
            "model_independent_execution": True,
            "everything_is_a_plugin": True,
            "scoped_dependency_injection": True,
            "typed_session_events": True,
            "waterfall_tool_guards": True,
            "reversible_plugin_lifecycle": True,
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
    state["resume_token"] = _stable_digest({
        "target": state.get("target"),
        "seq": state["checkpoint_seq"],
        "stage": stage,
        "timestamp": now,
    })
    event_log = state.setdefault("events", [])
    event_log.append({
        "seq": state["checkpoint_seq"],
        "timestamp": now,
        "type": _event_type(stage, phase),
        "stage": stage,
        "phase": phase,
        "message": str(message or "")[:500],
        "status": str(values.get("status") or "running"),
        "data": {
            "target": copy.deepcopy(state.get("target") or {}),
            "status": str(values.get("status") or "running"),
        },
    })
    del event_log[:-160]

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

    if "approval" in stage.lower() or values.get("approved") is not None:
        approvals = state.setdefault("approval_ledger", [])
        approvals.append({
            "timestamp": now,
            "stage": stage,
            "target": values.get("target") or values.get("change_target") or "",
            "approved": values.get("approved"),
            "operator": values.get("operator") or "",
        })
        del approvals[:-80]

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
        state.setdefault("budgets", {})["model_calls_used"] = len(calls)
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
    mutation_receipts: list[dict] = []
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("result") if isinstance(item.get("result"), dict) else {}
        receipt = raw.get("mutation_postcondition") if isinstance(raw, dict) else None
        if isinstance(receipt, dict):
            mutation_receipts.append(copy.deepcopy(receipt))
    trajectory = {
        "skill": plan.get("selected_skill_id") or "",
        "changes": plan.get("changes") or [],
        "result_status": result.get("status") or "",
        "verification_recovered": verification.get("recovered"),
        "verification_reason": verification.get("reason") or verification.get("message") or "",
        "failure_class": verification.get("failure_class") or result.get("blocked_reason") or "",
        "mutation_postconditions": [
            {
                "target": item.get("target"),
                "verified": item.get("verified"),
                "resource_version": item.get("resource_version"),
                "expected_patch_digest": item.get("expected_patch_digest"),
            }
            for item in mutation_receipts
        ],
    }
    digest = _stable_digest(trajectory)
    progress = {
        "status": result.get("status") or "",
        "recovered": completion["recovered"],
        "failure_class": trajectory["failure_class"],
        "resource_versions": sorted({
            str(item.get("resource_version") or "")
            for item in mutation_receipts
            if item.get("resource_version")
        }),
        "verified_mutations": sum(item.get("verified") is True for item in mutation_receipts),
        "ready_pods": len(verification.get("recovered_pods") or []),
        "unresolved": len(verification.get("unresolved") or []),
    }
    progress_digest = _stable_digest(progress)
    previous = str(state.get("last_trajectory_digest") or "")
    previous_progress = str(state.get("last_progress_digest") or "")
    if digest == previous and progress_digest == previous_progress and not completion["recovered"]:
        state["no_progress_count"] = int(state.get("no_progress_count") or 0) + 1
    else:
        state["no_progress_count"] = 0
    state["last_trajectory_digest"] = digest
    state["last_progress_digest"] = progress_digest
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
        "progress": not (digest == previous and progress_digest == previous_progress),
        "progress_digest": progress_digest,
        "mutation_postconditions": mutation_receipts,
    })
    del attempts[:-40]
    budgets = state.setdefault("budgets", {})
    budgets["strategy_attempts_used"] = len(attempts)
    budgets["mutations_used"] = sum(
        len(item.get("mutation_postconditions") or [])
        for item in attempts
    )
    receipts = state.setdefault("tool_receipts", [])
    for receipt in mutation_receipts:
        receipt_id = _stable_digest({
            "target": receipt.get("target"),
            "patch": receipt.get("expected_patch_digest"),
            "resource_version": receipt.get("resource_version"),
        })
        state.setdefault("idempotency_ledger", {})[receipt_id] = {
            "verified": receipt.get("verified") is True,
            "target": receipt.get("target") or "",
            "resource_version": receipt.get("resource_version"),
            "updated_at": state["updated_at"],
        }
        receipts.append({
            "timestamp": state["updated_at"],
            "stage": "mutation_postcondition",
            "action": "kubernetes_read_after_write",
            "target": receipt.get("target") or "",
            "status": "verified" if receipt.get("verified") is True else "failed",
            "receipt_id": receipt_id,
            "resource_version": receipt.get("resource_version"),
        })
    del receipts[:-80]
    ledger = state.setdefault("idempotency_ledger", {})
    if len(ledger) > 120:
        keep = list(ledger)[-120:]
        state["idempotency_ledger"] = {key: ledger[key] for key in keep}
    if completion["recovered"]:
        for todo in state.get("todos") or []:
            todo["status"] = "completed"
            todo["updated_at"] = state["updated_at"]
        state["current_phase"] = "verification"
    return state
