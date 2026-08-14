"""Stable VM/host domain requirements shared by every compute adapter."""

VIRTUAL_MACHINE_DOMAIN_SPEC = {
    "id": "virtual_machine",
    "name": "虚拟机 / 主机",
    "short_name": "VM",
    "description": "云主机、虚拟机、裸机的系统、进程、服务、磁盘、网络和基线风险。",
    "target_label": "主机 / 虚拟机",
    "adapter_directory": "backend/app/adapters/virtual_machine",
    "skill_namespace": "virtual_machine.*",
    "required_evidence": [
        "agent_status", "system_metrics", "service_status", "disk_and_inode",
        "network", "system_logs", "security_baseline", "recent_changes",
    ],
    "verification_contract": [
        "agent_online", "service_healthy", "resource_pressure_recovered",
        "business_probe_ok", "error_rate_recovered",
    ],
}
