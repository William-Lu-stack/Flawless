"""Composable Harness primitives for the CISRE operations control plane.

The design intentionally follows the public contracts of DeepSeek Harness:
plugins provide scoped services, subscribe to typed events and own reversible
effects.  CISRE keeps this implementation in-process because its Kubernetes
executor, approval ledger and recovery verifier are already production assets;
the upstream runtime is still an optional planner backend, never a mutation
authority.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


Listener = Callable[..., Any]
Disposer = Callable[[], Any]


class HarnessPolicyDenied(RuntimeError):
    """Raised when a monotonic Harness guard rejects a tool request."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Stable, serialisable description of one Harness plugin."""

    id: str
    version: str = "1.0.0"
    description: str = ""
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    scope: str = "global"
    priority: int = 0
    source: str = "cisre"
    permissions: tuple[str, ...] = ()
    runtime_type: str = "builtin"
    category: str = "shared"
    domains: tuple[str, ...] = ("common",)
    agents: tuple[str, ...] = ("all",)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "provides": list(self.provides),
            "requires": list(self.requires),
            "events": list(self.events),
            "scope": self.scope,
            "priority": self.priority,
            "source": self.source,
            "permissions": list(self.permissions),
            "runtime_type": self.runtime_type,
            "category": self.category,
            "domains": list(self.domains),
            "agents": list(self.agents),
        }


@dataclass(slots=True)
class _Binding:
    plugin_id: str
    scope: str
    value: Any
    priority: int
    sequence: int


@dataclass(slots=True)
class _EventBinding:
    plugin_id: str
    scope: str
    callback: Listener
    mode: str
    priority: int
    sequence: int


@dataclass(slots=True)
class _Registration:
    manifest: PluginManifest
    setup: Callable[["PluginContext"], Any]
    status: str = "pending"
    error: str = ""
    missing: list[str] = field(default_factory=list)
    disposers: list[Disposer] = field(default_factory=list)
    loaded_at: str = ""


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class PluginContext:
    """Lifecycle-bound registration surface handed to a plugin setup hook."""

    def __init__(self, runtime: "HarnessPluginRuntime", registration: _Registration):
        self.runtime = runtime
        self.registration = registration

    @property
    def manifest(self) -> PluginManifest:
        return self.registration.manifest

    def provide(
        self,
        name: str,
        value: Any,
        *,
        scope: str | None = None,
        priority: int | None = None,
    ) -> Disposer:
        disposer = self.runtime._provide(
            self.manifest.id,
            name,
            value,
            scope=scope or self.manifest.scope,
            priority=self.manifest.priority if priority is None else priority,
        )
        self.registration.disposers.append(disposer)
        return disposer

    def on(
        self,
        event: str,
        callback: Listener,
        *,
        mode: str = "observe",
        scope: str | None = None,
        priority: int | None = None,
    ) -> Disposer:
        disposer = self.runtime._listen(
            self.manifest.id,
            event,
            callback,
            mode=mode,
            scope=scope or self.manifest.scope,
            priority=self.manifest.priority if priority is None else priority,
        )
        self.registration.disposers.append(disposer)
        return disposer

    def effect(self, disposer: Disposer) -> None:
        self.registration.disposers.append(disposer)

    def resolve(self, name: str, scopes: tuple[str, ...] = ()) -> Any:
        return self.runtime.resolve(name, scopes=scopes)


class HarnessPluginRuntime:
    """Small Cordis-style runtime with scoped DI and reversible effects."""

    MODES = {"observe", "serial", "parallel", "waterfall"}

    def __init__(self, *, runtime_id: str = "CISREPluginHarness/v1") -> None:
        self.runtime_id = runtime_id
        self._sequence = 0
        self._plugins: dict[str, _Registration] = {}
        self._services: dict[str, list[_Binding]] = {}
        self._events: dict[str, list[_EventBinding]] = {}

    def mount(
        self,
        manifest: PluginManifest,
        setup: Callable[[PluginContext], Any],
    ) -> Disposer:
        if not manifest.id.strip():
            raise ValueError("plugin id is required")
        if manifest.id in self._plugins:
            raise ValueError(f"plugin already mounted: {manifest.id}")
        self._plugins[manifest.id] = _Registration(manifest=manifest, setup=setup)
        self._reconcile()
        return lambda: self.unmount(manifest.id)

    def _reconcile(self) -> None:
        """Activate every dependency-complete plugin in deterministic order."""
        progressed = True
        while progressed:
            progressed = False
            registrations = sorted(
                self._plugins.values(),
                key=lambda item: (item.manifest.priority, item.manifest.id),
                reverse=True,
            )
            for registration in registrations:
                if registration.status == "active":
                    continue
                missing = [
                    service
                    for service in registration.manifest.requires
                    if not self.has_service(service, scopes=(registration.manifest.scope,))
                ]
                registration.missing = missing
                if missing:
                    registration.status = "pending_dependencies"
                    continue
                registration.status = "loading"
                registration.error = ""
                try:
                    result = registration.setup(PluginContext(self, registration))
                    if inspect.isawaitable(result):
                        raise TypeError("plugin setup must be synchronous; event callbacks may be async")
                    registration.status = "active"
                    registration.loaded_at = datetime.now(timezone.utc).isoformat()
                    registration.missing = []
                    progressed = True
                except Exception as exc:
                    self._dispose_registration(registration)
                    registration.status = "failed"
                    registration.error = f"{type(exc).__name__}: {exc}"[:500]

    def unmount(self, plugin_id: str) -> bool:
        registration = self._plugins.get(plugin_id)
        if registration is None:
            return False
        provided = set(registration.manifest.provides)
        dependents = [
            item for item in self._plugins.values()
            if item.manifest.id != plugin_id
            and item.status == "active"
            and provided.intersection(item.manifest.requires)
        ]
        for dependent in dependents:
            self._dispose_registration(dependent)
            dependent.status = "pending_dependencies"
        self._dispose_registration(registration)
        del self._plugins[plugin_id]
        self._reconcile()
        return True

    def _dispose_registration(self, registration: _Registration) -> None:
        for disposer in reversed(registration.disposers):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    # Lifecycle teardown is deliberately synchronous. Async
                    # resources must register a synchronous cancellation hook.
                    result.close()
            except Exception:
                continue
        registration.disposers.clear()

    def _provide(
        self,
        plugin_id: str,
        name: str,
        value: Any,
        *,
        scope: str,
        priority: int,
    ) -> Disposer:
        self._sequence += 1
        binding = _Binding(plugin_id, scope, value, priority, self._sequence)
        self._services.setdefault(name, []).append(binding)

        def dispose() -> None:
            values = self._services.get(name) or []
            if binding in values:
                values.remove(binding)
            if not values:
                self._services.pop(name, None)

        return dispose

    def _listen(
        self,
        plugin_id: str,
        event: str,
        callback: Listener,
        *,
        mode: str,
        scope: str,
        priority: int,
    ) -> Disposer:
        if mode not in self.MODES:
            raise ValueError(f"unsupported event mode: {mode}")
        existing_modes = {item.mode for item in self._events.get(event, [])}
        if existing_modes and mode not in existing_modes:
            raise ValueError(
                f"event {event} already uses {sorted(existing_modes)[0]}, cannot register {mode}"
            )
        self._sequence += 1
        binding = _EventBinding(plugin_id, scope, callback, mode, priority, self._sequence)
        self._events.setdefault(event, []).append(binding)

        def dispose() -> None:
            values = self._events.get(event) or []
            if binding in values:
                values.remove(binding)
            if not values:
                self._events.pop(event, None)

        return dispose

    @staticmethod
    def _scope_order(scopes: tuple[str, ...]) -> list[str]:
        ordered = [scope for scope in scopes if scope and scope != "global"]
        ordered.append("global")
        return list(dict.fromkeys(ordered))

    def resolve(self, name: str, *, scopes: tuple[str, ...] = ()) -> Any:
        bindings = self._services.get(name) or []
        scope_order = self._scope_order(scopes)
        candidates = [item for item in bindings if item.scope in scope_order]
        if not candidates:
            raise KeyError(f"service not available: {name}")
        rank = {scope: index for index, scope in enumerate(scope_order)}
        candidates.sort(key=lambda item: (rank[item.scope], -item.priority, -item.sequence))
        return candidates[0].value

    def has_service(self, name: str, *, scopes: tuple[str, ...] = ()) -> bool:
        try:
            self.resolve(name, scopes=scopes)
            return True
        except KeyError:
            return False

    def _event_bindings(self, event: str, scopes: tuple[str, ...]) -> list[_EventBinding]:
        order = self._scope_order(scopes)
        rank = {scope: index for index, scope in enumerate(order)}
        bindings = [item for item in self._events.get(event, []) if item.scope in rank]
        bindings.sort(key=lambda item: (rank[item.scope], -item.priority, item.sequence))
        return bindings

    async def dispatch(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        scopes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        current = copy.deepcopy(payload or {})
        bindings = self._event_bindings(event, scopes)
        if not bindings:
            return current
        mode = bindings[0].mode
        if mode == "parallel":
            await asyncio.gather(*(_resolve(item.callback(copy.deepcopy(current))) for item in bindings))
            return current
        if mode == "observe":
            for item in bindings:
                await _resolve(item.callback(copy.deepcopy(current)))
            return current
        if mode == "serial":
            for item in bindings:
                result = await _resolve(item.callback(current))
                if isinstance(result, dict):
                    current = result
            return current

        async def run(index: int, value: dict[str, Any]) -> dict[str, Any]:
            if index >= len(bindings):
                return value

            async def next_handler(updated: dict[str, Any] | None = None) -> dict[str, Any]:
                return await run(index + 1, updated if isinstance(updated, dict) else value)

            result = await _resolve(bindings[index].callback(value, next_handler))
            return result if isinstance(result, dict) else value

        return await run(0, current)

    def diagnostics(self) -> dict[str, Any]:
        plugins = []
        for registration in sorted(self._plugins.values(), key=lambda item: item.manifest.id):
            item = registration.manifest.public()
            item.update({
                "status": registration.status,
                "missing_dependencies": list(registration.missing),
                "error": registration.error,
                "loaded_at": registration.loaded_at,
            })
            plugins.append(item)
        return {
            "runtime": self.runtime_id,
            "architecture": "everything-is-a-plugin",
            "plugins": plugins,
            "summary": {
                "total": len(plugins),
                "active": sum(item["status"] == "active" for item in plugins),
                "pending": sum(item["status"] == "pending_dependencies" for item in plugins),
                "failed": sum(item["status"] == "failed" for item in plugins),
                "services": sum(len(values) for values in self._services.values()),
                "event_subscriptions": sum(len(values) for values in self._events.values()),
            },
            "service_bindings": {
                name: [
                    {
                        "plugin_id": item.plugin_id,
                        "scope": item.scope,
                        "priority": item.priority,
                        "selected_globally": bool(
                            values and sorted(values, key=lambda value: (-value.priority, -value.sequence))[0] is item
                        ),
                    }
                    for item in values
                ]
                for name, values in sorted(self._services.items())
            },
        }


def _descriptor_setup(manifest: PluginManifest) -> Callable[[PluginContext], None]:
    def setup(context: PluginContext) -> None:
        for service in manifest.provides:
            context.provide(service, {
                "plugin_id": manifest.id,
                "version": manifest.version,
                "scope": manifest.scope,
            })
    return setup


def _approval_setup(manifest: PluginManifest) -> Callable[[PluginContext], None]:
    async def approval_gate(payload: dict[str, Any], next_handler: Listener) -> dict[str, Any]:
        mutation = bool(payload.get("mutation", True))
        if mutation and payload.get("approved") is not True:
            raise HarnessPolicyDenied("mutation requires a current human approval receipt")
        receipts = list(payload.get("guard_receipts") or [])
        receipts.append({
            "guard": manifest.id,
            "decision": "allow",
            "approved": payload.get("approved") is True,
        })
        return await next_handler({**payload, "guard_receipts": receipts})

    def setup(context: PluginContext) -> None:
        for service in manifest.provides:
            context.provide(service, {"plugin_id": manifest.id, "version": manifest.version})
        context.on("tools/pre-execute", approval_gate, mode="waterfall", priority=1000)

    return setup


def _target_guard_setup(manifest: PluginManifest) -> Callable[[PluginContext], None]:
    async def target_guard(payload: dict[str, Any], next_handler: Listener) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip()
        if payload.get("mutation", True) and not action:
            raise HarnessPolicyDenied("mutation action is missing")
        receipts = list(payload.get("guard_receipts") or [])
        receipts.append({"guard": manifest.id, "decision": "allow", "action": action})
        return await next_handler({**payload, "guard_receipts": receipts})

    def setup(context: PluginContext) -> None:
        for service in manifest.provides:
            context.provide(service, {"plugin_id": manifest.id, "version": manifest.version})
        context.on("tools/pre-execute", target_guard, mode="waterfall", priority=900)

    return setup


BUILTIN_PLUGIN_MANIFESTS = (
    PluginManifest(
        id="cisre.session-events",
        description="Append-only typed operation events and deterministic replay metadata.",
        provides=("session.events",),
        events=("session/event",),
    ),
    PluginManifest(
        id="cisre.context-compaction",
        description="Importance-aware bounded context for long incident chains.",
        provides=("context.compactor",),
        requires=("session.events",),
        events=("context/compact",),
    ),
    PluginManifest(
        id="cisre.deepseek-planner",
        description="DeepSeek/OpenAI-compatible structured root-cause planner.",
        provides=("planner.deepseek-compatible",),
        requires=("context.compactor",),
        events=("llm/request", "llm/response"),
    ),
    PluginManifest(
        id="cisre.skill-router",
        description="Evidence-ranked primary Skill selection with dependency-gated chaining.",
        provides=("skill.router",),
        requires=("planner.deepseek-compatible",),
        events=("skills/discover", "skills/selected"),
    ),
    PluginManifest(
        id="cisre.approval-gate",
        description="Fail-closed per-change human approval policy.",
        provides=("policy.approval",),
        requires=("skill.router",),
        events=("tools/pre-execute",),
        priority=1000,
    ),
    PluginManifest(
        id="cisre.execution-target-guard",
        description="Monotonic action/target and execution boundary guard.",
        provides=("policy.execution-target",),
        requires=("policy.approval",),
        events=("tools/pre-execute",),
        priority=900,
    ),
    PluginManifest(
        id="cisre.kubernetes-executor",
        description="Rancher or encrypted-kubeconfig Kubernetes mutation provider.",
        provides=("tool.executor.kubernetes",),
        requires=("policy.execution-target",),
        events=("tools/execute", "tools/result"),
        category="domain",
        domains=("kubernetes",),
        agents=("kubernetes",),
    ),
    PluginManifest(
        id="cisre.read-after-write",
        description="Same-transport exact patch postcondition verifier.",
        provides=("verifier.mutation",),
        requires=("tool.executor.kubernetes",),
        events=("mutation/readback",),
        category="domain",
        domains=("kubernetes",),
        agents=("kubernetes",),
    ),
    PluginManifest(
        id="cisre.recovery-verifier",
        description="Fresh Pod rollout, Ready, restart and error stability verifier.",
        provides=("verifier.recovery",),
        requires=("verifier.mutation",),
        events=("recovery/check", "recovery/complete"),
        category="domain",
        domains=("kubernetes",),
        agents=("kubernetes",),
    ),
    PluginManifest(
        id="cisre.goal-round-driver",
        description="Durable diagnose-change-verify continuation until recovery or intervention.",
        provides=("goal.driver",),
        requires=("verifier.recovery",),
        events=("goal/round", "goal/settled"),
    ),
    PluginManifest(
        id="cisre.owner-scoped-jobs",
        description="Owned background jobs with wait, cancellation and output bounds.",
        provides=("jobs.owner-scoped",),
        requires=("goal.driver",),
        events=("job/start", "job/end"),
    ),
    PluginManifest(
        id="cisre.telemetry",
        description="Langfuse/OpenTelemetry-compatible traces without secret payloads.",
        provides=("telemetry.ops",),
        requires=("session.events",),
        events=("session/event", "tools/result", "recovery/complete"),
    ),
    PluginManifest(
        id="cisre.agent-trace",
        description="Secret-safe context, model decision, Skill, plugin, tool, approval and verification trace projection.",
        provides=("telemetry.agent-trace", "telemetry.llm-trace"),
        requires=("session.events", "telemetry.ops"),
        events=("session/event", "llm/response", "tools/result", "recovery/complete"),
    ),
    PluginManifest(
        id="cisre.agent-loop",
        description="Bounded evidence-plan-act-verify loop with deterministic completion and stuck-trajectory switching.",
        provides=("agent.loop",),
        requires=("context.compactor", "planner.deepseek-compatible", "skill.router", "goal.driver"),
        events=("agent/round-start", "agent/decision", "agent/round-end"),
    ),
    PluginManifest(
        id="cisre.agent-orchestrator",
        description="Owner-scoped sequential or parallel specialist delegation with cancellation and output bounds.",
        provides=("agent.orchestration",),
        requires=("agent.loop", "jobs.owner-scoped"),
        events=("agent/delegate", "agent/join", "agent/cancel"),
    ),
    PluginManifest(
        id="cisre.agent.kubernetes",
        description="Kubernetes domain Agent composed from shared SRE capabilities and Kubernetes-only providers.",
        provides=("agent.domain.kubernetes",),
        requires=("agent.loop", "agent.orchestration", "tool.executor.kubernetes", "verifier.recovery"),
        events=("agent/kubernetes/start", "agent/kubernetes/end"),
        category="domain-agent",
        domains=("kubernetes",),
        agents=("kubernetes",),
    ),
    PluginManifest(
        id="cisre.agent.database",
        description="Database domain Agent activated when inventory, evidence and verification providers are installed.",
        provides=("agent.domain.database",),
        requires=("agent.loop", "agent.orchestration", "inventory.database", "evidence.database", "verification.database"),
        events=("agent/database/start", "agent/database/end"),
        category="domain-agent",
        domains=("database",),
        agents=("database",),
    ),
    PluginManifest(
        id="cisre.agent.virtual-machine",
        description="VM and host domain Agent activated by virtual-machine provider contracts.",
        provides=("agent.domain.virtual-machine",),
        requires=("agent.loop", "agent.orchestration", "inventory.virtual-machine", "evidence.virtual-machine", "verification.virtual-machine"),
        events=("agent/virtual-machine/start", "agent/virtual-machine/end"),
        category="domain-agent",
        domains=("virtual-machine",),
        agents=("virtual-machine",),
    ),
    PluginManifest(
        id="cisre.agent.storage",
        description="Storage domain Agent activated by storage inventory, evidence and verification providers.",
        provides=("agent.domain.storage",),
        requires=("agent.loop", "agent.orchestration", "inventory.storage", "evidence.storage", "verification.storage"),
        events=("agent/storage/start", "agent/storage/end"),
        category="domain-agent",
        domains=("storage",),
        agents=("storage",),
    ),
    PluginManifest(
        id="cisre.agent.middleware",
        description="Middleware domain Agent activated by middleware provider contracts.",
        provides=("agent.domain.middleware",),
        requires=("agent.loop", "agent.orchestration", "inventory.middleware", "evidence.middleware", "verification.middleware"),
        events=("agent/middleware/start", "agent/middleware/end"),
        category="domain-agent",
        domains=("middleware",),
        agents=("middleware",),
    ),
    PluginManifest(
        id="cisre.agent.cloud",
        description="Cloud resource domain Agent activated by cloud inventory, evidence and verification providers.",
        provides=("agent.domain.cloud",),
        requires=("agent.loop", "agent.orchestration", "inventory.cloud", "evidence.cloud", "verification.cloud"),
        events=("agent/cloud/start", "agent/cloud/end"),
        category="domain-agent",
        domains=("cloud",),
        agents=("cloud",),
    ),
    PluginManifest(
        id="cisre.agent.network",
        description="Network domain Agent activated by network inventory, evidence and verification providers.",
        provides=("agent.domain.network",),
        requires=("agent.loop", "agent.orchestration", "inventory.network", "evidence.network", "verification.network"),
        events=("agent/network/start", "agent/network/end"),
        category="domain-agent",
        domains=("network",),
        agents=("network",),
    ),
)


def build_cisre_harness_runtime() -> HarnessPluginRuntime:
    runtime = HarnessPluginRuntime()
    for manifest in BUILTIN_PLUGIN_MANIFESTS:
        if manifest.id == "cisre.approval-gate":
            setup = _approval_setup(manifest)
        elif manifest.id == "cisre.execution-target-guard":
            setup = _target_guard_setup(manifest)
        else:
            setup = _descriptor_setup(manifest)
        runtime.mount(manifest, setup)
    return runtime


CISRE_HARNESS_RUNTIME = build_cisre_harness_runtime()


async def guard_execution_request(
    change: dict[str, Any],
    plan: dict[str, Any],
    *,
    approved: bool,
) -> dict[str, Any]:
    """Run the fail-closed mutation request through mounted waterfall guards."""
    scopes = tuple(filter(None, (
        f"job:{plan.get('job_id')}" if plan.get("job_id") else "",
        f"cluster:{plan.get('cluster_id')}" if plan.get("cluster_id") else "",
    )))
    payload = {
        "action": str(change.get("type") or ""),
        "target": str(
            change.get("workload_name")
            or change.get("name")
            or change.get("pod_name")
            or change.get("resource_id")
            or plan.get("target")
            or ""
        ),
        "namespace": str(change.get("namespace") or plan.get("namespace") or "default"),
        "cluster_id": str(plan.get("cluster_id") or change.get("cluster_id") or "local"),
        "mutation": True,
        "approved": approved,
        "guard_receipts": [],
    }
    return await CISRE_HARNESS_RUNTIME.dispatch(
        "tools/pre-execute",
        payload,
        scopes=scopes,
    )


_PRIORITY_KEYS = {
    "error", "errors", "fatal", "warning", "warnings", "reason", "message",
    "current", "previous", "laststate", "state", "status", "conditions",
    "securitycontext", "volumes", "volumemounts", "pvc", "pv", "storage",
    "events", "log_triage", "workload", "pod", "pods", "containers",
}


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        if isinstance(value, (dict, list)):
            return "<depth-bounded>"
        return value
    if isinstance(value, str):
        lowered = value.lower()
        important = any(token in lowered for token in (
            "error", "fatal", "panic", "warning", "failed", "denied", "unable",
            "crashloop", "oom", "unbound", "not ready", "read-only",
        ))
        limit = 6000 if important else 1800
        return value if len(value) <= limit else value[:limit] + "…<truncated>"
    if isinstance(value, dict):
        items = sorted(
            value.items(),
            key=lambda item: (str(item[0]).lower() not in _PRIORITY_KEYS, str(item[0])),
        )
        return {
            str(key): _compact_value(item, depth=depth + 1)
            for key, item in items[:48]
        }
    if isinstance(value, list):
        important = [
            item for item in value
            if any(token in json.dumps(item, ensure_ascii=False, default=str).lower() for token in (
                "error", "fatal", "warning", "failed", "denied", "unable", "crashloop", "oom",
            ))
        ]
        selected = (important + value)[-20:]
        unique: list[Any] = []
        seen: set[str] = set()
        for item in selected:
            digest = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            unique.append(_compact_value(item, depth=depth + 1))
        return unique
    return value


def _hard_compact(value: Any, *, depth: int = 0) -> Any:
    """Second-pass compaction that still preserves root-cause-bearing fields."""
    if depth >= 5:
        return "<depth-bounded>" if isinstance(value, (dict, list)) else value
    if isinstance(value, str):
        important = any(token in value.lower() for token in (
            "error", "fatal", "warning", "failed", "denied", "unable", "crashloop",
            "oom", "read-only", "not ready",
        ))
        limit = 1200 if important else 350
        return value if len(value) <= limit else value[:limit] + "…<bounded>"
    if isinstance(value, dict):
        items = sorted(
            value.items(),
            key=lambda item: (str(item[0]).lower() not in _PRIORITY_KEYS, str(item[0])),
        )
        return {
            str(key): _hard_compact(item, depth=depth + 1)
            for key, item in items[:24]
        }
    if isinstance(value, list):
        important = [
            item for item in value
            if any(token in json.dumps(item, ensure_ascii=False, default=str).lower() for token in (
                "error", "fatal", "warning", "failed", "denied", "unable", "crashloop", "oom",
            ))
        ]
        return [_hard_compact(item, depth=depth + 1) for item in (important + value)[-8:]]
    return value


def _priority_error_strings(value: Any, output: list[str] | None = None) -> list[str]:
    output = output if output is not None else []
    if len(output) >= 8:
        return output
    if isinstance(value, str):
        if any(token in value.lower() for token in (
            "error", "fatal", "panic", "warning", "failed", "denied", "unable",
            "crashloop", "oom", "read-only", "not ready",
        )):
            output.append(value[:700])
        return output
    if isinstance(value, dict):
        for item in value.values():
            _priority_error_strings(item, output)
            if len(output) >= 8:
                break
    elif isinstance(value, list):
        for item in value:
            _priority_error_strings(item, output)
            if len(output) >= 8:
                break
    return output


def compact_planner_context(
    *,
    plan: dict[str, Any],
    evidence: dict[str, Any],
    failure: dict[str, Any],
    attempted_actions: set[str] | None = None,
    blocked_fingerprints: set[str] | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Build an auditable, bounded prompt context without dropping direct errors."""
    budget = max(8000, min(max_chars or int(os.getenv("OPS_PLANNER_CONTEXT_MAX_CHARS", "36000")), 90000))
    source = {
        "target": {
            "cluster_id": plan.get("cluster_id") or plan.get("cluster") or "local",
            "namespace": plan.get("namespace") or "default",
            "resource": plan.get("target") or "",
        },
        "objective": plan.get("summary") or plan.get("title") or "",
        "attempted_actions": sorted(attempted_actions or set()),
        "blocked_change_fingerprints": sorted(blocked_fingerprints or set()),
        "prior_attempts": (plan.get("_prior_attempts") or [])[-6:],
        "last_failure": failure,
        "plan": {key: value for key, value in plan.items() if not str(key).startswith("_")},
        "evidence": evidence,
    }
    compacted = _compact_value(source)
    encoded = json.dumps(compacted, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > budget:
        # Keep direct evidence and failure first. Lower-value historical context
        # is reduced before the only root-cause-bearing data is touched.
        compacted["prior_attempts"] = _compact_value((source["prior_attempts"] or [])[-2:])
        compacted["plan"] = _compact_value({
            key: source["plan"].get(key)
            for key in ("id", "title", "summary", "target", "namespace", "cluster_id", "selected_skill_id")
            if source["plan"].get(key) is not None
        })
        encoded = json.dumps(compacted, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > budget:
        compacted["evidence"] = _compact_value({
            key: evidence.get(key)
            for key in (
                "log_triage", "logs", "pod", "workload", "events", "storage",
                "pvc", "pv", "services", "nodes", "dependency_topology",
            )
            if evidence.get(key) is not None
        })
        encoded = json.dumps(compacted, ensure_ascii=False, sort_keys=True, default=str)
    compacted["_compaction"] = {
        "engine": "CISREImportanceCompactor/v1",
        "budget_chars": budget,
        "output_chars": min(len(encoded), budget),
        "source_digest": hashlib.sha256(
            json.dumps(source, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20],
        "truncated": len(encoded) > budget,
    }
    if len(encoded) > budget:
        # A JSON string slice would corrupt the contract and may cut away the
        # error itself. Apply a harder structured pass to direct evidence.
        compacted["evidence"] = _hard_compact({
            key: evidence.get(key)
            for key in (
                "log_triage", "logs", "pod", "workload", "events", "storage",
                "pvc", "pv", "services", "nodes",
            )
            if evidence.get(key) is not None
        })
        compacted["last_failure"] = _hard_compact(failure)
        compacted["prior_attempts"] = []
        compacted["_compaction"]["direct_evidence_preserved"] = True
        encoded = json.dumps(compacted, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) > budget:
            compacted["evidence"] = {
                "priority_errors": _priority_error_strings(evidence),
                "direct_evidence_preserved": True,
                "note": "non-error bulk evidence omitted by the final context budget",
            }
            compacted["last_failure"] = {
                "priority_errors": _priority_error_strings(failure),
            }
            compacted["plan"] = {
                "target": source["target"],
                "objective": source["objective"],
            }
            encoded = json.dumps(compacted, ensure_ascii=False, sort_keys=True, default=str)
    compacted["_compaction"]["output_chars"] = min(len(encoded), budget)
    return compacted


def official_deepseek_harness_status() -> dict[str, Any]:
    """Report the optional out-of-process official dsh planner boundary."""
    enabled = os.getenv("DEEPSEEK_HARNESS_UPSTREAM_ENABLED", "false").lower() in {
        "1", "true", "yes", "on",
    }
    config_path = os.getenv("DEEPSEEK_HARNESS_CORDIS_CONFIG", "").strip()
    gateway_url = os.getenv("DEEPSEEK_HARNESS_GATEWAY_URL", "").strip()
    safe_to_start = enabled and bool(config_path) and bool(gateway_url)
    return {
        "project": "deepseek-ai/deepseek-harness",
        "distribution": "@deepseek-ai/dsh",
        "audited_version": "0.1.0-rc.5",
        "release_stage": "developer-preview",
        "enabled": enabled,
        "safe_planner_configured": bool(config_path),
        "gateway_configured": bool(gateway_url),
        "ready": safe_to_start,
        "mode": "optional_out_of_process_planner",
        "mutation_authority": False,
        "reason": (
            "ready; official dsh runtime is isolated behind a planner-only gateway"
            if safe_to_start
            else "SRE-native compatible plugin runtime active; official Developer Preview runtime is optional"
        ),
    }


def harness_capabilities_payload() -> dict[str, Any]:
    payload = CISRE_HARNESS_RUNTIME.diagnostics()
    # Lazy imports avoid a bootstrap cycle: the package manager mounts into
    # this runtime, while the event store is an independent durable service.
    from backend.app.services.harness_events import HARNESS_EVENT_STORE
    from backend.app.services.harness_packages import HARNESS_PACKAGE_MANAGER
    from backend.app.kernel import scalability_profile

    payload["upstream_adapter"] = official_deepseek_harness_status()
    payload["composition"] = HARNESS_PACKAGE_MANAGER.diagnostics()
    payload["event_store"] = HARNESS_EVENT_STORE.diagnostics()
    payload["scalability"] = scalability_profile()
    all_plugins = list(payload.get("plugins") or []) + list(payload["composition"].get("packages") or [])
    shared_plugin_ids = sorted({
        str(item.get("id") or "") for item in all_plugins
        if item.get("category") == "shared" and item.get("id")
    })
    payload["agents"] = [
        {
            "id": str(item.get("id") or ""),
            "domain": str((item.get("domains") or ["common"])[0]),
            "status": item.get("status") or item.get("package_status") or "pending",
            "description": item.get("description") or "",
            "shared_plugins": shared_plugin_ids,
            "domain_plugins": sorted({
                str(candidate.get("id") or "") for candidate in all_plugins
                if candidate.get("id")
                and candidate.get("category") == "domain"
                and str((item.get("domains") or ["common"])[0]) in (candidate.get("domains") or [])
            }),
            "missing_dependencies": item.get("missing_dependencies") or [],
        }
        for item in all_plugins
        if item.get("category") == "domain-agent"
    ]
    payload["contracts"] = {
        "append_only_session_events": True,
        "hash_chained_durable_events": True,
        "session_replay_fork_resume": True,
        "session_branch_tombstone_delete": True,
        "child_session_drill_down": True,
        "scoped_dependency_injection": True,
        "typed_event_modes": ["observe", "serial", "parallel", "waterfall"],
        "reversible_plugin_effects": True,
        "out_of_tree_declarative_plugins": True,
        "profile_bundle_patch_composition": True,
        "versioned_service_requirements": True,
        "swappable_service_providers": True,
        "plugin_permission_policy": True,
        "unsigned_privileged_plugins_fail_closed": True,
        "progressive_context_loading": True,
        "monotonic_tool_guards": True,
        "approval_fail_closed": True,
        "owner_scoped_jobs": True,
        "goal_round_continuation": True,
        "official_runtime_cannot_mutate_kubernetes": True,
        "ui_manifest_validation_and_install": True,
        "ui_visual_plugin_authoring": True,
        "ui_plugin_invocation_and_security_details": True,
        "shared_and_domain_plugin_classification": True,
        "domain_agent_composition": True,
        "stable_kernel_ports": True,
        "backend_independent_scale_contract": True,
        "plaintext_plugin_secrets_rejected": True,
        "team_provider_templates": ["common", "kubernetes", "database", "virtual_machine", "storage", "middleware", "cloud", "network"],
    }
    return payload
