# CISRE 插件团队接入手册

本文给数据库、VM/主机、存储、中间件、云平台和网络团队使用。目标不是让各团队修改 CISRE 核心，而是独立交付一个 Provider 插件、一组 Skills 和一个受控执行适配器。

## 1. 与官方 DeepSeek Harness 的对应关系

CISRE 采用官方 DeepSeek Harness 的关键组合原则：Everything is a Plugin、Service Definition / Provider / Consumer 能力 seam、`provides` / `requires` 依赖注入、事件驱动、可逆生命周期、Profile / Bundle / Patch、热重载和仅追加会话事实流。

生产 SRE 场景额外收紧了两条边界：

- 官方动态包使用 JavaScript VM，但官方也明确说明 VM 不是安全边界；CISRE 不把外置代码动态导入 API 进程，只接受声明式插件或隔离的远程只读 Provider。
- 插件没有 Kubernetes、数据库或主机的直接写权限。真实变更必须经过 typed action、策略、逐项人工审批、受控执行器、同目标回读和恢复验证。

因此，团队开发体验保持“加插件、不改核心”，而生产执行权不会随插件一起扩散。

## 2. 一个领域插件应拆成什么

```text
team-<domain>-sre-plugin/
├── manifest.yaml              # 插件 ID、版本、provides/requires、scope、权限
├── provider/                  # 独立容器；只读 discover/evidence/verify
│   ├── Containerfile
│   ├── service.*
│   └── tests/
├── skills/
│   └── <incident-type>/
│       ├── SKILL.md           # 触发、取证、诊断、动作、回滚、验证合同
│       └── references/
├── action-catalog.yaml        # 允许的类型化动作；禁止任意 SQL/Shell
├── contract-tests/            # 成功、依赖缺失、权限拒绝、超时、回滚
└── README.md                  # 产品范围、限制、值班归属、升级兼容性
```

每项能力都按三个角色设计：

| 角色 | 团队负责 | CISRE 负责 |
|---|---|---|
| Service Definition | 稳定数据合同和 SemVer | 依赖解析与兼容门禁 |
| Provider | 厂商 API、指标和领域知识 | 生命周期、作用域、超时、审计 |
| Consumer | Skill、动作目录、恢复判据 | 模型路由、审批、执行、回读、成效记录 |

## 3. 插件 manifest

在“平台能力 → 插件中心 → 导入插件”选择数据库、VM 或存储模板。最小 manifest：

```yaml
apiVersion: cisre.io/v1alpha1
kind: HarnessPlugin
metadata:
  name: team.database-provider
  version: 1.0.0
  description: Database inventory, evidence and verification provider.
spec:
  category: domain
  domains: [database]
  agents: [database]
  scope: global
  priority: 50
  provides:
    - {name: inventory.database, version: 1.0.0}
    - {name: evidence.database, version: 1.0.0}
    - {name: verification.database, version: 1.0.0}
  requires:
    - {name: session.events, constraint: ">=1.0,<2"}
  events:
    - {name: inventory/discovered, mode: observe}
  permissions:
    - service:provide
    - inventory:read
    - evidence:read
    - events:publish
    - ops:propose
    - ui:contribute
  runtime: {type: declarative}
  config: {adapter_contract: database/v1}
  ui: {group: database, title: Database SRE Provider}
```

声明式插件用于注册合同和 UI 贡献。需要实时访问厂商 API 时，把 runtime 改为隔离远程 Provider：

```yaml
runtime:
  type: remote
  endpoint: http://database-sre-provider.platform.svc.cluster.local:9000/invoke
  operations:
    - {id: discover, read_only: true}
    - {id: collect_evidence, read_only: true}
    - {id: verify, read_only: true}
```

远程 Provider 需要 `network:egress`，镜像/manifest 摘要必须加入 `HARNESS_TRUSTED_PLUGIN_DIGESTS`，目标主机必须属于集群 `.svc` 或 `HARNESS_REMOTE_ALLOWED_HOSTS`。即使受信，它也拿不到 `ops:execute`、`kubernetes:mutate` 或 `secrets:read`。

## 4. 数据合同

机器可读合同可从 `GET /api/infrastructure/contracts` 获取。团队必须实现：

- `discover`：返回 `cisre.infrastructure.resource/v1`；稳定资源 ID、领域、厂商、环境、位置和非敏感 facts。
- `collect_evidence`：返回 `cisre.infrastructure.evidence/v1`；采集时间、健康状态、signals、metrics、events、dependencies 和 collection_errors。
- `verify`：返回 `cisre.infrastructure.verification/v1`；逐项 checks、`status` 和 `recovered`。只有 `status=completed` 才允许 `recovered=true`。

凭据只从 Kubernetes Secret、工作负载身份或企业凭据服务解析。请求、事件、manifest、日志和回执都不能包含密码、token、私钥或 API key。

## 5. 数据库组怎么做

数据库 Provider 至少覆盖：

- 资产：实例、集群、主从角色、引擎版本、业务归属、备份策略；
- 证据：连通性、连接利用率、慢 SQL 摘要、锁等待、长事务、复制延迟、表空间/磁盘、备份新鲜度；
- Skills：连接耗尽、锁阻塞、复制延迟、容量风险、备份失败、参数漂移；
- 类型化动作：`db_kill_session`、`db_apply_parameter_profile`、`db_failover`、`db_expand_storage` 等审批动作 ID；
- 验证：连接率回落、阻塞链消失、复制延迟回归、空间高水位解除、业务探针恢复且稳定窗口通过。

动作参数只能引用审批后的 profile、session ID 或资源 ID，不能传任意 SQL。数据库组的执行服务接收 `INFRASTRUCTURE_ACTION_WEBHOOK_URL` 发来的已审批合同，返回 `audit_id`、脱敏 evidence 和 rollback_hint；CISRE 随后再次调用数据库 Provider 验证。

## 6. VM/主机组怎么做

VM Provider 至少覆盖：

- 资产：主机/虚机、操作系统、资源池、业务归属、网络区、Agent 状态；
- 证据：CPU/内存/磁盘/ inode、进程与 systemd、系统日志摘要、网络连通性、文件句柄、内核/OOM、快照状态；
- Skills：磁盘满、inode 耗尽、服务退出、OOM、文件句柄耗尽、网络不可达、时间漂移、证书过期；
- 类型化动作：`vm_restart_service`、`vm_expand_disk`、`vm_apply_sysctl_profile`、`vm_revert_snapshot` 等动作 ID；
- 验证：目标服务 active、业务端口/探针正常、原始错误消失、资源水位恢复、稳定窗口内无重复异常。

VM 插件不能接收任意 Shell。真实执行由堡垒机、Ansible/AWX、SaltStack、虚拟化平台或企业脚本平台按动作目录完成。

## 7. Skill 合同

每个问题类型一个 Skill，至少声明：

1. `symptoms` 与适用资源；
2. `evidence_required`、可区分候选根因的渐进取证顺序和证据失败策略；
3. 根因判据及反证；
4. `allowed_actions`、风险、最小变更、幂等键；
5. rollback；
6. `success_criteria`、稳定窗口和未恢复时的换路策略。

路由默认先选一个最高匹配主 Skill；只有主 Skill 明确缺少另一个领域能力时，才串行组合辅助 Skill。调用次数按一次实际执行链记录，而不是按 UI 刷新或候选匹配次数累计。

网络团队按相同合同接入：Provider 至少提供拓扑、接口/链路状态、丢包/时延、路由、DNS、负载均衡、ACL/安全策略和最近变更证据；动作使用 `network_apply_policy`、`network_switch_route`、`network_update_load_balancer` 等类型化 ID，禁止任意设备命令；验证必须重新读取路径、策略命中、链路质量和业务连通性。

## 8. 本地与联调验收

团队交付前必须通过：

1. manifest schema、SemVer、依赖和权限校验；
2. Provider 在无凭据、超时、厂商 API 5xx 和部分数据时 fail-closed；
3. 插件卸载后服务与事件监听完全释放；
4. Profile 切换和热重载不重启 SRE 核心；
5. 动作未经审批不可执行，审批后的参数与目标不能被替换；
6. 变更回执后必须重新采集，不以“API 2xx”冒充恢复；
7. 只有 `verification.recovered=true` 才进入运维成效；
8. 全链路记录插件、Skill、动作、审批人、回执、恢复判据和最终结果。

生产首次启用前，在 ConfigMap 设置 `HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED=true` 才能从页面安装；Profile 在线切换另需 `HARNESS_PROFILE_RUNTIME_WRITE_ENABLED=true`。如果不希望开放页面写入，则保持 false，由平台组把 manifest 挂载到 `HARNESS_PLUGIN_ROOT` 后执行热重载。
