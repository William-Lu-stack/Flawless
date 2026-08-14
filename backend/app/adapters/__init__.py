"""Stable extension surface for non-Kubernetes infrastructure domains."""

from .contracts import (
    ADAPTER_CONTRACT_VERSION,
    AdapterDescriptor,
    EvidenceBundle,
    InfrastructureResource,
    ReadOnlyInfrastructureAdapter,
    VerificationResult,
    adapter_contract_payload,
)
from .registry import ADAPTER_REGISTRY, InfrastructureAdapterRegistry

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "ADAPTER_REGISTRY",
    "AdapterDescriptor",
    "EvidenceBundle",
    "InfrastructureAdapterRegistry",
    "InfrastructureResource",
    "ReadOnlyInfrastructureAdapter",
    "VerificationResult",
    "adapter_contract_payload",
]
