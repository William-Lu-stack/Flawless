"""Machine-readable deployment-scale readiness for the CISRE kernel."""
from __future__ import annotations

import os
from typing import Any

from .ports import KERNEL_CONTRACT_VERSION


_DISTRIBUTED_EVENT_BACKENDS = {"postgres", "cockroachdb", "kafka"}
_DISTRIBUTED_LEASE_BACKENDS = {"postgres", "redis", "etcd"}
_DURABLE_QUEUE_BACKENDS = {"postgres", "redis", "kafka", "rabbitmq", "nats-jetstream"}
_DISTRIBUTED_SNAPSHOT_BACKENDS = {"postgres", "cockroachdb"}


def _replicas() -> int:
    raw = os.getenv("CISRE_API_REPLICAS", "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def scalability_profile() -> dict[str, Any]:
    replicas = _replicas()
    event_backend = os.getenv("CISRE_EVENT_BACKEND", "file").strip().lower() or "file"
    lease_backend = os.getenv("CISRE_LEASE_BACKEND", "local").strip().lower() or "local"
    queue_backend = os.getenv("CISRE_JOB_QUEUE_BACKEND", "inline").strip().lower() or "inline"
    snapshot_backend = os.getenv("CISRE_SNAPSHOT_BACKEND", "file").strip().lower() or "file"

    checks = {
        "append_only_event_journal": event_backend in _DISTRIBUTED_EVENT_BACKENDS,
        "distributed_fenced_leases": lease_backend in _DISTRIBUTED_LEASE_BACKENDS,
        "durable_job_queue": queue_backend in _DURABLE_QUEUE_BACKENDS,
        "transactional_snapshots": snapshot_backend in _DISTRIBUTED_SNAPSHOT_BACKENDS,
    }
    violations: list[str] = []
    if replicas > 1:
        if not checks["append_only_event_journal"]:
            violations.append("multi-replica API requires a distributed append-only event backend")
        if not checks["distributed_fenced_leases"]:
            violations.append("multi-replica mutation workers require distributed fenced leases")
        if not checks["durable_job_queue"]:
            violations.append("multi-replica workers require a durable queue")
        if not checks["transactional_snapshots"]:
            violations.append("multi-replica read models require transactional snapshots")

    distributed_ready = all(checks.values())
    safe_for_configured_replicas = replicas == 1 or distributed_ready
    return {
        "contract_version": KERNEL_CONTRACT_VERSION,
        "mode": "distributed" if replicas > 1 else "single-replica",
        "replicas": replicas,
        "safe_for_configured_replicas": safe_for_configured_replicas,
        "distributed_ready": distributed_ready,
        "backends": {
            "events": event_backend,
            "leases": lease_backend,
            "queue": queue_backend,
            "snapshots": snapshot_backend,
        },
        "checks": checks,
        "violations": violations,
        "scale_invariants": [
            "stateless API processes",
            "idempotency keys on every mutation job",
            "fencing tokens prevent stale-worker commits",
            "partition by tenant/cluster/target",
            "bounded queues and backpressure instead of false overload failures",
            "append-only events rebuild disposable read models",
            "plugin and contract versions remain independent of backend choice",
        ],
        "recommended_production_profile": {
            "CISRE_EVENT_BACKEND": ["postgres"],
            "CISRE_LEASE_BACKEND": ["postgres", "redis"],
            "CISRE_JOB_QUEUE_BACKEND": ["kafka", "redis", "rabbitmq", "nats-jetstream"],
            "CISRE_SNAPSHOT_BACKEND": ["postgres", "cockroachdb"],
        },
    }
