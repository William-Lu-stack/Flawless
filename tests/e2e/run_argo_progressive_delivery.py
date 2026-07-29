#!/usr/bin/env python3
"""Exercise the Flawless progressive-delivery executor against a real cluster.

Prerequisites:
  * Argo Rollouts is installed.
  * The kubeconfig uses embedded credentials.
  * The two test images are already available to the cluster.

This test intentionally calls the same application executor and verifier used
by the SRE console.  It proves canary pause, human promotion, automatic abort,
stableRS capacity recovery, and source Deployment desired-state restoration.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


NAMESPACE = "flawless-codepath-e2e"
CLUSTER_ID = "flawless-codepath-e2e"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--gate-image", default="flawless-local:latest")
    parser.add_argument("--workload-image", default="rancher/mirrored-pause:3.6")
    parser.add_argument("--keep", action="store_true")
    return parser.parse_args()


def configure_runtime(temp_dir: str) -> None:
    paths = {
        "CLUSTER_REGISTRY_PATH": "clusters.db",
        "RELIABILITY_STORE_PATH": "reliability.json",
        "RELIABILITY_STORE_FALLBACK_PATH": "reliability-fallback.json",
        "OPS_JOB_STORE_PATH": "ops-jobs.json",
        "OPS_SKILL_STORE_PATH": "ops-skills.json",
        "MODEL_PROFILES_STORE": "model-profiles.json",
        "KNOWLEDGE_STORE_PATH": "knowledge.json",
        "EFFECTIVENESS_STORE_PATH": "effectiveness.json",
        "EFFECTIVENESS_STORE_FALLBACK_PATH": "effectiveness-fallback.json",
    }
    for key, filename in paths.items():
        os.environ[key] = str(Path(temp_dir) / filename)
    os.environ["GRAY_RELEASE_BASELINE_TIMEOUT_SECONDS"] = "180"
    os.environ["GRAY_RELEASE_CHANGE_TIMEOUT_SECONDS"] = "240"
    os.environ["GRAY_RELEASE_VERIFY_TIMEOUT_SECONDS"] = "180"
    os.environ["OPS_VERIFY_INTERVAL_SECONDS"] = "2"


def namespace_manifest() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": NAMESPACE},
    }


def gate_manifests(image: str) -> list[dict]:
    server = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        abort = self.path.startswith("/abort/")
        body = json.dumps({"data": {"safe": not abort, "abort": abort}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        return
HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
""".strip()
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "analysis-gate", "namespace": NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "analysis-gate"}},
                "template": {
                    "metadata": {"labels": {"app": "analysis-gate"}},
                    "spec": {
                        "containers": [{
                            "name": "gate",
                            "image": image,
                            "imagePullPolicy": "Never",
                            "command": ["python", "-c"],
                            "args": [server],
                            "readinessProbe": {
                                "tcpSocket": {"port": 8080},
                                "periodSeconds": 2,
                            },
                        }],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "analysis-gate", "namespace": NAMESPACE},
            "spec": {
                "selector": {"app": "analysis-gate"},
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        },
    ]


def workload(name: str, image: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {
            "replicas": 10,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "labels": {"app": name},
                    "annotations": {"e2e.flawless.io/version": "stable"},
                },
                "spec": {
                    "containers": [{
                        "name": "app",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                    }],
                },
            },
        },
    }


def release(name: str, release_id: str) -> dict:
    return {
        "id": release_id,
        "service": name,
        "cluster": CLUSTER_ID,
        "namespace": NAMESPACE,
        "workload_kind": "Deployment",
        "workload_name": name,
        "container_name": "app",
        "release_mode": "existing",
        "change_channel": "standard",
        "analysis_interval_seconds": 10,
        "analysis_count": 1,
        "gate": {
            "verdict": "pass",
            "selected_strategy": {
                "first_ratio": 0.10,
                "step_ratio": 0.10,
                "max_ratio": 0.10,
                "observation_window_min": 1,
            },
        },
    }


def start_change(name: str, release_id: str, version: str) -> dict:
    return {
        "type": "progressive_rollout",
        "namespace": NAMESPACE,
        "workload_type": "Deployment",
        "workload_name": name,
        "release": release(name, release_id),
        "patch": {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"e2e.flawless.io/version": version},
                    },
                },
            },
        },
        "human_approved": True,
    }


def plan(change: dict) -> dict:
    return {
        "id": f"plan-{change['workload_name']}",
        "cluster_id": CLUSTER_ID,
        "namespace": NAMESPACE,
        "target": change["workload_name"],
        "service": change["workload_name"],
        "changes": [change],
        "high_risk_confirmed": True,
        "skill_execution_exempt": True,
    }


async def wait_deployment(registry, name: str, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = registry.read_resource(
            CLUSTER_ID,
            api_version="apps/v1",
            kind="Deployment",
            name=name,
            namespace=NAMESPACE,
        )
        status = item.get("status") or {}
        if int(status.get("availableReplicas") or 0) >= int((item.get("spec") or {}).get("replicas") or 1):
            return
        await asyncio.sleep(2)
    raise TimeoutError(f"Deployment/{name} did not become Available")


async def wait_progressive(probe, expected: set[str], timeout: int = 180) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = await probe()
        if str(latest.get("progressive_phase") or "") in expected:
            return latest
        await asyncio.sleep(2)
    raise TimeoutError(f"progressive phase did not reach {sorted(expected)}: {latest}")


async def main() -> None:
    args = parse_args()
    temp_dir = tempfile.mkdtemp(prefix="flawless-codepath-e2e-")
    configure_runtime(temp_dir)

    from backend.app import application

    registry = application.CLUSTER_REGISTRY
    registry.save(
        content=Path(args.kubeconfig).read_text(),
        name=CLUSTER_ID,
        cluster_id=CLUSTER_ID,
    )
    try:
        try:
            registry.delete_resource(
                CLUSTER_ID,
                api_version="v1",
                kind="Namespace",
                name=NAMESPACE,
                namespace="",
            )
            await asyncio.sleep(5)
        except Exception:
            pass
        registry.apply_manifest(CLUSTER_ID, namespace_manifest())
        for manifest in gate_manifests(args.gate_image):
            registry.apply_manifest(CLUSTER_ID, manifest)
        for name in ("orders", "payments"):
            registry.apply_manifest(CLUSTER_ID, workload(name, args.workload_image))
        await wait_deployment(registry, "analysis-gate")
        await wait_deployment(registry, "orders")
        await wait_deployment(registry, "payments")

        safe_url = f"http://analysis-gate.{NAMESPACE}.svc.cluster.local:8080"
        os.environ["GRAY_RELEASE_ANALYSIS_URL"] = safe_url
        orders_change = start_change("orders", "rel-orders-safe", "canary-pass")
        orders_plan = plan(orders_change)
        orders_receipt = await application._execute_progressive_change(
            orders_change,
            orders_plan,
            cluster_id=CLUSTER_ID,
            namespace=NAMESPACE,
            use_managed_cluster=True,
            use_rancher=False,
        )

        async def probe_orders_start() -> dict:
            return await application._probe_plan_recovery(
                orders_plan,
                [{"status": "success", "result": orders_receipt}],
            )

        orders_canary = await wait_progressive(
            probe_orders_start,
            {"canary_validated"},
        )
        if orders_canary.get("recovered") is not True:
            raise AssertionError(orders_canary)

        promote_change = {
            "type": "promote_progressive_rollout",
            "namespace": NAMESPACE,
            "workload_type": "Deployment",
            "workload_name": "orders",
            "rollout_name": "orders-flawless",
            "human_approved": True,
        }
        promote_plan = plan(promote_change)
        promote_receipt = await application._execute_progressive_change(
            promote_change,
            promote_plan,
            cluster_id=CLUSTER_ID,
            namespace=NAMESPACE,
            use_managed_cluster=True,
            use_rancher=False,
        )

        async def probe_orders_promote() -> dict:
            return await application._probe_plan_recovery(
                promote_plan,
                [{"status": "success", "result": promote_receipt}],
            )

        orders_promoted = await wait_progressive(
            probe_orders_promote,
            {"fully_promoted"},
        )
        if orders_promoted.get("recovered") is not True:
            raise AssertionError(orders_promoted)

        os.environ["GRAY_RELEASE_ANALYSIS_URL"] = safe_url + "/abort"
        payments_change = start_change("payments", "rel-payments-abort", "canary-fail")
        payments_plan = plan(payments_change)
        payments_receipt = await application._execute_progressive_change(
            payments_change,
            payments_plan,
            cluster_id=CLUSTER_ID,
            namespace=NAMESPACE,
            use_managed_cluster=True,
            use_rancher=False,
        )

        async def probe_payments() -> dict:
            return await application._probe_plan_recovery(
                payments_plan,
                [{"status": "success", "result": payments_receipt}],
            )

        payments_rolled_back = await wait_progressive(
            probe_payments,
            {"rolled_back"},
        )
        if payments_rolled_back.get("recovered") is not True:
            raise AssertionError(payments_rolled_back)
        if payments_rolled_back.get("source_desired_state_restored") is not True:
            raise AssertionError(payments_rolled_back)
        live_payments = registry.read_resource(
            CLUSTER_ID,
            api_version="apps/v1",
            kind="Deployment",
            name="payments",
            namespace=NAMESPACE,
        )
        restored_version = (
            (((live_payments.get("spec") or {}).get("template") or {}).get("metadata") or {})
            .get("annotations", {})
            .get("e2e.flawless.io/version")
        )
        if restored_version != "stable":
            raise AssertionError(f"source Deployment template not restored: {restored_version}")

        print({
            "canary": orders_canary["progressive_phase"],
            "promotion": orders_promoted["progressive_phase"],
            "rollback": payments_rolled_back["progressive_phase"],
            "source_desired_state_restored": True,
        })
    finally:
        if not args.keep:
            try:
                registry.delete_resource(
                    CLUSTER_ID,
                    api_version="v1",
                    kind="Namespace",
                    name=NAMESPACE,
                    namespace="",
                )
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
