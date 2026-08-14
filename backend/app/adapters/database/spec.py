"""Stable database domain requirements shared by every database adapter."""

DATABASE_DOMAIN_SPEC = {
    "id": "database",
    "name": "数据库",
    "short_name": "DB",
    "description": "实例、集群、主从复制、连接、SQL、锁、容量、备份与恢复风险。",
    "target_label": "数据库实例 / 集群",
    "adapter_directory": "backend/app/adapters/database",
    "skill_namespace": "database.*",
    "required_evidence": [
        "connectivity", "instance_role", "active_sessions", "slow_queries",
        "lock_waits", "replication", "capacity", "backup_status", "recent_changes",
    ],
    "verification_contract": [
        "connectivity_recovered", "replication_healthy", "lock_wait_recovered",
        "business_probe_ok", "error_rate_recovered",
    ],
}
