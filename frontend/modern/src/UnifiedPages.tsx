import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BellRing,
  Bot,
  Boxes,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  CloudCog,
  Database,
  Download,
  FileClock,
  Eye,
  Gauge,
  GitBranch,
  HardDrive,
  Layers3,
  LineChart,
  Loader2,
  MessageSquareText,
  Network,
  Plus,
  RefreshCcw,
  Search,
  Send,
  ServerCog,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  Trash2,
  Upload,
  Workflow,
  X,
} from "lucide-react";
import { useAsync } from "./hooks/useAsync";
import { ApiState, adminAuthHeaders, apiDelete, apiGet, apiPost, asList as list, compactNumber, invalidateApiCache } from "./lib/api";
import { OpsPlanPanel } from "./components/OpsPlanPanel";

function timeText(value: string | undefined) {
  if (!value) return "-";
  try { return new Date(value).toLocaleString("zh-CN", { hour12: false }); } catch { return value; }
}

function SectionHead({ icon: Icon, title, meta, action }: { icon: any; title: string; meta?: string; action?: React.ReactNode }) {
  return <div className="section-head"><div><span><Icon size={16} />{title}</span>{meta && <small>{meta}</small>}</div>{action}</div>;
}

function StatusPill({ status, text }: { status: string; text?: string }) {
  const tone = /ok|up|connected|ready|enabled|healthy/i.test(status) ? "ok" : /disabled|not_configured|unknown/i.test(status) ? "muted" : "warn";
  return <span className={`status-pill ${tone}`}><i />{text || status}</span>;
}

function Empty({ text }: { text: string }) {
  return <div className="unified-empty"><CircleDot size={18} /><span>{text}</span></div>;
}

function Kpi({ label, value, detail, tone = "" }: { label: string; value: React.ReactNode; detail?: string; tone?: string }) {
  return <div className={`unified-kpi ${tone}`}><span>{label}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</div>;
}

export function DashboardPage() {
  const [cluster, setCluster] = useState("all");
  const [selectedProblem, setSelectedProblem] = useState<any>(null);
  const [advice, setAdvice] = useState<ApiState<any>>({ loading: false });
  const [rancher, refreshRancher] = useAsync<any>(() => apiGet("/api/rancher/status"), []);
  const [inventory, refreshInventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => apiGet("/api/dashboard")), []);
  const [metrics, refreshMetrics] = useAsync<any>(() => apiGet(`/api/prometheus/summary?cluster=${encodeURIComponent(cluster)}`), [cluster]);
  const [health, refreshHealth] = useAsync<any>(() => apiGet("/api/health"), []);

  const clusters = list(rancher.data?.clusters);
  const selectedInventory = useMemo(() => {
    const items = list(inventory.data?.inventory);
    if (cluster === "all") return items;
    return items.filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name);
  }, [cluster, inventory.data]);
  const pods = selectedInventory.flatMap((item: any) => list(item.pods));
  const workloads = selectedInventory.flatMap((item: any) => list(item.workloads));
  const nodes = selectedInventory.flatMap((item: any) => list(item.nodes));
  const values = metrics.data?.values || {};
  const fallback = inventory.data?.pods && !inventory.data?.inventory;
  const summary = fallback ? inventory.data : {
    pods: { total: pods.length, running: pods.filter((pod: any) => pod.phase === "Running").length, failed: pods.filter((pod: any) => pod.issue || !pod.ready).length },
    nodes: { total: nodes.length, ready: nodes.filter((node: any) => node.ready).length },
  };
  const problems = pods.filter((pod: any) => pod.issue || (!pod.ready && pod.phase !== "Succeeded"));

  async function askForAdvice(pod: any) {
    setSelectedProblem(pod);
    setAdvice({ loading: true });
    try {
      const data = await apiPost<any>("/api/chat", {
        message: `请基于真实集群证据分析 Pod ${pod.name} 的异常原因并给出简洁处置建议。当前信号：${pod.issue?.reason || pod.phase || "NotReady"}`,
        cluster: pod.cluster || cluster,
        cluster_id: pod.cluster_id || pod.cluster || cluster,
        namespace: pod.namespace || "default",
        deployment: pod.workload_name || pod.workload?.name || "",
        workload_type: pod.workload_kind || pod.workload?.kind || "Workload",
        severity: pod.issue?.severity || "P2",
        auto_healing_enabled: false,
      });
      setAdvice({ loading: false, data });
    } catch (error: any) { setAdvice({ loading: false, error: error.message }); }
  }

  const refresh = () => { refreshRancher(); refreshInventory(); refreshMetrics(); refreshHealth(); };
  return (
    <div className="unified-page">
      <div className="page-commandbar">
        <div className="scope-control"><span>监控范围</span><select value={cluster} onChange={(event) => setCluster(event.target.value)}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div>
        <button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button>
      </div>
      <section className="kpi-grid six">
        <Kpi label="集群" value={cluster === "all" ? (rancher.data?.cluster_count || selectedInventory.length || 1) : 1} detail={rancher.data?.status || "local"} />
        <Kpi label="Pods" value={summary.pods?.total || 0} detail={`${summary.pods?.running || 0} Running`} />
        <Kpi label="异常" value={summary.pods?.failed || problems.length || 0} detail="未就绪或运行异常" tone={(summary.pods?.failed || problems.length) ? "danger" : "good"} />
        <Kpi label="CPU" value={`${Number(values.cpu_cores || 0).toFixed(2)} C`} detail={metrics.data?.source || "metrics"} />
        <Kpi label="内存" value={`${(Number(values.memory_bytes || 0) / 1024 / 1024 / 1024).toFixed(2)} GiB`} detail="Working set" />
        <Kpi label="节点" value={summary.nodes?.total || nodes.length || 0} detail={`${summary.nodes?.ready || nodes.filter((node: any) => node.ready).length || 0} Ready`} />
      </section>
      <section className="unified-grid dashboard-grid">
        <div className="surface span-two">
          <SectionHead icon={AlertTriangle} title="需要关注" meta={`${problems.length} 项实时异常`} />
          {problems.length ? <div className="compact-list attention-scroll">{problems.map((pod: any) => <div className="compact-row attention-row" key={`${pod.cluster}-${pod.namespace}-${pod.name}`}><span className="resource-icon risk"><Boxes size={15} /></span><div><strong>{pod.name}</strong><small>{pod.cluster} / {pod.namespace} · {pod.issue?.reason || pod.phase || "NotReady"}</small></div><div className="attention-actions"><StatusPill status={pod.issue?.severity || "warning"} /><button className="row-icon-button" onClick={() => { setSelectedProblem(pod); setAdvice({ loading: false }); }} title="查看异常详情"><Eye size={14} /></button></div></div>)}</div> : <Empty text="当前范围没有发现异常 Pod" />}
        </div>
        <div className="surface">
          <SectionHead icon={Activity} title="平台服务" />
          <div className="service-matrix">{Object.entries(health.data?.services || {}).map(([name, value]: [string, any]) => <div key={name}><span>{name}</span><StatusPill status={value?.status || "unknown"} /></div>)}</div>
          {health.error && <div className="inline-error">{health.error}</div>}
        </div>
        {selectedProblem && <div className="surface span-three attention-detail">
          <SectionHead icon={Eye} title={selectedProblem.name} meta={`${selectedProblem.cluster || cluster} / ${selectedProblem.namespace || "default"}`} action={<button className="primary" onClick={() => askForAdvice(selectedProblem)} disabled={advice.loading}>{advice.loading ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}AI 建议</button>} />
          <div className="attention-detail-grid"><div><span>异常原因</span><strong>{selectedProblem.issue?.reason || selectedProblem.phase || "NotReady"}</strong></div><div><span>容器状态</span><strong>{selectedProblem.ready ? "Ready" : "NotReady"} · restart {selectedProblem.restart_count || 0}</strong></div><div><span>上游工作负载</span><strong>{selectedProblem.workload_kind || selectedProblem.workload?.kind || "-"}/{selectedProblem.workload_name || selectedProblem.workload?.name || "-"}</strong></div></div>
          {advice.error && <div className="inline-error">{advice.error}</div>}
          {advice.data?.answer && <div className="attention-advice"><BrainCircuit size={17} /><p>{advice.data.answer}</p></div>}
        </div>}
        <div className="surface span-three">
          <SectionHead icon={Layers3} title="工作负载健康" meta={`${workloads.length} workloads`} />
          {workloads.length ? <div className="workload-strip">{workloads.slice(0, 12).map((item: any) => {
            const healthy = Number(item.ready_replicas || 0) >= Number(item.replicas || 0);
            return <div key={`${item.cluster}-${item.namespace}-${item.kind}-${item.name}`}><span>{item.kind}</span><strong>{item.name}</strong><small>{item.cluster}/{item.namespace}</small><StatusPill status={healthy ? "healthy" : "degraded"} text={`${item.ready_replicas || 0}/${item.replicas || 0}`} /></div>;
          })}</div> : <Empty text="Rancher 尚未返回工作负载清单" />}
        </div>
      </section>
    </div>
  );
}

export function ResourcesPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/rancher/inventory"), []);
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [kind, setKind] = useState<"pods" | "workloads" | "nodes">("pods");
  const [query, setQuery] = useState("");
  const inventory = list(state.data?.inventory);
  const clusters = list(state.data?.clusters);
  const scoped = cluster === "all" ? inventory : inventory.filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name);
  const namespaces = Array.from(new Set(scoped.flatMap((item: any) => list(item.namespaces).map((item: any) => item.name)))).sort();
  const rows = scoped.flatMap((item: any) => list(item[kind])).filter((item: any) => namespace === "all" || item.namespace === namespace).filter((item: any) => !query || JSON.stringify(item).toLowerCase().includes(query.toLowerCase()));

  return <div className="unified-page">
    <div className="page-commandbar resource-toolbar">
      <div className="segmented"><button className={kind === "pods" ? "active" : ""} onClick={() => setKind("pods")}>Pods</button><button className={kind === "workloads" ? "active" : ""} onClick={() => setKind("workloads")}>Workloads</button><button className={kind === "nodes" ? "active" : ""} onClick={() => setKind("nodes")}>Nodes</button></div>
      <select value={cluster} onChange={(event) => { setCluster(event.target.value); setNamespace("all"); }}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select>
      {kind !== "nodes" && <select value={namespace} onChange={(event) => setNamespace(event.target.value)}><option value="all">所有 Namespace</option>{namespaces.map((item) => <option key={item} value={item}>{item}</option>)}</select>}
      <label className="search-field"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选资源" /></label>
      <button className="ghost" onClick={refresh}><RefreshCcw size={15} /></button>
    </div>
    <div className="surface resource-surface">
      <SectionHead icon={kind === "nodes" ? ServerCog : Boxes} title={kind === "pods" ? "Pod 清单" : kind === "workloads" ? "Workload 清单" : "Node 清单"} meta={`${rows.length} resources`} />
      {state.error && <div className="inline-error">{state.error}</div>}
      {rows.length ? <div className="resource-table"><div className="resource-table-head"><span>名称</span><span>位置</span><span>类型 / 状态</span><span>健康</span></div>{rows.slice(0, 200).map((item: any, index: number) => {
        const healthy = kind === "nodes" ? item.ready : kind === "workloads" ? Number(item.ready_replicas || 0) >= Number(item.replicas || 0) : item.ready || item.phase === "Succeeded";
        return <div className="resource-table-row" key={`${item.cluster}-${item.namespace}-${item.name}-${index}`}><div><strong>{item.name}</strong><small>{item.workload_kind && item.workload_name ? `${item.workload_kind}/${item.workload_name}` : item.kind || ""}</small></div><span>{item.cluster || "-"}<small>{item.namespace || "global"}</small></span><span>{item.kind || item.phase || "Node"}<small>{kind === "workloads" ? `${item.ready_replicas || 0}/${item.replicas || 0} ready` : item.issue?.reason || ""}</small></span><StatusPill status={healthy ? "healthy" : "degraded"} /></div>;
      })}</div> : !state.loading && <Empty text="当前筛选范围没有资源" />}
    </div>
  </div>;
}

export function InfrastructurePage({
  activeModelId = "",
  initialResourceType = "all",
  fixedResourceType = false,
  entryMode = "inventory",
}: {
  activeModelId?: string;
  initialResourceType?: string;
  fixedResourceType?: boolean;
  entryMode?: "inventory" | "operate" | "inspect";
}) {
  const [resourceType, setResourceType] = useState(initialResourceType);
  const [resourceId, setResourceId] = useState("");
  const [selectedFindingId, setSelectedFindingId] = useState("");
  const [providers, refreshProviders] = useAsync<any>(() => apiGet("/api/infrastructure/providers").catch(() => ({ catalog: [], resources: [], summary: {} })), []);
  const [resources, refreshResources] = useAsync<any>(() => apiGet(`/api/infrastructure/resources?resource_type=${encodeURIComponent(resourceType)}`).catch(() => ({ resources: [] })), [resourceType]);
  const [scan, setScan] = useState<ApiState<any>>({ loading: false });
  const catalog = list(providers.data?.catalog);
  const resourceRows = list(resources.data?.resources || providers.data?.resources);
  const scopedResources = resourceType === "all" ? resourceRows : resourceRows.filter((item: any) => item.type === resourceType);
  const selectedResource = resourceRows.find((item: any) => item.id === resourceId);
  const findings = list(scan.data?.findings);
  const selectedFinding = findings.find((item: any) => item.id === selectedFindingId) || findings[0];
  const selectedPlan = selectedFinding?.ops_plan;
  const summary = scan.data?.summary || {};
  const providerCatalog = list(providers.data?.provider_catalog);
  const discoveryAdapters = list(providers.data?.adapters);

  useEffect(() => {
    if (fixedResourceType) {
      setResourceType(initialResourceType);
      setResourceId("");
      setSelectedFindingId("");
      setScan({ loading: false });
    }
  }, [fixedResourceType, initialResourceType]);

  async function runScan() {
    setScan({ loading: true });
    setSelectedFindingId("");
    try {
      const data = await apiPost<any>("/api/infrastructure/scan", {
        resource_type: resourceType,
        resource_id: resourceId,
        model_profile_id: activeModelId,
        production_mode: true,
        include_probe: true,
      });
      setScan({ loading: false, data });
      const first = list(data.findings)[0];
      if (first?.id) setSelectedFindingId(first.id);
    } catch (error: any) {
      setScan({ loading: false, error: error.message });
    }
  }

  function refreshAll() {
    refreshProviders();
    refreshResources();
  }

  return <div className="unified-page infrastructure-page">
    <div className="page-commandbar infrastructure-toolbar">
      <div className="scope-control"><span>资源类型</span><select value={resourceType} disabled={fixedResourceType} onChange={(event) => { setResourceType(event.target.value); setResourceId(""); }}><option value="all">全部基础设施</option>{catalog.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div>
      <div className="scope-control"><span>目标资源</span><select value={resourceId} onChange={(event) => setResourceId(event.target.value)}><option value="">当前类型全部资源</option>{scopedResources.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div>
      <button className="primary" onClick={runScan} disabled={scan.loading}>{scan.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}{entryMode === "operate" ? "读取风险并生成方案" : "AI SRE 巡检"}</button>
      <button className="ghost" onClick={refreshAll}><RefreshCcw size={15} />刷新</button>
    </div>
    <section className="kpi-grid six">
      <Kpi label="纳管资源" value={providers.data?.summary?.total || resourceRows.length || 0} detail={providers.data?.summary?.configured ? "已配置 Provider" : "等待接入"} />
      <Kpi label="数据库" value={providers.data?.summary?.by_type?.database || 0} detail="MySQL / Oracle / Redis" />
      <Kpi label="虚拟机" value={providers.data?.summary?.by_type?.virtual_machine || 0} detail="VMware / ECS / Linux" />
      <Kpi label="中间件" value={providers.data?.summary?.by_type?.middleware || 0} detail="Kafka / MQ / ELK" />
      <Kpi label="网络" value={providers.data?.summary?.by_type?.network || 0} detail="交换 / 路由 / DNS / LB" />
      <Kpi label="本次异常" value={summary.total || 0} detail={`${summary.p1 || 0} P1 · ${summary.p2 || 0} P2`} tone={summary.total ? "danger" : "good"} />
      <Kpi label="受控执行器" value={providers.data?.summary?.action_webhook_configured ? "Ready" : "待配置"} detail="外部变更 Webhook" tone={providers.data?.summary?.action_webhook_configured ? "good" : ""} />
    </section>
    <section className="surface domestic-provider-surface">
      <SectionHead icon={CloudCog} title="国产全栈适配矩阵" meta={`${providerCatalog.length} 类 Provider · ${discoveryAdapters.length} 个只读发现适配器`} />
      <div className="domestic-provider-grid">
        {providerCatalog.map((item: any) => <article key={item.id} className={item.recommended ? "recommended" : ""}>
          <div><span className="resource-icon"><CloudCog size={15} /></span><strong>{item.name}</strong>{item.recommended && <StatusPill status="recommended" text="主力云" />}</div>
          <small>{item.kind}</small>
          <p>{list(item.products).join(" · ")}</p>
        </article>)}
      </div>
      <div className="infra-contract-strip">
        <span><b>库存推送</b><code>POST /api/infrastructure/resources/sync</code></span>
        <span><b>只读发现</b><code>POST /api/infrastructure/discover</code></span>
        <span><b>巡检</b><code>POST /api/infrastructure/scan</code></span>
        <span><b>受控变更</b><code>OpsJob → 审批 → Webhook → 验证</code></span>
      </div>
      <div className="quiet-note"><ShieldCheck size={14} />云凭据只从服务端 Secret/env 引用解析；库存和发现请求不接受明文 AccessKey、Token 或密码。</div>
    </section>
    <section className="unified-grid infrastructure-grid">
      <div className="surface">
        <SectionHead icon={CloudCog} title="Provider 目录" meta="K8s 之外的全栈资源入口" />
        <div className="infra-provider-list">
          {catalog.map((item: any) => <article key={item.id}>
            <span className="resource-icon">{item.id === "database" ? <Database size={15} /> : item.id === "virtual_machine" ? <ServerCog size={15} /> : item.id === "storage" ? <HardDrive size={15} /> : item.id === "network" ? <Network size={15} /> : <CloudCog size={15} />}</span>
            <div><strong>{item.name}</strong><p>{item.description}</p><small>{list(item.typical_actions).slice(0, 4).join(" · ")}</small></div>
          </article>)}
        </div>
      </div>
      <div className="surface span-two">
        <SectionHead icon={Layers3} title="资源清单" meta={`${scopedResources.length} resources`} />
        {resources.error && <div className="inline-error">{resources.error}</div>}
        {scopedResources.length ? <div className="infra-resource-grid">{scopedResources.slice(0, 60).map((item: any) => <button className={resourceId === item.id ? "selected" : ""} key={item.id} onClick={() => setResourceId(item.id)}>
          <span className="resource-icon">{item.type === "database" ? <Database size={15} /> : item.type === "virtual_machine" ? <ServerCog size={15} /> : item.type === "storage" ? <HardDrive size={15} /> : item.type === "network" ? <Network size={15} /> : <CloudCog size={15} />}</span>
          <div><strong>{item.name || item.id}</strong><small>{item.type} · {item.provider || item.subtype} · {item.cluster || "external"}</small><small>{item.business_service || item.owner || item.endpoint || item.host || "未绑定业务服务"}</small></div>
          <StatusPill status={item.actions_enabled ? "actions" : "read-only"} text={item.actions_enabled ? "可申请变更" : "只读诊断"} />
        </button>)}</div> : <div className="infra-config-guide">
          <Database size={22} />
          <div><strong>还没有接入 K8s 之外的资源</strong><p>在 ConfigMap 中配置 <code>INFRASTRUCTURE_RESOURCES_JSON</code> 或各域的 targets（包括 <code>NETWORK_TARGETS_JSON</code>）后，这里会自动出现数据库、虚拟机、中间件、存储、云和网络资源。</p></div>
        </div>}
      </div>
      <div className="surface">
        <SectionHead icon={AlertTriangle} title="异常队列" meta={`${findings.length} findings`} />
        {scan.error && <div className="inline-error">{scan.error}</div>}
        {scan.loading && <Empty text="正在探测资源、读取指标并让 AI SRE 生成预演" />}
        {!scan.loading && findings.length ? <div className="infra-finding-list">{findings.map((item: any) => <button className={selectedFinding?.id === item.id ? "selected" : ""} key={item.id} onClick={() => setSelectedFindingId(item.id)}>
          <StatusPill status={item.severity || "P2"} />
          <strong>{item.title}</strong>
          <small>{item.resource_type}/{item.resource_id}</small>
          <p>{item.summary}</p>
        </button>)}</div> : !scan.loading && <Empty text={providers.data?.summary?.configured ? "点击 AI SRE 巡检后展示异常和可执行预演" : "先配置资源 Provider，再进行全栈巡检"} />}
      </div>
      <div className="surface span-two infra-plan-shell">
        <SectionHead icon={BrainCircuit} title="AI SRE 运维预演" meta={selectedResource ? `${selectedResource.type}/${selectedResource.name}` : selectedPlan?.target || "waiting"} />
        {selectedPlan ? <OpsPlanPanel plan={selectedPlan} /> : <div className="infra-config-guide">
          <ShieldCheck size={22} />
          <div><strong>执行边界已经预留</strong><p>数据库、虚拟机、中间件、存储、云和网络的真实变更统一提交到 <code>INFRASTRUCTURE_ACTION_WEBHOOK_URL</code>。执行器负责对接各专业平台，页面保留审批、审计、写后回读和恢复验证。</p></div>
        </div>}
      </div>
    </section>
  </div>;
}

const HARNESS_PLUGIN_TEMPLATES = {
  database: `apiVersion: cisre.io/v1alpha1
kind: HarnessPlugin
metadata:
  name: team.database-provider
  version: 1.0.0
  description: Database inventory, evidence and recovery verification provider.
spec:
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
  permissions: [service:provide, inventory:read, evidence:read, events:publish, ops:propose, ui:contribute]
  runtime: {type: declarative}
  config: {adapter_contract: database/v1}
  ui: {group: database, title: Database SRE Provider}
`,
  virtual_machine: `apiVersion: cisre.io/v1alpha1
kind: HarnessPlugin
metadata:
  name: team.vm-provider
  version: 1.0.0
  description: VM inventory, evidence and recovery verification provider.
spec:
  scope: global
  priority: 50
  provides:
    - {name: inventory.virtual-machine, version: 1.0.0}
    - {name: evidence.virtual-machine, version: 1.0.0}
    - {name: verification.virtual-machine, version: 1.0.0}
  requires:
    - {name: session.events, constraint: ">=1.0,<2"}
  events:
    - {name: inventory/discovered, mode: observe}
  permissions: [service:provide, inventory:read, evidence:read, events:publish, ops:propose, ui:contribute]
  runtime: {type: declarative}
  config: {adapter_contract: virtual-machine/v1}
  ui: {group: virtual-machine, title: VM SRE Provider}
`,
  storage: `apiVersion: cisre.io/v1alpha1
kind: HarnessPlugin
metadata:
  name: team.storage-provider
  version: 1.0.0
  description: Storage inventory, evidence and recovery verification provider.
spec:
  scope: global
  priority: 50
  provides:
    - {name: inventory.storage, version: 1.0.0}
    - {name: evidence.storage, version: 1.0.0}
    - {name: verification.storage, version: 1.0.0}
  requires:
    - {name: session.events, constraint: ">=1.0,<2"}
  events:
    - {name: inventory/discovered, mode: observe}
  permissions: [service:provide, inventory:read, evidence:read, events:publish, ops:propose, ui:contribute]
  runtime: {type: declarative}
  config: {adapter_contract: storage/v1}
  ui: {group: storage, title: Storage SRE Provider}
`,
};

type PluginDomain = "common" | "kubernetes" | "database" | "virtual_machine" | "storage" | "middleware" | "cloud" | "network";

const PLUGIN_DOMAIN_LABELS: Record<PluginDomain, string> = {
  common: "公共",
  kubernetes: "Kubernetes",
  database: "数据库",
  virtual_machine: "VM / 主机",
  storage: "存储",
  middleware: "中间件",
  cloud: "云资源",
  network: "网络",
};

type PluginDraft = {
  domain: PluginDomain;
  id: string;
  version: string;
  title: string;
  description: string;
  scope: string;
  priority: number;
  provides: string;
  requires: string;
  eventName: string;
};

const PLUGIN_DRAFT_PRESETS: Record<PluginDraft["domain"], PluginDraft> = {
  common: {
    domain: "common", id: "team.shared-evidence", version: "1.0.0", title: "Shared SRE Capability",
    description: "Shared evidence, policy or telemetry capability for every domain Agent.", scope: "global", priority: 50,
    provides: "evidence.shared", requires: "session.events", eventName: "session/event",
  },
  kubernetes: {
    domain: "kubernetes", id: "team.kubernetes-provider", version: "1.0.0", title: "Kubernetes SRE Provider",
    description: "Kubernetes inventory, evidence and recovery verification provider.", scope: "global", priority: 50,
    provides: "inventory.kubernetes\nevidence.kubernetes\nverification.kubernetes", requires: "session.events", eventName: "inventory/discovered",
  },
  database: {
    domain: "database", id: "team.database-provider", version: "1.0.0", title: "Database SRE Provider",
    description: "Database inventory, evidence and recovery verification provider.", scope: "global", priority: 50,
    provides: "inventory.database\nevidence.database\nverification.database", requires: "session.events", eventName: "inventory/discovered",
  },
  virtual_machine: {
    domain: "virtual_machine", id: "team.vm-provider", version: "1.0.0", title: "VM SRE Provider",
    description: "VM inventory, evidence and recovery verification provider.", scope: "global", priority: 50,
    provides: "inventory.virtual-machine\nevidence.virtual-machine\nverification.virtual-machine", requires: "session.events", eventName: "inventory/discovered",
  },
  storage: {
    domain: "storage", id: "team.storage-provider", version: "1.0.0", title: "Storage SRE Provider",
    description: "Storage inventory, evidence and recovery verification provider.", scope: "global", priority: 50,
    provides: "inventory.storage\nevidence.storage\nverification.storage", requires: "session.events", eventName: "inventory/discovered",
  },
  middleware: {
    domain: "middleware", id: "team.middleware-provider", version: "1.0.0", title: "Middleware SRE Provider",
    description: "Middleware inventory, evidence and recovery verification provider.", scope: "global", priority: 50,
    provides: "inventory.middleware\nevidence.middleware\nverification.middleware", requires: "session.events", eventName: "inventory/discovered",
  },
  cloud: {
    domain: "cloud", id: "team.cloud-provider", version: "1.0.0", title: "Cloud SRE Provider",
    description: "Cloud resource inventory, evidence and recovery verification provider.", scope: "global", priority: 50,
    provides: "inventory.cloud\nevidence.cloud\nverification.cloud", requires: "session.events", eventName: "inventory/discovered",
  },
  network: {
    domain: "network", id: "team.network-provider", version: "1.0.0", title: "Network SRE Provider",
    description: "Network inventory, evidence and recovery verification provider.", scope: "global", priority: 50,
    provides: "inventory.network\nevidence.network\nverification.network", requires: "session.events", eventName: "inventory/discovered",
  },
};

function pluginDraftYaml(draft: PluginDraft) {
  const provides = draft.provides.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  const requires = draft.requires.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  const lines = [
    "apiVersion: cisre.io/v1alpha1",
    "kind: HarnessPlugin",
    "metadata:",
    `  name: ${draft.id.trim() || "team.new-provider"}`,
    `  version: ${draft.version.trim() || "1.0.0"}`,
    `  description: ${JSON.stringify(draft.description.trim() || "Team SRE provider.")}`,
    "spec:",
    `  category: ${draft.domain === "common" ? "shared" : "domain"}`,
    `  domains: [${draft.domain === "virtual_machine" ? "virtual-machine" : draft.domain}]`,
    `  agents: [${draft.domain === "common" ? "all" : draft.domain === "virtual_machine" ? "virtual-machine" : draft.domain}]`,
    `  scope: ${draft.scope.trim() || "global"}`,
    `  priority: ${Number.isFinite(draft.priority) ? draft.priority : 50}`,
    "  provides:",
    ...(provides.length ? provides.map((service) => `    - {name: ${service}, version: ${draft.version.trim() || "1.0.0"}}`) : ["    []"]),
    "  requires:",
    ...(requires.length ? requires.map((service) => `    - {name: ${service}, constraint: \">=1.0,<2\"}`) : ["    []"]),
    "  events:",
    `    - {name: ${draft.eventName.trim() || "inventory/discovered"}, mode: observe}`,
    "  permissions: [service:provide, inventory:read, evidence:read, events:publish, ops:propose, ui:contribute]",
    "  runtime: {type: declarative}",
    `  config: {adapter_contract: ${draft.domain.replace("_", "-")}/v1}`,
    `  ui: {group: ${draft.domain.replace("_", "-")}, title: ${JSON.stringify(draft.title.trim() || draft.id)}}`,
    "",
  ];
  return lines.join("\n");
}

function draftFromPlugin(plugin: any): PluginDraft {
  const rawDomain = String(list(plugin?.domains)[0] || plugin?.ui?.group || plugin?.id || "").toLowerCase();
  const domain: PluginDomain = rawDomain.includes("common") || plugin?.category === "shared" ? "common"
    : rawDomain.includes("kubernetes") ? "kubernetes"
    : rawDomain.includes("storage") ? "storage"
    : rawDomain.match(/vm|virtual/) ? "virtual_machine"
    : rawDomain.includes("middleware") ? "middleware"
    : rawDomain.includes("cloud") ? "cloud"
    : rawDomain.includes("network") ? "network" : "database";
  const preset = PLUGIN_DRAFT_PRESETS[domain];
  return {
    ...preset,
    id: String(plugin?.id || preset.id).replace(/^cisre\./, "team."),
    version: String(plugin?.version || "1.0.0"),
    title: String(plugin?.ui?.title || plugin?.description || preset.title),
    description: String(plugin?.description || preset.description),
    scope: String(plugin?.scope || "global"),
    priority: Number(plugin?.priority || 50),
    provides: list(plugin?.provides).join("\n") || preset.provides,
    requires: list(plugin?.requires).join("\n") || preset.requires,
    eventName: String(list(plugin?.events)[0] || preset.eventName),
  };
}

function HarnessConsole({ capabilities, refreshCapabilities }: { capabilities: any; refreshCapabilities: () => void }) {
  const [view, setView] = useState<"runtime" | "services" | "author" | "sessions" | "security">("runtime");
  const [selectedProfile, setSelectedProfile] = useState("");
  const [selectedSession, setSelectedSession] = useState("");
  const [sessionMode, setSessionMode] = useState<"events" | "trace">("trace");
  const [traceSearch, setTraceSearch] = useState("");
  const [pluginDomainFilter, setPluginDomainFilter] = useState<"all" | PluginDomain>("all");
  const [pluginSearch, setPluginSearch] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [composerMode, setComposerMode] = useState<"create" | "import">("create");
  const [pluginDraft, setPluginDraft] = useState<PluginDraft>(() => ({ ...PLUGIN_DRAFT_PRESETS.database }));
  const [pluginSource, setPluginSource] = useState(() => HARNESS_PLUGIN_TEMPLATES.database);
  const [pluginGoal, setPluginGoal] = useState("");
  const [pluginGeneration, setPluginGeneration] = useState<ApiState<any>>({ loading: false });
  const [selectedPluginId, setSelectedPluginId] = useState("");
  const [addToProfile, setAddToProfile] = useState(true);
  const [validation, setValidation] = useState<ApiState<any>>({ loading: false });
  const [action, setAction] = useState<ApiState<any>>({ loading: false });
  const [profiles, refreshProfiles] = useAsync<any>(() => apiGet("/api/harness/profiles"), []);
  const [sessions, refreshSessions] = useAsync<any>(() => apiGet("/api/harness/sessions?limit=80"), []);
  const [sessionEvents, refreshSessionEvents] = useAsync<any>(
    () => selectedSession ? apiGet(`/api/harness/sessions/${encodeURIComponent(selectedSession)}/events`) : Promise.resolve({ events: [], children: [] }),
    [selectedSession],
  );
  const [pluginDetail, refreshPluginDetail] = useAsync<any>(
    () => selectedPluginId ? apiGet(`/api/harness/plugins/${encodeURIComponent(selectedPluginId)}`) : Promise.resolve(undefined),
    [selectedPluginId],
  );
  const harness = capabilities?.harness || {};
  const composition = harness.composition || {};
  const profileRows = list(profiles.data?.profiles || composition.profiles);
  const sessionRows = list(sessions.data?.sessions);
  const runtimePluginRows = list(harness.plugins);
  const externalPackages = list(composition.packages);
  const pluginRows = useMemo(() => {
    const values = [...runtimePluginRows];
    for (const item of externalPackages) {
      if (!values.some((plugin: any) => plugin.id === item.id)) values.push({ ...item, status: item.package_status || "discovered" });
    }
    return values;
  }, [runtimePluginRows, externalPackages]);
  const pluginDomains = (plugin: any): string[] => {
    const explicit = list(plugin?.domains).map((item: any) => String(item).replace("_", "-"));
    if (explicit.length) return explicit;
    const group = String(plugin?.ui?.group || "").replace("_", "-");
    if (group) return [group];
    const value = `${plugin?.id || ""} ${list(plugin?.provides).join(" ")}`.toLowerCase();
    if (value.includes("kubernetes")) return ["kubernetes"];
    if (value.includes("database")) return ["database"];
    if (/virtual|\.vm|vm\./.test(value)) return ["virtual-machine"];
    if (value.includes("storage")) return ["storage"];
    if (value.includes("middleware")) return ["middleware"];
    if (value.includes("cloud")) return ["cloud"];
    if (value.includes("network")) return ["network"];
    return ["common"];
  };
  const pluginCategory = (plugin: any) => String(plugin?.category || (pluginDomains(plugin).includes("common") ? "shared" : "domain"));
  const filteredPluginRows = pluginRows.filter((plugin: any) => {
    const query = pluginSearch.trim().toLowerCase();
    const wanted = pluginDomainFilter === "virtual_machine" ? "virtual-machine" : pluginDomainFilter;
    const domainMatch = wanted === "all" || pluginDomains(plugin).includes(wanted) || (wanted !== "common" && pluginCategory(plugin) === "shared");
    const searchMatch = !query || `${plugin.id} ${plugin.description} ${list(plugin.provides).join(" ")}`.toLowerCase().includes(query);
    return domainMatch && searchMatch;
  });
  const services = Object.entries(composition.services || harness.service_bindings || {});
  const selectedSessionRow = sessionEvents.data?.session || sessionRows.find((item: any) => item.session_id === selectedSession);
  const traceSpans = list(sessionEvents.data?.trace?.spans).filter((span: any) => {
    const query = traceSearch.trim().toLowerCase();
    return !query || `${span.kind} ${span.name} ${span.plugin_id} ${span.tool} ${span.summary}`.toLowerCase().includes(query);
  });
  const traceExtent = Math.max(1, ...traceSpans.map((span: any) => Number(span.offset_ms || 0) + Number(span.duration_ms || 12)));

  useEffect(() => {
    if (!selectedProfile && (profiles.data?.active_profile || composition.active_profile)) setSelectedProfile(profiles.data?.active_profile || composition.active_profile);
  }, [profiles.data?.active_profile, composition.active_profile, selectedProfile]);
  useEffect(() => {
    if (!selectedSession && sessionRows.length) setSelectedSession(String(sessionRows[0].session_id));
  }, [selectedSession, sessionRows]);
  useEffect(() => {
    if (composerOpen && composerMode === "create") setPluginSource(pluginDraftYaml(pluginDraft));
  }, [composerOpen, composerMode, pluginDraft]);

  const refreshAll = () => { refreshCapabilities(); refreshProfiles(); refreshSessions(); refreshSessionEvents(); };
  async function runAction(task: () => Promise<any>) {
    setAction({ loading: true });
    try { setAction({ loading: false, data: await task() }); refreshAll(); }
    catch (error: any) { setAction({ loading: false, error: error.message }); }
  }
  async function branchSession() {
    if (!selectedSession) return;
    setAction({ loading: true });
    try {
      const data = await apiPost<any>(`/api/harness/sessions/${encodeURIComponent(selectedSession)}/fork`, {});
      setAction({ loading: false, data }); refreshSessions();
      if (data.session_id) setSelectedSession(data.session_id);
    } catch (error: any) { setAction({ loading: false, error: error.message }); }
  }
  function downloadSessionLog() {
    if (!selectedSession || !sessionEvents.data) return;
    const payload = {
      exported_at: new Date().toISOString(),
      session: sessionEvents.data.session,
      integrity: sessionEvents.data.integrity,
      trace: sessionEvents.data.trace,
      events: sessionEvents.data.events,
      disclosure: "Secret-safe audit export; credentials and private model reasoning are excluded.",
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = `cisre-session-${selectedSession}.json`;
    anchor.click();
    URL.revokeObjectURL(href);
  }
  async function deleteBranch(sessionId = selectedSession) {
    if (!sessionId) return;
    const row = sessionRows.find((item: any) => item.session_id === sessionId) || sessionEvents.data?.session;
    if (!row?.parent_session_id) {
      setAction({ loading: false, error: "根会话属于审计事实，不能删除；只能删除分支会话。" });
      return;
    }
    if (!window.confirm(`确定删除分支 ${sessionId}？界面会隐藏该分支，但审计事件仍会保留。`)) return;
    setAction({ loading: true });
    try {
      const data = await apiDelete<any>(`/api/harness/sessions/${encodeURIComponent(sessionId)}`);
      setAction({ loading: false, data });
      setSelectedSession(String(data.parent_session_id || ""));
      invalidateApiCache("/api/harness/");
      refreshSessions();
      refreshSessionEvents();
    } catch (error: any) { setAction({ loading: false, error: error.message }); }
  }
  function openCreatePlugin(domain: PluginDraft["domain"] = "database", source?: any) {
    const next = source ? draftFromPlugin(source) : { ...PLUGIN_DRAFT_PRESETS[domain] };
    setPluginDraft(next);
    setPluginSource(pluginDraftYaml(next));
    setComposerMode("create");
    setValidation({ loading: false });
    setComposerOpen(true);
  }
  function openImportPlugin() {
    setComposerMode("import");
    setValidation({ loading: false });
    setComposerOpen(true);
  }
  async function validatePlugin() {
    setValidation({ loading: true });
    try { setValidation({ loading: false, data: await apiPost("/api/harness/plugins/validate", { manifest: pluginSource, add_to_active_profile: addToProfile }) }); }
    catch (error: any) { setValidation({ loading: false, error: error.message }); }
  }
  async function installPlugin() {
    setAction({ loading: true });
    try {
      const data = await apiPost<any>("/api/harness/plugins/install", { manifest: pluginSource, add_to_active_profile: addToProfile });
      setAction({ loading: false, data });
      setValidation({ loading: false, data: { status: "installed", package: data.package } });
      setComposerOpen(false);
      refreshAll();
    } catch (error: any) { setAction({ loading: false, error: error.message }); }
  }
  async function generatePlugin() {
    if (pluginGoal.trim().length < 12) {
      setPluginGeneration({ loading: false, error: "请至少用一句完整需求描述插件要纳管的资源、证据和恢复判据。" });
      return;
    }
    setPluginGeneration({ loading: true });
    try {
      const data = await apiPost<any>("/api/harness/plugins/generate", {
        goal: pluginGoal, domain: pluginDraft.domain, include_agent_loop: true, orchestration_mode: "plan-execute",
      });
      setPluginGeneration({ loading: false, data });
      setPluginSource(data.manifest);
      setComposerMode("import");
      setValidation({ loading: false, data: { status: "generated", package: data.package } });
    } catch (error: any) { setPluginGeneration({ loading: false, error: error.message }); }
  }

  return <div className="capability-stack harness-console">
    <div className="surface harness-surface">
      <SectionHead icon={BrainCircuit} title="CISRE 插件中心" meta={`${harness.summary?.active || 0}/${pluginRows.length || 0} plugins active`} action={<div className="harness-head-actions"><button className="primary" onClick={() => openCreatePlugin()}><Plus size={14} />新建插件</button><button className="ghost" onClick={openImportPlugin}><Upload size={14} />导入 YAML</button><button className="ghost" onClick={refreshAll}><RefreshCcw size={14} />刷新运行时</button></div>} />
      <div className="harness-runtime-strip">
        <div><span>Runtime</span><strong>{String(harness.runtime || "CISREPluginHarness/v1")}</strong></div>
        <div><span>Active profile</span><strong>{composition.active_profile || "production"}</strong></div>
        <div><span>Durable sessions</span><strong>{harness.event_store?.sessions || 0} · {harness.event_store?.events || 0} events</strong></div>
        <div><span>Scale profile</span><strong>{harness.scalability?.distributed_ready ? "Distributed ready" : "Single replica"}</strong></div>
        <div><span>Safety boundary</span><strong>Typed action · Approval · Readback</strong></div>
      </div>
      <div className="harness-view-tabs">
        <button className={view === "runtime" ? "active" : ""} onClick={() => setView("runtime")}><Layers3 size={14} />插件与 Profile</button>
        <button className={view === "services" ? "active" : ""} onClick={() => setView("services")}><Workflow size={14} />服务依赖图</button>
        <button className={view === "author" ? "active" : ""} onClick={() => setView("author")}><TerminalSquare size={14} />团队接入</button>
        <button className={view === "sessions" ? "active" : ""} onClick={() => setView("sessions")}><GitBranch size={14} />事件与分支</button>
        <button className={view === "security" ? "active" : ""} onClick={() => setView("security")}><ShieldCheck size={14} />权限与审计</button>
      </div>
      {action.error && <div className="inline-error">{action.error}</div>}
      {action.data && <div className="harness-action-ok"><CheckCircle2 size={14} />运行时操作已写入审计事件流</div>}

      {composerOpen && <div className="harness-composer">
        <header><div><span>PLUGIN WORKBENCH</span><strong>{composerMode === "create" ? "在前端新建 SRE 插件" : "导入已有插件 Manifest"}</strong><small>{composerMode === "create" ? "填写服务、触发事件和作用域，页面自动生成可校验 Manifest。" : "适合已有团队仓库或远程 Provider；明文凭据会被拒绝。"}</small></div><button className="ghost tiny" onClick={() => setComposerOpen(false)}><X size={13} />关闭</button></header>
        <div className="harness-composer-mode"><button className={composerMode === "create" ? "active" : ""} onClick={() => { setComposerMode("create"); setPluginSource(pluginDraftYaml(pluginDraft)); }}><Plus size={13} />可视化新建</button><button className={composerMode === "import" ? "active" : ""} onClick={() => setComposerMode("import")}><TerminalSquare size={13} />YAML / 高级模式</button></div>
        <div className="harness-ai-author"><span><BrainCircuit size={16} /></span><div><strong>让模型帮我设计插件</strong><small>描述资源接口、要采集的证据、允许提议的动作和恢复判据；模型生成的 Manifest 仍需契约校验和人工安装。</small><textarea value={pluginGoal} onChange={(event) => setPluginGoal(event.target.value)} placeholder="例如：为数据库组接入 MySQL，提供实例清单、慢查询与主从延迟证据；允许提出参数调整和故障切换方案；以复制恢复、错误率下降和业务探针通过为恢复判据。" /></div><button className="primary" onClick={generatePlugin} disabled={pluginGeneration.loading}>{pluginGeneration.loading ? <Loader2 className="spin" size={14} /> : <BrainCircuit size={14} />}AI 生成</button></div>
        {pluginGeneration.error && <div className="inline-error">{pluginGeneration.error}</div>}
        {pluginGeneration.data?.explanation && <div className="harness-action-ok"><CheckCircle2 size={14} />{pluginGeneration.data.explanation}</div>}
        <div className="harness-template-switch"><span>插件分类模板</span><button onClick={() => openCreatePlugin("common")}><Layers3 size={13} />公共</button><button onClick={() => openCreatePlugin("kubernetes")}><Boxes size={13} />K8s</button><button onClick={() => openCreatePlugin("database")}><Database size={13} />数据库</button><button onClick={() => openCreatePlugin("virtual_machine")}><ServerCog size={13} />VM / 主机</button><button onClick={() => openCreatePlugin("storage")}><HardDrive size={13} />存储</button><button onClick={() => openCreatePlugin("middleware")}><Workflow size={13} />中间件</button><button onClick={() => openCreatePlugin("cloud")}><CloudCog size={13} />云资源</button><button onClick={() => openCreatePlugin("network")}><Network size={13} />网络</button></div>
        {composerMode === "create" && <div className="harness-draft-grid">
          <label>插件 ID<input value={pluginDraft.id} onChange={(event) => setPluginDraft({ ...pluginDraft, id: event.target.value.toLowerCase() })} placeholder="team.database-provider" /><small>全局唯一，只能使用小写字母、数字、点、横线。</small></label>
          <label>版本<input value={pluginDraft.version} onChange={(event) => setPluginDraft({ ...pluginDraft, version: event.target.value })} placeholder="1.0.0" /><small>使用 SemVer，升级接口时提升主版本。</small></label>
          <label>显示名称<input value={pluginDraft.title} onChange={(event) => setPluginDraft({ ...pluginDraft, title: event.target.value })} /></label>
          <label>Scope<input value={pluginDraft.scope} onChange={(event) => setPluginDraft({ ...pluginDraft, scope: event.target.value })} placeholder="global / cluster:x / job:x" /><small>调用时先匹配最近 scope，再比较 priority。</small></label>
          <label className="wide">插件说明<input value={pluginDraft.description} onChange={(event) => setPluginDraft({ ...pluginDraft, description: event.target.value })} /></label>
          <label>提供哪些服务<textarea value={pluginDraft.provides} onChange={(event) => setPluginDraft({ ...pluginDraft, provides: event.target.value })} /><small>每行一个；当运行时请求这些服务时，本插件进入候选。</small></label>
          <label>依赖哪些服务<textarea value={pluginDraft.requires} onChange={(event) => setPluginDraft({ ...pluginDraft, requires: event.target.value })} /><small>依赖不满足时保持 pending，不会带病启动。</small></label>
          <label>订阅事件<input value={pluginDraft.eventName} onChange={(event) => setPluginDraft({ ...pluginDraft, eventName: event.target.value })} placeholder="inventory/discovered" /><small>该事件发布时以 observe 模式调用。</small></label>
          <label>优先级<input type="number" value={pluginDraft.priority} onChange={(event) => setPluginDraft({ ...pluginDraft, priority: Number(event.target.value) })} /><small>同 scope 的同类 Provider，数值更高者优先。</small></label>
          <div className="harness-call-preview"><strong>这个插件何时会被调用？</strong><p>① 请求 <code>{pluginDraft.provides.split(/[\n,]/).filter(Boolean)[0] || "你声明的服务"}</code>；② scope 匹配 <code>{pluginDraft.scope || "global"}</code>；③ 依赖已满足；或收到 <code>{pluginDraft.eventName || "订阅事件"}</code>。</p><small><ShieldCheck size={12} />本向导只生成声明式插件：可读证据、可提案，不能直接写生产资源。</small></div>
        </div>}
        <div className="harness-manifest-editor"><div><strong>{composerMode === "create" ? "实时生成的 Manifest" : "插件 Manifest"}</strong><small>{composerMode === "create" ? "需要手写远程 Provider 时切换到 YAML / 高级模式。" : "支持声明式插件与隔离的远程只读 Provider。"}</small></div><textarea value={pluginSource} readOnly={composerMode === "create"} onChange={(event) => { setPluginSource(event.target.value); setValidation({ loading: false }); }} spellCheck={false} aria-label="插件 YAML" /></div>
        <footer><label><input type="checkbox" checked={addToProfile} onChange={(event) => setAddToProfile(event.target.checked)} />加入当前 Profile 并热加载</label><span>{profiles.data?.package_write_enabled ? "运行时写入已开启" : "生产写入锁定：需设置 HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED=true"}</span><button className="ghost" disabled={validation.loading} onClick={validatePlugin}>{validation.loading ? <Loader2 className="spin" size={14} /> : <ShieldCheck size={14} />}校验</button><button className="primary" disabled={action.loading || !profiles.data?.package_write_enabled} onClick={installPlugin}>{action.loading ? <Loader2 className="spin" size={14} /> : <Upload size={14} />}安装并重载</button></footer>
        {validation.error && <div className="inline-error">{validation.error}</div>}
        {validation.data && <div className="harness-action-ok"><CheckCircle2 size={14} />{validation.data.package?.id} · {validation.data.package?.version} · 契约校验通过</div>}
      </div>}

      {view === "runtime" && <div className="harness-runtime-grid">
        <div className="harness-agent-catalog"><div className="harness-agent-catalog-head"><div><small>DOMAIN AGENTS</small><h3>资源域 Agent 组合</h3><p>每个 Agent 复用公共插件，并只加载本资源域插件与 Skills。</p></div><code>Agent = shared plugins + domain plugins + skills</code></div><div>{list(harness.agents).map((agent: any) => <button key={agent.id} className={agent.status === "active" ? "active" : "pending"} onClick={() => { const domain = String(agent.domain || "common").replace("-", "_") as PluginDomain; setPluginDomainFilter(domain); }}><span><Bot size={16} /><strong>{PLUGIN_DOMAIN_LABELS[String(agent.domain || "common").replace("-", "_") as PluginDomain] || agent.domain}</strong></span><small>{list(agent.shared_plugins).length} 公共 · {list(agent.domain_plugins).length} 专属</small><StatusPill status={agent.status || "pending"} /></button>)}</div></div>
        <div className="harness-profile-panel"><small>PROFILE / BUNDLE / PATCH</small><h3>组合运行环境</h3><p>按环境整体替换模型、证据、Skill 或资源 Provider；插件更新无需修改控制面核心代码。</p><label>Profile<select value={selectedProfile} onChange={(event) => setSelectedProfile(event.target.value)}>{profileRows.map((item: any) => <option key={item.id} value={item.id}>{item.id}{item.active ? " · active" : ""}</option>)}</select></label><div><button className="primary" disabled={action.loading || !profiles.data?.runtime_write_enabled} onClick={() => runAction(() => apiPost("/api/harness/profiles/activate", { profile: selectedProfile }))}>应用 Profile</button><button className="ghost" disabled={action.loading} onClick={() => runAction(() => apiPost("/api/harness/plugins/reload", {}))}>热重载插件</button></div>{!profiles.data?.runtime_write_enabled && <small className="harness-lock-note">生产默认锁定；设置 HARNESS_PROFILE_RUNTIME_WRITE_ENABLED=true 后可由操作员切换。</small>}</div>
        <div className="harness-plugin-browser"><div className="harness-plugin-filters"><div>{(["all", ...Object.keys(PLUGIN_DOMAIN_LABELS)] as const).map((domain) => <button key={domain} className={pluginDomainFilter === domain ? "active" : ""} onClick={() => setPluginDomainFilter(domain as any)}>{domain === "all" ? "全部" : PLUGIN_DOMAIN_LABELS[domain as PluginDomain]}</button>)}</div><label><Search size={13} /><input value={pluginSearch} onChange={(event) => setPluginSearch(event.target.value)} placeholder="搜索插件 / 服务" /></label></div><div className="harness-plugin-list rich">{filteredPluginRows.map((plugin: any) => <button type="button" key={plugin.id} className={plugin.status === "active" ? "active" : "pending"} onClick={() => setSelectedPluginId(plugin.id)}><i /><span><strong>{plugin.id}<em>{plugin.version}</em></strong><small>{plugin.description}</small><div className="harness-plugin-class"><b>{pluginCategory(plugin) === "shared" ? "公共" : pluginCategory(plugin) === "domain-agent" ? "领域 Agent" : "领域专属"}</b>{pluginDomains(plugin).map((domain) => <code key={domain}>{domain}</code>)}</div><code>{list(plugin.provides).join(" · ") || list(plugin.events).join(" · ") || "lifecycle-only"}</code><u>查看调用条件与安全边界 <ChevronRight size={11} /></u></span><b>{plugin.status}</b></button>)}</div></div>
        {externalPackages.some((item: any) => item.ui?.title) && <div className="harness-ui-contributions"><small>PLUGIN UI CONTRIBUTIONS</small>{externalPackages.filter((item: any) => item.ui?.title).map((item: any) => <article key={item.id}><span><Layers3 size={14} /></span><div><strong>{item.ui.title}</strong><small>{item.ui.group || "extension"} · {item.id}</small><p>{item.description || list(item.provides).join(" · ")}</p></div><StatusPill status={item.package_status || "discovered"} /></article>)}</div>}
      </div>}

      {selectedPluginId && <div className="harness-detail-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelectedPluginId(""); }}><aside className="harness-plugin-detail">
        <header><div><span>PLUGIN DETAIL</span><strong>{pluginDetail.data?.plugin?.ui?.title || pluginDetail.data?.plugin?.id || selectedPluginId}</strong><small>{pluginDetail.data?.plugin?.description || "正在读取插件契约、调用条件和安全边界"}</small></div><button className="ghost tiny" onClick={() => setSelectedPluginId("")}><X size={13} />关闭</button></header>
        {pluginDetail.loading && <Empty text="正在解析插件服务、事件与权限" />}
        {pluginDetail.error && <div className="inline-error">{pluginDetail.error}<button className="ghost tiny" onClick={refreshPluginDetail}><RefreshCcw size={12} />重试</button></div>}
        {pluginDetail.data && <>
          <div className="harness-detail-summary"><div><span>状态</span><StatusPill status={pluginDetail.data.plugin.status || pluginDetail.data.plugin.package_status || "discovered"} /></div><div><span>版本 / 来源</span><strong>{pluginDetail.data.plugin.version} · {pluginDetail.data.plugin.source || pluginDetail.data.security.runtime_type}</strong></div><div><span>分类 / 资源域</span><strong>{pluginDetail.data.classification.shared ? "公共插件" : pluginDetail.data.classification.category === "domain-agent" ? "资源域 Agent" : "资源专属插件"} · {list(pluginDetail.data.classification.domains).join(" / ")}</strong></div><div><span>可供 Agent 使用</span><strong>{list(pluginDetail.data.classification.agents).join(" / ")}</strong></div><div><span>作用域 / 优先级</span><strong>{pluginDetail.data.invocation.scope} · p{pluginDetail.data.invocation.priority}</strong></div><div><span>最近调用记录</span><strong>{pluginDetail.data.recent_invocation_count}</strong></div></div>
          <section><h4><Workflow size={15} />什么情况下会调用它？</h4><ol>{list(pluginDetail.data.invocation.conditions).map((item: any, index: number) => <li key={`${item}-${index}`}>{item}</li>)}</ol><div className="harness-contract-columns"><div><b>Provides</b>{list(pluginDetail.data.invocation.provides).length ? list(pluginDetail.data.invocation.provides).map((item: any) => <code key={item}>{item}</code>) : <small>不提供服务</small>}</div><div><b>Requires</b>{list(pluginDetail.data.invocation.requires).length ? list(pluginDetail.data.invocation.requires).map((item: any) => <code key={item}>{item}</code>) : <small>无前置依赖</small>}</div><div><b>Events</b>{list(pluginDetail.data.invocation.events).length ? list(pluginDetail.data.invocation.events).map((item: any) => <code key={item.name}>{item.name} · {item.mode}</code>) : <small>不订阅事件</small>}</div></div></section>
          <section><h4><ShieldCheck size={15} />安全边界</h4><ul>{list(pluginDetail.data.security.boundaries).map((item: any) => <li key={item}>{item}</li>)}</ul><div className="harness-permission-groups"><div><b>已授予</b>{list(pluginDetail.data.security.granted_permissions).length ? list(pluginDetail.data.security.granted_permissions).map((item: any) => <code key={item}>{item}</code>) : <small>内置能力按核心策略执行</small>}</div><div><b>已拒绝</b>{list(pluginDetail.data.security.denied_permissions).length ? list(pluginDetail.data.security.denied_permissions).map((item: any) => <code className="denied" key={item}>{item}</code>) : <small>当前没有被拒权限</small>}</div></div></section>
          <section><h4><FileClock size={15} />最近调用与审计</h4>{list(pluginDetail.data.recent_invocations).length ? <div className="harness-plugin-events">{list(pluginDetail.data.recent_invocations).map((event: any) => <div key={event.event_id}><strong>{event.type}</strong><small>{timeText(event.timestamp)} · {event.session_id}</small><p>{event.message || event.status}</p></div>)}</div> : <Empty text="尚无带该 plugin_id 的调用事件；服务被实际调用后会在这里出现" />}</section>
          <footer><button className="ghost" onClick={() => { setView("author"); setSelectedPluginId(""); }}><Eye size={13} />查看开发说明</button><button className="primary" onClick={() => { openCreatePlugin("database", pluginDetail.data.plugin); setSelectedPluginId(""); }}><Plus size={13} />以此为模板新建</button></footer>
        </>}
      </aside></div>}

      {view === "services" && <div className="harness-service-map"><div className="harness-map-legend"><span><i className="provider" />Provider</span><span><i className="consumer" />Consumer dependency</span><b>{services.length} service seams</b></div>{services.length ? services.map(([service, providers]: [string, any]) => { const consumers = pluginRows.filter((plugin: any) => list(plugin.requires).includes(service)); return <div className="harness-service-row" key={service}><div className="harness-service-node"><Network size={14} /><strong>{service}</strong></div><div className="harness-provider-stack">{list(providers).map((provider: any) => <span key={`${provider.plugin_id}-${provider.scope}`} className={provider.active === false ? "muted" : ""}>{provider.plugin_id}<small>{provider.scope || "global"} · p{provider.priority || 0}</small></span>)}</div><ChevronRight size={14} /><div className="harness-consumer-stack">{consumers.length ? consumers.map((consumer: any) => <span key={consumer.id}>{consumer.id}</span>) : <small>无下游依赖</small>}</div></div>; }) : <Empty text="当前没有可展示的服务绑定" />}</div>}

      {view === "author" && <div className="harness-author-grid">
        <section className="harness-author-start"><div><span>START HERE</span><strong>无需修改核心代码：从一个领域模板开始</strong><p>先在前端声明插件提供的服务、依赖、触发事件和权限，再把真正的数据库/VM/存储访问实现为隔离 Provider；最后用 Skill 描述取证、变更、回滚和恢复判据。</p></div><button className="primary" onClick={() => openCreatePlugin()}><Plus size={14} />新建第一个插件</button></section>
        <article><span>01</span><Database size={21} /><strong>资源与证据 Provider</strong><p>数据库组或 VM 组只实现本领域的 inventory、evidence、health 与 verify 服务，不依赖 SRE 核心实现。</p><code>provides: inventory.database</code><code>requires: session.events</code></article>
        <article><span>02</span><BrainCircuit size={21} /><strong>Skill 与动作提案</strong><p>把诊断步骤、候选根因、最小变更、回滚和恢复判据写成 Skill；插件只能提案，不能越过审批直接执行。</p><code>permission: ops:propose</code><code>action: typed contract</code></article>
        <article><span>03</span><ShieldCheck size={21} /><strong>企业执行适配器</strong><p>真实写操作交给隔离 Provider、DBA/虚拟化执行器或企业脚本平台；CISRE 统一做审批、审计和写后回读。</p><code>approval → execute → readback</code><code>plaintext secrets: denied</code></article>
        <article><span>04</span><CheckCircle2 size={21} /><strong>验收与可量化成效</strong><p>提供幂等探测、超时、错误码和业务恢复判据；只有真实资源恢复后才记为插件解决的问题。</p><code>ready + stable + symptom absent</code><code>record: plugin / skill / outcome</code></article>
        <section><strong>团队交付最小集合</strong><div><b>1</b><span>插件 manifest：ID、SemVer、provides/requires、scope、permissions</span></div><div><b>2</b><span>只读 Provider：资源清单、证据采集、健康检查、恢复验证</span></div><div><b>3</b><span>Skill 包：触发信号、取证、根因、变更提案、回滚、验证合同</span></div><div><b>4</b><span>执行契约：类型化参数、幂等键、审批风险、回读结果；凭据只用 Secret 引用</span></div><div><b>5</b><span>契约测试：依赖缺失 fail-closed、权限拒绝、超时、回滚和恢复闭环</span></div></section>
      </div>}

      {view === "sessions" && <div className="harness-session-grid">
        <aside>{sessionRows.map((session: any) => <button key={session.session_id} className={selectedSession === session.session_id ? "active" : ""} onClick={() => setSelectedSession(session.session_id)}><span><strong>{session.session_id}</strong><small>{session.parent_session_id ? "branch" : "root"} · {session.phase || "runtime"} · {session.event_count} events</small></span><StatusPill status={session.status || "recorded"} /></button>)}</aside>
        <section>
          <div className="harness-session-toolbar"><div><strong>{selectedSession || "选择一个会话"}</strong><small>{sessionEvents.data?.integrity?.valid === false ? "哈希链校验失败" : `Append-only hash chain verified · ${list(sessionEvents.data?.children).length} child sessions`}</small></div><button className="ghost" disabled={!selectedSession || action.loading} onClick={branchSession}><GitBranch size={13} />从当前位置分支</button>{selectedSessionRow?.parent_session_id && <button className="danger-outline" disabled={action.loading || /running|executing|in_progress/i.test(String(selectedSessionRow.status || ""))} onClick={() => deleteBranch()}><Trash2 size={13} />删除分支</button>}<button className="primary" disabled={!selectedSession || action.loading} onClick={() => runAction(() => apiPost(`/api/harness/sessions/${encodeURIComponent(selectedSession)}/resume`, {}))}><Workflow size={13} />恢复会话</button></div>
          {selectedSessionRow?.parent_session_id && <div className="harness-branch-notice"><GitBranch size={13} /><span>这是从 <button onClick={() => setSelectedSession(selectedSessionRow.parent_session_id)}>{selectedSessionRow.parent_session_id}</button> 创建的分支。删除采用审计墓碑：界面隐藏，但事件证据不会被物理擦除。</span></div>}
          {list(sessionEvents.data?.children).length > 0 && <div className="harness-child-sessions">{list(sessionEvents.data?.children).map((child: any) => <div key={child.session_id}><button onClick={() => setSelectedSession(child.session_id)}><GitBranch size={11} />{child.session_id}<small>{child.status}</small></button><button className="delete" title="删除该分支" disabled={/running|executing|in_progress/i.test(String(child.status || ""))} onClick={() => deleteBranch(child.session_id)}><Trash2 size={11} /></button></div>)}</div>}
          <div className="harness-session-modebar"><div><button className={sessionMode === "events" ? "active" : ""} onClick={() => setSessionMode("events")}><MessageSquareText size={13} />会话</button><button className={sessionMode === "trace" ? "active" : ""} onClick={() => setSessionMode("trace")}><Activity size={13} />轨迹</button></div><button className="ghost" disabled={!selectedSession} onClick={downloadSessionLog}>Session log <Download size={13} /></button></div>
          {sessionMode === "trace" && sessionEvents.data?.trace && <div className="harness-agent-trace studio">
            <header><div><span>AGENT TRACE</span><strong>Input · Model · Agent · Tools</strong></div><small>结构化决策摘要与真实工具回执</small></header>
            <div className="harness-trace-console"><div className="harness-trace-kpis"><span><FileClock size={12} />Duration <b>{Number(sessionEvents.data.trace.summary?.duration_ms || 0) >= 1000 ? `${(Number(sessionEvents.data.trace.summary.duration_ms) / 1000).toFixed(1)}s` : `${sessionEvents.data.trace.summary?.duration_ms || 0}ms`}</b></span><span><MessageSquareText size={12} />Turns <b>{sessionEvents.data.trace.summary?.turns || 0}</b></span><span><TerminalSquare size={12} />Calls <b>{sessionEvents.data.trace.summary?.calls || 0}</b></span><label><Search size={13} /><input value={traceSearch} onChange={(event) => setTraceSearch(event.target.value)} placeholder="搜索 Span / Skill / Plugin / Tool" /></label></div>
              <div className="harness-trace-waterfall">{["Input", "Model", "Agent", "Tools"].map((lane) => <div className={`lane ${lane.toLowerCase()}`} key={lane}><b>{lane}</b><span>{traceSpans.filter((span: any) => span.lane === lane).map((span: any) => <button key={span.span_id} title={`${span.kind}: ${span.name} · ${span.duration_ms || 0}ms`} style={{ left: `${Math.min(98, (Number(span.offset_ms || 0) / traceExtent) * 100)}%`, width: `${Math.max(1.6, Math.min(100, (Number(span.duration_ms || 12) / traceExtent) * 100))}%` }} />)}</span></div>)}</div>
            </div>
            <div className="harness-trace-summary">{Object.entries(sessionEvents.data.trace.summary?.plugins || {}).slice(0, 10).map(([plugin, count]) => <code key={plugin}>{plugin} <b>{String(count)}</b></code>)}</div>
            <div className="harness-trace-spans">{traceSpans.map((span: any) => <article key={span.span_id}><i className={span.kind} /><div><strong><em>{String(span.kind).toUpperCase()}</em>{span.name}</strong><small>#{span.seq} · {span.plugin_id}{span.tool ? ` · ${span.tool}` : ""}{span.model_profile_id ? ` · model:${span.model_profile_id}` : ""} · {span.duration_ms || 0}ms</small><p>{span.summary || span.status}</p></div><StatusPill status={span.status || "recorded"} /></article>)}</div>
            <footer><ShieldCheck size={12} />展示上下文摘要、Skill、插件、工具、审批、变更与验证；密钥、原始 Prompt 和模型私有思维链不会暴露。</footer>
          </div>}
          {sessionMode === "events" && <div className="harness-event-timeline">{list(sessionEvents.data?.events).map((event: any) => <div key={event.event_id}><i /><span><strong>{event.type}</strong><small>#{event.seq} · {event.plugin_id || event.tool || event.stage || "runtime"} · {timeText(event.timestamp)}</small><p>{event.message || event.status}</p>{list(event.data?.artifact_paths).length > 0 && <footer>{list(event.data.artifact_paths).map((path: any) => <code key={path}>{path}</code>)}</footer>}</span><StatusPill status={event.status || "recorded"} /></div>)}</div>}
        </section>
      </div>}

      {view === "security" && <div className="harness-security-grid"><div><ShieldCheck size={22} /><strong>外置代码不进入 API 进程</strong><p>第三方插件只能使用声明式或隔离远程 Provider；未签名插件无法请求文件、子进程、凭据和 K8s 写权限。</p></div><div><GitBranch size={22} /><strong>操作可追溯、可分支</strong><p>会话事件追加写入并串联哈希，支持 replay、fork、resume 和子会话下钻。</p></div><div><CheckCircle2 size={22} /><strong>单调安全门禁</strong><p>任何插件都不能跳过 typed action、风险策略、逐项人工审批、同目标回读和恢复验证。</p></div><div className="harness-permission-table"><strong>允许的外置权限</strong>{list(composition.security?.safe_external_permissions).map((item: any) => <code key={item}>{item}</code>)}<strong>始终由核心保留</strong>{["kubernetes:mutate", "ops:execute", "secrets:read"].map((item) => <code className="denied" key={item}>{item}</code>)}</div></div>}
    </div>
    <div className="surface"><SectionHead icon={TerminalSquare} title="受控运维能力" meta={capabilities?.planner} /><div className="capability-grid">{list(capabilities?.actions).map((item: any) => <div className="capability-card" key={item.action || item.id}><span>{item.risk || "controlled"}</span><strong>{item.action || item.id || item.name}</strong><p>{item.description || item.summary || "证据、预演、审批、执行、写后回读与恢复验证"}</p></div>)}</div></div>
  </div>;
}

export function PluginCenterPage() {
  const [capabilities, refreshCapabilities] = useAsync<any>(() => apiGet("/api/ops/capabilities"), []);
  return <div className="unified-page plugin-center-page">
    {capabilities.error && <div className="inline-error">{capabilities.error}</div>}
    {capabilities.loading && !capabilities.data ? <div className="surface"><Empty text="正在装载插件运行时与服务依赖图" /></div> : <HarnessConsole capabilities={capabilities.data} refreshCapabilities={refreshCapabilities} />}
  </div>;
}

export function OperationsPage() {
  const [tab, setTab] = useState<"scan" | "incidents" | "alerts" | "postmortems" | "capabilities">("scan");
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [intent, setIntent] = useState("crashloop");
  const [severity, setSeverity] = useState("auto");
  const [scan, setScan] = useState<ApiState<any>>({ loading: false });
  const [inventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [], inventory: [] })), []);
  const [incidents, refreshIncidents] = useAsync<any>(() => apiGet("/api/incidents"), []);
  const [alerts, refreshAlerts] = useAsync<any>(() => apiGet("/api/alerts"), []);
  const [postmortems, refreshPostmortems] = useAsync<any>(() => apiGet("/api/postmortems"), []);
  const [capabilities, refreshCapabilities] = useAsync<any>(() => apiGet("/api/ops/capabilities"), []);
  const sources = { incidents: list(incidents.data?.incidents), alerts: list(alerts.data?.alerts), postmortems: list(postmortems.data?.postmortems) };
  const rows = tab === "scan" || tab === "capabilities" ? [] : sources[tab];
  const clusters = list(inventory.data?.clusters);
  const scoped = cluster === "all" ? list(inventory.data?.inventory) : list(inventory.data?.inventory).filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name);
  const namespaces = Array.from(new Set(scoped.flatMap((item: any) => list(item.namespaces).map((entry: any) => String(entry.name))))).sort();
  const refresh = () => { refreshIncidents(); refreshAlerts(); refreshPostmortems(); refreshCapabilities(); };
  async function runScan() {
    setScan({ loading: true });
    try { setScan({ loading: false, data: await apiPost("/api/alert/scan", { cluster, namespace, intent, severity, auto_healing_enabled: false }) }); }
    catch (error: any) { setScan({ loading: false, error: error.message }); }
  }
  return <div className="unified-page">
    <div className="page-commandbar"><div className="segmented"><button className={tab === "scan" ? "active" : ""} onClick={() => setTab("scan")}>扫描诊断</button><button className={tab === "incidents" ? "active" : ""} onClick={() => setTab("incidents")}>事件</button><button className={tab === "alerts" ? "active" : ""} onClick={() => setTab("alerts")}>告警</button><button className={tab === "postmortems" ? "active" : ""} onClick={() => setTab("postmortems")}>复盘</button><button className={tab === "capabilities" ? "active" : ""} onClick={() => setTab("capabilities")}>运维工具</button></div><button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button></div>
    {tab === "capabilities" && <HarnessConsole capabilities={capabilities.data} refreshCapabilities={refreshCapabilities} />}
    {tab === "scan" ? <div className="operations-scan-grid"><div className="surface"><SectionHead icon={Search} title="证据扫描" meta="仅在发现真实信号后触发 AI 诊断" /><div className="ops-scan-form"><label>集群<select value={cluster} onChange={(event) => { setCluster(event.target.value); setNamespace("all"); }}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></label><label>Namespace<select value={namespace} onChange={(event) => setNamespace(event.target.value)}><option value="all">所有 Namespace</option>{namespaces.map((item) => <option key={item} value={item}>{item}</option>)}</select></label><label>异常类型<select value={intent} onChange={(event) => setIntent(event.target.value)}><option value="crashloop">CrashLoop / 镜像 / OOM</option><option value="pending">Pending / 调度</option><option value="highcpu">高 CPU</option></select></label><label>严重级别<select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="auto">自动识别</option><option value="P1">P1</option><option value="P2">P2</option><option value="P3">P3</option></select></label><button className="primary" onClick={runScan} disabled={scan.loading}>{scan.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}扫描并诊断</button></div></div><div className="surface"><SectionHead icon={BrainCircuit} title="诊断结果" meta={scan.data?.status || "waiting"} />{scan.error && <div className="inline-error">{scan.error}</div>}{scan.data ? <div className="scan-result"><StatusPill status={scan.data.status || "ok"} /><h3>{scan.data.reason || scan.data.scan?.findings?.[0]?.issue?.reason || "扫描完成"}</h3><p>{scan.data.results?.[0]?.answer || scan.data.answer || `检查 ${scan.data.evidence?.pods_checked ?? list(scan.data.scan?.findings).length} 个 Pod，发现 ${list(scan.data.scan?.findings).length} 条匹配信号。`}</p><div className="compact-list">{list(scan.data.scan?.findings).map((item: any) => <div className="compact-row" key={`${item.cluster}-${item.namespace}-${item.name}`}><span className="resource-icon risk"><Boxes size={14} /></span><div><strong>{item.name}</strong><small>{item.cluster}/{item.namespace} · {item.issue?.reason || item.phase}</small></div><StatusPill status={scan.data.scan?.severity || "warning"} /></div>)}</div></div> : <Empty text="选择范围和异常类型后开始扫描" />}</div></div> : tab === "capabilities" ? <div className="capability-stack"><div className="surface harness-surface"><SectionHead icon={BrainCircuit} title="DeepSeek Harness 内核" meta={`${capabilities.data?.harness?.summary?.active || 0}/${capabilities.data?.harness?.summary?.total || 0} plugins active`} /><div className="harness-runtime-strip"><div><span>Runtime</span><strong>{String(capabilities.data?.harness?.runtime || "CISREPluginHarness/v1")}</strong></div><div><span>架构</span><strong>Everything is a Plugin</strong></div><div><span>事件模式</span><strong>Waterfall · Serial · Parallel</strong></div><div><span>生产边界</span><strong>审批后执行 · 写后回读</strong></div></div><div className="harness-plugin-list">{list(capabilities.data?.harness?.plugins).map((plugin: any) => <div key={plugin.id} className={plugin.status === "active" ? "active" : "pending"}><i /><span><strong>{plugin.id}</strong><small>{plugin.description}</small></span><b>{plugin.status}</b></div>)}</div></div><div className="surface"><SectionHead icon={TerminalSquare} title="受控运维能力" meta={capabilities.data?.planner} /><div className="capability-grid">{list(capabilities.data?.actions).map((item: any) => <div className="capability-card" key={item.action || item.id}><span>{item.risk || "controlled"}</span><strong>{item.action || item.id || item.name}</strong><p>{item.description || item.summary || "通过证据、预演、审批和恢复验证执行"}</p></div>)}</div></div></div> : <div className="surface"><SectionHead icon={tab === "incidents" ? BellRing : tab === "alerts" ? AlertTriangle : FileClock} title={tab === "incidents" ? "事件时间线" : tab === "alerts" ? "告警记录" : "复盘报告"} meta={`${rows.length} records`} />{rows.length ? <div className="timeline-list">{rows.slice().reverse().map((item: any, index: number) => <div className="timeline-item" key={item.incident_id || item.id || index}><i /><div><div><strong>{item.title || item.alert_name || item.name || "记录"}</strong><StatusPill status={item.status || item.severity || "recorded"} /></div><p>{item.summary || item.description || item.root_cause || item.report || "已进入审计时间线"}</p><small>{item.cluster || ""} {item.namespace || ""} · {timeText(item.created_at || item.timestamp)}</small></div></div>)}</div> : <Empty text="暂无记录；新的告警与处置会自动进入这里" />}</div>}
  </div>;
}

type SkillChoice = {
  id: string;
  label?: string;
  name?: string;
  description?: string;
  when_to_use?: string;
  operator_note?: string;
  rollback?: string;
  risk?: string;
  auto_allowed?: boolean;
  allowed_targets?: string[];
  required_evidence?: string[];
};

const fallbackSkillOptions = {
  applies_to: [
    { id: "Pod", label: "Pod", description: "单个运行实例，适合日志、重启、挂载、探针和调度问题。" },
    { id: "Deployment", label: "Deployment", description: "无状态工作负载，适合模板、镜像、副本和发布问题。" },
    { id: "StatefulSet", label: "StatefulSet", description: "有状态工作负载，需关注稳定身份和持久卷。" },
    { id: "Service", label: "Service", description: "服务发现和流量入口，适合 selector、端口和 Endpoint 问题。" },
    { id: "Node", label: "Node", description: "集群节点，适合压力、NotReady、隔离和恢复调度问题。" },
    { id: "PVC", label: "PVC", description: "存储声明，适合 Pending、扩容和绑定问题。" },
    { id: "Database", label: "Database", description: "数据库实例或集群，适合连接、慢 SQL、锁、复制、容量和备份问题。" },
    { id: "MySQL", label: "MySQL", description: "MySQL / MariaDB 实例，关注连接池、主从复制、慢查询和 InnoDB 锁。" },
    { id: "Oracle", label: "Oracle", description: "Oracle 数据库实例，关注表空间、会话、归档、锁等待和 Data Guard。" },
    { id: "Redis", label: "Redis", description: "缓存与内存型数据库，关注内存、主从、慢命令、过期策略和连接数。" },
    { id: "VirtualMachine", label: "VirtualMachine", description: "虚拟机、云主机或物理主机，适合系统服务、磁盘、网络和 Agent 问题。" },
    { id: "LinuxHost", label: "LinuxHost", description: "Linux 主机，适合 systemd、文件系统、内核、进程和网络排障。" },
    { id: "StorageArray", label: "StorageArray", description: "企业存储或 NAS/SAN 后端，适合容量、路径、ACL 和快照问题。" },
  ],
  evidence_required: [
    { id: "previous_logs", label: "上一次容器日志", description: "CrashLoop 场景优先读取，定位上次退出前的错误。" },
    { id: "events", label: "Kubernetes Events", description: "确认调度、挂载、镜像、探针和准入失败。" },
    { id: "workload_spec", label: "Workload 配置", description: "读取镜像、探针、资源、卷和安全上下文。" },
    { id: "dependency_topology", label: "依赖拓扑", description: "读取 CMDB、调用链和跨集群中间件数据流。" },
    { id: "db_connectivity", label: "数据库连通性", description: "确认实例、监听端口、账号权限和网络路径。" },
    { id: "db_slow_queries", label: "慢 SQL 证据", description: "读取慢 SQL、执行计划和热点表信息。" },
    { id: "db_locks", label: "锁等待 / 长事务", description: "确认阻塞会话、锁等待、长事务和影响范围。" },
    { id: "db_replication", label: "复制 / HA 状态", description: "确认主从、延迟、只读、故障转移和同步状态。" },
    { id: "db_capacity", label: "数据库容量", description: "检查表空间、磁盘、连接数、内存和日志空间。" },
    { id: "vm_agent_status", label: "主机 Agent 状态", description: "确认监控、云助手、虚拟化 Agent 或安全 Agent 是否在线。" },
    { id: "vm_system_metrics", label: "主机系统指标", description: "读取 CPU、内存、磁盘、IO、网络和文件句柄。" },
    { id: "vm_service_status", label: "系统服务状态", description: "读取 systemd / Windows Service 状态和最近错误。" },
    { id: "vm_disk_usage", label: "主机磁盘使用", description: "确认文件系统、inode、挂载点、增长目录和扩容能力。" },
  ],
  success_criteria: [
    { id: "pod_ready", label: "Pod Ready", description: "目标 Pod 连续通过 readiness 并保持稳定。" },
    { id: "rollout_complete", label: "发布完成", description: "期望副本全部可用，generation 已收敛。" },
    { id: "restart_count_stable", label: "重启数稳定", description: "观察窗口内重启数不再增长。" },
    { id: "error_rate_recovered", label: "错误率恢复", description: "错误率回到 SLO 或变更前基线。" },
    { id: "db_connection_recovered", label: "数据库连接恢复", description: "业务连接成功率和实例连接数恢复到安全区间。" },
    { id: "db_replication_caught_up", label: "复制追平", description: "复制延迟回到阈值内，HA 状态正常。" },
    { id: "db_slow_query_reduced", label: "慢 SQL 降低", description: "慢查询和锁等待回落，核心 SQL 不再阻塞业务。" },
    { id: "vm_agent_online", label: "主机 Agent 在线", description: "监控、虚拟化或云助手 Agent 恢复在线。" },
    { id: "vm_service_active", label: "服务运行正常", description: "关键服务 active/running，业务探针恢复。" },
    { id: "vm_disk_pressure_relieved", label: "磁盘压力解除", description: "磁盘、inode 或挂载点容量回到安全阈值。" },
  ],
  script_triggers: [
    { id: "symptom_matched", label: "症状精确命中", description: "日志、事件或告警命中 Skill 症状关键词。" },
    { id: "required_evidence_collected", label: "必要证据已齐", description: "本 Skill 选择的必要证据全部采集完成。" },
    { id: "root_cause_confirmed", label: "根因已确认", description: "证据评分达到确认阈值，不凭猜测执行。" },
    { id: "manual_confirmation", label: "必须人工确认", description: "运维人员查看影响和参数后点击确认。" },
  ],
};

const fallbackActionOptions: SkillChoice[] = [
  { id: "apply_manifest", label: "应用 Kubernetes YAML", description: "创建或更新任意 Kubernetes API 资源。", risk: "high", when_to_use: "AI 已基于真实对象生成完整 YAML。", operator_note: "展示完整 YAML 与差异后逐步确认。" },
  { id: "patch_resource", label: "修改 Kubernetes 资源", description: "Patch Workload、ConfigMap、Secret、PV/PVC 等资源。", risk: "high", when_to_use: "已锁定 apiVersion、kind、namespace、name 和最小 Patch。", operator_note: "必须保存原对象快照。" },
  { id: "delete_resource", label: "删除 Kubernetes 资源", description: "删除一个明确目标资源。", risk: "high", when_to_use: "证据证明删除必要且影响可控。", operator_note: "必须显示恢复快照并逐步确认。" },
  { id: "run_shell", label: "执行平台 Shell", description: "在 CISRE 运行环境执行完整命令。", risk: "high", when_to_use: "Skill 明确要求平台侧命令。", operator_note: "逐步确认、超时、输出审计。" },
  { id: "exec_pod", label: "执行 Pod 命令", description: "进入指定 Pod/container 执行命令。", risk: "high", when_to_use: "必须在容器内取证或处置。", operator_note: "显示完整命令与目标后逐步确认。" },
  { id: "exec_node", label: "执行节点命令", description: "通过受控节点执行器运行命令。", risk: "high", when_to_use: "必须在指定 Kubernetes 节点处置。", operator_note: "特权高风险，逐步确认并审计。" },
  { id: "patch_workload", label: "修改 Workload 配置", description: "修正镜像、探针、资源、副本、环境变量或安全上下文。", risk: "medium", when_to_use: "证据确认 Deployment、StatefulSet 或 DaemonSet 模板配置有误。", operator_note: "执行前展示差异，可恢复原模板回滚。" },
  { id: "restart", label: "滚动重启组件", description: "触发受控滚动重启，不修改 Workload 配置。", risk: "medium", when_to_use: "配置正确但进程卡死、连接未刷新或需要重新拉起 Pod。", operator_note: "不会修复错误配置，需确认副本和 PDB 安全。" },
  { id: "scale_out", label: "增加副本", description: "在平台上限内增加 Workload 副本。", risk: "medium", when_to_use: "CPU、流量或并发证据证明容量不足。", operator_note: "观察资源配额和下游依赖承载能力。" },
  { id: "recreate_pod", label: "重建异常 Pod", description: "删除单个异常 Pod，由控制器按原模板重建。", risk: "medium", when_to_use: "只有单个 Pod 状态异常，模板和其他副本正常。", operator_note: "不适合模板级或存储级故障。" },
  { id: "rollback_workload", label: "回滚 Workload", description: "回滚到真实观测过的稳定镜像或模板 revision。", risk: "high", when_to_use: "故障与最近发布高度相关，并存在稳定回滚点。", operator_note: "高风险，必须人工确认。" },
  { id: "create_pvc", label: "创建缺失 PVC", description: "按批准存储策略创建 Workload 缺失的 PVC。", risk: "high", when_to_use: "Workload 明确引用不存在的 PVC，容量和访问模式已确认。", operator_note: "不能由 LLM 编造 StorageClass 和容量策略。" },
  { id: "create_pv", label: "创建静态 PV", description: "按存储管理员批准模板创建静态 PV。", risk: "high", when_to_use: "动态供卷不可用且后端路径、回收策略已批准。", operator_note: "严禁编造 NFS、LUN 或目录路径。" },
  { id: "patch_workload_volume", label: "修正卷引用", description: "修正 Workload 的 PVC、volume 或 mount 引用。", risk: "high", when_to_use: "完整存储链证据证明原卷引用错误。", operator_note: "需要保存原配置回滚点。" },
  { id: "patch_service", label: "修正 Service", description: "修正 selector、port 或 targetPort 不匹配。", risk: "high", when_to_use: "Service 没有 Endpoint，且证据证明配置不匹配。", operator_note: "错误修改会造成流量黑洞。" },
  { id: "patch_service_account", label: "修正 ServiceAccount", description: "绑定企业批准的 imagePullSecret。", risk: "medium", when_to_use: "镜像拉取失败且缺少批准的凭据引用。", operator_note: "不读取或修改 Secret 明文。" },
  { id: "create_configmap", label: "恢复 ConfigMap", description: "从运维人员批准模板恢复缺失 ConfigMap。", risk: "high", when_to_use: "Workload 引用的配置缺失且存在批准模板。", operator_note: "不能让 LLM 自行生成生产配置值。" },
  { id: "patch_hpa", label: "调整 HPA 范围", description: "调整 HPA 最小和最大副本。", risk: "medium", when_to_use: "HPA 上下限阻止合理扩缩容，指标语义正常。", operator_note: "不修改 HPA 指标算法。" },
  { id: "expand_pvc", label: "扩容 PVC", description: "扩展支持在线扩容的已绑定 PVC。", risk: "high", when_to_use: "卷容量逼近上限且 StorageClass 支持扩容。", operator_note: "通常不可逆，需核对备份和文件系统。" },
  { id: "cordon_node", label: "隔离节点", description: "停止在问题节点上调度新 Pod。", risk: "high", when_to_use: "节点明确存在压力、NotReady 或硬件故障。", operator_note: "不会自动迁移已有 Pod。" },
  { id: "evict_pod", label: "受控驱逐 Pod", description: "通过 Eviction API 迁移 Pod，并遵守 PDB。", risk: "high", when_to_use: "节点维护或隔离后需要迁移工作负载。", operator_note: "高风险且必须人工确认。" },
  { id: "uncordon_node", label: "恢复节点调度", description: "将已恢复节点重新加入调度。", risk: "high", when_to_use: "节点 Ready、压力和系统组件均已恢复。", operator_note: "恢复前必须完成健康验证。" },
  { id: "patch_pdb", label: "修正 PDB", description: "修正导致发布或驱逐死锁的中断预算。", risk: "high", when_to_use: "PDB 与副本数形成死锁且业务可用性证据充分。", operator_note: "持续观察可用副本和 SLO。" },
  { id: "db_expand_storage", label: "扩容数据库存储", description: "通过 DBA/存储受控执行器扩容数据库表空间或磁盘。", risk: "high", when_to_use: "数据库容量证据达到阈值，备份和扩容策略已确认。", operator_note: "通常不可逆，必须保留变更单和容量审批。" },
  { id: "db_kill_session", label: "终止阻塞会话", description: "终止确认阻塞业务的数据库会话。", risk: "high", when_to_use: "锁等待、长事务和会话来源证据完整。", operator_note: "必须展示会话、SQL、业务影响和回滚说明。" },
  { id: "db_failover", label: "数据库主备切换", description: "按 HA 预案触发数据库故障转移。", risk: "high", when_to_use: "主库故障或复制链路异常且备用节点健康。", operator_note: "必须二次确认 RPO/RTO、只读状态和回切方案。" },
  { id: "db_apply_parameter", label: "调整数据库参数", description: "按批准模板调整数据库运行参数。", risk: "high", when_to_use: "证据证明参数导致连接、锁或性能故障。", operator_note: "不能由 LLM 编造生产参数值。" },
  { id: "db_restart_instance", label: "重启数据库实例", description: "通过受控执行器重启数据库实例。", risk: "high", when_to_use: "只在 HA、窗口、备份和影响范围均确认后使用。", operator_note: "高风险，通常作为最后手段。" },
  { id: "vm_restart_service", label: "重启主机服务", description: "重启虚拟机或主机上的指定系统服务。", risk: "medium", when_to_use: "服务进程异常且配置、依赖、磁盘和权限已确认。", operator_note: "必须指定服务名和恢复探针。" },
  { id: "vm_expand_disk", label: "扩容主机磁盘", description: "扩展虚拟磁盘并执行文件系统扩容。", risk: "high", when_to_use: "磁盘或 inode 压力达到阈值，快照和挂载点已确认。", operator_note: "需要外部虚拟化/云平台执行器。" },
  { id: "vm_reboot", label: "重启虚拟机", description: "对故障主机执行受控重启。", risk: "high", when_to_use: "内核、Agent、系统服务无法恢复，且业务冗余已确认。", operator_note: "必须作为高风险动作二次确认。" },
  { id: "middleware_rebalance", label: "中间件再均衡", description: "对 Kafka/MQ 等中间件执行分区或实例再均衡。", risk: "high", when_to_use: "消费者滞后、Broker 压力或分区分布异常证据充分。", operator_note: "需要限速、窗口和回滚策略。" },
  { id: "storage_expand_volume", label: "扩容存储卷", description: "通过存储受控执行器扩展企业存储卷。", risk: "high", when_to_use: "存储池、卷、映射和业务挂载关系确认无误。", operator_note: "必须由存储团队批准容量策略。" },
  { id: "network_apply_policy", label: "调整网络策略", description: "通过网络受控执行器修改一个明确的 ACL 或安全策略。", risk: "high", when_to_use: "策略命中与路径证据确认规则是直接根因。", operator_note: "展示网段、业务影响、结构化差异和回滚快照。" },
  { id: "network_switch_route", label: "切换网络路由", description: "将故障路径切换到已验证的备用下一跳。", risk: "high", when_to_use: "路径、冗余和收敛证据完整。", operator_note: "必须评估双向路径与爆炸半径。" },
  { id: "network_update_load_balancer", label: "调整负载均衡 / DNS", description: "修改监听、成员池、权重或 DNS 记录。", risk: "high", when_to_use: "健康检查或解析证据确认入口配置异常。", operator_note: "必须保留成员、权重、TTL 和原配置。" },
  { id: "network_restore_interface", label: "恢复接口 / 链路", description: "恢复接口、VLAN 或链路聚合配置。", risk: "high", when_to_use: "接口状态和错误计数证明链路配置异常。", operator_note: "必须确认冗余路径和带外管理。" },
  { id: "infra_run_approved_action", label: "执行批准基础设施动作", description: "调用外部执行器中已经登记的企业标准动作。", risk: "high", when_to_use: "非 K8s 对象需要平台外动作，且动作已在企业执行器登记。", operator_note: "平台只传递结构化计划，不执行任意命令。" },
];

function createEmptySkillForm() {
  return {
    id: "",
    name: "",
    category: "runtime",
    summary: "",
    symptoms: "",
    applies_to: ["Pod", "Deployment", "StatefulSet", "Service"],
    evidence_required: ["previous_logs", "events", "workload_spec"],
    diagnostic_steps: "",
    allowed_actions: ["patch_workload", "recreate_pod"],
    success_criteria: ["pod_ready", "restart_count_stable"],
    risk: "medium",
    owner: "",
    script_enabled: false,
    script_id: "",
    script_trigger_conditions: ["required_evidence_collected", "root_cause_confirmed", "manual_confirmation"],
    script_trigger_description: "",
    script_timeout_seconds: 120,
  };
}

type SkillForm = ReturnType<typeof createEmptySkillForm>;

function splitSkillList(value: string) {
  return value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean);
}

function SkillMultiSelect({
  title,
  selected,
  options,
  onChange,
  onInspect,
  hint,
}: {
  title: string;
  selected: string[];
  options: SkillChoice[];
  onChange: (value: string[]) => void;
  onInspect: (option: SkillChoice, title: string) => void;
  hint: string;
}) {
  function toggle(id: string) {
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  }
  const selectedOptions = selected.map((id) => options.find((item) => item.id === id) || { id, label: id });
  return <div className="skill-multiselect">
    <div className="skill-field-title"><span>{title}</span><small>可多选 · {selected.length} 项</small></div>
    <details>
      <summary>{selected.length ? selectedOptions.slice(0, 3).map((item) => item.label || item.name || item.id).join("、") + (selected.length > 3 ? ` 等 ${selected.length} 项` : "") : hint}<ChevronRight size={14} /></summary>
      <div className="skill-option-menu">
        {options.map((option) => <div className={selected.includes(option.id) ? "selected" : ""} key={option.id}>
          <label><input type="checkbox" checked={selected.includes(option.id)} onChange={() => toggle(option.id)} /><span><b>{option.label || option.name || option.id}</b><small>{option.description || option.when_to_use || option.id}</small></span></label>
          <button type="button" onClick={() => onInspect(option, title)} title={`查看${option.label || option.id}说明`}><Eye size={14} /></button>
        </div>)}
      </div>
    </details>
    <div className="skill-selected-chips">
      {selectedOptions.map((option) => <button type="button" key={option.id} onClick={() => toggle(option.id)} title="点击移除">{option.label || option.name || option.id}<X size={11} /></button>)}
    </div>
  </div>;
}

export function OpsSkillsPage() {
  const [skills, refreshSkills] = useAsync<any>(() => apiGet("/api/ops/skills"), []);
  const [records, refreshRecords] = useAsync<any>(() => apiGet("/api/ops/records?limit=100"), []);
  const [capabilities] = useAsync<any>(() => apiGet("/api/ops/capabilities"), []);
  const [form, setForm] = useState<SkillForm>(() => createEmptySkillForm());
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [matchQuestion, setMatchQuestion] = useState("Pod CrashLoopBackOff，previous log 提示 permission denied，挂载 PVC 后启动失败");
  const [match, setMatch] = useState<ApiState<any>>({ loading: false });
  const [inspected, setInspected] = useState<{ title: string; option: SkillChoice } | null>(null);
  const [importing, setImporting] = useState(false);
  const importInput = useRef<HTMLInputElement>(null);
  const actions = (list(capabilities.data?.actions).length ? list(capabilities.data?.actions) : fallbackActionOptions) as SkillChoice[];
  const optionCatalog = capabilities.data?.skill_options || fallbackSkillOptions;
  const appliesToOptions = (list(optionCatalog.applies_to).length ? list(optionCatalog.applies_to) : fallbackSkillOptions.applies_to) as SkillChoice[];
  const evidenceOptions = (list(optionCatalog.evidence_required).length ? list(optionCatalog.evidence_required) : fallbackSkillOptions.evidence_required) as SkillChoice[];
  const successOptions = (list(optionCatalog.success_criteria).length ? list(optionCatalog.success_criteria) : fallbackSkillOptions.success_criteria) as SkillChoice[];
  const scriptTriggerOptions = (list(optionCatalog.script_triggers).length ? list(optionCatalog.script_triggers) : fallbackSkillOptions.script_triggers) as SkillChoice[];
  const approvedScripts = list(capabilities.data?.approved_scripts) as SkillChoice[];
  const selectedScript = approvedScripts.find((item) => item.id === form.script_id);

  function update<K extends keyof SkillForm>(key: K, value: SkillForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function inspectOption(option: SkillChoice, title: string) {
    setInspected({ title, option });
  }

  function editSkill(skill: any) {
    const scriptPolicy = skill.script_policy || {};
    setForm({
      id: skill.id || "",
      name: skill.name || "",
      category: skill.category || "runtime",
      summary: skill.summary || "",
      symptoms: list(skill.symptoms).join("\n"),
      applies_to: list(skill.applies_to),
      evidence_required: list(skill.evidence_required),
      diagnostic_steps: list(skill.diagnostic_steps).join("\n"),
      allowed_actions: list(skill.allowed_actions),
      success_criteria: list(skill.success_criteria),
      risk: skill.risk || "medium",
      owner: skill.owner || "",
      script_enabled: Boolean(scriptPolicy.enabled),
      script_id: scriptPolicy.script_id || "",
      script_trigger_conditions: list(scriptPolicy.trigger_conditions),
      script_trigger_description: scriptPolicy.trigger_description || "",
      script_timeout_seconds: Number(scriptPolicy.timeout_seconds || 120),
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function saveSkill() {
    setSaving(true);
    setMessage("");
    try {
      await apiPost("/api/ops/skills", {
        id: form.id,
        name: form.name,
        category: form.category,
        summary: form.summary,
        symptoms: splitSkillList(form.symptoms),
        applies_to: form.applies_to,
        evidence_required: form.evidence_required,
        diagnostic_steps: splitSkillList(form.diagnostic_steps),
        allowed_actions: form.allowed_actions,
        success_criteria: form.success_criteria,
        risk: form.risk,
        owner: form.owner,
        script_policy: {
          enabled: form.script_enabled,
          script_id: form.script_enabled ? form.script_id : "",
          trigger_conditions: form.script_enabled ? form.script_trigger_conditions : [],
          trigger_description: form.script_enabled ? form.script_trigger_description : "",
          timeout_seconds: form.script_timeout_seconds,
          require_confirmation: true,
        },
      });
      setForm(createEmptySkillForm());
      setMessage("Skill 已保存，会参与 SRE Run 和 AI 巡检的自动匹配。");
      refreshSkills();
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function disableSkill(skill: any) {
    setMessage("");
    try {
      await apiPost(`/api/ops/skills/${encodeURIComponent(skill.id)}/delete`, {});
      setMessage(skill.builtin ? "内置 Skill 已禁用。" : "自定义 Skill 已删除。");
      refreshSkills();
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  async function testMatch() {
    setMatch({ loading: true });
    try {
      setMatch({ loading: false, data: await apiPost("/api/ops/skills/match", { question: matchQuestion, top_k: 5 }) });
    } catch (error: any) {
      setMatch({ loading: false, error: error.message });
    }
  }

  async function importSkill(file?: File) {
    if (!file) return;
    setImporting(true);
    setMessage("");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch("/api/ops/skills/import", { method: "POST", body, headers: adminAuthHeaders({ Accept: "application/json" }) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `${response.status} ${response.statusText}`);
      invalidateApiCache("/api/ops/skills");
      invalidateApiCache("/api/ops/capabilities");
      setMessage(data.message || `已导入 ${list(data.imported).length} 个标准 Agent Skill。`);
      refreshSkills();
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setImporting(false);
      if (importInput.current) importInput.current.value = "";
    }
  }

  function exportSkill(skill: any) {
    window.location.assign(`/api/ops/skills/${encodeURIComponent(skill.id)}/export`);
  }

  async function deleteRecord(record: any) {
    if (!window.confirm(`确定删除运维记录 ${record.id}？聚合 Skill 统计不会随单条记录删除。`)) return;
    try {
      await apiDelete(`/api/ops/records/${encodeURIComponent(record.id)}`);
      refreshRecords();
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  return <div className="skill-workbench">
    <div className="surface">
      <SectionHead icon={BrainCircuit} title="运维 Skill 注入" meta="保存即生成标准 SKILL.md，可跨智能体复用" action={<div className="skill-head-actions"><input ref={importInput} type="file" accept=".zip,application/zip" hidden onChange={(event) => importSkill(event.target.files?.[0])} /><button className="ghost" onClick={() => importInput.current?.click()} disabled={importing}>{importing ? <Loader2 className="spin" size={15} /> : <Upload size={15} />}导入 Skill</button><button className="ghost" onClick={refreshSkills}><RefreshCcw size={15} />刷新</button></div>} />
      <div className="skill-form">
        <label>Skill 名称<input value={form.name} onChange={(event) => update("name", event.target.value)} placeholder="例如：PVC Pending 静态 PV 恢复" /></label>
        <label>类别<select value={form.category} onChange={(event) => update("category", event.target.value)}><option value="runtime">运行时</option><option value="database">数据库</option><option value="virtual_machine">虚拟机 / 主机</option><option value="middleware">中间件</option><option value="storage">存储</option><option value="network">网络</option><option value="release">发布</option><option value="security">安全</option><option value="cloud">云资源</option><option value="custom">自定义</option></select></label>
        <label>风险<select value={form.risk} onChange={(event) => update("risk", event.target.value)}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select></label>
        <label>负责人<input value={form.owner} onChange={(event) => update("owner", event.target.value)} placeholder="团队或姓名" /></label>
        <label className="span-two">一句话说明<textarea value={form.summary} onChange={(event) => update("summary", event.target.value)} placeholder="这条经验解决什么场景，AI 什么时候应该考虑它；可以是 K8s、数据库、虚拟机、存储或中间件。" /></label>
        <label>症状关键词<textarea value={form.symptoms} onChange={(event) => update("symptoms", event.target.value)} placeholder="一行一个，例如 FailedMount、permission denied、ImagePullBackOff、表空间不足、锁等待、主机磁盘满" /></label>
        <label>诊断步骤<textarea value={form.diagnostic_steps} onChange={(event) => update("diagnostic_steps", event.target.value)} placeholder="按真实运维流程写，一行一步。" /></label>
        <SkillMultiSelect title="适用对象" selected={form.applies_to} options={appliesToOptions} onChange={(value) => update("applies_to", value)} onInspect={inspectOption} hint="选择 K8s、数据库、虚拟机、中间件、存储或云资源对象" />
        <SkillMultiSelect title="需要证据" selected={form.evidence_required} options={evidenceOptions} onChange={(value) => update("evidence_required", value)} onInspect={inspectOption} hint="选择执行前必须读取的真实证据" />
        <SkillMultiSelect title="允许动作" selected={form.allowed_actions} options={actions} onChange={(value) => update("allowed_actions", value)} onInspect={inspectOption} hint="选择经过平台门禁的受控动作" />
        <SkillMultiSelect title="恢复判据" selected={form.success_criteria} options={successOptions} onChange={(value) => update("success_criteria", value)} onInspect={inspectOption} hint="选择如何客观判断问题已恢复" />
        <div className="skill-script-policy span-two">
          <div className="skill-script-header">
            <div><ShieldCheck size={16} /><span><strong>企业批准脚本</strong><small>可选能力，脚本正文不进入 Skill</small></span></div>
            <label className="skill-toggle"><input type="checkbox" checked={form.script_enabled} onChange={(event) => update("script_enabled", event.target.checked)} /><i /><span>{form.script_enabled ? "允许作为候选" : "不使用脚本"}</span></label>
          </div>
          {form.script_enabled && <div className="skill-script-body">
            <label>批准脚本<select value={form.script_id} onChange={(event) => update("script_id", event.target.value)}>
              <option value="">选择 ConfigMap 中登记的脚本</option>
              {approvedScripts.filter((item: any) => item.enabled !== false).map((item) => <option key={item.id} value={item.id}>{item.name || item.id} · {item.risk || "high"}</option>)}
            </select></label>
            <div className="script-inspect">
              <button type="button" className="ghost tiny" disabled={!selectedScript} onClick={() => selectedScript && inspectOption(selectedScript, "企业批准脚本")}><Eye size={13} />查看脚本说明</button>
              {!approvedScripts.length && <small>尚未配置 OPS_APPROVED_SCRIPTS_JSON，脚本模式不能保存。</small>}
            </div>
            <SkillMultiSelect title="脚本触发条件" selected={form.script_trigger_conditions} options={scriptTriggerOptions} onChange={(value) => update("script_trigger_conditions", value)} onInspect={inspectOption} hint="选择必须同时满足的触发门槛" />
            <label>最长执行时间<select value={form.script_timeout_seconds} onChange={(event) => update("script_timeout_seconds", Number(event.target.value))}><option value={30}>30 秒</option><option value={60}>60 秒</option><option value={120}>120 秒</option><option value={300}>300 秒</option><option value={600}>600 秒</option></select></label>
            <label className="span-two">具体触发场景<textarea value={form.script_trigger_description} onChange={(event) => update("script_trigger_description", event.target.value)} placeholder="例如：Pod 连续 3 次 CrashLoop，previous log 明确出现 permission denied，PVC 已 Bound，且 securityContext 与存储目录权限不一致时，允许调用该脚本；仅凭用户描述不得触发。" /></label>
            <div className="skill-script-guard span-two"><ShieldCheck size={15} /><span>脚本必须先在 ConfigMap 批准目录登记。命中 Skill 后只会成为候选，仍需证据齐全、影响范围检查、人工确认、超时控制和执行审计。</span></div>
          </div>}
        </div>
        {inspected && <div className="skill-info-panel span-two">
          <header><div><Eye size={15} /><span><small>{inspected.title}</small><strong>{inspected.option.label || inspected.option.name || inspected.option.id}</strong></span></div><button type="button" onClick={() => setInspected(null)} title="关闭说明"><X size={16} /></button></header>
          <p>{inspected.option.description || inspected.option.when_to_use || "暂无详细说明。"}</p>
          <div>
            {inspected.option.when_to_use && <span><b>何时使用</b>{inspected.option.when_to_use}</span>}
            {inspected.option.operator_note && <span><b>操作注意</b>{inspected.option.operator_note}</span>}
            {inspected.option.risk && <span><b>风险等级</b>{inspected.option.risk}</span>}
            {typeof inspected.option.auto_allowed === "boolean" && <span><b>自动执行</b>{inspected.option.auto_allowed ? "满足门禁时允许" : "必须人工确认"}</span>}
            {inspected.option.rollback && <span><b>回退方式</b>{inspected.option.rollback}</span>}
            {list(inspected.option.allowed_targets).length > 0 && <span><b>允许对象</b>{list(inspected.option.allowed_targets).join("、")}</span>}
            {list(inspected.option.required_evidence).length > 0 && <span><b>脚本前置证据</b>{list(inspected.option.required_evidence).join("、")}</span>}
          </div>
        </div>}
        <div className="skill-portability-note span-two"><Workflow size={15} /><span><strong>兼容 Agent Skills 开放规范</strong><small>平台会生成 SKILL.md、agents/openai.yaml 与 references/ops-policy.yaml；原有证据门禁和执行审批保持不变。</small></span></div>
        <button className="primary span-two" onClick={saveSkill} disabled={saving || !form.name.trim()}>{saving ? <Loader2 className="spin" size={15} /> : <CheckCircle2 size={15} />}保存并生成 Skill 包</button>
        {message && <div className={message.includes("已") ? "success-box span-two" : "inline-error span-two"}>{message}</div>}
      </div>
    </div>
    <div className="surface">
      <SectionHead icon={Search} title="匹配测试" meta="模拟 AI 如何选择专家经验" />
      <div className="skill-match-box">
        <textarea value={matchQuestion} onChange={(event) => setMatchQuestion(event.target.value)} />
        <button className="primary" onClick={testMatch} disabled={match.loading}>{match.loading ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}测试匹配</button>
      </div>
      {match.error && <div className="inline-error">{match.error}</div>}
      <div className="skill-match-list">
        {list(match.data?.matches).map((item: any) => <div className="skill-card matched" key={item.skill?.id}><span>{Math.round(Number(item.confidence || 0) * 100)}%</span><strong>{item.skill?.name}</strong><p>{item.why}</p><small>{list(item.matched_terms).slice(0, 8).join(" / ")}</small></div>)}
      </div>
    </div>
    <div className="surface span-two">
      <SectionHead icon={TerminalSquare} title="Skill 库" meta={`${skills.data?.summary?.enabled || 0}/${skills.data?.summary?.total || 0} enabled · ${skills.data?.summary?.candidates || 0} 待审核`} />
      {skills.error && <div className="inline-error">{skills.error}</div>}
      <div className="skill-grid">
        {list(skills.data?.skills).map((skill: any) => <article className={`skill-card ${skill.enabled ? "" : "disabled"}`} key={skill.id}>
          <div><span>{skill.lifecycle === "candidate" ? "候选待审核" : skill.category} · {skill.skill_type || "recovery"} · {skill.risk} · v{skill.version || "1.0.0"}</span><strong>{skill.name}</strong></div>
          <p>{skill.summary}</p>
          {list(skill.dimensions).length > 0 && <div className="chips">{list(skill.dimensions).map((item: any) => <span key={item}>{item}</span>)}</div>}
          {list(skill.progressive_evidence).length > 0 && <small>渐进取证：{list(skill.progressive_evidence).map((item: any) => item.stage).join(" → ")}</small>}
          <div className="chips">{list(skill.allowed_actions).slice(0, 4).map((item: any) => <span key={item}>{item}</span>)}</div>
          {skill.script_policy?.enabled && <div className="skill-script-badge"><TerminalSquare size={13} /><span>批准脚本：{skill.script_policy.script_id}</span></div>}
          <footer><small>{skill.builtin ? "内置" : skill.lifecycle === "candidate" ? "AI 从成功恢复中沉淀" : "自定义"} · {skill.owner || "operator"} · {skill.execution_model === "executable_builtin_skill" ? `内置可执行 Handler：${skill.runtime_handler}` : skill.execution_ready ? "可执行动作映射" : "指令型"}</small><div><button className="ghost tiny" onClick={() => exportSkill(skill)} title="导出标准 Agent Skill ZIP"><Download size={13} />导出</button><button className="ghost tiny" onClick={() => editSkill(skill)}>{skill.lifecycle === "candidate" ? "审核并发布" : "编辑"}</button><button className="ghost tiny" onClick={() => disableSkill(skill)}>{skill.builtin ? "禁用" : "删除"}</button></div></footer>
        </article>)}
      </div>
    </div>
    <div className="surface span-two">
      <SectionHead
        icon={FileClock}
        title="运维 Records 与 Skill 成效"
        meta={`保留 ${records.data?.retention_days || 365} 天 · 按成功恢复问题数排序，匹配按故障去重`}
        action={<div className="skill-head-actions"><button className="ghost" onClick={() => window.location.assign("/api/ops/records/export?limit=1000")}><Download size={14} />导出</button><button className="ghost" onClick={refreshRecords}><RefreshCcw size={14} />刷新</button></div>}
      />
      <div className="skill-stat-grid">
        {list(records.data?.skill_stats?.skills).map((item: any) => <article key={item.skill_id}>
          <strong>{item.skill_name}</strong><small>{item.skill_id}</small>
          <div className="skill-effectiveness-primary">
            <span>解决问题<b>{item.incidents_resolved || 0}</b></span>
            <span>处理问题<b>{item.incidents_handled || 0}</b></span>
            <span>恢复成功率<b>{item.success_rate == null ? "暂无" : `${Math.round(Number(item.success_rate) * 100)}%`}</b></span>
          </div>
          <div><span>去重匹配<b>{item.matched || 0}</b></span><span>选中<b>{item.selected || 0}</b></span><span>生成方案<b>{item.planned || 0}</b></span><span>审批<b>{item.approval_requested || 0}</b></span><span>真实动作<b>{item.executed || 0}</b></span><span>动作成功<b>{item.succeeded || 0}</b></span><span>失败阶段<b>{item.failed || 0}</b></span><span>回滚<b>{item.rolled_back || 0}</b></span></div>
        </article>)}
      </div>
      <div className="ops-record-list">
        {list(records.data?.items).map((record: any) => <article key={record.id}>
          <div><StatusPill status={record.status || "unknown"} /><strong>{record.target || record.id}</strong><small>{record.cluster || "-"} / {record.namespace || "-"} · {timeText(record.created_at)}</small></div>
          <p>{record.message || record.stage}{record.result?.verification?.residual_risk ? ` · 残余风险：${record.result.verification.residual_risk}` : ""}</p>
          <footer><span>{list(record.history).length} 轮 · {list(record.events).length} 条事件</span><button className="ghost tiny" onClick={() => deleteRecord(record)} disabled={["queued", "running", "awaiting_approval", "cancelling", "resume_pending"].includes(record.status)}><Trash2 size={12} />删除</button></footer>
        </article>)}
        {!list(records.data?.items).length && <Empty text="完成一次运维执行后，这里会保存脱敏证据、审批、Skill 与验证记录" />}
      </div>
    </div>
  </div>;
}

function flatten(value: any, prefix = "", rows: Array<{ label: string; value: string }> = []) {
  if (rows.length >= 9) return rows;
  if (Array.isArray(value)) { rows.push({ label: prefix || "items", value: value.length ? value.slice(0, 3).map((item) => Array.isArray(item) ? item.join(" → ") : typeof item === "object" ? Object.values(item).join(" · ") : String(item)).join("；") : "0 项" }); return rows; }
  if (value && typeof value === "object") { Object.entries(value).forEach(([key, item]) => flatten(item, prefix ? `${prefix}.${key}` : key, rows)); return rows; }
  if (prefix) rows.push({ label: prefix.split(".").pop()!.replaceAll("_", " "), value: value === undefined || value === null || value === "" ? "-" : String(value) });
  return rows;
}

export function AlgorithmsPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/algorithms/workbench"), []);
  const cases = list(state.data?.cases);
  const decisions = list(state.data?.recent_decisions);
  return <div className="unified-page"><div className="page-commandbar"><div className="quiet-note"><BrainCircuit size={15} />算法只在实际决策链路中展示，不做静态概念陈列</div><button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button></div>
    <div className="algorithm-overview">{list(state.data?.module_map).map((item: any, index: number) => <div className="algorithm-stage" key={item.algorithm}><span>0{index + 1}</span><div><strong>{item.module}</strong><small>{item.algorithm}</small></div><ChevronRight size={16} /><p>{item.effect}</p></div>)}</div>
    {cases.length ? <div className="algorithm-case-grid">{cases.map((item: any) => <div className="surface algorithm-case" key={item.id}><SectionHead icon={Workflow} title={item.title} meta={item.where_used} /><div className="decision-flow"><div><span>输入证据</span>{flatten(item.input).map((row) => <b key={row.label}>{row.label}<small>{row.value}</small></b>)}</div><i>→</i><div className="algorithm-core"><BrainCircuit size={22} /><strong>{item.algorithm}</strong></div><i>→</i><div><span>决策输出</span>{flatten(item.output).map((row) => <b key={row.label}>{row.label}<small>{row.value}</small></b>)}</div></div><p className="algorithm-effect">{item.action_effect}</p></div>)}</div> : <div className="surface"><Empty text="运行一次巡检、拓扑分析或变更门禁后，这里会出现真实算法样本" /></div>}
    <div className="surface"><SectionHead icon={FileClock} title="决策审计" meta={`${decisions.length} decisions`} />{decisions.length ? <div className="audit-grid">{decisions.slice(0, 12).map((item: any, index: number) => <div key={`${item.timestamp}-${index}`}><StatusPill status="recorded" text={item.algorithm} /><strong>{item.used_by}</strong><p>{item.action_effect}</p><small>{timeText(item.timestamp)}</small></div>)}</div> : <Empty text="暂无算法审计记录" />}</div>
  </div>;
}

export function SignalsPage() {
  const [cluster, setCluster] = useState("all");
  const [inventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [] })), []);
  const [metrics, refreshMetrics] = useAsync<any>(() => apiGet(`/api/prometheus/summary?cluster=${encodeURIComponent(cluster)}`), [cluster]);
  const [llm, refreshLlm] = useAsync<any>(() => apiGet("/api/llm-observability?limit=200"), []);
  const [integrations, refreshIntegrations] = useAsync<any>(() => apiGet("/api/integrations"), []);
  const [logs, setLogs] = useState<ApiState<any>>({ loading: false });
  const [traces, setTraces] = useState<ApiState<any>>({ loading: false });
  const [logQuery, setLogQuery] = useState('{namespace=~".+"}');
  const [traceService, setTraceService] = useState("");
  const [selectedCall, setSelectedCall] = useState<any>(null);
  const values = metrics.data?.values || {};
  const summary = llm.data?.summary || {};
  const analytics = llm.data?.analytics || {};
  const weekly = analytics.weekly_analysis || {};
  const langfuse = llm.data?.langfuse || {};
  const clusters = list(inventory.data?.clusters);
  const sources = list(integrations.data?.items).filter((item: any) => item.category === "observability");
  async function queryLogs() {
    setLogs({ loading: true });
    try { setLogs({ loading: false, data: await apiPost("/api/observability/logs", { query: logQuery, limit: 80 }) }); } catch (error: any) { setLogs({ loading: false, error: error.message }); }
  }
  async function queryTraces() {
    setTraces({ loading: true });
    try { setTraces({ loading: false, data: await apiGet(`/api/observability/traces?service=${encodeURIComponent(traceService)}&limit=30`) }); } catch (error: any) { setTraces({ loading: false, error: error.message }); }
  }
  const refresh = () => { refreshMetrics(); refreshLlm(); refreshIntegrations(); };
  const maxDailyTokens = Math.max(1, ...list(analytics.daily_usage).map((item: any) => Number(item.tokens || 0)));
  return <div className="unified-page"><div className="page-commandbar"><div className="scope-control"><span>Metrics scope</span><select value={cluster} onChange={(event) => setCluster(event.target.value)}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div><button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button></div>
    <section className="kpi-grid six"><Kpi label="CPU" value={`${Number(values.cpu_cores || 0).toFixed(2)} C`} detail={metrics.data?.source || "Prometheus"} /><Kpi label="内存" value={`${(Number(values.memory_bytes || 0) / 1024 / 1024 / 1024).toFixed(2)} GiB`} detail="working set" /><Kpi label="重启 / 1h" value={values.pod_restarts_1h || 0} /><Kpi label="LLM 调用" value={summary.total || 0} detail={`${summary.failures || 0} failed`} /><Kpi label="Token" value={compactNumber(summary.total_tokens)} detail={`$${Number(summary.estimated_cost_usd || 0).toFixed(4)} · ${compactNumber(summary.input_tokens)} in`} /><Kpi label="P95" value={`${summary.p95_latency_ms || 0} ms`} detail={`${summary.throughput_per_min || 0} req/min`} /></section>
    <section className="unified-grid signals-grid">
      <div className="surface span-two"><SectionHead icon={Activity} title="信号源" meta="Metrics · Logs · Traces · LLM" /><div className="integration-strip">{sources.map((item: any) => <div key={item.id}><span className="resource-icon"><Database size={15} /></span><div><strong>{item.name}</strong><small>{item.capability}</small></div><StatusPill status={item.status} /></div>)}</div></div>
      <div className="surface"><SectionHead icon={Gauge} title="模型调用分布" /><div className="mini-bars">{list(analytics.by_model).slice(0, 7).map((item: any) => <div key={item.name}><span>{item.name}</span><i><b style={{ width: `${Math.min(100, Number(item.calls || 0) * 10)}%` }} /></i><strong>{item.calls || 0}</strong></div>)}</div></div>
      <div className="surface span-three langfuse-lens"><SectionHead icon={GitBranch} title="Langfuse 黑盒拆解" meta={`${summary.langfuse_traces || 0} traces · ${langfuse.active ? "active" : langfuse.configured ? "configured" : "not configured"}`} /><div className="langfuse-chain">{["User", "Session", "Trace", "Generation", "Tool Call", "Score"].map((item) => <div key={item}><span>{item}</span><small>{item === "User" ? "Operator / Alert" : item === "Session" ? "Incident / Inspection" : item === "Trace" ? "SRE Workflow" : item === "Generation" ? "LLM Tokens" : item === "Tool Call" ? "MCP / Healing" : "Quality Eval"}</small></div>)}</div><div className="quality-strip">{list(analytics.quality_scores).length ? list(analytics.quality_scores).map((item: any) => <div key={item.name}><span>{item.name}</span><i><b style={{ width: `${Math.round(Number(item.avg || 0) * 100)}%` }} /></i><strong>{Math.round(Number(item.avg || 0) * 100)}</strong></div>) : <Empty text="运行 SRE Run 或巡检后展示 Langfuse 质量评分" />}</div></div>
      <div className="surface span-two"><SectionHead icon={LineChart} title="每日 Token 用量" meta={`${weekly.observed_days || 0} observed days`} /><div className="usage-chart">{list(analytics.daily_usage).length ? list(analytics.daily_usage).map((item: any) => <div key={item.date}><div><i style={{ height: `${Math.max(4, Number(item.tokens || 0) / maxDailyTokens * 100)}%` }} /></div><strong>{compactNumber(item.tokens)}</strong><span>{item.date?.slice(5)}</span></div>) : <Empty text="产生 LLM 调用后展示每日 Token 曲线" />}</div></div>
      <div className="surface"><SectionHead icon={BrainCircuit} title="一周用量预测" /><div className="weekly-forecast"><div><span>不开自动巡检</span><strong>{compactNumber(weekly.weekly_tokens_without_auto_inspection)}</strong></div><div><span>开启自动巡检</span><strong>{compactNumber(weekly.weekly_tokens_with_auto_inspection)}</strong></div><p>每 {weekly.inspection_interval_minutes || 30} 分钟巡检，预计增加 {compactNumber(weekly.auto_inspection_extra_tokens)} Token / 周</p></div></div>
      <div className="surface"><SectionHead icon={Workflow} title="LLM 数据流" /><div className="flow-list">{list(analytics.data_flows).map((item: any, index: number) => <div key={item.name}><span>{String(index + 1).padStart(2, "0")}</span><strong>{item.name}</strong><small>{item.count} calls</small></div>)}</div></div>
      <div className="surface span-two"><SectionHead icon={FileClock} title="调用审计" meta={`${summary.shown || 0} shown`} /><div className="call-table"><div><span>时间</span><span>来源 / 模型</span><span>延迟</span><span>状态</span><span /></div>{list(llm.data?.items).slice(0, 80).map((item: any) => <button key={item.id} onClick={() => setSelectedCall(item)}><span>{timeText(item.timestamp)}</span><span>{item.source}<small>{item.llm?.model_profile_id || item.llm?.model}{item.trace_id ? ` · ${String(item.trace_id).slice(0, 10)}` : ""}</small></span><span>{item.latency_ms || 0} ms</span><StatusPill status={item.status || "unknown"} /><Eye size={14} /></button>)}</div></div>
      {selectedCall && <div className="surface span-three"><SectionHead icon={Eye} title="调用详情" meta={selectedCall.id} action={<button className="ghost tiny" onClick={() => setSelectedCall(null)}>关闭</button>} /><div className="call-detail-grid"><div><span>输入范围</span><pre>{JSON.stringify(selectedCall.metadata || selectedCall.input, null, 2)}</pre></div><div><span>Agent 链</span><pre>{JSON.stringify(selectedCall.chain || [], null, 2)}</pre></div><div><span>输出摘要</span><pre>{JSON.stringify(selectedCall.output || {}, null, 2)}</pre></div></div></div>}
      <div className="surface span-three"><SectionHead icon={HardDrive} title="日志查询" meta="受限 LogQL，只读访问 Loki" /><div className="querybar"><input value={logQuery} onChange={(event) => setLogQuery(event.target.value)} /><button className="primary" onClick={queryLogs} disabled={logs.loading}>{logs.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}查询</button></div>{logs.error && <div className="inline-error">{logs.error}</div>}{list(logs.data?.streams).length ? <div className="log-view">{list(logs.data.streams).flatMap((stream: any) => list(stream.values).map((value: any[], index: number) => <div key={`${value[0]}-${index}`}><span>{value[0]}</span><code>{value[1]}</code></div>)).slice(0, 120)}</div> : <Empty text="配置 Loki 后可在这里关联检索日志；未连接时不会伪造数据" />}</div>
      <div className="surface span-three"><SectionHead icon={GitBranch} title="链路查询" meta="Tempo / TraceQL backend" /><div className="querybar"><input value={traceService} onChange={(event) => setTraceService(event.target.value)} placeholder="service.name，可留空查看最近链路" /><button className="primary" onClick={queryTraces} disabled={traces.loading}>{traces.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}查询</button></div>{traces.error && <div className="inline-error">{traces.error}</div>}{list(traces.data?.traces).length ? <div className="trace-list">{list(traces.data.traces).map((trace: any, index: number) => <div key={trace.traceID || index}><strong>{trace.rootServiceName || trace.serviceName || "trace"}</strong><code>{trace.traceID}</code><span>{trace.durationMs || trace.duration || "-"} ms</span></div>)}</div> : <Empty text="配置 Tempo 并上报 OTLP Trace 后可关联检索调用链" />}</div>
    </section>
  </div>;
}

export function IntegrationsPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/integrations"), []);
  const [cloud] = useAsync<any>(() => apiGet("/api/cloud/adapters"), []);
  const [testing, setTesting] = useState("");
  const [feedback, setFeedback] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);
  const [clusters, refreshClusters] = useAsync<any>(() => apiGet("/api/clusters"), []);
  const [rancherConnection, refreshRancherConnection] = useAsync<any>(() => apiGet("/api/rancher/connection"), []);
  const [connectionMode, setConnectionMode] = useState<"kubeconfig" | "rancher">("kubeconfig");
  const [kubeconfig, setKubeconfig] = useState("");
  const [dynamicToken, setDynamicToken] = useState("");
  const [clusterName, setClusterName] = useState("");
  const [contexts, setContexts] = useState<any[]>([]);
  const [context, setContext] = useState("");
  const [editingClusterId, setEditingClusterId] = useState("");
  const [savingCluster, setSavingCluster] = useState(false);
  const [rancherUrl, setRancherUrl] = useState("");
  const [rancherToken, setRancherToken] = useState("");
  const [rancherVerifySsl, setRancherVerifySsl] = useState(true);
  const [savingRancher, setSavingRancher] = useState(false);
  useEffect(() => {
    const active = rancherConnection.data;
    if (!active) return;
    setRancherUrl((current) => current || active.base_url || "");
    setRancherVerifySsl(active.verify_ssl !== false);
  }, [rancherConnection.data]);
  const groups = [
    ["infrastructure", "基础设施", Network],
    ["observability", "可观测", Activity],
    ["collaboration", "协作通道", MessageSquareText],
    ["ai", "AI 与知识", BrainCircuit],
  ] as const;
  async function testChannel(channel: string) {
    setTesting(channel); setFeedback(null);
    try {
      await apiPost("/api/integrations/notify/test", { channel });
      setFeedback({ tone: "ok", text: `${channel} 测试通知已送达` });
    } catch (error: any) {
      setFeedback({ tone: "warn", text: error.message });
    } finally { setTesting(""); }
  }
  async function inspectKubeconfig(content = kubeconfig) {
    if (!content.trim()) return;
    try {
      const result = await apiPost<any>("/api/clusters/contexts", { kubeconfig: content });
      const values = list(result.contexts);
      setContexts(values);
      setContext(values.find((item: any) => item.current)?.name || values[0]?.name || "");
      setFeedback({ tone: "ok", text: `识别到 ${values.length} 个 context` });
    } catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
  }
  async function importCluster() {
    setSavingCluster(true); setFeedback(null);
    try {
      const result = editingClusterId && !kubeconfig.trim()
        ? await apiPost<any>(`/api/clusters/${encodeURIComponent(editingClusterId)}/token`, { bearer_token: dynamicToken })
        : await apiPost<any>("/api/clusters", { kubeconfig, name: clusterName, context, cluster_id: editingClusterId, bearer_token: dynamicToken || undefined });
      setKubeconfig(""); setDynamicToken(""); setClusterName(""); setContexts([]); setContext(""); setEditingClusterId("");
      refreshClusters();
      setFeedback({ tone: "ok", text: `${result.name} 已连接并纳管` });
    } catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
    finally { setSavingCluster(false); }
  }
  async function removeCluster(id: string, name: string) {
    if (!window.confirm(`确定停止纳管集群“${name}”？本地加密凭据与缓存将一并删除。`)) return;
    try { await apiDelete(`/api/clusters/${encodeURIComponent(id)}`); refreshClusters(); setFeedback({ tone: "ok", text: `${name} 已删除` }); }
    catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
  }
  async function connectRancher() {
    setSavingRancher(true); setFeedback(null);
    try {
      const result = await apiPost<any>("/api/rancher/connection", {
        rancher_url: rancherUrl,
        bearer_token: rancherToken,
        verify_ssl: rancherVerifySsl,
      });
      setRancherToken("");
      refreshRancherConnection(); refresh();
      setFeedback({ tone: "ok", text: `Rancher 已验证并纳管 ${result.cluster_count || 0} 个集群` });
    } catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
    finally { setSavingRancher(false); }
  }
  async function restoreEnvironmentRancher() {
    if (!window.confirm("删除页面保存的 Rancher 覆盖配置，并恢复使用当前 ConfigMap/Secret？")) return;
    try {
      const result = await apiDelete<any>("/api/rancher/connection");
      const active = result.active_connection || {};
      setRancherUrl(active.base_url || ""); setRancherToken("");
      setRancherVerifySsl(active.verify_ssl !== false);
      refreshRancherConnection(); refresh();
      setFeedback({ tone: "ok", text: result.message || "已恢复 ConfigMap/Secret 配置" });
    } catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
  }
  const cloudAdapters = list(cloud.data?.available || cloud.data?.adapters);
  return <div className="unified-page"><div className="page-commandbar"><div className="quiet-note"><ShieldCheck size={15} />凭据在服务端加密存储，前端不回传敏感内容</div>{feedback && <span className={`channel-feedback ${feedback.tone}`}>{feedback.text}</span>}<button className="ghost" onClick={refresh}><RefreshCcw size={15} />检测</button></div>
    <section className="surface">
      <SectionHead
        icon={ServerCog}
        title="Kubernetes 集群纳管"
        meta="选择 Rancher 或加密 kubeconfig"
        action={<div className="segmented"><button className={connectionMode === "kubeconfig" ? "active" : ""} onClick={() => setConnectionMode("kubeconfig")}><Upload size={13} />添加 kubeconfig</button><button className={connectionMode === "rancher" ? "active" : ""} onClick={() => setConnectionMode("rancher")}><Network size={13} />连接 Rancher</button></div>}
      />
      {connectionMode === "kubeconfig" ? <div className="cluster-connect-grid">
        <div className="cluster-connect-form">
          {editingClusterId && <div className="quiet-note"><RefreshCcw size={14} />可只填写新 Token 进行原子刷新，或上传完整 kubeconfig；验证成功后才替换原密文。<button className="ghost tiny" onClick={() => { setEditingClusterId(""); setClusterName(""); setDynamicToken(""); }}>取消</button></div>}
          <label>集群名称<input value={clusterName} onChange={(event) => setClusterName(event.target.value)} placeholder="例如：生产集群" /></label>
          <label>上传 kubeconfig<input type="file" accept=".yaml,.yml,.conf,.config" onChange={async (event) => { const file = event.target.files?.[0]; if (!file) return; const text = await file.text(); setKubeconfig(text); await inspectKubeconfig(text); }} /></label>
          <label>或粘贴 kubeconfig<textarea value={kubeconfig} onChange={(event) => setKubeconfig(event.target.value)} rows={8} placeholder="apiVersion: v1…" /></label>
          <label>动态 Bearer Token（可选）<input type="password" autoComplete="off" value={dynamicToken} onChange={(event) => setDynamicToken(event.target.value)} placeholder="含 exec/auth-provider 的 kubeconfig 必须单独填写当前 Token" /><small>Token 仅在服务端加密保存；平台不会执行 kubeconfig 中的外部认证命令。</small></label>
          <div className="querybar"><button className="ghost" onClick={() => inspectKubeconfig()} disabled={!kubeconfig.trim()}>读取 Context</button>{contexts.length > 0 && <select value={context} onChange={(event) => setContext(event.target.value)}>{contexts.map((item: any) => <option key={item.name} value={item.name}>{item.name}{item.current ? "（当前）" : ""}</option>)}</select>}<button className="primary" onClick={importCluster} disabled={savingCluster || (editingClusterId ? (!dynamicToken.trim() && (!kubeconfig.trim() || !context)) : (!kubeconfig.trim() || !context))}>{savingCluster ? <Loader2 className="spin" size={15} /> : <Upload size={15} />}{editingClusterId && !kubeconfig.trim() ? "验证并刷新 Token" : editingClusterId ? "验证并更新" : "验证并纳管"}</button></div>
        </div>
        <div className="managed-cluster-list">{list(clusters.data?.items).length ? list(clusters.data.items).map((item: any) => <div key={item.id}><span className="resource-icon"><ServerCog size={16} /></span><div><strong>{item.name}</strong><p>{item.context_name} · Kubernetes {item.version || "待验证"}</p><small>{item.node_count || 0} nodes · 最近验证 {timeText(item.last_checked_at)}</small>{item.last_error && <small className="inline-error">{item.last_error}</small>}</div><StatusPill status={item.status} /><button className="channel-test" onClick={() => { setEditingClusterId(item.id); setClusterName(item.name); setKubeconfig(""); setDynamicToken(""); setContexts([]); setContext(""); }} title="更新 kubeconfig 或动态 Token"><Upload size={13} /></button><button className="channel-test" onClick={async () => { await apiPost(`/api/clusters/${encodeURIComponent(item.id)}/verify`, {}); refreshClusters(); }} title="重新验证"><RefreshCcw size={13} /></button><button className="channel-test danger" onClick={() => removeCluster(item.id, item.name)} title="删除集群"><Trash2 size={13} /></button></div>) : <Empty text="尚未通过 kubeconfig 纳管集群；Rancher 接入仍可并行使用" />}</div>
      </div> : <div className="cluster-connect-grid">
        <div className="cluster-connect-form">
          <div className="quiet-note"><ShieldCheck size={14} />先验证新连接，再原子替换运行时配置；验证失败不会影响当前 ConfigMap/Secret。</div>
          <label>Rancher URL<input value={rancherUrl} onChange={(event) => setRancherUrl(event.target.value)} placeholder="https://rancher.example.com" /></label>
          <label>Rancher Bearer Token<input type="password" autoComplete="new-password" value={rancherToken} onChange={(event) => setRancherToken(event.target.value)} placeholder="输入新的 Bearer Token，服务端不会回显" /></label>
          <label>TLS 校验<select value={rancherVerifySsl ? "true" : "false"} onChange={(event) => setRancherVerifySsl(event.target.value === "true")}><option value="true">验证 Rancher 证书</option><option value="false">不验证证书（仅限企业自签名环境）</option></select></label>
          <div className="querybar"><button className="primary" onClick={connectRancher} disabled={savingRancher || !rancherUrl.trim() || !rancherToken.trim()}>{savingRancher ? <Loader2 className="spin" size={15} /> : <Network size={15} />}{rancherConnection.data?.configured ? "验证并更新 Rancher" : "验证并纳管 Rancher"}</button></div>
        </div>
        <div className="rancher-connection-card">
          <div><span className="resource-icon"><Network size={17} /></span><div><strong>当前 Rancher 连接</strong><p>{rancherConnection.data?.base_url || "尚未配置"}</p></div><StatusPill status={rancherConnection.data?.configured ? "connected" : "not_configured"} /></div>
          {rancherConnection.data?.configured && <><dl><div><dt>配置来源</dt><dd>{rancherConnection.data.source === "environment" ? "ConfigMap / Secret" : "页面加密配置"}</dd></div><div><dt>TLS 校验</dt><dd>{rancherConnection.data.verify_ssl === false ? "关闭" : "开启"}</dd></div><div><dt>已发现集群</dt><dd>{rancherConnection.data.cluster_count ?? "连接后读取"}</dd></div><div><dt>最近验证</dt><dd>{timeText(rancherConnection.data.last_checked_at)}</dd></div></dl>{rancherConnection.data.source === "environment" ? <div className="quiet-note"><ShieldCheck size={14} />当前配置来自 Deployment 注入的 ConfigMap/Secret。直接更换镜像不会覆盖 URL 或 Token。</div> : <div className="quiet-note"><ShieldCheck size={14} />URL、Token 与 TLS 策略已使用 Fernet 加密并保存在持久化运行目录。</div>}{rancherConnection.data.editable && <button className="ghost danger" onClick={restoreEnvironmentRancher}><Trash2 size={13} />删除页面覆盖并恢复 ConfigMap</button>}</>}
          {!rancherConnection.data?.configured && <Empty text="填写 Rancher URL 和 Token，验证通过后开始发现集群" />}
          {rancherConnection.data?.runtime_error && <div className="inline-error">{rancherConnection.data.runtime_error}</div>}
        </div>
      </div>}
    </section>
    <div className="integration-groups">{groups.map(([id, title, Icon]) => <section className="surface" key={id}><SectionHead icon={Icon} title={title} /><div className="integration-cards">{list(state.data?.items).filter((item: any) => item.category === id).map((item: any) => <div key={item.id}><span className="resource-icon"><CloudCog size={16} /></span><div><strong>{item.name}</strong><p>{item.capability}</p><small>{item.configuration_hint}</small></div><div className="integration-actions"><StatusPill status={item.status} />{id === "collaboration" && item.status === "configured" && <button className="channel-test" onClick={() => testChannel(item.id)} disabled={testing === item.id} title={`发送 ${item.name} 测试通知`}>{testing === item.id ? <Loader2 className="spin" size={13} /> : <Send size={13} />}</button>}</div></div>)}</div></section>)}</div>
    <section className="surface"><SectionHead icon={GitBranch} title="云资源适配器" meta="Rancher · Generic CSI Storage · Virtualization Platform · Public Cloud" /><div className="capability-grid">{cloudAdapters.length ? cloudAdapters.map((item: any) => <div className="capability-card" key={item.id || item.provider}><span>{item.enabled ? "enabled" : "available"}</span><strong>{item.display_name || item.name || item.provider}</strong><p>{list(item.capabilities).join(" · ") || item.description}</p><small>{item.auth_mode} · {item.inventory_scope}</small></div>) : <Empty text="通过 CLOUD_ADAPTERS_JSON 接入阿里云、通用 CSI 存储、虚拟化平台或其他云适配器" />}</div></section>
    <section className="surface"><SectionHead icon={CheckCircle2} title="能力覆盖" meta="持续演进的全栈智能可靠性能力" /><div className="coverage-table"><div><strong>能力</strong><strong>本系统</strong><strong>说明</strong></div>{list(state.data?.coverage).map((item: any) => <div key={item.capability}><span>{item.capability}</span><StatusPill status={item.status} /><small>{item.detail}</small></div>)}</div></section>
  </div>;
}

type AssistantMessage = {
  role: "user" | "assistant";
  text: string;
  at: number;
  page?: string;
  domain?: "app" | "ops";
  sourceCount?: number;
};

const ASSISTANT_OPS_PATTERN = /pod|k8s|kubernetes|故障|排障|集群|网络|存储|告警|修复|巡检|拓扑|prometheus|cmdb|rancher|namespace|workload|deployment|statefulset/i;

function assistantSuggestions(page: string) {
  if (page.includes("SRE")) return ["这次诊断如何安全执行？", "帮我把回答整理成操作步骤", "如果修复失败下一步做什么？"];
  if (page.includes("巡检")) return ["如何只看新增风险？", "生产模式会检查哪些隐患？", "怎样开启人工确认修复？"];
  if (page.includes("拓扑")) return ["怎么读懂影响范围？", "解释关键路径和放大系数", "Kafka/ELK 数据流在哪里看？"];
  if (page.includes("模型")) return ["怎么接入 OAuth 模型？", "怎么比较模型运维能力？", "如何做影子测评？"];
  if (page.includes("知识")) return ["Runbook 应该怎么沉淀？", "产品使用知识和运维知识区别？", "如何让助手使用这些知识？"];
  return ["当前页面怎么用？", "给我推荐下一步", "遇到异常先看哪里？"];
}

function assistantDomain(question: string, page: string): "app" | "ops" {
  return ASSISTANT_OPS_PATTERN.test(`${page}\n${question}`) ? "ops" : "app";
}

export function AssistantDock({ page }: { page: string }) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<AssistantMessage[]>(() => {
    try { return JSON.parse(localStorage.getItem("cisre-unified-assistant") || localStorage.getItem("flawless-unified-assistant") || "[]"); } catch { return []; }
  });
  const suggestions = useMemo(() => assistantSuggestions(page), [page]);
  const scroller = useRef<HTMLDivElement | null>(null);
  useEffect(() => { localStorage.setItem("cisre-unified-assistant", JSON.stringify(messages.slice(-40))); requestAnimationFrame(() => { if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight; }); }, [messages]);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setOpen(true); }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
  async function ask(override?: string) {
    const question = (override || input).trim();
    if (!question || loading) return;
    setInput(""); setOpen(true); setLoading(true);
    const domain = assistantDomain(question, page);
    setMessages((current) => [...current, { role: "user", text: question, at: Date.now(), page, domain }]);
    try {
      const data = await apiPost<any>("/api/knowledge/ask", {
        question: `当前页面：${page}\n用户问题：${question}`,
        domain,
        include_principle: /原理|机制|为什么|principle/i.test(question),
      });
      setMessages((current) => [...current, { role: "assistant", text: data.answer || "没有检索到答案。", at: Date.now(), page, domain, sourceCount: list(data.sources).length }]);
    } catch (error: any) {
      setMessages((current) => [...current, { role: "assistant", text: `助手暂时不可用：${error.message}`, at: Date.now(), page, domain }]);
    } finally { setLoading(false); }
  }
  return <>
    <button className="assistant-launcher" onClick={() => setOpen(true)} title="打开 CISRE 助手"><Bot size={19} /><span>助手</span><kbd>⌘K</kbd></button>
    <aside className={`assistant-drawer ${open ? "open" : ""}`} aria-hidden={!open}>
      <header>
        <div><span className="assistant-mark"><Bot size={18} /></span><div><strong>CISRE 助手</strong><small>当前页面：{page}</small></div></div>
        <div className="assistant-header-actions">
          <button onClick={() => setMessages([])} title="清空对话"><RefreshCcw size={15} /></button>
          <button onClick={() => setOpen(false)} title="关闭"><X size={18} /></button>
        </div>
      </header>
      <div className="assistant-context">
        <span><BrainCircuit size={14} />知识库路由</span>
        <strong>{ASSISTANT_OPS_PATTERN.test(page) ? "运维 Runbook" : "产品使用 + 运维 RAG"}</strong>
      </div>
      <div className="assistant-suggestions">
        {suggestions.map((item) => <button key={item} onClick={() => ask(item)} disabled={loading}>{item}</button>)}
      </div>
      <div className="assistant-messages" ref={scroller}>
        {messages.length ? messages.map((item, index) => <div className={`assistant-message ${item.role}`} key={`${item.at}-${index}`}>
          <span>{item.role === "assistant" ? "CISRE" : "你"}{item.page ? ` · ${item.page}` : ""}{item.domain ? ` · ${item.domain === "ops" ? "运维知识" : "产品知识"}` : ""}</span>
          <p>{item.text}</p>
          {item.role === "assistant" && typeof item.sourceCount === "number" && <small>{item.sourceCount} 个知识片段参与回答</small>}
        </div>) : <div className="assistant-welcome"><BrainCircuit size={24} /><strong>需要我帮你怎么用这套系统？</strong><p>我会结合当前页面、产品知识库和运维 Runbook 给出下一步。</p></div>}
        {loading && <div className="assistant-thinking"><i /><i /><i />正在检索知识库</div>}
      </div>
      <footer>
        <textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(); } }} placeholder="问产品用法、运维 Runbook 或当前页面下一步" />
        <button onClick={() => ask()} disabled={loading || !input.trim()}><Send size={16} /></button>
      </footer>
    </aside>
    {open && <button className="assistant-backdrop" onClick={() => setOpen(false)} aria-label="关闭助手遮罩" />}
  </>;
}
