import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from agents.runtime_resilience import AsyncBulkhead, BulkheadRejected
from backend.app import application as server
from backend.app.services.ops_harness import checkpoint_event, new_ops_harness, record_attempt


class OpsHarnessAndCoverageTests(unittest.TestCase):
    def test_every_common_kubernetes_skill_materializes_into_the_unified_execution_contract(self):
        skill_runbooks = [
            ("skill-storage-pvc-pv", "storage_mount"),
            ("skill-memory-oom-recovery", "oom"),
            ("skill-probe-slow-start-recovery", "probe"),
            ("skill-image-pull-runtime-recovery", "image_auth"),
            ("skill-config-reference-recovery", "config_missing"),
            ("skill-service-endpoint-flow", "service_selector"),
            ("skill-rollout-regression-recovery", "rollout_regression"),
            ("skill-node-pressure-containment", "node_pressure"),
            ("skill-pdb-rollout-deadlock-recovery", "pdb_deadlock"),
            ("skill-cpu-capacity-recovery", "cpu_saturation"),
            ("skill-dns-cni-recovery", "dns_cni"),
        ]
        for skill_id, runbook_id in skill_runbooks:
            with self.subTest(skill_id=skill_id, runbook_id=runbook_id):
                engine_plan = {
                    "runbook_id": runbook_id,
                    "reason": f"validated {runbook_id} evidence",
                    "steps": [{"id": "confirm", "title": "confirm evidence"}],
                    "changes": [{
                        "type": "restart",
                        "namespace": "apps",
                        "workload_type": "Deployment",
                        "workload_name": "api",
                        "reason": "bounded test change",
                        "risk": "medium",
                        "auto_allowed": False,
                    }],
                    "success_criteria": ["pod_ready", "restart_count_stable"],
                    "hypotheses": [{"id": runbook_id, "confidence": 0.99}],
                }
                plan = {
                    "namespace": "apps",
                    "target": "Deployment/api",
                    "summary": f"{runbook_id} incident",
                    "evidence": {
                        "pod": {
                            "name": "api-1",
                            "workload_kind": "Deployment",
                            "workload_name": "api",
                        },
                    },
                }
                with patch.object(server, "build_remediation_plan", return_value=engine_plan):
                    materialized = server._materialize_executable_skill(
                        plan,
                        {
                            "question": plan["summary"],
                            "evidence": plan["evidence"],
                            "diagnosis": {"root_cause": runbook_id},
                        },
                        skill_id,
                    )
                self.assertIsNotNone(materialized)
                self.assertEqual(materialized["selected_skill_id"], skill_id)
                self.assertTrue(materialized["skill_runtime"]["continuation_capable"])
                self.assertEqual(materialized["changes"][0]["skill_id"], skill_id)
                self.assertEqual(
                    materialized["changes"][0]["selection_source"],
                    "executable_skill_handler",
                )
                self.assertTrue(materialized["success_criteria"])

    def test_request_bulkhead_queue_waits_instead_of_rejecting(self):
        async def scenario():
            gate = AsyncBulkhead(1, acquire_timeout=0.01)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def first():
                async with gate.slot():
                    entered.set()
                    await release.wait()

            async def second():
                await entered.wait()
                async with gate.slot(queue=True):
                    return "admitted"

            first_task = asyncio.create_task(first())
            second_task = asyncio.create_task(second())
            await entered.wait()
            await asyncio.sleep(0.03)
            self.assertEqual(gate.snapshot()["queued"], 1)
            release.set()
            self.assertEqual(await second_task, "admitted")
            await first_task
            self.assertEqual(gate.snapshot()["rejected"], 0)
            self.assertEqual(gate.snapshot()["admitted"], 2)

        asyncio.run(scenario())

    def test_bulkhead_fail_fast_mode_remains_available_for_best_effort_io(self):
        async def scenario():
            gate = AsyncBulkhead(1, acquire_timeout=0.01)
            async with gate.slot():
                with self.assertRaises(BulkheadRejected):
                    async with gate.slot():
                        pass
            self.assertEqual(gate.snapshot()["rejected"], 1)

        asyncio.run(scenario())

    def test_ops_control_routes_are_reserved_from_general_request_admission(self):
        paths = [
            "/api/ops/jobs",
            "/api/ops/jobs/ops-123",
            "/api/ops/jobs/ops-123/approve-step",
            "/api/ops/jobs/ops-123/cancel",
            "/api/health",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(server._priority_control_route(path))

    def test_expensive_inventory_route_still_uses_general_request_admission(self):
        self.assertFalse(server._priority_control_route("/api/cmdb/topology"))

    def test_harness_detects_repeated_non_progress_and_only_closes_on_verified_recovery(self):
        plan = {
            "title": "repair workload",
            "target": "Deployment/example",
            "selected_skill_id": "skill-volume-permission-recovery",
            "changes": [{"type": "patch_workload_runtime_security", "patch": {"spec": {}}}],
        }
        state = new_ops_harness(plan)
        unresolved = {
            "status": "unresolved",
            "verification": {"recovered": False, "message": "Pod is not Ready"},
        }
        state = record_attempt(state, plan, unresolved)
        state = record_attempt(state, plan, unresolved)
        state = record_attempt(state, plan, unresolved)
        self.assertTrue(state["stuck_detected"])
        self.assertFalse(state["completion"]["recovered"])

        recovered = {
            "status": "completed",
            "verification": {"recovered": True, "message": "new Pod Ready and stable"},
        }
        state = record_attempt(state, plan, recovered)
        state = checkpoint_event(state, "recovered", "recovery contract passed")
        self.assertEqual(state["completion"]["status"], "recovered")
        self.assertTrue(all(item["status"] == "completed" for item in state["todos"]))

    def test_beyla_coverage_compares_ready_collectors_with_every_linux_node(self):
        payload = {
            "inventory": [{
                "cluster": {"id": "c-1", "name": "prod"},
                "nodes": [
                    {"name": "node-a", "os": "linux"},
                    {"name": "node-b", "os": "linux"},
                    {"name": "node-win", "os": "windows"},
                ],
                "pods": [{
                    "namespace": "flawless-ebpf",
                    "name": "flawless-beyla-a",
                    "workload_name": "flawless-beyla",
                    "labels": {"app": "flawless-beyla"},
                    "phase": "Running",
                    "ready": True,
                    "node": "node-a",
                }],
            }],
        }

        async def scenario():
            request = server.ExternalTrafficFlowRequest(cluster="prod", namespace="all")
            with patch.object(server, "rancher_inventory", new=AsyncMock(return_value=payload)):
                coverage = await server._beyla_node_coverage(
                    request,
                    [{"cluster": "prod", "node": "node-a"}],
                )
            self.assertEqual(coverage["expected_linux_nodes"], 2)
            self.assertEqual(coverage["ready_collector_nodes"], 1)
            self.assertEqual(coverage["flow_observed_nodes"], 1)
            self.assertEqual(coverage["status"], "partial")
            self.assertEqual(
                coverage["missing_collector_nodes"],
                [{"cluster": "prod", "node": "node-b"}],
            )

        asyncio.run(scenario())

    def test_beyla_direct_fallback_reads_real_flow_from_each_managed_collector(self):
        payload = {
            "inventory": [{
                "cluster": {"id": "c-1", "name": "prod", "source": "rancher"},
                "nodes": [{"name": "node-a", "os": "linux"}],
                "pods": [{
                    "namespace": "flawless-ebpf",
                    "name": "flawless-beyla-a",
                    "workload_name": "flawless-beyla",
                    "labels": {"app": "flawless-beyla"},
                    "phase": "Running",
                    "ready": True,
                    "node": "node-a",
                    "containers": [{"name": "beyla"}],
                }],
            }],
        }
        log_line = (
            "network_flow: k8s.cluster.name=prod k8s.src.namespace=apps "
            "k8s.src.name=api-1 k8s.src.type=Pod k8s.src.owner.name=api "
            "k8s.src.owner.type=Deployment k8s.dst.namespace=apps "
            "k8s.dst.name=db k8s.dst.type=Service dst.port=5432 transport=TCP"
        )

        async def scenario():
            request = server.ExternalTrafficFlowRequest(cluster="prod", namespace="all")
            with (
                patch.object(server, "rancher_inventory", new=AsyncMock(return_value=payload)),
                patch.object(
                    server,
                    "_rancher_k8s_get",
                    new=AsyncMock(return_value=log_line),
                ) as fetch,
            ):
                flows, status = await server._fetch_beyla_direct_pod_log_flows(request)
            self.assertEqual(len(flows), 1)
            self.assertEqual(status["status"], "connected")
            self.assertEqual(status["collectors"], 1)
            self.assertEqual(status["collectors_with_flows"], 1)
            self.assertEqual(
                status["observed_flow_nodes"],
                [{"cluster": "prod", "node": "node-a"}],
            )
            self.assertEqual(status["node_coverage"]["status"], "complete")
            self.assertEqual(fetch.await_count, 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
