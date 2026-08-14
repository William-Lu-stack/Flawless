# 云资源 Adapter 接入

本目录由云平台团队维护。建议每个云厂商一个模块，再按计算、网络、负载均衡、数据库和存储拆分客户端。优先使用 RAM Role/工作负载身份；AccessKey 只能来自 Secret Manager。Adapter 只读，扩缩容、安全组等写操作统一交给受控云管执行器。
