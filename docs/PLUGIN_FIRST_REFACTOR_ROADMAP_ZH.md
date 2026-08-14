# CISRE Plugin-first 重构路线

## 结论

CISRE 应重构为插件优先的 AI SRE 应用，但不应把所有代码都做成插件，也不应一次性推倒重写。

目标形态是“稳定内核 + 可替换插件 + 领域 Skill”：</n+
- 内核只负责 Agent Loop、事件存储、能力依赖、任务状态、权限策略、人工审批、目标冻结、审计和恢复完成语义；
- 公共插件负责模型、上下文、知识、Trace、通知、CMDB 等跨域能力；
- 领域插件负责 Kubernetes、数据库、VM、存储、中间件、云和网络的发现、证据、动作适配与验证；
- Skill 负责某一类问题的触发、渐进取证、根因分流、动作选择、回滚和成功判据；
- Executor 是隔离的真实副作用边界，不能由普通插件或模型直接替代。

```text
Domain Agent = Agent Loop + 公共插件 + 本域插件 + 匹配到的 Skills
```

## 为什么仍需要重构

当前版本已经具备插件 Manifest、`provides/requires`、Profile/Bundle/Patch、事件、权限和热重载，并能从 UI 创建、校验和安装声明式插件。Kubernetes 闭环已经可执行；其他资源域具备合同和工作台。

但它仍是过渡架构：

- `backend/app/application.py` 仍承载历史兼容组合和部分业务函数；
- Kubernetes Provider、Executor 与 Verifier 虽已登记为领域插件，部分实现仍由既有服务模块提供；
- 非 Kubernetes 域只有标准合同，具体产品插件需要各专业组交付；
- JSONL 会话存储适合单副本，规模化前需要可并发的事件后端和分布式执行租约。

因此，“插件中心存在”不等于“全仓库已经插件化”。完成重构的判据是：新增一个产品或一种运维能力时，核心目录无需修改。

## 哪些东西不能插件化

以下能力属于可信计算基（TCB），只能由平台核心维护：

1. 凭据解析与脱敏；
2. 类型化动作目录和目标绑定；
3. RBAC、风险与爆炸半径门禁；
4. 逐项人工审批；
5. 执行租约、幂等和取消；
6. 同目标写后回读；
7. 恢复验证和唯一完成语义；
8. 追加式审计、签名/哈希链和策略执行。

插件可以提出动作、提供事实或执行受信 Provider 合同，但不能覆盖或绕过这些门禁。

## 目标目录

```text
backend/app/kernel/                 # 稳定内核；只有平台核心组修改
backend/app/plugin_runtime/         # Manifest、DI、事件、生命周期、Profile
backend/app/contracts/              # Resource/Evidence/Action/Verification v1/v2
plugins/
├── shared/                         # 模型、知识、Trace、通知、CMDB
├── kubernetes/                     # K8s Provider/Executor/Verifier
├── database/                       # 各数据库产品插件
├── virtual-machine/                # OS/虚拟化插件
├── storage/                        # 阵列/CSI/文件存储插件
├── middleware/                     # MQ/缓存/注册配置插件
├── cloud/                          # 公有云/私有云插件
└── network/                        # 交换路由、DNS、LB、ACL/策略插件
```

插件可以在同一仓库开发，也可以独立仓库发布。生产运行时只认签名 Manifest、服务合同和版本依赖，不依赖源码位置。

## 渐进迁移

### 阶段 0：冻结合同

- 冻结 Resource、Evidence、Action、Verification 和 Session Event v1；
- 冻结 `cisre.kernel.ports/v1`：Event Journal、Fenced Lease、Durable Queue、CAS Snapshot；
- 所有新增功能先定义合同，不向组合根添加产品分支；
- 建立插件兼容矩阵和契约测试。

### 阶段 1：抽离只读能力

- 先迁移库存、日志、指标、拓扑和健康证据 Provider；
- 对现有实现加兼容适配器，API 和生产部署不变；
- 以数据库或网络插件作为第一个非 K8s 验收样板。

### 阶段 2：抽离领域规划与验证

- 将每个域的证据标准化、动作候选和 Verifier 迁入插件；
- Agent 只依赖 `inventory.*`、`evidence.*`、`verification.*` 服务名；
- Skill 与插件通过能力声明连接，不引用具体 Python 模块。

### 阶段 3：隔离执行器

- K8s、数据库、VM、网络等执行器均运行在独立身份/进程；
- 插件只提交类型化动作，核心门禁签发一次性执行许可；
- 执行回执、写后回读和恢复证据全部进入同一事件链。

### 阶段 4：删除兼容单体

- 当所有路由和实现都有插件/服务所有者后，逐步缩小 `application.py`；
- 最终只保留应用装配、兼容重定向和启动生命周期；
- 以“核心无领域分支”作为重构完成标准。

## 规模增长时不改业务核心

```text
Console / API（无状态，可水平扩展）
        ↓ durable queue
Agent / Evidence Worker（按 tenant / cluster / target 分区）
        ↓ typed action + approval permit
Executor Worker（fencing lease + idempotency key）
        ↓ append event / CAS snapshot
Transactional Event Store + Read Models
```

本地文件、进程内锁和内联任务只是这些 Port 的单机实现。规模化时更换基础设施 Adapter，不改变 Agent Loop、插件 Manifest、Skill 或动作/验证合同。多副本启用前必须通过 `GET /api/harness/scalability`；本地后端会被判定为不满足分布式条件，防止误部署。

## 团队验收标准

一个插件只有同时满足以下条件才算完成：

1. 安装、升级和卸载不修改核心代码；
2. 声明提供、依赖、适用 Agent、权限和事件；
3. 缺依赖、无凭据、超时、上游 5xx 时 fail-closed；
4. 不保存明文凭据，不返回未脱敏原始内容；
5. 真实动作只能使用平台动作 ID 并逐项审批；
6. 变更后重新取证，成功判据与原始症状对应；
7. 合同测试覆盖成功、拒绝、超时、回滚和验证失败；
8. Trace 能指出由哪个 Agent、插件、Skill 和动作解决了哪个问题。

## 这是否算“彻底的 AI 应用”

算，但 AI 不等于把业务逻辑全交给模型。CISRE 的 AI 部分负责多假设推理、动态 Skill 路由、计划和解释；Harness 负责长期任务和工具编排；插件把真实世界能力提供给 Agent；确定性门禁和 Verifier 约束副作用。这个组合比“LLM + 一堆工具调用”更适合生产 SRE。
