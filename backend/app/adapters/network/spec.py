"""Stable network operation-domain contract."""

NETWORK_DOMAIN_SPEC = {
    "id": "network",
    "name": "网络",
    "short_name": "Network",
    "description": "交换/路由、链路、负载均衡、DNS、ACL 与安全策略风险。",
    "target_label": "网络域 / 设备 / 链路 / 策略",
    "adapter_directory": "backend/app/adapters/network",
    "skill_namespace": "network.*",
    "required_evidence": [
        "topology", "interface_state", "latency", "packet_loss", "routing",
        "dns", "load_balancer", "policy", "recent_changes",
    ],
    "verification_contract": [
        "path_reachable", "link_quality_recovered", "policy_effective",
        "dns_load_balancer_healthy", "business_probe_ok",
    ],
    "implementation": "extension_contract",
}
