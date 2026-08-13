# SRE execution harness and eBPF coverage

## Why the model is not the executor

The language model ranks root-cause hypotheses and chooses the most relevant
Skill. A persistent, model-independent harness owns the production lifecycle:

1. collect direct evidence;
2. diagnose and bind one primary Skill;
3. display an exact change, risk and rollback for human approval;
4. execute through a target-aware Kubernetes mutation lease;
5. verify the replacement Pod and Workload until recovery is proven;
6. retain the failed trajectory and choose a different strategy when recovery
   is not proven.

Every state transition updates the job checkpoint. Change/tool receipts and
attempt fingerprints are retained with the incident. Repeating the same
non-progress trajectory triggers the stuck detector. A job only becomes
`recovered` when the deterministic verifier returns `recovered=true`; an LLM
answer cannot close a job by itself.

This design adopts the useful production patterns described by the
[Microsoft Agent Framework harness](https://learn.microsoft.com/en-us/agent-framework/agents/harness),
[LangGraph persistence and checkpoints](https://docs.langchain.com/oss/python/langgraph/persistence),
and the [OpenHands agent SDK state model](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/state.py),
without coupling Kubernetes execution to any one agent framework.

DeepSeek's official documentation currently lists integrations with third-party
agents such as OpenCode, but does not publish an official DeepSeek-owned harness
contract. Flawless therefore keeps the harness provider-neutral and uses the
configured DeepSeek-compatible model only through the planner/router boundary.

## Concurrency model

- API work queues at the general request gate instead of returning a short
  timeout as an operations-overload error.
- Job create/read/approve/cancel endpoints bypass the general request gate so
  dashboards and slow inventory calls cannot starve operator control.
- Diagnosis and human approval do not consume a Kubernetes mutation slot.
- Only a real write holds a mutation lease.
- Writes to the same cluster/namespace/resource are single-flight; unrelated
  targets may execute in parallel up to `OPS_MAX_CONCURRENT_JOBS`.

## Proving eBPF node coverage

`manifests/ebpf-beyla.yaml` schedules one privileged Beyla collector on every
compatible Linux node, including tainted nodes. `manifests/grafana-observability.yaml`
schedules Alloy with the same coverage, adds `cluster` and `node` Loki labels,
and forwards Beyla `network_flow` records to Loki.

The topology API reports two separate facts:

- collector coverage: expected Linux nodes versus nodes with a Ready Beyla Pod;
- flow coverage: nodes that emitted real `network_flow` data in the selected
  time window.

A quiet node can have a healthy collector and zero flows. The UI does not label
this as a collector failure. Conversely, a missing DaemonSet Pod is listed by
cluster and node and prevents the system from claiming complete coverage.

Loki remains the scalable primary path. If it is reachable but returns no
parseable flow, the API performs a bounded, read-only fallback through each
managed Rancher or kubeconfig connection and reads the current Beyla Pod log
on every collector node. The source diagnostic then distinguishes an Alloy/Loki
delivery gap from missing collectors and from a genuinely quiet time window.

For multiple clusters, deploy Beyla and Alloy in every target cluster and set
Alloy's `LOKI_PUSH_URL` to a central Loki endpoint reachable from those
clusters. Keep `LOCAL_CLUSTER_NAME` unique per cluster so node/flow identities
do not collide.
