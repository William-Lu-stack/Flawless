from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.api import reliability as reliability_api
from backend.app.services.progressive_delivery import (
    build_analysis_template,
    build_rollout,
    derive_canary_policy,
    progressive_status,
)
from backend.app.services.release_execution import submit_release_job
from backend.app.services.reliability_store import ReliabilityStore


def release_payload(**values):
    return {
        "id": "rel-orders-v2",
        "service": "orders",
        "cluster": "cluster-a",
        "namespace": "prod",
        "workload_kind": "Deployment",
        "workload_name": "orders",
        "container_name": "app",
        "image": "registry.example.com/orders:v2.0.0",
        "release_mode": "existing",
        "change_channel": "standard",
        "approved_by": "operator",
        "gate": {
            "verdict": "pass",
            "selected_strategy": {
                "first_ratio": 0.01,
                "step_ratio": 0.02,
                "max_ratio": 0.10,
                "observation_window_min": 20,
            },
        },
        "analysis_interval_seconds": 30,
        "analysis_count": 2,
        "max_error_rate": 0.01,
        "max_p99_latency_ms": 1000,
        **values,
    }


def live_deployment(replicas=20):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "orders", "namespace": "prod"},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": "orders"}},
            "template": {
                "metadata": {"labels": {"app": "orders"}},
                "spec": {"containers": [{"name": "app", "image": "registry.example.com/orders:v1.0.0"}]},
            },
        },
    }


class ProgressivePolicyTests(unittest.TestCase):
    def test_replica_floor_blocks_hidden_blast_radius_increase(self):
        policy = derive_canary_policy(release_payload(), 3)
        self.assertTrue(policy["unsupported"])
        self.assertEqual(policy["replica_weight_floor"], 34)
        self.assertEqual(policy["required_replicas_for_envelope"], 10)
        self.assertIn("超过算法批准", policy["blocked_reason"])

    def test_policy_uses_only_weights_inside_approved_envelope(self):
        policy = derive_canary_policy(release_payload(), 20)
        self.assertFalse(policy["unsupported"])
        self.assertEqual(policy["weights"], [5, 7, 9, 10])
        self.assertEqual(max(policy["weights"]), 10)
        self.assertTrue(policy["manual_full_promotion"])

    def test_rollout_has_analysis_after_every_batch_and_final_hard_pause(self):
        release = release_payload()
        policy = derive_canary_policy(release, 20)
        template = build_analysis_template(
            release,
            policy,
            "http://flawless.prod.svc.cluster.local:8080",
        )
        rollout = build_rollout(release, live_deployment(), policy)
        metric = template["spec"]["metrics"][0]
        self.assertEqual(metric["successCondition"], "result.safe == true")
        self.assertEqual(metric["failureCondition"], "result.abort == true")
        self.assertEqual(metric["failureLimit"], 0)
        self.assertEqual(metric["inconclusiveLimit"], 0)
        self.assertIn("/api/releases/rel-orders-v2/analysis", metric["provider"]["web"]["url"])
        steps = rollout["spec"]["strategy"]["canary"]["steps"]
        self.assertEqual(steps[-1], {"pause": {}})
        self.assertEqual(
            len([item for item in steps if "analysis" in item]),
            len(policy["weights"]),
        )
        self.assertEqual(rollout["spec"]["workloadRef"]["scaleDown"], "progressively")
        self.assertEqual(rollout["spec"]["strategy"]["canary"]["maxUnavailable"], 0)

    def test_final_pause_is_canary_validation_not_full_success(self):
        state = progressive_status(
            {
                "metadata": {"name": "orders-flawless", "namespace": "prod"},
                "spec": {"replicas": 20},
                "status": {
                    "phase": "Paused",
                    "readyReplicas": 20,
                    "availableReplicas": 20,
                    "currentStepIndex": 8,
                    "pauseConditions": [{"reason": "CanaryPauseStep"}],
                    "currentPodHash": "new",
                    "stableRS": "old",
                },
            },
            command="start",
            expected_steps=9,
        )
        self.assertTrue(state["recovered"])
        self.assertEqual(state["progressive_phase"], "canary_validated")
        self.assertNotEqual(state["current_pod_hash"], state["stable_rs"])

    def test_inconclusive_analysis_stops_before_canary_ceiling(self):
        state = progressive_status(
            {
                "metadata": {"name": "orders-flawless", "namespace": "prod"},
                "spec": {"replicas": 20},
                "status": {
                    "phase": "Paused",
                    "readyReplicas": 20,
                    "availableReplicas": 20,
                    "currentStepIndex": 2,
                    "pauseConditions": [{"reason": "InconclusiveAnalysisRun"}],
                },
            },
            command="start",
            expected_steps=9,
        )
        self.assertFalse(state["recovered"])
        self.assertEqual(state["progressive_phase"], "analysis_inconclusive")
        self.assertTrue(state["terminal_unresolved"])

    def test_failed_analysis_is_closed_only_after_stable_capacity_recovers(self):
        state = progressive_status(
            {
                "metadata": {"name": "orders-flawless", "namespace": "prod"},
                "spec": {"replicas": 10},
                "status": {
                    "phase": "Degraded",
                    "abort": True,
                    "readyReplicas": 10,
                    "availableReplicas": 10,
                    "currentPodHash": "new",
                    "stableRS": "old",
                },
            },
            command="start",
        )
        self.assertTrue(state["recovered"])
        self.assertFalse(state["release_succeeded"])
        self.assertEqual(state["progressive_phase"], "rolled_back")


class ProgressiveExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_deployment_release_uses_progressive_action(self):
        enqueue = AsyncMock(return_value={"id": "ops-release", "status": "queued"})
        await submit_release_job(release_payload(), "operator", enqueue)
        plan = enqueue.await_args.args[0]
        change = plan["changes"][0]
        self.assertEqual(change["type"], "progressive_rollout")
        self.assertTrue(plan["skill_execution_exempt"])
        self.assertTrue(plan["progressive_delivery"]["manual_full_promotion"])
        self.assertEqual(
            change["patch"]["spec"]["template"]["spec"]["containers"][0]["image"],
            "registry.example.com/orders:v2.0.0",
        )

    async def test_missing_prometheus_is_inconclusive_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReliabilityStore(str(Path(directory) / "reliability.json"))
            release = store.add_release(
                release_payload(
                    error_rate_promql="",
                    latency_p99_promql="",
                )
            )
            with patch.dict(
                os.environ,
                {
                    "GRAY_RELEASE_REQUIRE_PROMETHEUS": "true",
                    "GRAY_RELEASE_DEFAULT_ERROR_RATE_PROMQL": "",
                },
                clear=False,
            ):
                analysis = await reliability_api._evaluate_progressive_analysis(store, release)
        self.assertFalse(analysis["safe"])
        self.assertFalse(analysis["abort"])
        self.assertFalse(analysis["metric_evidence_ready"])

    async def test_hard_metric_violation_requests_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReliabilityStore(str(Path(directory) / "reliability.json"))
            release = store.add_release(
                release_payload(error_rate_promql="error_ratio")
            )
            with patch.object(
                reliability_api,
                "_scan_progressive_metrics",
                AsyncMock(return_value=(
                    {"status": "ok", "value": 0.05, "series": 1},
                    {"status": "not_configured"},
                )),
            ):
                analysis = await reliability_api._evaluate_progressive_analysis(store, release)
        self.assertFalse(analysis["safe"])
        self.assertTrue(analysis["abort"])
        self.assertFalse(analysis["metrics"]["error_rate"]["passed"])


if __name__ == "__main__":
    unittest.main()
