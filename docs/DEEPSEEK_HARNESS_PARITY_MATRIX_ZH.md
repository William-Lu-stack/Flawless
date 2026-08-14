# CISRE 与官方 DeepSeek Harness 能力对照矩阵

更新时间：2026-08-14  
官方参考：[`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness)，CISRE 审计基线 `0.1.0-rc.5`。

## 结论

CISRE 采用官方 Harness 的组合与运行时思想，但不复制其生产权限模型。通用 Agent 能力通过插件服务组合；企业基础设施 mutation 永远留在 CISRE 的可信内核和人工审批链中。这样各领域团队可以只交付插件和 Skill，同时不把 kubeconfig、数据库管理员账号、云凭据或任意 Bash 暴露给动态代码。

## 对照

| 能力 | CISRE 5.6.0 | 企业约束 |
|---|---|---|
| Everything is a Plugin | 公共插件、领域插件、Domain Agent、Planner、Skill Router、Executor、Verifier 都有 Manifest 与稳定服务名 | 历史组合根继续收缩；新增领域能力不得堆入核心 |
| Provider / Consumer 与依赖注入 | `provides` / `requires`、SemVer 约束、作用域和优先级解析；内置服务已绑定真实可调用 Provider | 缺失依赖保持 pending，禁止缺省全权限 |
| Profile / Bundle / Patch | 有序组合、环境覆盖、启停和 Provider 替换 | 生产在线写默认关闭，变更全审计 |
| 事件驱动 | observe、serial、parallel、waterfall typed events | 门禁只能增加限制，不能短路审批 |
| 可逆 Effect / 生命周期 | mount、unmount、reload、逆序释放、依赖重新解析 | 资源释放失败不吞掉审计事实 |
| Agent Loop | 有界证据→计划→动作→验证循环、失败换路、停止条件和 owner-scoped jobs | 模型输出不能直接执行 |
| 多 Agent 编排 | Domain Agent + 公共能力 + 本域插件/Skills；支持有界委派、join、cancel | 子 Agent 继承更窄权限和目标范围 |
| Context Injection / Compaction | 重要度压缩，优先保存 ERROR/WARNING、Pod/Workload、PVC/PV、审批和失败动作 | Token、kubeconfig 和完整私有思维链不注入 |
| Tool pipeline | typed action → risk/blast radius → approval → target guard → executor | 任意 Bash/SQL/HTTP mutation 不是插件合同 |
| Event sourcing | 追加事件、哈希链、replay、fork、resume、子会话、审计墓碑 | 删除是可追溯墓碑，不抹掉根会话事实 |
| Trace | Input/Model/Tool 时间轴、上下文摘要、模型决策摘要、Skill、插件、审批、执行和恢复 Span | 展示可审计决策，不展示隐藏思维链或密钥 |
| Sandbox / 权限 | 外置 Provider 独立容器、只读根文件系统、权限清单、摘要信任、网络白名单 | `kubernetes:mutate`、`ops:execute`、`secrets:read` 永不下放 |
| 模型辅助插件开发 | 表单或兼容模型生成 Manifest，Schema/权限校验后可下载完整源码工程 | 生成工程默认 `implemented: false`，不能冒充已接入 |
| 插件市场基础 | 包目录、版本、依赖、Profile、加载/卸载、详情、调用记录与团队分类 | 企业内部先做审核目录，再考虑开放市场 |
| Kubernetes 闭环 | Rancher/kubeconfig 取证、单主 Skill、逐项审批、真实 Patch、同通道写后回读、新 Pod/rollout/重启/日志/Events 稳定验证 | HTTP 2xx、旧 Pod 健康或模型声明都不是成功 |

## 不直接替换为官方运行时的原因

1. 官方当前仍是 Developer Preview，版本和插件 ABI 可能破坏性变化。
2. 官方面向通用 Agent；CISRE 还必须处理生产凭据、真实基础设施写权限、爆炸半径、逐项审批、同目标回读和 SRE 恢复证明。
3. 动态代码隔离不等于安全边界。CISRE 把外置 Provider 放进独立容器，只让它返回只读事实和类型化 proposal。
4. CISRE 已有 Rancher/kubeconfig、Skill 记忆、变更执行、恢复验证、Records 和企业部署资产；整体重写会重复建设并引入迁移风险。

官方 Harness 可以继续作为可选的 Planner/实验运行时接入，但不拥有任何生产 mutation authority。
