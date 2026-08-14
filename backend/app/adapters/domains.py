"""Unified operation-domain catalog consumed by the API and Web entry gate."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .database import DATABASE_DOMAIN_SPEC
from .storage import STORAGE_DOMAIN_SPEC
from .virtual_machine import VIRTUAL_MACHINE_DOMAIN_SPEC


DOMAIN_CATALOG = (
    {
        "id": "kubernetes",
        "name": "Kubernetes",
        "short_name": "K8s",
        "description": "集群、Namespace、Workload、Pod、Service、配置、节点和 PV/PVC 风险。",
        "target_label": "集群 / Namespace / Workload",
        "adapter_directory": "backend/app/services/cluster_registry.py",
        "skill_namespace": "kubernetes.*",
        "required_evidence": ["logs", "events", "pod_status", "workload", "storage", "service", "node"],
        "verification_contract": ["rollout_complete", "pod_ready", "restart_count_stable", "error_absent"],
        "implementation": "built_in",
    },
    DATABASE_DOMAIN_SPEC,
    VIRTUAL_MACHINE_DOMAIN_SPEC,
    STORAGE_DOMAIN_SPEC,
    {
        "id": "middleware",
        "name": "中间件",
        "short_name": "Middleware",
        "description": "消息、注册配置、缓存和日志中间件的集群、积压、客户端与容量风险。",
        "target_label": "中间件集群 / 实例",
        "adapter_directory": "backend/app/adapters/middleware",
        "skill_namespace": "middleware.*",
        "required_evidence": ["cluster_health", "lag", "client_errors", "capacity", "recent_changes"],
        "verification_contract": ["cluster_healthy", "lag_recovered", "business_probe_ok"],
        "implementation": "extension_contract",
    },
    {
        "id": "cloud_service",
        "name": "云资源",
        "short_name": "Cloud",
        "description": "公有云和私有云计算、网络、负载均衡、配额及托管服务风险。",
        "target_label": "账号 / Region / 云资源",
        "adapter_directory": "backend/app/adapters/cloud_service",
        "skill_namespace": "cloud_service.*",
        "required_evidence": ["resource_status", "quota", "network_policy", "monitoring", "recent_changes"],
        "verification_contract": ["resource_healthy", "quota_safe", "business_probe_ok"],
        "implementation": "extension_contract",
    },
)


def operations_domain_catalog(counts: dict[str, int] | None = None) -> dict[str, Any]:
    counts = counts or {}
    domains = []
    for raw in DOMAIN_CATALOG:
        item = deepcopy(raw)
        domain_id = str(item["id"])
        count = None if domain_id == "kubernetes" else int(counts.get(domain_id) or 0)
        item["resource_count"] = count
        item["status"] = (
            "ready" if domain_id == "kubernetes"
            else "configured" if count
            else "adapter_ready"
        )
        item.setdefault("implementation", "extension_contract")
        domains.append(item)
    return {
        "contract_version": "cisre.operations.domain-catalog/v1",
        "default_domain": "kubernetes",
        "domains": domains,
        "routing": {
            "kubernetes": "existing SRE chat and AI inspection pipeline",
            "external": "infrastructure adapter -> evidence -> Skill -> OpsJob -> approval -> webhook -> verification",
        },
    }
