# 企业统一运维平台提案：把 CISRE 确立为内部 SRE Harness

> 用途：技术评审 / 立项汇报 / 说服领导与同事
> 版本：CISRE 5.3.0 · 基线对比：`@deepseek-ai/dsh` 0.1.0-rc.6（deepseek-harness）

---

## 0. 一句话结论

**CISRE 已经是一个可持久化、可审批、可验证恢复的 SRE 执行底座（harness），并且比通用编码 Agent 的 harness 更严格——模型只规划、平台管执行。**
它足够作为企业内部统一的运维基础框架；要真正覆盖"从云到数据库"的全栈，缺的不是推倒重来，而是补齐**编排面（多智能体/工作流）、执行面（非 K8s 目标的受控执行器）和基础设施面（多副本存储 + OTel + 多端入口）**三个明确缺口。

---

## 1. CISRE 现在是什么（不是"运维聊天框"）

CISRE 把"发现问题 → 采集证据 → 根因诊断 → 动态匹配 Skill → 变更预演 → 人工审批 → 执行变更 → 恢复验证 → 经验沉淀"连成一个**可审计闭环**。核心原则：

- **模型只负责理解证据、提出假设、排序 Skill、给出方案**；不能直接写集群。
- **平台负责**动作目录、RBAC、风险门禁、人工逐项审批、执行、回滚、写后回读、恢复验证。
- **Kubernetes API 返回 2xx 不等于恢复**：只有新 Pod Ready + 原始错误消失 + `verification.recovered=true` 才结案。
- 未恢复就保留失败轨迹、换差异化策略继续，绝不"自报成功"。

已具备的关键资产（均已在 5.x 版本落地并有测试证据）：

| 能力 | CISRE 落点 |
|---|---|
| 持久化 SRE 执行 | `CISREDurableHarness/v3` + `ResumableSREHarness/v1`：阶段检查点、工具/变更回执、重复轨迹停滞检测、确定性完成判定 |
| Everything-is-a-Plugin | `CISREPluginHarness/v1`：作用域 DI、可逆 Effect、`observe/serial/parallel/waterfall` 四类事件、12 个内置插件 |
| 上下文压缩 | `CISREImportanceCompactor/v1`：优先保留 ERROR/FATAL/WARNING、securityContext、PVC/PV |
| Skill 路由 | Contextual Bayesian Skill Router：主 Skill 优先、依赖门禁、成效后验 |
| 审批门禁 | fail-closed 瀑布门禁 + `approval_id + change_fingerprint` 一次性审批凭据 |
| 写后回读 | 同通道递归逐字段比对 + resourceVersion 前进校验 |
| 恢复验证 | Rollout + 新 Pod + 日志/Events + 业务判据 |
| 目标驱动续跑 | Goal Round Driver：诊断→变更→验证 直到恢复或人工干预 |
| 后台任务 | owner-scoped jobs、single-flight 写入租约、排队不饿死控制链路 |
| 模型路由 | 多 OpenAI-compatible / OAuth 网关，可替换、可回退确定性引擎 |

---

## 2. 与 DeepSeek Harness 的差距分析

> DSH 是"通用编码 Agent 的 harness"：插件/沙箱/审批/会话/目标/子代理/工作流/多端入口一整套运行时。CISRE 已经把它的**控制面（插件内核、事件、压缩、目标、任务、审批、Telemetry）同构落入 Python**，缺的主要是**编排与执行广度**。

### 2.1 已经对齐（CISRE 已具备，无需重写）

Everything-is-a-Plugin、作用域 DI、可逆 Effect、四类事件、Append-only Session Event、Importance Compaction、Skill Registry/portable packages、Goal Round、Owner-scoped Jobs、Todo/阶段检查点、人工审批（比 DSH 的 ask-user 更强，是一次性绑定审批凭据）、变更预演（近似 Plan mode 的"变更前预演"语义）、Telemetry（Langfuse + 部分 OTel）、多模型路由、Web 控制台、MCP Server（对外暴露 K8s 工具）。

### 2.2 缺口（DSH 有、CISRE 尚未或部分具备）

| # | DSH 能力 | CISRE 现状 | 对"统一运维平台"的意义 | 优先级 |
|---|---|---|---|---|
| 1 | **Subagent 委托与控制**（spawn/fork、list/send/interrupt） | 只有固定的 A2A 命名代理（healing/incident/postmortem/observability），无通用子代理委托原语 | 证据并行采集、跨集群/跨资源并行处置、专家子代理隔离 | 高 |
| 2 | **Workflow 编排**（脚本 fan-out：pipeline/parallel/phases/agent） | 无通用编排引擎 | 全集群巡检、批量发现、多目标并行、分阶段编排 | 高 |
| 3 | **Ralph 循环**（fresh-agent + 共享持久记忆迭代） | 无 | 长链路自愈的"换新脑重试"韧性模式 | 中 |
| 4 | **通用沙箱执行器**（bash/pwsh/fs/str-replace + sandbox 策略） | 刻意不把自由 Shell 放进模型边界（正确），但缺"受控、审计、可回滚"的**通用 Runbook/CLI 执行器** | 数据库(psql/mysql)、VM(SSH)、云(CLI/SDK) 的变更执行 | 高 |
| 5 | **通用 MCP 客户端**（消费第三方 MCP 工具） | 只对外提供 MCP Server，不消费外部 MCP | 接入 DBA 工具、ITSM、云 CLI 等现成 MCP 能力 | 中 |
| 6 | **Plan mode 作为一等模式** | 仅对"变更"有预演+审批，缺通用"只规划不执行"模式 | 复杂跨资源变更的规划评审 | 中 |
| 7 | **统一会话/任务存储 + 可检索 + 标题 + 导出** | Job checkpoint/audit/records 已持久化，但单副本 JSON 存储、无统一会话检索 | API 多副本、审计留存、合规检索 | 高 |
| 8 | **OTel GenAI 语义导出** | 有 Langfuse + 部分 OTel，未统一导出 GenAI semantic conventions | 统一的模型/工具/轨迹可观测与成本核算 | 中 |
| 9 | **多端入口**（Web/Headless/CLI/TUI） | 有 Web + API，无 headless/CLI/TUI | CI/CD 批处理、值班终端、自动化接入 | 中 |
| 10 | **Web 搜索 / 反馈 / 权限预设抽象** | 有知识库 RAG、成效统计；无实时 Web 检索、逐条反馈、可复用权限预设 | 值班检索、质量反馈闭环、策略复用 | 低 |

> 一句话：**控制面已 70–80% 对齐，编排面和执行面是主要缺口。** 这些缺口正是"从 K8s 单域走向云→数据库全栈"所必需的能力。

---

## 3. 够不够做内部 SRE Harness？——判定：够，且更严格

**结论：够。** 判定标准不是"是否复刻了 DSH 的每一行 TypeScript"，而是 harness 的四个本质属性：

1. **持久执行（durable execution）**：进程/网络/主机故障后能从确定检查点恢复 —— CISRE 有 `ResumableSREHarness/v1` 检查点 + 孤儿任务自恢复。
2. **模型无关的完成判定（deterministic completion）**：模型不能自报成功 —— CISRE 的 `RecoveryVerifier` 只有实时恢复证据才能结案。
3. **可逆副作用 + 审批（reversible effects + approval）**：每次变更可回滚、逐项人工审批 —— CISRE 的审批凭据 + 回滚 Patch + 写后回读。
4. **可观测与沉淀（observability + learning）**：轨迹、回执、成效入库 —— CISRE 的 records/effectiveness/Skill 指标。

CISRE 与通用 DSH 的关键差异是**风险边界更严**：通用 harness 把 Shell/FS 作为默认工具交给模型，CISRE 把这些全部挡在模型边界之外、只通过动作目录 + 审批 + 回读放行。**这正是企业敢把它用于生产基础设施的原因。**

**边界条件（诚实声明）**：当前"够"是针对 Kubernetes/Rancher 单域。要做到"云→数据库全栈"，还需把第 2.2 节的执行面补上——否则它仍是一个优秀的 K8s SRE harness，而不是"统一运维平台"。

---

## 4. 如何成为"从云到数据库"的统一运维平台

### 4.1 目标架构（三层）

```text
体验层   Web 控制台 · Headless/CLI · TUI · 统一检索 · 逐条反馈
控制面   CISREDurableHarness/v3 + CISREPluginHarness/v1
         + 目标驱动自主运行 · 多智能体编排(Subagent/Workflow) · 统一任务/会话存储
执行面   Guarded Executor（动作目录 · 审批 · 回滚 · 回读 · 恢复验证）
         ├─ Kubernetes/Rancher/kubeconfig（已就绪）
         ├─ 基础设施 Adapter Contract（DB / VM / 中间件 / 存储 / 云）—— 补"变更执行+恢复验证"
         ├─ 受控 Runbook/CLI 执行器（psql/mysql/ssh/cloud CLI，沙箱+审计）—— 新增
         └─ 通用 MCP 客户端（消费 DBA/ITSM/云 MCP 工具）—— 新增
```

### 4.2 执行面统一化（最关键的一步）

把今天只对 K8s 成立的"证据→审批→执行→回读→验证"**抽象成对任意资源成立的一份合同**，每类资源 Adapter 必须实现：

- 只读发现（discover / inventory / scan）
- 动作 schema（允许的动作 + 参数 + 风险等级）
- 变更预演（dry-run / explain）
- 回滚元数据（before snapshot + rollback action）
- **该资源类型的"恢复判据"**（数据库=连接/慢查询/主备状态；VM=探针/CPU/磁盘；云=实例状态/配额/账单——而非只会看 Pod Ready）

> 复用现有 `INFRASTRUCTURE_ACTION_WEBHOOK_URL`（审批后交给 DBA/云管/ITSM 执行）作为过渡，最终把 Adapter 的变更动作也纳入统一 Guarded Executor 的审批→执行→验证闭环。

### 4.3 控制面补齐（三件事）

1. **多智能体编排**：引入 subagent 委托 + workflow fan-out，用于证据并行采集、全集群巡检、多集群并行处置。
2. **统一持久层**：把单副本 JSON Job Store 升级为 PostgreSQL/Redis 租约（或 Temporal-compatible），支持 API 多副本 + 审计检索。
3. **目标驱动运行 + OTel GenAI**：把 Goal Round 从"事故"泛化为任意"目标"，统一导出 OTel GenAI 语义。

---

## 5. 分阶段路线图

| 阶段 | 目标 | 关键交付 |
|---|---|---|
| **P0 确立底座（0–2 周）** | 确认 CISRE 为内部 SRE 基础框架 | 内部镜像/Helm 上线、SSO/审计基线、RBAC preset、试点 1–2 个集群 |
| **P1 执行面扩围（1–3 月）** | 从 K8s 到数据库/VM/云 | Infrastructure Adapter SDK + OpenAPI、每类资源的恢复判据、受控 Runbook 执行器、阿里云 RAM 参考实现 |
| **P2 编排与规模（2–6 月）** | 全栈自主运维 + 多副本 | Subagent/Workflow、PostgreSQL/Redis 租约、OTel GenAI、Headless/CLI |
| **P3 生态与治理（持续）** | 平台即基础设施 OS | Skill 网络、故障注入基准集、模型/Skill 离线评测门禁、合规报告 |

---

## 6. 风险与对策（诚实清单）

| 风险 | 说明 | 对策 |
|---|---|---|
| 上游 DSH 为 Developer Preview | Python SDK `0.0.0.dev0`，可能破坏性变更 | 不替换现有执行面；上游只作可选 Planner，需稳定版+SBOM+漏洞扫描后才启用 |
| 单副本存储 | 当前 API 要求副本=1 | P2 引入分布式租约/事务存储前不宣称多副本 |
| License（PolyForm Noncommercial） | 源码可看、非商业可用；商用/打包/再分发需授权 | 内部自用属非商业，落地前与维护者取得书面授权，规避合规风险 |
| 供应链 | 镜像/依赖/模型网关 | 国内镜像、哈希锁定、pip-audit、SBOM、TLS |
| 模型不确定性 | 模型输出不可信 | 已由动作目录+审批+回读+验证四道闸约束，模型只规划 |

---

## 7. 给领导 / 同事的论证要点（价值主张）

1. **不是再买一个工具，而是把已有资产"升级为平台底座"**：CISRE 已具备 SRE harness 的本质能力，投入是补缺口而非重写。
2. **可证明的可靠性**：写后回读 + 恢复验证 + 审批凭据，让"它到底修好了没有"从口头承诺变成可审计证据。
3. **安全边界明确**：模型只规划、平台管执行，天然符合"人审、可回滚、可追溯"的企业治理要求。
4. **一次建设、全栈复用**：统一资源合同 + Adapter 模式，K8s/数据库/VM/云共享同一套审批与验证机制。
5. **持续增值**：每解决一次故障，Skill 与成效入库，平台越用越强——这是"基础框架"的复利。
6. **对齐行业方向**：设计与 Microsoft Agent Framework Harness / LangGraph / Temporal / DeepSeek Harness 的成熟模式同构，不闭门造车。

---

## 附：配套汇报材料

- 完整架构：`docs/CISRE_ARCHITECTURE_ZH.md`
- Harness 适配设计：`docs/DEEPSEEK_HARNESS_INTEGRATION_ZH.md`
- 技术/测试证据：`docs/PROJECT_TECHNICAL_MATERIALS_ZH.md`
- PPT：`docs/CISRE_ENTERPRISE_OPS_PLATFORM.pptx`（由本提案生成）
