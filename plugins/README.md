# CISRE 插件工作区

这里是各领域团队日常开发和维护 CISRE 能力的入口。新增产品、证据源、模型、通知或验证能力时，优先新增/升级插件，不修改核心编排。

## 分类

| 分类 | 适用范围 | 示例 |
|---|---|---|
| `shared` | 所有 Domain Agent | 模型、知识、Trace、通知、CMDB |
| `domain` | 一个或多个资源域 | 网络证据、数据库巡检、VM 验证 |
| `domain-agent` | 领域 Agent 组合声明 | Network Agent、Database Agent |

复制 [`templates/domain-provider`](templates/domain-provider/) 开始开发。完整规则见：

- [`docs/HARNESS_PLUGIN_DEVELOPMENT_ZH.md`](../docs/HARNESS_PLUGIN_DEVELOPMENT_ZH.md)
- [`docs/PLUGIN_TEAM_ONBOARDING_ZH.md`](../docs/PLUGIN_TEAM_ONBOARDING_ZH.md)
- [`docs/PLUGIN_FIRST_REFACTOR_ROADMAP_ZH.md`](../docs/PLUGIN_FIRST_REFACTOR_ROADMAP_ZH.md)

一个合格插件至少包含 Manifest、Provider 合同、Skills、类型化动作、合同测试和维护说明。生产凭据只能通过 Secret、工作负载身份或凭据服务注入。
