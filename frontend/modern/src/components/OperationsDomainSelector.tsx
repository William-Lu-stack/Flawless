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
    <nav className="domain-gate-grid" aria-label="基础设施运维入口">
      {domains.map((item) => <button key={item.id} onClick={() => onChange(item.id)} aria-label={`进入${item.name}`}>
        <span className="domain-card-icon"><DomainIcon id={item.id} size={27} /></span>
        <div><strong>{item.name}</strong><small>{item.short_name}</small></div>
        <span className="domain-card-enter" aria-hidden="true">→</span>
      </button>)}
    </nav>
  </section>;
}
