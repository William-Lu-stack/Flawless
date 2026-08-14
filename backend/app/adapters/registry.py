"""Thread-safe registry for in-process read-only infrastructure adapters."""
from __future__ import annotations

import inspect
import threading
from typing import Any

from .contracts import (
    AdapterDescriptor,
    EvidenceBundle,
    InfrastructureResource,
    ReadOnlyInfrastructureAdapter,
    VerificationResult,
)


class InfrastructureAdapterRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._adapters: dict[str, ReadOnlyInfrastructureAdapter] = {}

    def register(self, adapter: ReadOnlyInfrastructureAdapter, *, replace: bool = False) -> None:
        descriptor = getattr(adapter, "descriptor", None)
        if not isinstance(descriptor, AdapterDescriptor):
            raise TypeError("adapter.descriptor must be AdapterDescriptor")
        descriptor.validate()
        for method in ("discover", "collect_evidence", "verify"):
            if not callable(getattr(adapter, method, None)):
                raise TypeError(f"adapter {descriptor.id} is missing {method}()")
        with self._lock:
            if descriptor.id in self._adapters and not replace:
                raise ValueError(f"adapter already registered: {descriptor.id}")
            self._adapters[descriptor.id] = adapter

    def unregister(self, adapter_id: str) -> bool:
        with self._lock:
            return self._adapters.pop(adapter_id, None) is not None

    def get(self, adapter_id: str) -> ReadOnlyInfrastructureAdapter:
        with self._lock:
            adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise KeyError(f"adapter not registered: {adapter_id}")
        return adapter

    def list(self, *, domain: str = "") -> list[dict[str, Any]]:
        with self._lock:
            adapters = list(self._adapters.values())
        return [
            adapter.descriptor.public()
            for adapter in sorted(adapters, key=lambda item: item.descriptor.id)
            if not domain or adapter.descriptor.domain == domain
        ]

    async def discover(self, adapter_id: str, request: dict[str, Any]) -> list[dict[str, Any]]:
        adapter = self.get(adapter_id)
        result = adapter.discover(request)
        resources = await result if inspect.isawaitable(result) else result
        return [self._resource(item).public() for item in resources]

    async def collect_evidence(
        self,
        adapter_id: str,
        resource: InfrastructureResource,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        adapter = self.get(adapter_id)
        result = adapter.collect_evidence(resource, request)
        evidence = await result if inspect.isawaitable(result) else result
        if not isinstance(evidence, EvidenceBundle):
            raise TypeError("collect_evidence() must return EvidenceBundle")
        return evidence.public()

    async def verify(
        self,
        adapter_id: str,
        resource: InfrastructureResource,
        receipt: dict[str, Any],
        criteria: list[str],
    ) -> dict[str, Any]:
        adapter = self.get(adapter_id)
        result = adapter.verify(resource, receipt, criteria)
        verification = await result if inspect.isawaitable(result) else result
        if not isinstance(verification, VerificationResult):
            raise TypeError("verify() must return VerificationResult")
        return verification.public()

    @staticmethod
    def _resource(value: Any) -> InfrastructureResource:
        if not isinstance(value, InfrastructureResource):
            raise TypeError("discover() must return InfrastructureResource items")
        return value


ADAPTER_REGISTRY = InfrastructureAdapterRegistry()
