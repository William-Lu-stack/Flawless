# SRE initiate Harness 插件开发与运行手册

官方 DeepSeek Harness 的插件、Service seam、事件、Profile/Bundle/Patch 和动态扩展设计已映射到本运行时；跨团队的数据库/VM/存储接入清单见 [PLUGIN_TEAM_ONBOARDING_ZH.md](./PLUGIN_TEAM_ONBOARDING_ZH.md)。

## 1. 目标与边界

CISRE 已把 DeepSeek Harness 的核心组合思想落到现有生产运维控制面：功能以插件提供服务，消费者只依赖服务名；Profile 按顺序组合 Bundle 并应用 Patch；插件加载、卸载、事件与运维阶段写入追加式会话事件流。

这不是把 Kubernetes 写权限交给第三方插件。任何真实变更仍必须走 CISRE 的固定链路：

`typed action → policy/risk → human approval → executor → same-target readback → recovery verifier → record`

外置插件不能注册 `kubernetes:mutate`、`ops:execute` 或 `secrets:read`，也不能在 API 进程内执行任意 Python。需要接企业系统的执行能力时，应实现独立隔离服务，再由 CISRE 的受信 Provider/Adapter 契约调用。

## 2. 目录契约

默认根目录由 `HARNESS_PLUGIN_ROOT` 指定，生产清单使用 `/var/lib/flawless/harness`，它位于 API 的 runtime-store PVC 中：

```text
harness/
├── packages/
│   └── database-risk-provider.yaml
├── bundles/
│   └── fullstack-readonly.yaml
└── profiles/
    └── team-production.yaml
```

示例位于 `examples/harness/`。把对应文件放入上述目录，调用 `POST /api/harness/plugins/reload` 即可发现，不需要修改、重新编译或重新发布 CISRE 核心镜像。

## 3. 插件声明

插件必须声明稳定 ID、语义化版本、提供/需要的服务、事件模式、作用域与权限：

```yaml
apiVersion: cisre.io/v1alpha1
kind: HarnessPlugin
metadata:
  name: team.database-inventory
  version: 1.2.0
spec:
  scope: global
  provides:
    - name: inventory.database
      version: 1.2.0
  requires:
    - name: session.events
      constraint: ">=1.0,<2"
  events:
    - name: inventory/discovered
      mode: observe
  permissions: [service:provide, inventory:read, events:publish]
  runtime: {type: declarative}
```

运行时会先解析依赖与版本约束。依赖缺失时插件保持 `pending_dependencies`；权限不允许时为 `policy_denied`；不会以缺省全权限继续运行。

支持的事件模式：

- `observe`：只观察，不改变下游载荷；
- `serial`：按优先级串行处理；
- `parallel`：并行观察；
- `waterfall`：中间件式单调门禁，可增加限制，不能绕过后续门禁。

作用域可使用 `global`、`agent:<id>`、`job:<id>` 或 `cluster:<id>`。解析服务时更近的作用域优先，全局 Provider 仍保留为后备。

## 4. Profile、Bundle 与 Patch

Bundle 是有序插件集合及默认 Patch。Profile 依次叠加 Bundle，最后应用自己的 Patch：

```yaml
apiVersion: cisre.io/v1alpha1
kind: HarnessProfile
metadata: {name: team-production}
spec:
  bundles: [fullstack-readonly]
  plugins: [team.extra-provider]
  patches:
    - id: team.database-inventory
      priority: 80
      config: {region: primary}
    - id: team.old-provider
      enabled: false
```

同一服务可有多个 Provider。运行时按作用域、优先级和注册顺序确定当前 Provider；因此开发环境和生产环境可以使用不同模型、证据源、数据库/VM/存储适配器，而消费者代码不需要环境判断。

Profile 在线切换默认关闭。确认组织治理方式后设置：

```yaml
HARNESS_PROFILE_RUNTIME_WRITE_ENABLED: "true"
HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED: "true"
```

随后可在“平台能力 → 插件中心 → 插件与 Profile”操作，或调用 `POST /api/harness/profiles/activate`。切换和重载都会写入审计事件流。

## 5. 权限与隔离

外置声明式插件默认可申请：

`service:provide`、`events:publish`、`ui:contribute`、`inventory:read`、`evidence:read`、`model:invoke`、`ops:propose`

文件读写、网络、子进程属于特权能力。签名摘要必须列入 `HARNESS_TRUSTED_PLUGIN_DIGESTS`，且权限还要列入 `HARNESS_TRUSTED_PLUGIN_PERMISSIONS`。即使签名通过，`kubernetes:mutate`、`ops:execute` 和 `secrets:read` 仍由核心保留。

这比把第三方代码动态导入 API 进程更容易证明隔离边界。独立 Provider 服务应使用容器级只读根文件系统、NetworkPolicy、专用 ServiceAccount 和资源限制；SRE initiate 只保存其非敏感服务声明。

签名 remote Provider 可以声明只读 JSON 操作：

```yaml
runtime:
  type: remote
  endpoint: http://database-provider.cisre.svc.cluster.local:9000/invoke
  operations:
    - {id: discover, read_only: true}
    - {id: collect_evidence, read_only: true}
```

目标必须是集群内 `.svc` 地址或 `HARNESS_REMOTE_ALLOWED_HOSTS` 白名单；重定向被禁止，超时最多 30 秒，响应最多 1 MiB。调用入口为 `POST /api/harness/services/{service}/invoke`。标记为非只读的操作不会注册；真实变更必须转入 OpsJob 审批执行链。

## 6. 事件溯源、回放与分支

运维 Harness 每个阶段都会持久化 `cisre.session-event/v1`：事件包含 session、sequence、parent、phase、plugin/tool、target 和前一事件哈希。敏感字段和 Bearer 值在写盘前脱敏。

接口：

- `GET /api/harness/sessions`：会话索引；
- `GET /api/harness/sessions/{id}/events`：事件、完整性和子会话；
- `GET /api/harness/sessions/{id}/replay`：从事件重建投影；
- `POST /api/harness/sessions/{id}/fork`：从指定事件创建分支；
- `POST /api/harness/sessions/{id}/resume`：写入恢复点并返回最新投影。

生产 API 仍要求单副本，直到接入分布式执行租约与支持跨进程串行追加的事件后端。当前 JSONL 事实流与 OpsJob 快照互补：前者负责审计/回放，后者负责快速页面读取和现有故障恢复。

## 7. 验收清单

1. 新增 package 文件后，不改核心代码即可在运行时看到插件和服务；
2. 依赖版本不满足时插件不激活；
3. Profile Patch 能切换同一服务的 Provider；
4. 卸载/重载按逆序释放事件与服务注册；
5. 未签名特权插件 fail closed；
6. 运维事件重启后仍可读取，哈希链可校验；
7. fork 产生 parent/child 关系，replay/resume 可重建阶段状态；
8. 所有 K8s 写操作仍逐项审批、写后回读并以新 Pod 恢复证据闭环。
