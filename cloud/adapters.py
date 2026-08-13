"""Cloud adapter registry.

Production deployments can implement each adapter with the relevant SDK. The
contract is intentionally stable: discover accounts/clusters, normalize topology,
and expose safety boundaries for remediation.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class CloudAdapterSpec:
    id: str
    provider: str
    display_name: str
    enabled: bool
    capabilities: list[str]
    auth_mode: str
    regions: list[str]
    inventory_scope: str
    safety_boundary: str


DEFAULT_ADAPTERS = [
    CloudAdapterSpec("rancher", "private-cloud", "Rancher Multi-Cluster", True, ["kubernetes", "topology", "remediation"], "token", ["local"], "all-clusters", "namespace/workload allowlist + human approval"),
    CloudAdapterSpec("generic-storage", "csi-storage", "通用 CSI / NFS / Ceph", False, ["storage", "csi", "metrics", "topology"], "secret-ref-or-service-account", [], "storage-pool/cluster", "read-only discovery + explicit PVC/PV change approval"),
    CloudAdapterSpec("domestic-hci", "private-cloud", "国产 HCI / 私有云", False, ["hci", "virtualization", "network", "storage", "security", "topology"], "api-token-or-service-account", [], "tenant/resource-pool", "tenant-scoped token + manual approval for infrastructure changes"),
    CloudAdapterSpec("aliyun", "aliyun", "阿里云全栈资源", False, ["ack", "ecs", "rds", "polardb", "oceanbase", "slb", "nas", "oss", "arms", "sls", "cms", "rocketmq"], "ram-role-or-secret-ref", [], "account/resource-group/region", "RAM least privilege + read-only discovery + approved change window"),
    CloudAdapterSpec("huawei-cloud", "huawei-cloud", "华为云", False, ["cce", "ecs", "rds", "gaussdb", "dcs", "elb", "obs", "cloud-eye", "lts"], "agency-or-secret-ref", [], "account/project/region", "IAM least privilege + approved change window"),
    CloudAdapterSpec("tencent-cloud", "tencent-cloud", "腾讯云", False, ["tke", "cvm", "cdb", "tdsql", "clb", "cfs", "cos", "cls", "monitor"], "cam-role-or-secret-ref", [], "account/project/region", "CAM least privilege + approved change window"),
    CloudAdapterSpec("domestic-database", "database", "国产数据库", False, ["oceanbase", "gaussdb", "tidb", "dameng", "kingbase", "gbase"], "external-secret", [], "instance/cluster", "read-only evidence + DBA approval for mutation"),
    CloudAdapterSpec("openstack", "private-cloud", "OpenStack / 国产虚拟化", False, ["compute", "network", "storage", "kubernetes"], "service-account", [], "tenant/project", "tenant-scoped account + approval"),
]


def _bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _custom_adapters() -> list[CloudAdapterSpec]:
    raw = os.getenv("CLOUD_ADAPTERS_JSON", "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    adapters = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        adapters.append(CloudAdapterSpec(
            id=str(item.get("id") or item.get("provider")),
            provider=str(item.get("provider") or "custom"),
            display_name=str(item.get("display_name") or item.get("name") or item.get("provider")),
            enabled=_bool(item.get("enabled"), True),
            capabilities=list(item.get("capabilities") or []),
            auth_mode=str(item.get("auth_mode") or "external-secret"),
            regions=list(item.get("regions") or []),
            inventory_scope=str(item.get("inventory_scope") or "account"),
            safety_boundary=str(item.get("safety_boundary") or "least privilege + approval"),
        ))
    return adapters


def cloud_adapters_payload() -> dict[str, Any]:
    adapters = {item.id: item for item in DEFAULT_ADAPTERS}
    for item in _custom_adapters():
        adapters[item.id] = item
    values = [asdict(item) for item in adapters.values()]
    return {
        "status": "ok",
        "enabled": [x for x in values if x["enabled"]],
        "available": values,
        "contract": {
            "discover": "accounts/projects/clusters/namespaces/workloads/cloud resources",
            "topology": "normalize resources into typed graph nodes and dependency edges",
            "observe": "metrics/logs/traces/events through provider-native observability APIs",
            "remediate": "guarded changes with dry-run, approval, audit and rollback metadata",
        },
    }
