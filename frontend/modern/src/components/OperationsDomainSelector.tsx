import {
  Boxes,
  CloudCog,
  Database,
  HardDrive,
  Layers3,
  ServerCog,
} from "lucide-react";
import { useAsync } from "../hooks/useAsync";
import { apiGet, asList } from "../lib/api";

export type OperationsDomainId =
  | ""
  | "kubernetes"
  | "database"
  | "virtual_machine"
  | "storage"
  | "middleware"
  | "cloud_service";

type Domain = {
  id: Exclude<OperationsDomainId, "">;
  name: string;
  short_name: string;
  description: string;
  target_label: string;
  status: string;
  resource_count?: number | null;
};

const FALLBACK_DOMAINS: Domain[] = [
  { id: "kubernetes", name: "Kubernetes", short_name: "K8s", description: "集群、Workload、Pod、网络、配置和 PV/PVC 风险。", target_label: "集群 / Namespace / Workload", status: "ready" },
  { id: "database", name: "数据库", short_name: "DB", description: "实例、连接、SQL、锁、复制、容量和备份风险。", target_label: "数据库实例 / 集群", status: "adapter_ready" },
  { id: "virtual_machine", name: "虚拟机 / 主机", short_name: "VM", description: "系统、服务、磁盘、网络、进程和安全基线风险。", target_label: "主机 / 虚拟机", status: "adapter_ready" },
  { id: "storage", name: "企业存储", short_name: "Storage", description: "存储池、卷、路径、ACL、快照、容量和时延风险。", target_label: "存储系统 / 存储池 / 卷", status: "adapter_ready" },
  { id: "middleware", name: "中间件", short_name: "Middleware", description: "消息、缓存、注册配置和日志中间件风险。", target_label: "中间件集群 / 实例", status: "adapter_ready" },
  { id: "cloud_service", name: "云资源", short_name: "Cloud", description: "计算、网络、负载均衡、配额和托管服务风险。", target_label: "账号 / Region / 云资源", status: "adapter_ready" },
];

function DomainIcon({ id, size = 20 }: { id: string; size?: number }) {
  if (id === "kubernetes") return <Boxes size={size} />;
  if (id === "database") return <Database size={size} />;
  if (id === "virtual_machine") return <ServerCog size={size} />;
  if (id === "storage") return <HardDrive size={size} />;
  if (id === "middleware") return <Layers3 size={size} />;
  return <CloudCog size={size} />;
}

export function OperationsDomainSelector({
  value,
  onChange,
  compact = false,
  mode = "operate",
}: {
  value: OperationsDomainId;
  onChange: (value: OperationsDomainId) => void;
  compact?: boolean;
  mode?: "operate" | "inspect";
}) {
  const [catalog] = useAsync<any>(() => apiGet("/api/operations/domains").catch(() => ({ domains: FALLBACK_DOMAINS })), []);
  const domains = (asList(catalog.data?.domains).length ? asList(catalog.data?.domains) : FALLBACK_DOMAINS) as Domain[];
  const selected = domains.find((item) => item.id === value);

  if (compact && selected) {
    return <div className="domain-selector compact">
      <span className="domain-selector-icon"><DomainIcon id={selected.id} size={17} /></span>
      <div><small>当前运维域</small><strong>{selected.name}</strong></div>
      <select value={value} onChange={(event) => onChange(event.target.value as OperationsDomainId)} aria-label="切换运维对象域">
        {domains.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      <p>{selected.target_label} · {mode === "inspect" ? "巡检、根因、Skill、审批与验证" : "风险诊断、方案、审批与验证"}</p>
    </div>;
  }

  return <section className="domain-gate">
    <div className="domain-gate-copy">
      <span>UNIFIED SRE ENTRY</span>
      <h2>先选择要解决哪一类基础设施风险</h2>
      <p>每个运维域拥有独立证据和 Skill，但共用同一套人工审批、执行审计、写后回读与恢复验证。</p>
      <label>运维对象域
        <select value={value} onChange={(event) => onChange(event.target.value as OperationsDomainId)}>
          <option value="">请选择</option>
          {domains.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.target_label}</option>)}
        </select>
      </label>
    </div>
    <div className="domain-gate-grid">
      {domains.map((item) => <button key={item.id} onClick={() => onChange(item.id)}>
        <span className="domain-card-icon"><DomainIcon id={item.id} /></span>
        <div><small>{item.short_name}</small><strong>{item.name}</strong></div>
        <p>{item.description}</p>
        <footer><span>{item.resource_count == null ? "内置完整能力" : item.resource_count ? `${item.resource_count} resources` : "Adapter 接口已就绪"}</span><b>进入 →</b></footer>
      </button>)}
    </div>
  </section>;
}
