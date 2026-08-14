# VM / 主机 Adapter 接入

本目录由 OS、虚拟化或云主机团队维护。一个平台一个模块，统一输出 `virtual_machine` 域资源。

最低证据合同见 `spec.py`：Agent、系统指标、服务、磁盘/inode、网络、系统日志、安全基线和最近变更。写操作不能在 Adapter 里运行 SSH/Shell，必须把批准动作交给堡垒机、AWX、SaltStack、云管平台等外部受控执行器。

推荐模块：`aliyun_ecs.py`、`openstack.py`、`fusioncompute.py`、`hci.py`、`linux_agent.py`、`windows_agent.py`。
