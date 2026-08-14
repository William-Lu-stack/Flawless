"""Stable enterprise-storage requirements shared by every storage adapter."""

STORAGE_DOMAIN_SPEC = {
    "id": "storage",
    "name": "企业存储",
    "short_name": "Storage",
    "description": "NAS、SAN、CSI、Ceph 与存储阵列的池容量、卷、路径、ACL、快照和性能风险。",
    "target_label": "存储系统 / 存储池 / 卷",
    "adapter_directory": "backend/app/adapters/storage",
    "skill_namespace": "storage.*",
    "required_evidence": [
        "system_health", "pool_capacity", "volume_status", "path_health",
        "latency_iops", "acl", "snapshot", "replication", "recent_changes",
    ],
    "verification_contract": [
        "paths_healthy", "volume_online", "capacity_safe", "latency_recovered",
        "consumer_mount_ok", "business_probe_ok",
    ],
}
