"""Runtime dispatch for built-in executable operations Skills.

Portable/custom Skills describe evidence and allowed actions.  The two built-in
recovery Skills below additionally own a versioned server-side handler.  This
keeps chat, inspection preview and background OpsJobs on the same planner path
without allowing an imported Skill to claim a privileged built-in handler.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable


VOLUME_PERMISSION_SKILL_ID = "skill-volume-permission-recovery"
PVC_PV_SKILL_ID = "skill-storage-pvc-pv"


@dataclass(frozen=True)
class BuiltinSkillHandler:
    skill_id: str
    handler_id: str
    version: str
    continuation_capable: bool


BUILTIN_SKILL_HANDLERS: dict[str, BuiltinSkillHandler] = {
    VOLUME_PERMISSION_SKILL_ID: BuiltinSkillHandler(
        skill_id=VOLUME_PERMISSION_SKILL_ID,
        handler_id="volume-write-permission-recovery",
        version="2.0.0",
        continuation_capable=True,
    ),
    PVC_PV_SKILL_ID: BuiltinSkillHandler(
        skill_id=PVC_PV_SKILL_ID,
        handler_id="pvc-pv-binding-recovery",
        version="2.0.0",
        continuation_capable=True,
    ),
}


DIRECT_WRITE_PERMISSION_TERMS = (
    "permission denied",
    "operation not permitted",
    "read-only file system",
    "read only file system",
    "can't create directory",
    "cannot create directory",
    "failed to create directory",
    "mkdir:",
    "eacces",
    "eperm",
    "权限不足",
    "目录权限",
    "无法创建目录",
)

# Frameworks and databases frequently hide EACCES behind a higher-level error.
# These are treated as a *write-path hypothesis*, not automatic proof that root
# is required.  The Skill must correlate them with mounts/securityContext and
# starts with the least-privileged stage.
INDIRECT_WRITE_PATH_TERMS = (
    "unable to open database file",
    "can't open database file",
    "cannot open database file",
    "could not open database file",
    "attempt to write a readonly database",
    "attempt to write a read-only database",
    "database is read-only",
    "database is readonly",
    "failed to create lock file",
    "unable to create lock file",
    "could not create lock file",
    "failed to open pid file",
    "unable to open pid file",
    "failed to create wal",
    "could not create wal",
    "failed to create temporary file",
    "could not create temporary file",
    "cannot write to data directory",
    "data directory is not writable",
    "storage directory is not writable",
)

DATABASE_BOOTSTRAP_WRITE_TERMS = (
    "unable to open database file",
    "can't open database file",
    "cannot open database file",
    "could not open database file",
    "attempt to write a readonly database",
    "attempt to write a read-only database",
    "database is read-only",
    "database is readonly",
)

CAPACITY_OR_CORRUPTION_TERMS = (
    "no space left on device",
    "disk quota exceeded",
    "filesystem full",
    "database disk image is malformed",
    "database corruption",
    "input/output error",
)


def _flatten_text(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() not in {"token", "password", "secret", "authorization"}:
                    visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item[:100]:
                visit(child, depth + 1)
        elif item is not None:
            parts.append(str(item))

    visit(value)
    return " ".join(parts).lower()


def _pod_and_workload(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    deep = plan.get("_runtime_evidence") or {}
    evidence = plan.get("evidence") or {}
    pod = deep.get("pod") or evidence.get("pod") or {}
    workload = deep.get("workload") or evidence.get("workload") or {}
    return pod if isinstance(pod, dict) else {}, workload if isinstance(workload, dict) else {}


def _mount_paths(pod: dict[str, Any], workload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    containers = list(pod.get("containers") or [])
    pod_template = (((workload.get("spec") or {}).get("template") or {}).get("spec") or {})
    containers.extend(pod_template.get("containers") or [])
    for container in containers:
        if not isinstance(container, dict):
            continue
        for mount in container.get("volume_mounts") or container.get("volumeMounts") or []:
            if not isinstance(mount, dict):
                continue
            path = str(mount.get("mount_path") or mount.get("mountPath") or "").rstrip("/")
            if path and path not in paths:
                paths.append(path)
    return paths


def _security_context_present(pod: dict[str, Any], workload: dict[str, Any]) -> bool:
    if "security_context" in pod or "securityContext" in pod:
        return True
    containers = pod.get("containers") or []
    if any(
        isinstance(item, dict) and ("security_context" in item or "securityContext" in item)
        for item in containers
    ):
        return True
    template_spec = (((workload.get("spec") or {}).get("template") or {}).get("spec") or {})
    return bool(
        template_spec.get("securityContext")
        or any((item or {}).get("securityContext") for item in template_spec.get("containers") or [])
    )


def _candidate_path(text: str) -> str:
    patterns = (
        r"[A-Z][A-Z0-9_]*(?:PATH|PATHS|DATA|DIR)[A-Z0-9_]*\s*=\s*[\"']?"
        r"(/[^\s\"',;]+)[\"']?\s+(?:is\s+)?not\s+writable",
        r"(?:configured\s+)?(?:data\s+)?(?:path|dir(?:ectory)?)\s*[=:]?\s*[\"']?"
        r"(/[^\s\"',;]+)[\"']?\s+(?:is\s+)?not\s+writable",
        r"(?:mkdir(?:\s+-p)?|create\s+(?:a\s+)?directory|open(?:ing)?|database(?:\s+file)?)"
        r"\s*(?::|for|at)?\s*[\"']?(/[^\s\"',;]+)",
        r"(?:path|dir(?:ectory)?|file)\s*[=:]\s*[\"']?(/[^\s\"',;]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return str(match.group(1)).rstrip(").,;:")
    return ""


def _configured_identity(pod: dict[str, Any], workload: dict[str, Any]) -> dict[str, Any]:
    pod_context = pod.get("security_context") or pod.get("securityContext") or {}
    pod_template = (((workload.get("spec") or {}).get("template") or {}).get("spec") or {})
    template_context = pod_template.get("securityContext") or {}
    containers = [
        *(pod.get("containers") or []),
        *(pod_template.get("containers") or []),
    ]
    container_contexts = [
        (item.get("security_context") or item.get("securityContext") or {})
        for item in containers
        if isinstance(item, dict)
    ]

    def first_int(*values: Any) -> int | None:
        for value in values:
            if value is None or isinstance(value, bool):
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    uid = first_int(
        *(item.get("runAsUser") for item in container_contexts),
        pod_context.get("runAsUser"),
        template_context.get("runAsUser"),
    )
    gid = first_int(
        *(item.get("runAsGroup") for item in container_contexts),
        pod_context.get("runAsGroup"),
        template_context.get("runAsGroup"),
        pod_context.get("fsGroup"),
        template_context.get("fsGroup"),
    )
    run_as_non_root = next(
        (
            bool(value)
            for value in [
                *(item.get("runAsNonRoot") for item in container_contexts),
                pod_context.get("runAsNonRoot"),
                template_context.get("runAsNonRoot"),
            ]
            if value is not None
        ),
        None,
    )
    return {
        "run_as_user": uid,
        "run_as_group": gid,
        "run_as_non_root": run_as_non_root,
        "non_root_enforced": bool((uid is not None and uid > 0) or run_as_non_root is True),
    }


def _rank_permission_strategies(
    *,
    explicit_path_error: bool,
    database_bootstrap_error: bool,
    path_on_mount: bool,
    identity: dict[str, Any],
    text: str,
) -> list[dict[str, Any]]:
    """Rank recovery strategies from evidence instead of a fixed stage list."""
    capacity_or_corruption = sorted({
        term for term in CAPACITY_OR_CORRUPTION_TERMS if term in text
    })
    root_squash_risk = any(
        term in text for term in ("root_squash", "rootsquash", "nfs root squash")
    )
    root_score = 0.30
    root_support: list[str] = []
    root_contradictions: list[str] = []
    if explicit_path_error:
        root_score += 0.22
        root_support.append("application_explicitly_reports_configured_path_not_writable")
    if database_bootstrap_error:
        root_score += 0.15
        root_support.append("database_bootstrap_fails_on_same_data_path")
    if path_on_mount:
        root_score += 0.16
        root_support.append("configured_data_path_is_inside_volume_mount")
    if identity.get("non_root_enforced"):
        root_score += 0.14
        root_support.append("workload_security_context_enforces_non_root_identity")
    if root_squash_risk:
        root_score -= 0.30
        root_contradictions.append("storage_reports_root_squash")
    if capacity_or_corruption:
        root_score -= 0.45
        root_contradictions.extend(capacity_or_corruption)

    nonroot_score = 0.46
    nonroot_support: list[str] = []
    if identity.get("non_root_enforced"):
        nonroot_score += 0.13
        nonroot_support.append("known_non_root_uid_gid")
    if path_on_mount:
        nonroot_score += 0.10
        nonroot_support.append("volume_mount_can_receive_fs_group")
    if root_squash_risk:
        nonroot_score += 0.16
        nonroot_support.append("root_squash_favors_storage_supported_group_ownership")
    if explicit_path_error and database_bootstrap_error:
        nonroot_score -= 0.04

    candidates = [
        {
            "strategy_id": "root_workload_security_context",
            "confidence": round(max(0.0, min(root_score, 0.99)), 4),
            "hypothesis": "当前非 root 身份无法写入已挂载的数据目录，需把 Pod 与故障容器运行身份提升为 UID/GID 0。",
            "supporting_evidence": root_support,
            "contradicting_evidence": root_contradictions,
            "requires_human_approval": True,
        },
        {
            "strategy_id": "nonroot_group_ownership",
            "confidence": round(max(0.0, min(nonroot_score, 0.95)), 4),
            "hypothesis": "卷支持 fsGroup 或目录属组修复，可保持业务容器非 root。",
            "supporting_evidence": nonroot_support,
            "contradicting_evidence": (
                ["application_still_reports_exact_data_path_not_writable"]
                if explicit_path_error and database_bootstrap_error else []
            ),
            "requires_human_approval": True,
        },
        {
            "strategy_id": "path_capacity_or_database_integrity",
            "confidence": round(0.90 if capacity_or_corruption else 0.28, 4),
            "hypothesis": "目录容量、I/O 或数据库损坏导致打开失败，而非容器身份权限。",
            "supporting_evidence": capacity_or_corruption,
            "contradicting_evidence": (
                ["configured_path_explicitly_not_writable"]
                if explicit_path_error and not capacity_or_corruption else []
            ),
            "requires_human_approval": False,
        },
    ]
    candidates.sort(key=lambda item: -float(item["confidence"]))
    return candidates


def classify_volume_write_failure(plan: dict[str, Any], summary_text: str = "") -> dict[str, Any]:
    """Return a transparent root-cause hypothesis for volume/path write errors."""
    deep = plan.get("_runtime_evidence") or {}
    evidence = plan.get("evidence") or {}
    text = _flatten_text({
        "summary": summary_text,
        "plan_summary": plan.get("summary"),
        "reason": plan.get("reason"),
        "state": evidence.get("state_text"),
        "logs": deep.get("logs") or evidence.get("logs"),
        "events": deep.get("events") or evidence.get("events"),
        "storage": deep.get("storage") or evidence.get("storage"),
        "pod": deep.get("pod") or evidence.get("pod"),
        "workload": deep.get("workload") or evidence.get("workload"),
    })
    direct = sorted({term for term in DIRECT_WRITE_PERMISSION_TERMS if term in text})
    indirect = sorted({term for term in INDIRECT_WRITE_PATH_TERMS if term in text})
    explicit_path_error = bool(re.search(
        r"(?:[A-Z][A-Z0-9_]*(?:PATH|PATHS|DATA|DIR)[A-Z0-9_]*\s*=\s*[\"']?/"
        r"|(?:path|dir(?:ectory)?)\s*[=:]?\s*[\"']?/)[^\n]{0,240}?"
        r"(?:is\s+)?not\s+writable",
        text,
        flags=re.I,
    ))
    if explicit_path_error:
        direct.append("configured_path_is_not_writable")
        direct = sorted(set(direct))
    database_bootstrap_error = any(term in text for term in DATABASE_BOOTSTRAP_WRITE_TERMS)
    pod, workload = _pod_and_workload(plan)
    mounts = _mount_paths(pod, workload)
    path = _candidate_path(text)
    path_on_mount = bool(
        path
        and any(path == mount or path.startswith(f"{mount}/") for mount in mounts)
    )
    has_storage = bool(
        deep.get("storage")
        or evidence.get("storage")
        or mounts
        or any(term in text for term in ("pvc", "persistentvolumeclaim", "volume", "mount"))
    )
    has_security_context = _security_context_present(pod, workload)
    identity = _configured_identity(pod, workload)
    corroboration = []
    if mounts:
        corroboration.append("container_volume_mount")
    if path_on_mount:
        corroboration.append("failing_path_inside_mount")
    if has_security_context:
        corroboration.append("runtime_security_context")
    if deep.get("storage") or evidence.get("storage"):
        corroboration.append("storage_chain")

    detected = bool(direct or indirect)
    if explicit_path_error and path_on_mount:
        confidence = 0.99 if database_bootstrap_error and has_security_context else 0.94
        signal_class = "explicit_mounted_path_not_writable"
    elif direct:
        confidence = 0.96 if has_storage or has_security_context else 0.84
        signal_class = "direct_permission_error"
    elif indirect:
        # An application-level error can also mean a missing parent directory,
        # bad path or corrupt DB.  Keep it as a strong candidate only when the
        # Kubernetes configuration supplies corroborating write-path evidence.
        confidence = 0.88 if path_on_mount else 0.78 if mounts and has_security_context else 0.68 if mounts else 0.56
        signal_class = "indirect_write_path_error"
    else:
        confidence = 0.0
        signal_class = "none"
    strategy_candidates = _rank_permission_strategies(
        explicit_path_error=explicit_path_error,
        database_bootstrap_error=database_bootstrap_error,
        path_on_mount=path_on_mount,
        identity=identity,
        text=text,
    ) if detected else []
    recommended_strategy = (
        str((strategy_candidates[0] or {}).get("strategy_id") or "")
        if strategy_candidates else ""
    )
    return {
        "detected": detected,
        "confidence": confidence,
        "signal_class": signal_class,
        "matched_signals": direct or indirect,
        "direct_signals": direct,
        "indirect_signals": indirect,
        "candidate_path": path,
        "mount_paths": mounts,
        "path_on_mount": path_on_mount,
        "corroboration": corroboration,
        "explicit_path_error": explicit_path_error,
        "database_bootstrap_error": database_bootstrap_error,
        "configured_identity": identity,
        "strategy_candidates": strategy_candidates,
        "recommended_strategy": recommended_strategy,
        "requires_write_probe": bool(indirect and confidence < 0.72),
    }


class ExecutableOpsSkillRuntime:
    """Dispatch a selected built-in Skill to its versioned planning handler."""

    engine = "UnifiedExecutableOpsSkillRuntime/v1"

    @staticmethod
    def descriptor(skill_id: str) -> BuiltinSkillHandler | None:
        return BUILTIN_SKILL_HANDLERS.get(str(skill_id or ""))

    def is_executable(self, skill_id: str) -> bool:
        return self.descriptor(skill_id) is not None

    def continuation_capable(self, skill_id: str) -> bool:
        descriptor = self.descriptor(skill_id)
        return bool(descriptor and descriptor.continuation_capable)

    def materialize(
        self,
        skill_id: str,
        plan: dict[str, Any],
        signal: dict[str, Any],
        handlers: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]],
    ) -> dict[str, Any] | None:
        descriptor = self.descriptor(skill_id)
        if not descriptor:
            return None
        handler = handlers.get(descriptor.handler_id)
        if not handler:
            raise RuntimeError(f"built-in Skill handler is not registered: {descriptor.handler_id}")
        generated = handler(deepcopy(plan), deepcopy(signal))
        if not generated:
            return None
        generated = deepcopy(generated)
        generated["selected_skill_id"] = descriptor.skill_id
        generated["skill_runtime"] = {
            "engine": self.engine,
            "handler_id": descriptor.handler_id,
            "handler_version": descriptor.version,
            "continuation_capable": descriptor.continuation_capable,
            "source": "executable_builtin_skill",
        }
        generated["skill_execution_mode"] = "executable_skill_serial"
        generated["change_source"] = "executable_skill"
        generated["planning_engine"] = self.engine
        for change in generated.get("changes") or []:
            if isinstance(change, dict):
                change["skill_id"] = descriptor.skill_id
                change["selection_source"] = "executable_skill_handler"
        return generated


OPS_SKILL_RUNTIME = ExecutableOpsSkillRuntime()


def public_runtime_catalog() -> list[dict[str, Any]]:
    return [
        {
            "skill_id": item.skill_id,
            "handler_id": item.handler_id,
            "version": item.version,
            "continuation_capable": item.continuation_capable,
        }
        for item in BUILTIN_SKILL_HANDLERS.values()
    ]
