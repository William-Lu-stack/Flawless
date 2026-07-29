# Argo Rollouts 灰度发布与 SRE 门禁

## 1. 这次实现解决了什么

Flawless 的标准 Deployment 发布不再一次性修改全部副本。发布计划先由爆炸半径算法计算允许的灰度上限，再由 Argo Rollouts 在 Kubernetes 中执行真实 canary：

```text
人工批准发布
  → 创建 AnalysisTemplate 和 workloadRef Rollout
  → 等待原 Deployment 成为 Healthy stableRS
  → 写入新 Pod template
  → setWeight(小批次)
  → AnalysisRun 读取实时 SLO/Prometheus
  → 通过后才进入下一批
  → 算法上限处无限暂停
  → 人工二次批准后全量晋级
```

任何一批出现硬性 SLI 或错误预算违规时，Argo 自动恢复 stableRS。Flawless 随后还会把源 Deployment 的 Pod template 恢复为 stableRS 对应模板，避免声明态残留失败版本。只有运行副本和源声明态都恢复后，回滚才记为完成。

## 2. SRE 约束

- 灰度权重来自现有发布风险与爆炸半径算法，不由前端写死。
- 副本权重的最小粒度是一个 Pod。若一个 Pod 已超过算法批准的最大影响面，发布会被阻断并提示先扩容。例如最大灰度 10% 时至少需要 10 个副本。
- 每个 `setWeight` 后都有独立 AnalysisRun，不会只在最后检查一次。
- 默认必须取得 Prometheus 错误率；缺少、过期、格式错误或不可达的指标均为“不确定”，绝不会按通过处理。
- 错误预算冻结、错误率超过阈值 2 倍、P99 超过阈值 1.5 倍会触发硬失败和自动回退。
- 达到灰度上限只代表 `canary_validated`，不是发布成功。全量晋级是新的高风险动作，需要用户再次确认。
- `failureLimit=0` 与 `inconclusiveLimit=0` 表示第一条失败或不确定测量立即停止扩大影响面。
- `maxUnavailable=0`、`maxSurge=1`，并使用 preferred anti-affinity 减少同节点相关故障。

StatefulSet、DaemonSet 和紧急变更暂时沿用原有受控 rolling 流程；当前真实 Argo 灰度只用于现有 Deployment。没有 Rancher 或 Web kubeconfig 执行面的集群不会伪装成支持。

## 3. 安装控制器

固定版本为 Argo Rollouts `v1.9.1`。默认国内镜像已锁定 tag 和 digest：

```text
quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1@sha256:15c0d41f2c69a382d4399bcb28ed4f03ee9f58b56cfc9e6cd55bcbf0f311c06d
```

联网安装：

```bash
chmod +x deploy/argo-rollouts/install-domestic.sh
deploy/argo-rollouts/install-domestic.sh
```

控制节点不能访问 GitHub时，先把官方 `install.yaml` 放到机器上：

```bash
ARGO_ROLLOUTS_INSTALL_MANIFEST=./argo-rollouts-v1.9.1-install.yaml \
  deploy/argo-rollouts/install-domestic.sh
```

完全离线时先把镜像同步到企业仓库：

```bash
docker pull quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1
docker tag \
  quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1 \
  registry.example.com/platform/argo-rollouts:v1.9.1
docker push registry.example.com/platform/argo-rollouts:v1.9.1

ARGO_ROLLOUTS_INSTALL_MANIFEST=./argo-rollouts-v1.9.1-install.yaml \
ARGO_ROLLOUTS_IMAGE=registry.example.com/platform/argo-rollouts:v1.9.1 \
  deploy/argo-rollouts/install-domestic.sh
```

## 4. 部署 Flawless 变更

Helm Chart 和原生 RBAC 已包含以下能力：

- 创建、读取和更新 Rollout、AnalysisTemplate、AnalysisRun；
- patch `rollouts/status` 以提交人工晋级或中止；
- 读取 stable ReplicaSet；
- patch 源 Deployment 以完成声明态回滚。

应用更新：

```bash
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/deployment.yaml
```

Helm：

```bash
helm upgrade --install flawless ./charts/flawless \
  --namespace k8s-agent \
  --create-namespace \
  -f charts/flawless/values-production.example.yaml
```

AnalysisRun 从 Argo 控制器所在 namespace 回调 Flawless，因此地址必须使用跨 namespace 可解析的 FQDN，例如：

```text
http://k8s-agent-api.k8s-agent.svc.cluster.local:8080
```

Chart 会按 Helm release 名称和 namespace 自动生成这个 FQDN。

## 5. 生产配置

```yaml
data:
  GRAY_RELEASE_ENABLED: "true"
  GRAY_RELEASE_BASELINE_TIMEOUT_SECONDS: "180"
  GRAY_RELEASE_CHANGE_TIMEOUT_SECONDS: "240"
  GRAY_RELEASE_VERIFY_TIMEOUT_SECONDS: "3600"
  GRAY_RELEASE_REQUIRE_PROMETHEUS: "true"
  GRAY_RELEASE_DEFAULT_ERROR_RATE_PROMQL: |
    sum(rate(http_requests_total{service="{service}",status=~"5.."}[5m]))
    /
    clamp_min(sum(rate(http_requests_total{service="{service}"}[5m])), 0.000001)
  GRAY_RELEASE_DEFAULT_P99_LATENCY_PROMQL: |
    histogram_quantile(
      0.99,
      sum by (le) (rate(http_request_duration_seconds_bucket{service="{service}"}[5m]))
    ) * 1000
```

可用占位符为 `{service}`、`{namespace}`、`{workload}`、`{cluster}`。不同业务指标命名不一致时，应在发布界面为本次发布填写 PromQL，或在生产 ConfigMap 中设置组织级默认查询。

## 6. 状态语义

| 状态 | 含义 | 是否发布成功 |
|---|---|---|
| `canary_running` | 正在逐批灰度和分析 | 否 |
| `canary_validated` | 已通过算法上限内门禁，等待人工晋级 | 否 |
| `analysis_inconclusive` | 指标缺失或不确定，停止扩大 | 否 |
| `degraded` | 分析或 Workload 健康失败 | 否 |
| `rolled_back` | stableRS 与源 Deployment 模板都已恢复 | 否，发布已中止 |
| `fully_promoted` | 新 stableRS 与期望 Ready/Available 全部收敛 | 是 |

## 7. 验证

单元测试：

```bash
.venv/bin/python -m pytest -q \
  tests/test_progressive_delivery.py \
  tests/test_cluster_registry.py \
  tests/test_slo_and_execution.py
```

真实 K3s/Argo 控制器测试：

```bash
.venv/bin/python tests/e2e/run_argo_progressive_delivery.py \
  --kubeconfig /path/to/kubeconfig
```

该测试直接调用控制台使用的执行器和恢复验证器，并验证：

1. 10% canary 成功后停在人工批准点；
2. `rollouts/status` 人工晋级后全量收敛；
3. AnalysisRun 硬失败后自动恢复 stableRS；
4. 源 Deployment template 恢复为旧稳定版本。
