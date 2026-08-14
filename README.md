# CISRE — 企业统一 SRE 平台

CISRE（Cloud Infrastructure Site Reliability Engine）把基础设施的风险发现、证据采集、根因诊断、人工审批、受控变更、恢复验证和经验沉淀连接成一条可审计闭环。

当前版本：**5.6.0**。

## 一句话理解

模型负责理解、规划和解释；Skill 负责领域处置知识；插件负责提供可组合能力；Harness 负责状态、权限和编排；受控执行器负责真实变更；Verifier 负责证明目标已经恢复。

```text
发现 → 取证 → 诊断 → Skill 路由 → 变更预览 → 人工审批
    → 执行 → 同目标回读 → 稳定性验证 → Records / Skill 成效
                         ↘ 未恢复：保留证据并换策略继续
```

执行 API 返回成功、模型声称成功或旧实例仍然健康，都不等于故障恢复。只有真实目标的新证据满足恢复合同，任务才会闭环。

## 产品入口

- **SRE Run**：围绕真实资源进行取证、诊断、审批、执行和恢复验证。
- **AI 巡检**：定时或手动发现风险，并复用与 SRE Run 相同的运维内核。
- **拓扑影响**：展示资源依赖、流量和爆炸半径，辅助风险门禁。
- **Skill 库**：维护问题触发条件、渐进取证、动作、回滚与成功判据。
- **插件中心**：查看插件详情、调用条件、服务依赖、安全边界、最近调用；支持可视化或模型辅助创建，并一键下载可运行的 Provider + Skill + 测试 + 部署工程。
- **平台能力**：运行总览、资源事件、插件与 Profile、Agent Trace、运维成效。
- **运维成效**：只统计已经通过恢复验证的问题；可展开根因、Skill、变更和恢复证据。

## 当前能力边界

| 领域 | 当前状态 | 接入方式 |
|---|---|---|
| Kubernetes | 完整闭环 | Rancher、上传/粘贴 kubeconfig、集群内 ServiceAccount |
| 数据库 | 扩展合同就绪 | 领域插件 + 只读 Provider + 类型化动作执行器 |
| VM / 主机 | 扩展合同就绪 | 领域插件 + 只读 Provider + 企业执行平台 |
| 存储 | 扩展合同就绪 | 领域插件 + 阵列/CSI/存储平台 Provider |
| 中间件 / 云资源 | 扩展合同就绪 | 按稳定 Adapter 和 Harness 服务合同接入 |
| 网络 | 扩展合同就绪 | 交换/路由、负载均衡、DNS、ACL/安全策略与链路 Provider |

“合同就绪”表示接口、权限、审计和闭环语义已具备，不表示某个具体产品已经连接。页面不得伪造资源或健康数据。

## Harness 与插件模型

CISRE 吸收了官方 DeepSeek Harness 的组合思想，但保留独立的生产执行边界：

- Everything is a Plugin：新增领域能力优先交付插件，不修改核心编排。
- Service Provider / Consumer：插件用 `provides` / `requires` 声明能力和依赖，由运行时解析。
- Profile / Bundle / Patch：开发、测试、生产可组合不同 Provider，不在业务代码中堆环境判断。
- 事件驱动：支持 observe、serial、parallel、waterfall；高风险门禁只能收紧，不能被插件绕过。
- 可逆生命周期：加载、卸载、热重载和资源释放有确定顺序。
- 事件溯源：会话事件追加写入、脱敏并哈希串联，支持 replay、fork、resume 和审计墓碑删除。
- Agent Trace：展示上下文摘要、模型决策摘要、Skill、插件、工具、审批、变更与验证 Span；不展示原始凭据、完整私有思维链或未脱敏 Prompt。
- Agent Loop / 编排：插件可声明最大步数、委派关系和所需服务，但真实副作用仍通过 CISRE 审批执行链。

资源域按 Agent 组合：Kubernetes、数据库、VM/主机、存储、中间件、云资源和网络各有一个 Domain Agent。Agent 复用公共 Planner、上下文、审批、Trace、事件和任务插件，再加载本资源域的 Provider、执行器、Verifier 与 Skills。插件 Manifest 必须声明 `category`、`domains` 和 `agents`，便于运行时依赖解析与前端分类。

当前版本是 Plugin-first 过渡架构，不应误解为所有历史代码都已抽离：插件运行时和跨团队合同已经可用，Kubernetes 闭环仍通过兼容服务实现，`backend/app/application.py` 仍在逐步缩小。目标不是重写全部系统，而是让后续领域功能做到“只提交插件和 Skill，核心零改动”。迁移边界和完成判据见 [Plugin-first 重构路线](docs/PLUGIN_FIRST_REFACTOR_ROADMAP_ZH.md)。

任何真实变更必须经过：

```text
typed action → policy / blast radius → human approval → executor
             → same-target readback → recovery verifier → record
```

外置插件不能直接取得 `kubernetes:mutate`、`ops:execute`、`secrets:read`，也不能把任意 Bash、SQL 或 HTTP mutation 注入 API 进程。

## 插件中心怎么用

进入 **平台能力 → 插件中心**：

1. 点击插件卡片查看触发条件、提供/依赖服务、事件模式、权限边界和最近调用。
2. 点击“新建插件”，选择数据库、VM、存储等领域模板，用表单生成 Manifest。
3. 或在“AI 辅助开发”中描述目标，让当前兼容模型生成草案。
4. 检查生成的 `provides`、`requires`、权限、Agent Loop 和安全边界。
5. 点击“下载完整插件项目”，得到独立 Provider、领域 Skill、合同测试、安全说明、Dockerfile 和 Kubernetes YAML。
6. 先校验，再安装/热重载；生产页面写入默认关闭，需要平台配置显式开启。

模型生成的只是声明式草案，必须通过 Schema 和权限校验，不会自动获得凭据或执行权。

## 领域团队最小交付物

数据库、VM、存储、中间件、云资源或网络团队无需修改核心代码，应交付：

```text
team-<domain>-sre-plugin/
├── manifest.yaml                 # ID、SemVer、provides/requires、权限与事件
├── provider/                     # 独立只读 discover/evidence/verify 服务
├── skills/<incident>/SKILL.md    # 触发、证据、根因、动作、回滚、成功判据
├── action-catalog.yaml           # 类型化动作；禁止任意 Shell/SQL
├── contract-tests/               # 成功、超时、权限拒绝、回滚和验证
└── README.md                     # 范围、限制、值班归属与兼容性
```

推荐开发顺序：

1. 定义资源、证据和验证合同。
2. 实现只读 Provider；无凭据、超时和上游 5xx 时 fail-closed。
3. 编写一个问题一个 Skill，先匹配一个主 Skill，只有明确跨域依赖时再串行组合辅助 Skill。
4. 把真实操作映射为受控动作 ID，不接受任意命令。
5. 实现变更后的重新取证、业务探针和稳定窗口。
6. 用故障注入或沙箱目标证明完整闭环，再申请生产启用。

详见：

- [插件开发与运行手册](docs/HARNESS_PLUGIN_DEVELOPMENT_ZH.md)
- [插件团队接入手册](docs/PLUGIN_TEAM_ONBOARDING_ZH.md)
- [代码架构、团队协作与扩展指南](docs/TEAM_ARCHITECTURE_AND_EXTENSION_GUIDE_ZH.md)
- [Harness 适配设计](docs/DEEPSEEK_HARNESS_INTEGRATION_ZH.md)
- [Harness 能力对照矩阵](docs/DEEPSEEK_HARNESS_PARITY_MATRIX_ZH.md)
- [Plugin-first 重构路线](docs/PLUGIN_FIRST_REFACTOR_ROADMAP_ZH.md)
- [企业平台评审材料](docs/CISRE_ENTERPRISE_OPS_PLATFORM.pptx)
- [企业平台评审逐页讲稿](docs/CISRE_ENTERPRISE_REVIEW_SPEAKER_NOTES_ZH.md)

## 代码架构

```text
frontend/modern/src/              React/TypeScript 控制台
backend/app/api/features/         API 路由边界
backend/app/services/             编排、Harness、状态与通用服务
backend/app/adapters/<domain>/    数据库/VM/存储/中间件/云只读适配
plugins/                          团队维护的公共/领域插件与模板
agents/                           模型推理；不持有写权限
mcp_servers/                      Kubernetes 类型化工具与执行边界
manifests/ charts/ deploy/        Kubernetes 发布与可选组件
tests/                            合同、回归和闭环测试
docs/                             架构、插件、部署与团队手册
```

不要向 `backend/app/application.py` 继续堆厂商 SDK 或新业务分支。新能力应优先作为外置插件交付；必须进入可信内核时，先定义稳定合同，再落入 `services/` 或 `adapters/<domain>/`，通过 `api/features/` 暴露，并补充失败、超时与脱敏测试。

## 核心稳定与规模化

核心通过 `cisre.kernel.ports/v1` 固定四个可替换端口：追加式 Event Journal、带 fencing token 的分布式 Lease、持久 Job Queue、支持 CAS 的 Snapshot Store。Agent、插件和 Skill 只依赖这些合同，不依赖 PostgreSQL、Redis、Kafka 等具体实现。

```http
GET /api/harness/scalability
```

当前默认文件/进程内后端适合单副本；页面会明确显示 `Single replica`，不会把它误报为分布式就绪。业务量上升前，按同一 Port 换成事务事件存储、分布式租约、持久队列和事务快照，再水平扩展无状态 API/Worker。多副本却仍使用本地后端时，就绪检查会返回明确违规项。

规模化不改变以下稳定语义：每个变更有幂等键、同一目标只有带最新 fencing token 的 Worker 能提交、队列有界且用背压代替虚假的“运维过载”、事件可重放、读模型可重建、插件协议按版本兼容。

## 本地开发

后端：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest tests
python scripts/run_local_stack.py --host 127.0.0.1 --api-port 8080
```

前端：

```bash
cd frontend/modern
npm ci
npm run dev
npm run build
```

提交前至少执行：

```bash
python -m pytest tests
cd frontend/modern && npm run build
```

## Kubernetes 配置原则

- 集群可从 Rancher 配置或页面粘贴/上传 kubeconfig 纳管。
- 凭据必须通过 Kubernetes Secret、工作负载身份或企业凭据服务注入。
- ConfigMap 只放非敏感配置；源码、Fixture、日志、事件和提交历史不得出现 Token、密码、私钥、内网地址或 kubeconfig。
- 生产开启插件页面写入前，先配置持久卷、权限策略、签名摘要、网络白名单和回滚方案。
- 所有 mutation 继续使用逐项人工审批，不允许模型、浏览器或外置插件直接执行任意命令。

## 团队协作

建议使用短生命周期分支和 Merge Request：

```bash
git checkout -b feature/<team>-<capability>
git add <changed-files>
git commit -m "feat(<domain>): add <capability> plugin"
git push -u origin feature/<team>-<capability>
```

Merge Request 应附：合同变化、权限清单、失败路径、回滚方式、测试结果、闭环证据和文档更新。破坏性协议变化必须新增并行 v2，不能静默改变 v1 语义。

## 安全红线

- 不在代码、文档、Fixture、日志或 Git 历史中保存凭据、内网 URL/IP、个人数据。
- 不让模型输出直接成为执行指令；必须归一化为类型化动作。
- 不让插件绕过审批、目标冻结、写后回读和恢复验证。
- 不把外置动态代码的 VM 当作安全边界；高权限 Provider 必须独立进程/容器隔离。
- 不以 HTTP 2xx、命令退出码 0 或模型结论冒充恢复成功。
