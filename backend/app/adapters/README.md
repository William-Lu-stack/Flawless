# CISRE 基础设施 Adapter SDK

这里是 Kubernetes 之外的数据库、虚拟机、存储、中间件、云资源和网络接入边界。各领域团队只在自己的目录中实现厂商差异，不把厂商 SDK、字段判断或凭据处理写进 `backend/app/application.py`。

## 安全边界

进程内 Adapter 只能实现三件只读能力：

1. `discover`：发现资源并返回统一 `InfrastructureResource`。
2. `collect_evidence`：采集诊断证据并返回 `EvidenceBundle`。
3. `verify`：在变更后重新取证并返回 `VerificationResult`。

Adapter **不能执行变更**。所有数据库命令、主机脚本、存储变更和云 API 写操作必须走：

```text
Skill 方案 -> OpsJob -> 逐项人工审批 -> 动作白名单
           -> INFRASTRUCTURE_ACTION_WEBHOOK_URL -> 写后回读 -> Adapter.verify
```

模型输出的任意 Shell、SQL、HTTP URL 或 Secret 都不能直接成为执行输入。

## 目录所有权

```text
backend/app/adapters/
├── contracts.py          # 版本化数据合同；平台核心组维护
├── registry.py           # Adapter 注册与运行时类型校验；平台核心组维护
├── domains.py            # 前端风险域目录；新增域走评审
├── database/             # DBA / 数据库平台组
├── virtual_machine/      # OS / 虚拟化平台组
├── storage/              # 存储平台组
├── middleware/           # 中间件平台组
├── cloud_service/        # 云平台组
└── network/              # 网络平台组
```

每个产品一个模块，例如 `database/mysql.py`、`storage/oceanstor.py`。不要把多个厂商实现堆在 `infrastructure_providers.py`。

## 最小实现

```python
from datetime import datetime, timezone

from backend.app.adapters import (
    AdapterDescriptor,
    EvidenceBundle,
    InfrastructureResource,
    VerificationResult,
)


class ExampleDatabaseAdapter:
    descriptor = AdapterDescriptor(
        id="example-database",
        domain="database",
        display_name="Example Database",
        version="1.0.0",
        provider="example",
        supported_products=("ExampleDB",),
    )

    async def discover(self, request):
        return [InfrastructureResource(
            id="db-prod-01",
            domain="database",
            name="db-prod-01",
            provider="example",
            endpoint_ref="secret://cisre-adapters/example-db",
            facts={"engine": "exampledb", "role": "primary"},
        )]

    async def collect_evidence(self, resource, request):
        return EvidenceBundle(
            resource_id=resource.id,
            domain=resource.domain,
            observed_at=datetime.now(timezone.utc).isoformat(),
            health="degraded",
            signals=[{"type": "replication_lag", "value": 90, "unit": "seconds"}],
        )

    async def verify(self, resource, receipt, criteria):
        # 必须重新访问真实目标，不能仅相信执行器回执。
        checks = [{"criterion": item, "passed": True} for item in criteria]
        return VerificationResult(
            resource_id=resource.id,
            recovered=True,
            status="completed",
            checked_at=datetime.now(timezone.utc).isoformat(),
            checks=checks,
        )
```

启动阶段由组合根或插件模块显式注册：

```python
from backend.app.adapters import ADAPTER_REGISTRY

ADAPTER_REGISTRY.register(ExampleDatabaseAdapter())
```

禁止在被 import 时偷偷联网、写数据库或执行资源变更。注册必须显式、可测试、可卸载。

## 三种接入方式

1. **资产推送**：现有 CMDB 调用 `POST /api/infrastructure/resources/sync`，最快，适合先完成资源纳管。
2. **外部 HTTP Adapter**：在 `INFRASTRUCTURE_ADAPTERS_JSON` 中登记服务端白名单地址，适合 Adapter 独立发布。
3. **进程内 Python Adapter**：实现本目录的强类型协议，适合低延迟且依赖可控的只读取证。

三种方式输出同一资源/证据语义；真实写操作始终走外部受控执行器。

## 版本与兼容

- 当前协议：`cisre.infrastructure.adapter/v1`。
- v1 只能新增可选字段；已有字段含义、枚举值和审批语义不可修改。
- 破坏性变化必须并行发布 v2，并至少保留一个完整发布线的 v1。
- API 可实时查看合同：`GET /api/infrastructure/contracts`。
- 风险域可实时查看：`GET /api/operations/domains`。

## 凭据规则

- 凭据只能来自 Kubernetes Secret、环境变量、工作负载身份或企业 Secret Manager。
- Resource、Evidence、日志、异常、回执和测试夹具中不得包含 password/token/key/credential 字段。
- `endpoint_ref` 只保存不可逆引用，不保存 DSN 密码。
- 新 Adapter 必须有脱敏测试和凭据泄漏测试。

## 合并门禁

新增 Adapter 至少提交：实现、领域 README 更新、发现/取证/验证合同测试、异常与超时测试、脱敏测试、示例配置。合并前执行：

```bash
python -m pytest tests
cd frontend/modern && npm run build
```
