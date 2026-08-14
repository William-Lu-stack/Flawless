"""Versioned contracts implemented by database, VM and storage adapters.

Adapters in this package are read-only capability providers. They may discover
resources, collect evidence and verify recovery. Mutations are deliberately not
part of this protocol: an approved change is submitted by the existing OpsJob
executor to the external action webhook, preserving one approval and audit
boundary for every infrastructure domain.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


ADAPTER_CONTRACT_VERSION = "cisre.infrastructure.adapter/v1"
RESOURCE_CONTRACT_VERSION = "cisre.infrastructure.resource/v1"
EVIDENCE_CONTRACT_VERSION = "cisre.infrastructure.evidence/v1"
VERIFICATION_CONTRACT_VERSION = "cisre.infrastructure.verification/v1"
SUPPORTED_DOMAINS = {
    "database", "virtual_machine", "storage", "middleware", "cloud_service", "network",
}
_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}$")
_SENSITIVE_FIELD_PATTERN = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)")


def _assert_safe_mapping(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE_FIELD_PATTERN.search(str(key)):
                raise ValueError(f"{path} must not contain secret field {key!r}")
            _assert_safe_mapping(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_safe_mapping(item, path=f"{path}[{index}]")


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    id: str
    domain: str
    display_name: str
    version: str
    contract_version: str = ADAPTER_CONTRACT_VERSION
    provider: str = "custom"
    capabilities: tuple[str, ...] = ("discover", "collect_evidence", "verify")
    supported_products: tuple[str, ...] = ()
    read_only: bool = True

    def validate(self) -> None:
        if not _SAFE_ID.fullmatch(self.id):
            raise ValueError("adapter id must be a stable URL-safe identifier")
        if self.domain not in SUPPORTED_DOMAINS:
            raise ValueError(f"unsupported adapter domain: {self.domain}")
        if self.contract_version != ADAPTER_CONTRACT_VERSION:
            raise ValueError(
                f"adapter contract mismatch: {self.contract_version}; expected {ADAPTER_CONTRACT_VERSION}"
            )
        if not self.read_only:
            raise ValueError("in-process infrastructure adapters must remain read-only")
        unknown = set(self.capabilities) - {"discover", "collect_evidence", "verify"}
        if unknown:
            raise ValueError(f"unsupported adapter capabilities: {sorted(unknown)}")

    def public(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        payload["supported_products"] = list(self.supported_products)
        return payload


@dataclass(slots=True)
class InfrastructureResource:
    id: str
    domain: str
    name: str
    provider: str
    subtype: str = ""
    environment: str = "unknown"
    location: str = "external"
    business_service: str = ""
    criticality: str = "medium"
    endpoint_ref: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    contract_version: str = RESOURCE_CONTRACT_VERSION

    def validate(self) -> None:
        if not _SAFE_ID.fullmatch(self.id):
            raise ValueError("resource id must be stable and URL-safe")
        if self.domain not in SUPPORTED_DOMAINS:
            raise ValueError(f"unsupported resource domain: {self.domain}")
        if self.contract_version != RESOURCE_CONTRACT_VERSION:
            raise ValueError("resource contract version mismatch")
        _assert_safe_mapping(self.labels, path="resource.labels")
        _assert_safe_mapping(self.facts, path="resource.facts")

    def public(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(slots=True)
class EvidenceBundle:
    resource_id: str
    domain: str
    observed_at: str
    health: str
    signals: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    collection_errors: list[dict[str, Any]] = field(default_factory=list)
    contract_version: str = EVIDENCE_CONTRACT_VERSION

    def validate(self) -> None:
        if self.domain not in SUPPORTED_DOMAINS:
            raise ValueError(f"unsupported evidence domain: {self.domain}")
        if self.contract_version != EVIDENCE_CONTRACT_VERSION:
            raise ValueError("evidence contract version mismatch")
        _assert_safe_mapping(asdict(self), path="evidence")

    def public(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(slots=True)
class VerificationResult:
    resource_id: str
    recovered: bool | None
    status: str
    checked_at: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    contract_version: str = VERIFICATION_CONTRACT_VERSION

    def validate(self) -> None:
        if self.contract_version != VERIFICATION_CONTRACT_VERSION:
            raise ValueError("verification contract version mismatch")
        if self.status not in {"completed", "failed", "unknown"}:
            raise ValueError(f"unsupported verification status: {self.status}")
        if self.recovered is True and self.status != "completed":
            raise ValueError("recovered=true requires status=completed")
        _assert_safe_mapping(self.checks, path="verification.checks")

    def public(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@runtime_checkable
class ReadOnlyInfrastructureAdapter(Protocol):
    """Only interface teams need to implement inside the CISRE process."""

    descriptor: AdapterDescriptor

    async def discover(self, request: dict[str, Any]) -> list[InfrastructureResource]: ...

    async def collect_evidence(
        self,
        resource: InfrastructureResource,
        request: dict[str, Any],
    ) -> EvidenceBundle: ...

    async def verify(
        self,
        resource: InfrastructureResource,
        receipt: dict[str, Any],
        criteria: list[str],
    ) -> VerificationResult: ...


def adapter_contract_payload() -> dict[str, Any]:
    """Machine-readable contract catalog returned by the control-plane API."""
    return {
        "contract_version": ADAPTER_CONTRACT_VERSION,
        "security_boundary": {
            "in_process_adapters": "read-only discovery, evidence and verification",
            "mutations": "OpsJob -> per-change approval -> action webhook -> verification",
            "credentials": "server-side Secret/env/workload identity only; never API payload fields",
        },
        "interfaces": {
            "discover": {
                "input": ["domain", "account_ref", "regions", "filters", "read_only=true"],
                "output": RESOURCE_CONTRACT_VERSION,
            },
            "collect_evidence": {
                "input": ["resource", "intent", "evidence_types", "deadline_seconds"],
                "output": EVIDENCE_CONTRACT_VERSION,
            },
            "verify": {
                "input": ["resource", "redacted_action_receipt", "success_criteria"],
                "output": VERIFICATION_CONTRACT_VERSION,
            },
        },
        "required_adapter_fields": [
            "id", "domain", "display_name", "version", "contract_version",
            "provider", "capabilities", "read_only",
        ],
        "supported_domains": sorted(SUPPORTED_DOMAINS),
        "compatibility": {
            "v1": "additive fields allowed; existing field meaning and enum values are immutable",
            "breaking_change": "publish /v2 in parallel and keep /v1 for at least one release line",
        },
    }
