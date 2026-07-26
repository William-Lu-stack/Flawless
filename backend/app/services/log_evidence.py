"""Severity-first Kubernetes log evidence extraction.

The operations loop uses this module before requesting broad topology evidence.
It intentionally recognizes both structured severity markers and common fatal
startup messages (for example SQLite/Grafana writable-path failures) that often
do not contain a literal ``ERROR`` token.
"""

from __future__ import annotations

import re
from typing import Any


_ERROR_RE = re.compile(
    r"""(?ix)
    (?:^|[\s\[\]":,|])(?:error|err|fatal|panic|critical|emerg|alert)(?:$|[\s\[\]":,|])
    |traceback\s*\(most\s+recent\s+call\s+last\)
    |\bexception\b
    |\b(?:failed|failure)\b
    |\b(?:unable|cannot|can't|could\s+not)\s+(?:to\s+)?(?:open|create|write|mkdir|mount|attach|bind|start|connect)\b
    |\b(?:permission\s+denied|operation\s+not\s+permitted|read-?only\s+file\s+system)\b
    |\b(?:is\s+not\s+writable|not\s+writable|no\s+space\s+left\s+on\s+device)\b
    |\b(?:database\s+is\s+read-?only|attempt\s+to\s+write\s+a\s+read-?only\s+database)\b
    |\b(?:crashloopbackoff|imagepullbackoff|errimagepull|oomkilled|failedmount|failedscheduling)\b
    """,
)
_WARNING_RE = re.compile(
    r"""(?ix)
    (?:^|[\s\[\]":,|])(?:warn|warning)(?:$|[\s\[\]":,|])
    |\b(?:deprecated|retrying|back-?off|unhealthy|degraded)\b
    """,
)
_STACK_CONTINUATION_RE = re.compile(
    r"""(?x)
    ^\s*(?:File\s+"|at\s+|Caused\s+by:|During\s+handling|[\w.]+(?:Error|Exception):|\^|\.{3})
    """,
)


def _line_severity(line: str) -> str:
    if _ERROR_RE.search(line):
        return "error"
    if _WARNING_RE.search(line):
        return "warning"
    return "info"


def _priority_excerpt(text: str, *, context_lines: int = 2, max_lines: int = 36) -> dict[str, Any]:
    lines = str(text or "").splitlines()
    errors: list[int] = []
    warnings: list[int] = []
    for index, line in enumerate(lines):
        severity = _line_severity(line)
        if severity == "error":
            errors.append(index)
        elif severity == "warning":
            warnings.append(index)

    selected_indexes: set[int] = set()
    for index in errors + warnings:
        selected_indexes.update(range(max(0, index - context_lines), min(len(lines), index + context_lines + 1)))
        cursor = index + 1
        while cursor < len(lines) and cursor <= index + 8 and _STACK_CONTINUATION_RE.search(lines[cursor]):
            selected_indexes.add(cursor)
            cursor += 1

    ordered = sorted(selected_indexes)
    if len(ordered) > max_lines:
        ordered = ordered[-max_lines:]
    priority_lines = [lines[index] for index in ordered if lines[index].strip()]
    fallback_lines = [line for line in lines[-max_lines:] if line.strip()]
    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "priority_excerpt": "\n".join(priority_lines),
        "fallback_excerpt": "\n".join(fallback_lines),
        "actionable": bool(errors),
    }


def triage_kubernetes_logs(logs: dict[str, Any] | None) -> dict[str, Any]:
    """Return ERROR/WARN-first evidence without discarding the full log tail."""
    streams: list[dict[str, Any]] = []
    total_errors = 0
    total_warnings = 0
    for container, content in (logs or {}).items():
        if not isinstance(content, dict):
            content = {"current": str(content or "")}
        for stream in ("current", "previous"):
            text = str(content.get(stream) or "")
            if not text:
                continue
            item = _priority_excerpt(text)
            item.update({"container": str(container), "stream": stream})
            streams.append(item)
            total_errors += int(item["error_count"])
            total_warnings += int(item["warning_count"])

    priority = [
        {
            "container": item["container"],
            "stream": item["stream"],
            "excerpt": item["priority_excerpt"],
            "error_count": item["error_count"],
            "warning_count": item["warning_count"],
        }
        for item in streams
        if item["priority_excerpt"]
    ]
    fallback = [
        {
            "container": item["container"],
            "stream": item["stream"],
            "excerpt": item["fallback_excerpt"],
        }
        for item in streams
        if item["fallback_excerpt"]
    ]
    return {
        "strategy": "error_warning_first",
        "error_count": total_errors,
        "warning_count": total_warnings,
        "actionable": total_errors > 0,
        "priority": priority,
        "fallback": fallback,
    }
