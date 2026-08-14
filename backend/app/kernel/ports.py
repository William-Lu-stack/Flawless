"""Versioned storage and coordination ports for the stable CISRE kernel.

The interfaces are deliberately provider-neutral. Local development adapters
may use files and in-process locks; production adapters can use a transactional
database, distributed lease service and durable queue without changing Agents,
plugins, Skills or HTTP contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


KERNEL_CONTRACT_VERSION = "cisre.kernel.ports/v1"


@dataclass(frozen=True, slots=True)
class AppendRequest:
    stream: str
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str
    expected_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AppendReceipt:
    stream: str
    sequence: int
    event_id: str
    committed_at: str
    duplicate: bool = False


@runtime_checkable
class EventJournal(Protocol):
    """Append-only journal with optimistic concurrency and replay cursors."""

    async def append(self, request: AppendRequest) -> AppendReceipt: ...

    async def read(
        self, stream: str, *, after_sequence: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]: ...

    async def subscribe(
        self, *, after_cursor: str = "", topics: tuple[str, ...] = ()
    ) -> AsyncIterator[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class LeaseReceipt:
    key: str
    owner: str
    fencing_token: int
    expires_at: str


@runtime_checkable
class ExecutionLeaseStore(Protocol):
    """Distributed lease with fencing; stale workers cannot commit effects."""

    async def acquire(self, key: str, owner: str, ttl_seconds: int) -> LeaseReceipt | None: ...

    async def renew(self, receipt: LeaseReceipt, ttl_seconds: int) -> LeaseReceipt | None: ...

    async def release(self, receipt: LeaseReceipt) -> bool: ...


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    job_id: str
    kind: str
    payload: dict[str, Any]
    idempotency_key: str
    partition_key: str = ""
    not_before: str = ""


@runtime_checkable
class JobDispatcher(Protocol):
    """Durable queue port with bounded retries and explicit acknowledgement."""

    async def enqueue(self, job: JobEnvelope) -> str: ...

    async def claim(self, worker_id: str, *, limit: int, lease_seconds: int) -> list[JobEnvelope]: ...

    async def acknowledge(self, job_id: str, worker_id: str) -> bool: ...

    async def fail(self, job_id: str, worker_id: str, *, retryable: bool, reason: str) -> bool: ...


@runtime_checkable
class SnapshotStore(Protocol):
    """Read model/snapshot store using compare-and-swap revisions."""

    async def get(self, key: str) -> tuple[int, dict[str, Any]] | None: ...

    async def compare_and_swap(
        self, key: str, expected_revision: int | None, value: dict[str, Any]
    ) -> int: ...
