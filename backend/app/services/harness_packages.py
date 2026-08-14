"""Out-of-tree Harness package, profile and permission management.

External extensions are declarative service providers.  They are discovered
from a mounted directory and composed through ordered Bundle/Profile/Patch
layers, so adding or swapping a provider never requires editing CISRE core.
Untrusted packages cannot execute Python inside the API process and can never
become Kubernetes mutation authorities.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from backend.app.services.harness_events import HARNESS_EVENT_STORE, redact_event_value
from backend.app.services.harness_plugins import (
    CISRE_HARNESS_RUNTIME,
    HarnessPluginRuntime,
    PluginContext,
    PluginManifest,
)


PACKAGE_API_VERSION = "cisre.io/v1alpha1"
PACKAGE_KIND = "HarnessPlugin"
BUNDLE_KIND = "HarnessBundle"
PROFILE_KIND = "HarnessProfile"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
_SAFE_EXTERNAL_PERMISSIONS = {
    "service:provide",
    "events:publish",
    "ui:contribute",
    "inventory:read",
    "evidence:read",
    "model:invoke",
    "ops:propose",
}
_PRIVILEGED_PERMISSIONS = {
    "filesystem:read",
    "filesystem:write",
    "network:egress",
    "subprocess:execute",
    "secrets:read",
    "kubernetes:mutate",
    "ops:execute",
}
_SENSITIVE_KEY = re.compile(r"(?:password|passwd|token|secret|api[_-]?key|private[_-]?key|credential)", re.I)
_SENSITIVE_VALUE = re.compile(r"(?:bearer\s+[a-z0-9._~+/=-]{12,}|token-[a-z0-9]+:[a-z0-9]{12,})", re.I)
_SECRET_REFERENCE_KEY = re.compile(
    r"(?:^|[_-])(?:password|passwd|token|secret|api[_-]?key|private[_-]?key|credential)[_-]?(?:ref|reference)$",
    re.I,
)
_SAFE_REFERENCE_VALUE = re.compile(r"^[a-z0-9](?:[a-z0-9._/-]{0,251}[a-z0-9])?$", re.I)


class HarnessPackageError(ValueError):
    """A package is invalid or violates a fail-closed policy."""


@dataclass(slots=True)
class RemoteServiceProvider:
    """Narrow JSON RPC client for a signed, isolated read-only Provider."""

    plugin_id: str
    service: str
    endpoint: str
    operations: tuple[str, ...]
    timeout_seconds: float = 12.0

    async def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in self.operations:
            raise HarnessPackageError(f"operation is not declared read-only: {operation}")
        request_payload = redact_event_value(payload)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False) as client:
            response = await client.post(self.endpoint, json={
                "service": self.service,
                "operation": operation,
                "payload": request_payload,
            })
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise HarnessPackageError("remote provider response exceeds 1 MiB")
            value = response.json()
        if not isinstance(value, dict):
            raise HarnessPackageError("remote provider must return a JSON object")
        sanitized = redact_event_value(value)
        HARNESS_EVENT_STORE.append(
            f"plugin-{self.plugin_id}",
            "service/invoked",
            phase="runtime",
            stage="service_invoked",
            status="completed",
            message=f"{self.service}.{operation} completed",
            plugin_id=self.plugin_id,
            tool=operation,
            data={"service": self.service},
        )
        return sanitized


def _load_document(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 512 * 1024:
        raise HarnessPackageError(f"manifest too large: {path.name}")
    try:
        if path.suffix.lower() == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise HarnessPackageError(f"cannot parse {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessPackageError(f"manifest must be an object: {path.name}")
    return value


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contains_sensitive_value(value: Any, *, key: str = "") -> bool:
    """Reject credentials before a manifest can be persisted by the UI."""
    if key and _SENSITIVE_KEY.search(key):
        if _SECRET_REFERENCE_KEY.search(key):
            if isinstance(value, str):
                return not bool(_SAFE_REFERENCE_VALUE.fullmatch(value)) or bool(_SENSITIVE_VALUE.search(value))
            if isinstance(value, dict):
                return any(
                    not isinstance(item, str)
                    or not bool(_SAFE_REFERENCE_VALUE.fullmatch(item))
                    or bool(_SENSITIVE_VALUE.search(item))
                    for name, item in value.items()
                    if str(name) in {"name", "key", "namespace"}
                ) or any(str(name) not in {"name", "key", "namespace"} for name in value)
            return True
        return value not in (None, "", "[REDACTED]")
    if isinstance(value, dict):
        return any(_contains_sensitive_value(item, key=str(name)) for name, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_value(item) for item in value)
    return isinstance(value, str) and bool(_SENSITIVE_VALUE.search(value))


def _service_rows(value: Any) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for item in value or []:
        if isinstance(item, str):
            rows.append((item.strip(), ""))
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("service") or "").strip()
            rows.append((name, str(item.get("version") or item.get("constraint") or "").strip()))
    return tuple((name, version) for name, version in rows if name)


def _event_rows(value: Any) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for item in value or []:
        if isinstance(item, str):
            rows.append((item.strip(), "observe"))
        elif isinstance(item, dict):
            rows.append((str(item.get("name") or "").strip(), str(item.get("mode") or "observe").strip()))
    return tuple((name, mode) for name, mode in rows if name)


def _version_satisfies(version: str, constraint: str) -> bool:
    if not constraint:
        return True
    try:
        return Version(version) in SpecifierSet(constraint)
    except (InvalidVersion, InvalidSpecifier):
        return False


@dataclass(slots=True)
class HarnessPackage:
    manifest: PluginManifest
    path: Path
    digest: str
    runtime_type: str = "declarative"
    endpoint: str = ""
    operations: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    requested_permissions: tuple[str, ...] = ()
    granted_permissions: tuple[str, ...] = ()
    denied_permissions: tuple[str, ...] = ()
    service_versions: tuple[tuple[str, str], ...] = ()
    requirement_versions: tuple[tuple[str, str], ...] = ()
    event_modes: tuple[tuple[str, str], ...] = ()
    ui: dict[str, Any] = field(default_factory=dict)
    trusted: bool = False
    enabled: bool = True
    status: str = "discovered"
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            **self.manifest.public(),
            "path": str(self.path),
            "digest": self.digest,
            "runtime_type": self.runtime_type,
            "remote_ready": bool(self.endpoint and self.operations and not self.denied_permissions),
            "operations": list(self.operations),
            "trusted": self.trusted,
            "enabled": self.enabled,
            "package_status": self.status,
            "package_error": self.error,
            "requested_permissions": list(self.requested_permissions),
            "granted_permissions": list(self.granted_permissions),
            "denied_permissions": list(self.denied_permissions),
            "service_versions": dict(self.service_versions),
            "requirement_versions": dict(self.requirement_versions),
            "event_modes": dict(self.event_modes),
            "ui": copy.deepcopy(self.ui),
        }


class HarnessPackageManager:
    """Discover and transactionally mount declarative plugin packages."""

    def __init__(self, runtime: HarnessPluginRuntime, root: str | Path) -> None:
        self.runtime = runtime
        self.root = Path(root)
        self.packages: dict[str, HarnessPackage] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.bundles: dict[str, dict[str, Any]] = {}
        self.runtime_write_enabled = os.getenv("HARNESS_PROFILE_RUNTIME_WRITE_ENABLED", "false").lower() in {
            "1", "true", "yes", "on",
        }
        self.package_write_enabled = os.getenv("HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED", "false").lower() in {
            "1", "true", "yes", "on",
        }
        self.development_mode = os.getenv("HARNESS_PLUGIN_DEVELOPMENT_MODE", "false").lower() in {
            "1", "true", "yes", "on",
        }
        configured_profile = os.getenv("HARNESS_PROFILE", "").strip()
        self.active_profile = configured_profile or ("development" if self.development_mode else "production")
        self.last_error = ""
        self.refresh()

    @property
    def _trusted_digests(self) -> set[str]:
        return {
            item.strip().lower()
            for item in os.getenv("HARNESS_TRUSTED_PLUGIN_DIGESTS", "").split(",")
            if item.strip()
        }

    def _permissions(self, requested: tuple[str, ...], digest: str) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
        trusted = digest.lower() in self._trusted_digests
        granted = set(requested).intersection(_SAFE_EXTERNAL_PERMISSIONS)
        # A signature can grant explicit host-side integration permissions,
        # but mutation authority remains reserved to the built-in typed executor.
        if trusted:
            allowlisted = {
                item.strip() for item in os.getenv("HARNESS_TRUSTED_PLUGIN_PERMISSIONS", "network:egress").split(",")
                if item.strip()
            }
            granted.update(set(requested).intersection(allowlisted))
        denied = set(requested) - granted
        denied.update(set(requested).intersection({"kubernetes:mutate", "ops:execute", "secrets:read"}))
        granted.difference_update({"kubernetes:mutate", "ops:execute", "secrets:read"})
        return tuple(sorted(granted)), tuple(sorted(denied)), trusted

    def _endpoint_allowed(self, endpoint: str) -> bool:
        parsed = urlparse(endpoint)
        host = str(parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return False
        allowlist = {
            item.strip().lower()
            for item in os.getenv("HARNESS_REMOTE_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if host in allowlist or host.endswith(".svc") or host.endswith(".svc.cluster.local"):
            return True
        return self.development_mode and host in {"127.0.0.1", "localhost", "::1"}

    def _parse_package(self, path: Path) -> HarnessPackage:
        raw = _load_document(path)
        if raw.get("apiVersion") != PACKAGE_API_VERSION or raw.get("kind") != PACKAGE_KIND:
            raise HarnessPackageError(f"unsupported plugin contract: {path.name}")
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        spec = raw.get("spec") if isinstance(raw.get("spec"), dict) else {}
        plugin_id = str(metadata.get("name") or "").strip().lower()
        version = str(metadata.get("version") or "").strip()
        if not _SAFE_ID.fullmatch(plugin_id):
            raise HarnessPackageError(f"invalid plugin id: {plugin_id!r}")
        try:
            Version(version)
        except InvalidVersion as exc:
            raise HarnessPackageError(f"invalid plugin version: {version!r}") from exc
        provided = _service_rows(spec.get("provides"))
        required = _service_rows(spec.get("requires"))
        events = _event_rows(spec.get("events"))
        for _, mode in events:
            if mode not in HarnessPluginRuntime.MODES:
                raise HarnessPackageError(f"unsupported event mode: {mode}")
        runtime = spec.get("runtime") if isinstance(spec.get("runtime"), dict) else {}
        runtime_type = str(runtime.get("type") or "declarative")
        if runtime_type not in {"declarative", "remote"}:
            raise HarnessPackageError("external plugins must use declarative or remote runtime")
        digest = _file_digest(path)
        requested = tuple(sorted({str(item).strip() for item in spec.get("permissions") or [] if str(item).strip()}))
        granted, denied, trusted = self._permissions(requested, digest)
        endpoint = str(runtime.get("endpoint") or "").strip()
        operations = tuple(sorted({
            str(item.get("id") or item.get("name") or "").strip()
            for item in runtime.get("operations") or []
            if isinstance(item, dict) and item.get("read_only") is True
        }))
        if runtime_type == "remote" and (
            "network:egress" not in granted or not self._endpoint_allowed(endpoint) or not operations
        ):
            denied = tuple(sorted(set(denied).union({"network:egress"})))
        manifest = PluginManifest(
            id=plugin_id,
            version=version,
            description=str(metadata.get("description") or spec.get("description") or "")[:500],
            provides=tuple(name for name, _ in provided),
            requires=tuple(name for name, _ in required),
            events=tuple(name for name, _ in events),
            scope=str(spec.get("scope") or "global")[:160],
            priority=max(-10000, min(10000, int(spec.get("priority") or 0))),
            source="external-package",
            permissions=requested,
            runtime_type=runtime_type,
        )
        return HarnessPackage(
            manifest=manifest,
            path=path,
            digest=digest,
            runtime_type=runtime_type,
            endpoint=endpoint,
            operations=operations,
            config=redact_event_value(spec.get("config") if isinstance(spec.get("config"), dict) else {}),
            requested_permissions=requested,
            granted_permissions=granted,
            denied_permissions=denied,
            service_versions=provided,
            requirement_versions=required,
            event_modes=events,
            ui=redact_event_value(spec.get("ui") if isinstance(spec.get("ui"), dict) else {}),
            trusted=trusted,
        )

    def _discover_documents(self, subdir: str) -> list[Path]:
        directory = self.root / subdir
        if not directory.exists():
            return []
        return sorted(path for path in directory.rglob("*") if path.suffix.lower() in {".yaml", ".yml", ".json"})

    def _load_compositions(self) -> None:
        self.bundles = {}
        self.profiles = {
            "production": {
                "apiVersion": PACKAGE_API_VERSION,
                "kind": PROFILE_KIND,
                "metadata": {"name": "production", "description": "Built-in fail-closed production profile"},
                "spec": {"bundles": [], "plugins": [], "patches": []},
            },
            "development": {
                "apiVersion": PACKAGE_API_VERSION,
                "kind": PROFILE_KIND,
                "metadata": {"name": "development", "description": "Auto-discovers every safe mounted package"},
                "spec": {"bundles": [], "plugins": [], "patches": []},
            },
        }
        for kind, subdir, target in (
            (BUNDLE_KIND, "bundles", self.bundles),
            (PROFILE_KIND, "profiles", self.profiles),
        ):
            for path in self._discover_documents(subdir):
                try:
                    raw = _load_document(path)
                    if raw.get("apiVersion") != PACKAGE_API_VERSION or raw.get("kind") != kind:
                        continue
                    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
                    name = str(metadata.get("name") or "").strip().lower()
                    if _SAFE_ID.fullmatch(name):
                        target[name] = raw
                except HarnessPackageError:
                    continue

    def _profile_plan(self, profile_name: str) -> dict[str, dict[str, Any]]:
        profile = self.profiles.get(profile_name)
        if not profile:
            raise HarnessPackageError(f"unknown profile: {profile_name}")
        spec = profile.get("spec") if isinstance(profile.get("spec"), dict) else {}
        ordered: list[str] = []
        patches: list[dict[str, Any]] = []
        for bundle_name in spec.get("bundles") or []:
            bundle = self.bundles.get(str(bundle_name))
            if not bundle:
                raise HarnessPackageError(f"missing bundle: {bundle_name}")
            bundle_spec = bundle.get("spec") if isinstance(bundle.get("spec"), dict) else {}
            ordered.extend(str(item) for item in bundle_spec.get("plugins") or [])
            patches.extend(item for item in bundle_spec.get("patches") or [] if isinstance(item, dict))
        ordered.extend(str(item) for item in spec.get("plugins") or [])
        patches.extend(item for item in spec.get("patches") or [] if isinstance(item, dict))
        plan: dict[str, dict[str, Any]] = {plugin_id: {"enabled": True} for plugin_id in ordered}
        # With no explicit list, discover all packages. This makes dropping one
        # package into the mounted directory sufficient in development.
        if not ordered and profile_name != "production":
            plan = {plugin_id: {"enabled": True} for plugin_id in self.packages}
        for patch in patches:
            plugin_id = str(patch.get("id") or "")
            if plugin_id:
                plan.setdefault(plugin_id, {}).update({
                    key: copy.deepcopy(value)
                    for key, value in patch.items()
                    if key in {"enabled", "priority", "scope", "config"}
                })
        return plan

    def _check_requirements(self, package: HarnessPackage, candidates: dict[str, HarnessPackage]) -> list[str]:
        missing: list[str] = []
        runtime_plugins = self.runtime.diagnostics().get("plugins") or []
        for service, constraint in package.requirement_versions:
            versions = [
                version or item.manifest.version for item in candidates.values()
                for name, version in item.service_versions
                if name == service and item.enabled
            ]
            versions.extend(
                str(item.get("version") or "")
                for item in runtime_plugins
                if item.get("status") == "active" and service in (item.get("provides") or [])
            )
            if not versions:
                missing.append(f"{service}{constraint}")
            elif constraint and not any(_version_satisfies(version, constraint) for version in versions):
                missing.append(f"{service}{constraint}")
        return missing

    @staticmethod
    def _setup(package: HarnessPackage):
        def setup(context: PluginContext) -> None:
            versions = dict(package.service_versions)
            descriptor = {
                "plugin_id": package.manifest.id,
                "version": package.manifest.version,
                "runtime_type": package.runtime_type,
                "permissions": list(package.granted_permissions),
                "config": copy.deepcopy(package.config),
            }
            for service in package.manifest.provides:
                if package.runtime_type == "remote":
                    value: Any = RemoteServiceProvider(
                        plugin_id=package.manifest.id,
                        service=service,
                        endpoint=package.endpoint,
                        operations=package.operations,
                        timeout_seconds=max(1.0, min(float(package.config.get("timeout_seconds") or 12.0), 30.0)),
                    )
                else:
                    value = {**descriptor, "service": service, "service_version": versions.get(service) or package.manifest.version}
                context.provide(service, value)
            for event, mode in package.event_modes:
                if event.startswith("tools/") and "ops:execute" not in package.granted_permissions:
                    continue
                # Declarative plugins contribute an auditable observation. They
                # cannot execute arbitrary code in the control-plane process.
                if mode == "waterfall":
                    async def waterfall(payload, next_handler, _plugin_id=package.manifest.id):
                        trace = list(payload.get("plugin_trace") or [])
                        trace.append(_plugin_id)
                        return await next_handler({**payload, "plugin_trace": trace})
                    context.on(event, waterfall, mode=mode)
                else:
                    def observe(_payload, _plugin_id=package.manifest.id):
                        return {"observed_by": _plugin_id}
                    context.on(event, observe, mode=mode)
        return setup

    def _unmount_external(self) -> None:
        diagnostics = self.runtime.diagnostics()
        for plugin in diagnostics.get("plugins") or []:
            if plugin.get("source") == "external-package":
                self.runtime.unmount(str(plugin.get("id") or ""))

    def refresh(self, profile_name: str | None = None) -> dict[str, Any]:
        self._unmount_external()
        self.packages = {}
        self.last_error = ""
        for path in self._discover_documents("packages"):
            try:
                package = self._parse_package(path)
                if package.manifest.id in self.packages:
                    raise HarnessPackageError(f"duplicate plugin id: {package.manifest.id}")
                self.packages[package.manifest.id] = package
            except HarnessPackageError as exc:
                self.last_error = str(exc)[:500]
        self._load_compositions()
        selected = profile_name or self.active_profile
        try:
            plan = self._profile_plan(selected)
        except HarnessPackageError as exc:
            self.last_error = str(exc)
            selected = "production"
            plan = self._profile_plan(selected)
        candidates: dict[str, HarnessPackage] = {}
        for plugin_id, patch in plan.items():
            package = self.packages.get(plugin_id)
            if not package:
                self.last_error = f"profile references missing plugin: {plugin_id}"
                continue
            package.enabled = patch.get("enabled") is not False
            if "priority" in patch:
                package.manifest = replace(package.manifest, priority=int(patch["priority"]))
            if patch.get("scope"):
                package.manifest = replace(package.manifest, scope=str(patch["scope"]))
            if isinstance(patch.get("config"), dict):
                package.config.update(redact_event_value(patch["config"]))
            if package.enabled:
                candidates[plugin_id] = package
        for package in candidates.values():
            missing = self._check_requirements(package, candidates)
            if package.denied_permissions:
                package.status = "policy_denied"
                package.error = f"permissions denied: {', '.join(package.denied_permissions)}"
                continue
            if missing:
                package.status = "pending_dependencies"
                package.error = f"missing compatible services: {', '.join(missing)}"
                continue
            try:
                self.runtime.mount(package.manifest, self._setup(package))
                registration = next(
                    (item for item in self.runtime.diagnostics().get("plugins") or [] if item.get("id") == package.manifest.id),
                    {},
                )
                package.status = str(registration.get("status") or "failed")
                package.error = str(registration.get("error") or "")
            except (ValueError, TypeError) as exc:
                package.status = "failed"
                package.error = f"{type(exc).__name__}: {exc}"[:500]
        # A provider mounted later in the same reconciliation pass can activate
        # an earlier pending consumer. Reflect the runtime's final state rather
        # than the intermediate result observed immediately after mount.
        final_registrations = {
            str(item.get("id") or ""): item
            for item in self.runtime.diagnostics().get("plugins") or []
            if item.get("source") == "external-package"
        }
        for plugin_id, package in candidates.items():
            registration = final_registrations.get(plugin_id)
            if registration and package.status != "policy_denied":
                package.status = str(registration.get("status") or package.status)
                package.error = str(registration.get("error") or package.error)
        self.active_profile = selected
        HARNESS_EVENT_STORE.append(
            "harness-runtime",
            "plugins/reconciled",
            stage="plugin_reconcile",
            phase="runtime",
            status="completed" if not self.last_error else "warning",
            message=f"Profile {selected} reconciled",
            data={"profile": selected, "packages": len(self.packages), "error": self.last_error},
            plugin_id="cisre.plugin-loader",
        )
        return self.diagnostics()

    def activate(self, profile_name: str) -> dict[str, Any]:
        if not self.runtime_write_enabled:
            raise PermissionError("HARNESS_PROFILE_RUNTIME_WRITE_ENABLED=false")
        return self.refresh(profile_name)

    def validate_source(self, source: str) -> dict[str, Any]:
        if not source.strip():
            raise HarnessPackageError("plugin manifest is empty")
        if len(source.encode("utf-8")) > 512 * 1024:
            raise HarnessPackageError("plugin manifest exceeds 512 KiB")
        try:
            raw = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise HarnessPackageError(f"cannot parse plugin manifest: {exc}") from exc
        if not isinstance(raw, dict):
            raise HarnessPackageError("plugin manifest must be an object")
        if _contains_sensitive_value(raw):
            raise HarnessPackageError("plugin manifest must not contain credentials or plaintext secrets")
        with tempfile.TemporaryDirectory(prefix="cisre-plugin-validate-") as directory:
            path = Path(directory) / "plugin.yaml"
            path.write_text(source.strip() + "\n", encoding="utf-8")
            package = self._parse_package(path)
        return package.public()

    def install_source(self, source: str, *, add_to_active_profile: bool = True) -> dict[str, Any]:
        """Atomically persist a validated declarative package and reconcile it."""
        if not self.package_write_enabled:
            raise PermissionError("HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED=false")
        preview = self.validate_source(source)
        plugin_id = str(preview["id"])
        packages_dir = self.root / "packages"
        packages_dir.mkdir(parents=True, exist_ok=True)
        target = packages_dir / f"{plugin_id}.yaml"
        previous = target.read_bytes() if target.exists() else None
        profile_target = self.root / "profiles" / f"{self.active_profile}.yaml"
        previous_profile = profile_target.read_bytes() if profile_target.exists() else None
        staging = packages_dir / f".{plugin_id}.{uuid.uuid4().hex}.tmp"
        try:
            staging.write_text(source.strip() + "\n", encoding="utf-8")
            staging.chmod(0o600)
            os.replace(staging, target)
            if add_to_active_profile:
                self._add_package_to_profile(plugin_id, self.active_profile)
            composition = self.refresh()
        except Exception:
            staging.unlink(missing_ok=True)
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                rollback = packages_dir / f".{plugin_id}.{uuid.uuid4().hex}.rollback"
                rollback.write_bytes(previous)
                rollback.chmod(0o600)
                os.replace(rollback, target)
            if add_to_active_profile:
                if previous_profile is None:
                    profile_target.unlink(missing_ok=True)
                else:
                    profile_target.parent.mkdir(parents=True, exist_ok=True)
                    profile_rollback = profile_target.parent / f".{self.active_profile}.{uuid.uuid4().hex}.rollback"
                    profile_rollback.write_bytes(previous_profile)
                    profile_rollback.chmod(0o600)
                    os.replace(profile_rollback, profile_target)
            self.refresh()
            raise
        package = next((item for item in composition["packages"] if item["id"] == plugin_id), preview)
        return {"status": "installed", "package": package, "composition": composition}

    def _add_package_to_profile(self, plugin_id: str, profile_name: str) -> None:
        if not _SAFE_ID.fullmatch(profile_name):
            raise HarnessPackageError(f"invalid profile id: {profile_name!r}")
        profile = copy.deepcopy(self.profiles.get(profile_name) or {
            "apiVersion": PACKAGE_API_VERSION,
            "kind": PROFILE_KIND,
            "metadata": {"name": profile_name},
            "spec": {},
        })
        spec = profile.get("spec") if isinstance(profile.get("spec"), dict) else {}
        plugins = [str(item) for item in spec.get("plugins") or []]
        if plugin_id not in plugins:
            plugins.append(plugin_id)
        spec["plugins"] = plugins
        profile["spec"] = spec
        profiles_dir = self.root / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        target = profiles_dir / f"{profile_name}.yaml"
        staging = profiles_dir / f".{profile_name}.{uuid.uuid4().hex}.tmp"
        staging.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8")
        staging.chmod(0o600)
        os.replace(staging, target)

    async def invoke(self, service: str, operation: str, payload: dict[str, Any], *, scopes: tuple[str, ...] = ()) -> dict[str, Any]:
        provider = self.runtime.resolve(service, scopes=scopes)
        if not isinstance(provider, RemoteServiceProvider):
            raise HarnessPackageError(f"service is not an invokable remote provider: {service}")
        return await provider.invoke(operation, payload)

    def diagnostics(self) -> dict[str, Any]:
        runtime = self.runtime.diagnostics()
        services: dict[str, list[dict[str, Any]]] = {}
        for plugin in runtime.get("plugins") or []:
            for service in plugin.get("provides") or []:
                services.setdefault(service, []).append({
                    "plugin_id": plugin.get("id"),
                    "version": plugin.get("version"),
                    "scope": plugin.get("scope"),
                    "priority": plugin.get("priority"),
                    "active": plugin.get("status") == "active",
                })
        edges = [
            {"from": plugin.get("id"), "to": service, "type": "requires"}
            for plugin in runtime.get("plugins") or [] for service in plugin.get("requires") or []
        ] + [
            {"from": service, "to": plugin.get("id"), "type": "provides"}
            for plugin in runtime.get("plugins") or [] for service in plugin.get("provides") or []
        ]
        return {
            "root": str(self.root),
            "active_profile": self.active_profile,
            "runtime_write_enabled": self.runtime_write_enabled,
            "package_write_enabled": self.package_write_enabled,
            "development_mode": self.development_mode,
            "hot_reload_supported": True,
            "last_error": self.last_error,
            "packages": [item.public() for item in sorted(self.packages.values(), key=lambda value: value.manifest.id)],
            "profiles": [
                {
                    "id": name,
                    "description": str((value.get("metadata") or {}).get("description") or ""),
                    "active": name == self.active_profile,
                    "bundles": list((value.get("spec") or {}).get("bundles") or []),
                    "plugins": list((value.get("spec") or {}).get("plugins") or []),
                }
                for name, value in sorted(self.profiles.items())
            ],
            "bundles": sorted(self.bundles),
            "services": services,
            "dependency_graph": {"nodes": len(runtime.get("plugins") or []) + len(services), "edges": edges},
            "security": {
                "external_code_in_api_process": False,
                "unsigned_privileged_plugins": False,
                "external_kubernetes_mutation_authority": False,
                "safe_external_permissions": sorted(_SAFE_EXTERNAL_PERMISSIONS),
                "privileged_permissions": sorted(_PRIVILEGED_PERMISSIONS),
                "profile_activation_audited": True,
            },
        }


def _default_root() -> Path:
    configured = os.getenv("HARNESS_PLUGIN_ROOT", "").strip()
    return Path(configured) if configured else Path("/var/lib/cisre/harness")


HARNESS_PACKAGE_MANAGER = HarnessPackageManager(CISRE_HARNESS_RUNTIME, _default_root())
