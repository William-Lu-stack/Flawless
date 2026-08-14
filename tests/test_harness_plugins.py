import asyncio
import json
import unittest

from backend.app.services.harness_plugins import (
    HarnessPluginRuntime,
    HarnessPolicyDenied,
    PluginManifest,
    compact_planner_context,
    guard_execution_request,
    harness_capabilities_payload,
)


class HarnessPluginRuntimeTests(unittest.TestCase):
    def test_dependencies_activate_after_provider_and_unwind_on_unmount(self):
        runtime = HarnessPluginRuntime(runtime_id="test")
        disposed: list[str] = []

        consumer = PluginManifest(
            id="consumer",
            requires=("service.inventory",),
            provides=("service.planner",),
        )

        def setup_consumer(context):
            context.provide("service.planner", {"provider": "consumer"})
            context.effect(lambda: disposed.append("consumer"))

        runtime.mount(consumer, setup_consumer)
        self.assertEqual(runtime.diagnostics()["plugins"][0]["status"], "pending_dependencies")

        provider = PluginManifest(id="provider", provides=("service.inventory",))
        runtime.mount(provider, lambda context: context.provide("service.inventory", {"ready": True}))
        self.assertEqual(runtime.resolve("service.planner"), {"provider": "consumer"})
        self.assertEqual(runtime.diagnostics()["summary"]["active"], 2)

        runtime.unmount("provider")
        self.assertIn("consumer", disposed)
        consumer_status = next(
            item for item in runtime.diagnostics()["plugins"] if item["id"] == "consumer"
        )
        self.assertEqual(consumer_status["status"], "pending_dependencies")

    def test_nearest_scope_service_wins_without_replacing_global_provider(self):
        runtime = HarnessPluginRuntime(runtime_id="test")
        global_plugin = PluginManifest(id="global", provides=("config",))
        job_plugin = PluginManifest(id="job", scope="job:42", provides=("config",))
        runtime.mount(global_plugin, lambda context: context.provide("config", "global"))
        runtime.mount(job_plugin, lambda context: context.provide("config", "job"))

        self.assertEqual(runtime.resolve("config"), "global")
        self.assertEqual(runtime.resolve("config", scopes=("job:42",)), "job")
        self.assertEqual(runtime.resolve("config", scopes=("job:7",)), "global")

    def test_waterfall_guards_run_in_priority_order_and_can_fail_closed(self):
        runtime = HarnessPluginRuntime(runtime_id="test")
        calls: list[str] = []

        async def outer(payload, next_handler):
            calls.append("outer-before")
            result = await next_handler({**payload, "outer": True})
            calls.append("outer-after")
            return result

        async def inner(payload, next_handler):
            calls.append("inner")
            if not payload.get("approved"):
                raise HarnessPolicyDenied("approval missing")
            return await next_handler(payload)

        runtime.mount(
            PluginManifest(id="outer", events=("tools/pre-execute",), priority=100),
            lambda context: context.on("tools/pre-execute", outer, mode="waterfall"),
        )
        runtime.mount(
            PluginManifest(id="inner", events=("tools/pre-execute",), priority=10),
            lambda context: context.on("tools/pre-execute", inner, mode="waterfall"),
        )

        allowed = asyncio.run(runtime.dispatch("tools/pre-execute", {"approved": True}))
        self.assertTrue(allowed["outer"])
        self.assertEqual(calls, ["outer-before", "inner", "outer-after"])
        with self.assertRaisesRegex(HarnessPolicyDenied, "approval missing"):
            asyncio.run(runtime.dispatch("tools/pre-execute", {"approved": False}))

    def test_builtin_runtime_exposes_deepseek_harness_parity_contracts(self):
        payload = harness_capabilities_payload()
        self.assertGreaterEqual(payload["summary"]["active"], 10)
        agents = {item["domain"]: item for item in payload["agents"]}
        self.assertEqual(agents["kubernetes"]["status"], "active")
        for domain in {"database", "virtual-machine", "storage", "middleware", "cloud", "network"}:
            self.assertEqual(agents[domain]["status"], "pending_dependencies")
            self.assertTrue(agents[domain]["missing_dependencies"])
        self.assertTrue(payload["contracts"]["monotonic_tool_guards"])
        self.assertTrue(payload["contracts"]["official_runtime_cannot_mutate_kubernetes"])

    def test_builtin_approval_gate_rejects_unapproved_mutation(self):
        with self.assertRaises(HarnessPolicyDenied):
            asyncio.run(guard_execution_request(
                {"type": "patch_workload", "workload_name": "api"},
                {"cluster_id": "prod", "namespace": "apps", "target": "Deployment/api"},
                approved=False,
            ))
        receipt = asyncio.run(guard_execution_request(
            {"type": "patch_workload", "workload_name": "api"},
            {"cluster_id": "prod", "namespace": "apps", "target": "Deployment/api"},
            approved=True,
        ))
        self.assertEqual(
            [item["guard"] for item in receipt["guard_receipts"]],
            ["cisre.approval-gate", "cisre.execution-target-guard"],
        )

    def test_context_compaction_keeps_direct_error_and_bounds_history(self):
        context = compact_planner_context(
            plan={
                "target": "Deployment/grafana",
                "_prior_attempts": [{"message": "noise " * 1000} for _ in range(20)],
            },
            evidence={
                "logs": {"current": "ERROR unable to open database file (14)"},
                "bulk": ["ordinary line " * 1000 for _ in range(100)],
            },
            failure={"message": "Pod is not Ready"},
            max_chars=8000,
        )
        encoded = str(context)
        self.assertIn("unable to open database file", encoded)
        self.assertLessEqual(len(context["prior_attempts"]), 2)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 8000)
        self.assertEqual(context["_compaction"]["engine"], "CISREImportanceCompactor/v1")


if __name__ == "__main__":
    unittest.main()
