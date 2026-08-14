"""Durable, append-only Harness session events for CISRE.

The operation job document remains the fast UI projection.  This module is the
durable fact stream used for audit, replay, forks and child-session drill-down.
It deliberately stores sanitized metadata rather than credentials or arbitrary
file contents.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVENT_SCHEMA = "cisre.session-event/v1"
_SECRET_KEYS = re.compile(
    r"(?:token|password|passwd|secret|authorization|api[_-]?key|client[_-]?secret|kubeconfig)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def redact_event_value(value: Any, *, key: str = "") -> Any:
    """Remove credentials and bound untrusted payloads before persistence."""
    if _SECRET_KEYS.search(str(key or "")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key)[:120]: redact_event_value(item, key=str(item_key))
            for item_key, item in list(value.items())[:80]
        }
    if isinstance(value, (list, tuple)):
        return [redact_event_value(item) for item in list(value)[:80]]
    if isinstance(value, str):
        return _BEARER.sub(r"\1[REDACTED]", value[:4000])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


class HarnessEventStore:
    """Single-writer-safe JSONL store with per-session hash chains."""

    def __init__(self, path: str | Path, *, max_events: int = 20000) -> None:
        self.primary_path = Path(path)
        self.path = self.primary_path
        self.fallback_path = Path(
            os.getenv("HARNESS_EVENT_STORE_FALLBACK_PATH", "/tmp/cisre/harness-events.jsonl")
        )
        self.fallback_used = False
        self.fallback_reason = ""
        self.max_events = max(100, int(max_events))
        self._lock = threading.RLock()
        self._memory: list[dict[str, Any]] = []
        self.persistence_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("schema") == EVENT_SCHEMA:
                        self._memory.append(item)
            self._memory = self._memory[-self.max_events :]
        except OSError as exc:
            self.persistence_error = f"{type(exc).__name__}: {exc}"[:500]

    def _persist(self, event: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
            self.persistence_error = ""
        except OSError as exc:
            primary_error = f"{type(exc).__name__}: {exc}"[:500]
            if self.path == self.fallback_path:
                self.persistence_error = primary_error
                return
            # Match the existing CISRE reliability-store behavior: keep the
            # control loop alive locally, surface degraded durability, and use
            # an explicitly bounded fallback rather than silently dropping events.
            try:
                self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
                with self.fallback_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                    handle.flush()
                self.path = self.fallback_path
                self.fallback_used = True
                self.fallback_reason = primary_error
                self.persistence_error = ""
            except OSError as fallback_exc:
                self.persistence_error = f"{primary_error}; fallback: {type(fallback_exc).__name__}: {fallback_exc}"[:500]

    def append(
        self,
        session_id: str,
        event_type: str,
        *,
        phase: str = "",
        stage: str = "",
        message: str = "",
        status: str = "running",
        data: dict[str, Any] | None = None,
        actor: str = "system",
        plugin_id: str = "",
        tool: str = "",
        target: dict[str, Any] | str | None = None,
        parent_session_id: str = "",
        parent_seq: int | None = None,
    ) -> dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        with self._lock:
            prior = [item for item in self._memory if item.get("session_id") == sid]
            previous_hash = str(prior[-1].get("hash") or "") if prior else ""
            seq = int(prior[-1].get("seq") or 0) + 1 if prior else 1
            body = {
                "schema": EVENT_SCHEMA,
                "event_id": f"evt-{uuid.uuid4().hex}",
                "session_id": sid,
                "seq": seq,
                "previous_hash": previous_hash,
                "timestamp": _now(),
                "type": str(event_type or "session/event")[:160],
                "phase": str(phase or "")[:80],
                "stage": str(stage or "")[:120],
                "status": str(status or "running")[:80],
                "message": str(redact_event_value(message))[:1000],
                "actor": str(redact_event_value(actor))[:160],
                "plugin_id": str(plugin_id or "")[:160],
                "tool": str(tool or "")[:160],
                "target": redact_event_value(target or {}),
                "data": redact_event_value(data or {}),
                "parent_session_id": str(parent_session_id or "")[:180],
                "parent_seq": int(parent_seq) if parent_seq is not None else None,
            }
            body["hash"] = _digest(body)
            self._memory.append(body)
            self._memory = self._memory[-self.max_events :]
            self._persist(body)
            return copy.deepcopy(body)

    def events(self, session_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            values = [
                item for item in self._memory
                if item.get("session_id") == session_id and int(item.get("seq") or 0) > after_seq
            ]
            return copy.deepcopy(values[: max(1, min(int(limit), 2000))])

    @staticmethod
    def _deleted(events: list[dict[str, Any]]) -> bool:
        return any(str(item.get("type") or "") == "session/deleted" for item in events)

    def sessions(self, *, limit: int = 100, include_deleted: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for event in self._memory:
                grouped.setdefault(str(event.get("session_id") or ""), []).append(event)
            output = []
            for session_id, events in grouped.items():
                if not session_id or not events:
                    continue
                if self._deleted(events) and not include_deleted:
                    continue
                last = events[-1]
                first = events[0]
                output.append({
                    "session_id": session_id,
                    "parent_session_id": next((str(item.get("parent_session_id") or "") for item in events if item.get("parent_session_id")), ""),
                    "created_at": first.get("timestamp"),
                    "updated_at": last.get("timestamp"),
                    "event_count": len(events),
                    "last_type": last.get("type"),
                    "phase": last.get("phase"),
                    "status": last.get("status"),
                    "target": last.get("target") or first.get("target") or {},
                })
            output.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return output[: max(1, min(int(limit), 500))]

    def session(self, session_id: str, *, include_deleted: bool = True) -> dict[str, Any] | None:
        values = self.sessions(limit=500, include_deleted=include_deleted)
        return next((item for item in values if item.get("session_id") == session_id), None)

    def events_for_plugin(self, plugin_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            values = [
                item for item in reversed(self._memory)
                if str(item.get("plugin_id") or "") == str(plugin_id or "")
            ]
            return copy.deepcopy(values[: max(1, min(int(limit), 500))])

    @staticmethod
    def _trace_kind(event: dict[str, Any]) -> str:
        text = " ".join(str(event.get(key) or "") for key in ("type", "phase", "stage", "tool")).lower()
        plugin_id = str(event.get("plugin_id") or "").lower()
        if any(token in text for token in ("approval", "approved", "pre-execute")):
            return "approval"
        if any(token in text for token in ("recovery", "verify", "verification", "readback", "rollout")):
            return "verify"
        if any(token in text for token in ("bash", "shell", "execute", "change", "mutation", "patch", "tool")):
            return "tool"
        if "skill" in text or "skill" in plugin_id:
            return "skill"
        if any(token in text for token in ("plugin/", "service/", "provider/")):
            return "plugin"
        if any(token in text for token in ("llm", "diagnos", "root_cause", "planning", "reason")):
            return "think"
        if any(token in text for token in ("evidence", "collect", "context", "log", "event", "inventory")):
            return "context"
        if any(token in text for token in ("goal", "round", "agent", "job", "fork", "resume", "child")):
            return "agent"
        return "event"

    @staticmethod
    def _trace_plugin(event: dict[str, Any], kind: str) -> str:
        explicit = str(event.get("plugin_id") or "")
        if explicit:
            return explicit
        return {
            "context": "cisre.context-compaction",
            "think": "cisre.deepseek-planner",
            "skill": "cisre.skill-router",
            "plugin": "cisre.plugin-loader",
            "approval": "cisre.approval-gate",
            "tool": "cisre.kubernetes-executor",
            "verify": "cisre.recovery-verifier",
            "agent": "cisre.goal-round-driver",
            "event": "cisre.session-events",
        }[kind]

    def trace(self, session_id: str, *, limit: int = 2000) -> dict[str, Any]:
        """Project immutable events into a secret-safe Agent/LLM/tool trace.

        This deliberately exposes structured decisions and receipts rather than
        private chain-of-thought or raw prompts. Persisted event data has already
        passed the same credential redaction used by the audit store.
        """
        events = self.events(session_id, limit=limit)
        spans: list[dict[str, Any]] = []
        category_counts: dict[str, int] = {}
        plugin_counts: dict[str, int] = {}
        tool_counts: dict[str, int] = {}
        model_calls = 0
        parsed_times: list[datetime] = []
        for event in events:
            try:
                parsed_times.append(datetime.fromisoformat(str(event.get("timestamp") or "").replace("Z", "+00:00")))
            except ValueError:
                parsed_times.append(datetime.now(timezone.utc))
        origin = parsed_times[0] if parsed_times else datetime.now(timezone.utc)
        turn_count = 0
        tool_call_count = 0
        lanes = {
            "context": "Input",
            "think": "Model",
            "tool": "Tools",
            "approval": "Tools",
            "verify": "Tools",
            "skill": "Agent",
            "plugin": "Agent",
            "agent": "Agent",
            "event": "Agent",
        }
        for index, event in enumerate(events):
            kind = self._trace_kind(event)
            plugin_id = self._trace_plugin(event, kind)
            tool = str(event.get("tool") or "")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            model_profile = str(data.get("model_profile_id") or "")
            if kind == "think" or model_profile:
                model_calls += 1
            if kind == "tool" or tool:
                tool_call_count += 1
            event_type = str(event.get("type") or "").lower()
            if event_type in {"agent/round-start", "goal/round", "session/created"}:
                turn_count += 1
            category_counts[kind] = category_counts.get(kind, 0) + 1
            plugin_counts[plugin_id] = plugin_counts.get(plugin_id, 0) + 1
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            offset_ms = max(0, int((parsed_times[index] - origin).total_seconds() * 1000))
            next_gap_ms = (
                int((parsed_times[index + 1] - parsed_times[index]).total_seconds() * 1000)
                if index + 1 < len(parsed_times) else 12
            )
            explicit_duration = data.get("duration_ms") or data.get("latency_ms") or 0
            try:
                duration_ms = int(float(explicit_duration)) if explicit_duration else max(12, min(max(0, next_gap_ms), 30000))
            except (TypeError, ValueError):
                duration_ms = max(12, min(max(0, next_gap_ms), 30000))
            spans.append({
                "span_id": str(event.get("event_id") or ""),
                "seq": event.get("seq"),
                "timestamp": event.get("timestamp"),
                "kind": kind,
                "lane": lanes[kind],
                "offset_ms": offset_ms,
                "duration_ms": duration_ms,
                "name": str(event.get("stage") or event.get("type") or kind),
                "event_type": event.get("type"),
                "phase": event.get("phase"),
                "status": event.get("status"),
                "summary": event.get("message"),
                "plugin_id": plugin_id,
                "tool": tool,
                "model_profile_id": model_profile,
                "actor": event.get("actor"),
                "target": event.get("target") or {},
                "attributes": data,
                "reasoning_policy": "structured-summary-only" if kind == "think" else "not-applicable",
            })
        return {
            "session_id": session_id,
            "trace_schema": "cisre.agent-trace/v1",
            "trace_id": f"cisre-{session_id}",
            "spans": spans,
            "summary": {
                "span_count": len(spans),
                "model_calls": model_calls,
                "duration_ms": max(0, int((parsed_times[-1] - origin).total_seconds() * 1000)) if parsed_times else 0,
                "turns": turn_count or (1 if spans else 0),
                "calls": model_calls + tool_call_count,
                "categories": category_counts,
                "plugins": plugin_counts,
                "tools": tool_counts,
            },
            "disclosure": "Shows structured model decisions and execution receipts; raw secrets, prompts and private chain-of-thought are not exposed.",
            "integrity": self.verify_chain(session_id),
        }

    def verify_chain(self, session_id: str) -> dict[str, Any]:
        events = self.events(session_id, limit=2000)
        previous = ""
        for event in events:
            supplied = str(event.get("hash") or "")
            body = {key: value for key, value in event.items() if key != "hash"}
            if str(event.get("previous_hash") or "") != previous or _digest(body) != supplied:
                return {"valid": False, "failed_seq": event.get("seq"), "events": len(events)}
            previous = supplied
        return {"valid": True, "failed_seq": None, "events": len(events), "head": previous}

    def replay(self, session_id: str, *, until_seq: int | None = None) -> dict[str, Any]:
        events = self.events(session_id, limit=2000)
        if until_seq is not None:
            events = [item for item in events if int(item.get("seq") or 0) <= int(until_seq)]
        projection: dict[str, Any] = {
            "session_id": session_id,
            "checkpoint_seq": 0,
            "current_phase": "evidence",
            "status": "pending",
            "target": {},
            "timeline": [],
        }
        for event in events:
            projection.update({
                "checkpoint_seq": event.get("seq"),
                "current_phase": event.get("phase") or projection["current_phase"],
                "status": event.get("status") or projection["status"],
                "target": event.get("target") or projection["target"],
                "updated_at": event.get("timestamp"),
            })
            projection["timeline"].append({
                key: event.get(key)
                for key in ("seq", "timestamp", "type", "stage", "phase", "status", "message", "plugin_id", "tool")
            })
        projection["integrity"] = self.verify_chain(session_id)
        return projection

    def fork(self, session_id: str, *, at_seq: int | None = None, actor: str = "operator") -> dict[str, Any]:
        parent_events = self.events(session_id, limit=2000)
        if not parent_events:
            raise KeyError(f"session not found: {session_id}")
        boundary = int(at_seq or parent_events[-1].get("seq") or 0)
        boundary_event = next((item for item in reversed(parent_events) if int(item.get("seq") or 0) <= boundary), None)
        if boundary_event is None:
            raise ValueError("fork boundary does not exist")
        child_id = f"fork-{uuid.uuid4().hex[:16]}"
        self.append(
            child_id,
            "session/forked",
            phase=str(boundary_event.get("phase") or "evidence"),
            stage="forked",
            status="paused",
            message=f"Forked from {session_id} at event {boundary}",
            data={"source_session_id": session_id, "source_seq": boundary},
            actor=actor,
            target=boundary_event.get("target") or {},
            parent_session_id=session_id,
            parent_seq=boundary,
        )
        self.append(
            session_id,
            "session/child-created",
            phase=str(boundary_event.get("phase") or "evidence"),
            stage="child_created",
            status=str(boundary_event.get("status") or "running"),
            message=f"Child session {child_id} created",
            data={"child_session_id": child_id, "fork_seq": boundary},
            actor=actor,
            target=boundary_event.get("target") or {},
        )
        return {"session_id": child_id, "parent_session_id": session_id, "parent_seq": boundary}

    def resume(self, session_id: str, *, actor: str = "operator") -> dict[str, Any]:
        replay = self.replay(session_id)
        if not replay.get("checkpoint_seq"):
            raise KeyError(f"session not found: {session_id}")
        event = self.append(
            session_id,
            "session/resumed",
            phase=str(replay.get("current_phase") or "evidence"),
            stage="resumed",
            status="running",
            message="Session resumed from durable event stream",
            actor=actor,
            target=replay.get("target") or {},
            data={"resume_from_seq": replay.get("checkpoint_seq")},
        )
        return {"event": event, "projection": self.replay(session_id)}

    def children(self, session_id: str) -> list[dict[str, Any]]:
        return [item for item in self.sessions(limit=500) if item.get("parent_session_id") == session_id]

    def delete_branch(self, session_id: str, *, actor: str = "operator") -> dict[str, Any]:
        """Tombstone a paused/settled child branch without destroying its audit trail."""
        summary = self.session(session_id, include_deleted=True)
        if not summary:
            raise KeyError(f"session not found: {session_id}")
        if summary.get("status") == "deleted":
            return {"status": "deleted", "session_id": session_id, "already_deleted": True}
        parent_session_id = str(summary.get("parent_session_id") or "")
        if not parent_session_id:
            raise ValueError("root sessions are audit records and cannot be deleted")
        if self.children(session_id):
            raise ValueError("branch has child sessions; delete its leaf branches first")
        if str(summary.get("status") or "").lower() in {"running", "executing", "in_progress"}:
            raise RuntimeError("running branches must be interrupted or settled before deletion")

        target = summary.get("target") or {}
        event = self.append(
            session_id,
            "session/deleted",
            phase=str(summary.get("phase") or "runtime"),
            stage="deleted",
            status="deleted",
            message="Branch hidden from active session views; immutable events retained for audit",
            actor=actor,
            target=target,
            data={"parent_session_id": parent_session_id, "retention": "append-only-tombstone"},
            parent_session_id=parent_session_id,
        )
        self.append(
            parent_session_id,
            "session/child-deleted",
            phase=str(summary.get("phase") or "runtime"),
            stage="child_deleted",
            status=str((self.session(parent_session_id) or {}).get("status") or "recorded"),
            message=f"Child branch {session_id} deleted",
            actor=actor,
            target=target,
            data={"child_session_id": session_id, "retention": "append-only-tombstone"},
        )
        return {
            "status": "deleted",
            "session_id": session_id,
            "parent_session_id": parent_session_id,
            "event": event,
            "audit_retained": True,
        }

    def diagnostics(self) -> dict[str, Any]:
        sessions = self.sessions(limit=500)
        return {
            "schema": EVENT_SCHEMA,
            "path": str(self.path),
            "primary_path": str(self.primary_path),
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "persistent": not bool(self.persistence_error),
            "durability": "process-persistent" if self.fallback_used else "volume-persistent",
            "persistence_error": self.persistence_error,
            "events": len(self._memory),
            "sessions": len(sessions),
            "hash_chained": True,
            "replay": True,
            "fork": True,
            "resume": True,
            "branch_delete": "append-only-tombstone",
            "child_drill_down": True,
            "agent_trace_projection": True,
            "llm_trace_projection": "structured-summary-only",
        }


def _default_path() -> Path:
    configured = os.getenv("HARNESS_EVENT_STORE_PATH", "").strip()
    if configured:
        return Path(configured)
    existing_store = os.getenv("OPS_JOB_STORE_PATH", "").strip() or os.getenv("RELIABILITY_STORE_PATH", "").strip()
    if existing_store:
        return Path(existing_store).parent / "harness-events.jsonl"
    return Path("/tmp/cisre/harness-events.jsonl")


HARNESS_EVENT_STORE = HarnessEventStore(
    _default_path(),
    max_events=int(os.getenv("HARNESS_EVENT_STORE_MAX_EVENTS", "20000")),
)
