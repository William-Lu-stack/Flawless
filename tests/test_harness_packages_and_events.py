import json
import hashlib
import asyncio
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.services.harness_events import HarnessEventStore
from backend.app.services.harness_packages import HarnessPackageManager, RemoteServiceProvider
from backend.app.services.harness_plugins import BUILTIN_PLUGIN_MANIFESTS, HarnessPluginRuntime, PluginManifest


def write_yaml(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(value).strip() + "\n", encoding="utf-8")


class HarnessPackageManagerTests(unittest.TestCase):
    def test_plugin_contract_distinguishes_shared_and_domain_capabilities(self):
        shared = PluginManifest(id="team.audit", category="shared", domains=("common",), agents=("all",)).public()
        domain = PluginManifest(
            id="team.database-evidence",
            category="domain",
            domains=("database",),
            agents=("database",),
        ).public()
        builtins = {item.id: item.public() for item in BUILTIN_PLUGIN_MANIFESTS}

        self.assertEqual(shared["category"], "shared")
        self.assertEqual(shared["domains"], ["common"])
        self.assertEqual(domain["agents"], ["database"])
        self.assertEqual(builtins["cisre.agent.kubernetes"]["category"], "domain-agent")
        self.assertEqual(builtins["cisre.kubernetes-executor"]["domains"], ["kubernetes"])
        self.assertIn("cisre.agent.database", builtins)
        self.assertIn("cisre.agent.network", builtins)

    def test_ui_install_validates_persists_and_mounts_without_core_change(self):
        source = """
            apiVersion: cisre.io/v1alpha1
            kind: HarnessPlugin
            metadata: {name: team.database-provider, version: 1.0.0}
            spec:
              provides: [{name: inventory.database, version: 1.0.0}]
              requires: [{name: session.events, constraint: ">=1.0"}]
              permissions: [service:provide, inventory:read]
        """
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED": "true",
            "HARNESS_PROFILE": "production",
        }):
            runtime = HarnessPluginRuntime(runtime_id="ui-install-test")
            runtime.mount(
                PluginManifest(id="test.session-events", version="1.0.0", provides=("session.events",)),
                lambda context: context.provide("session.events", {"version": "1.0.0"}),
            )
            manager = HarnessPackageManager(runtime, directory)
            result = manager.install_source(textwrap.dedent(source), add_to_active_profile=True)

            self.assertEqual(result["status"], "installed")
            self.assertEqual(runtime.resolve("inventory.database")["plugin_id"], "team.database-provider")
            self.assertTrue((Path(directory) / "packages" / "team.database-provider.yaml").exists())
            self.assertIn("team.database-provider", (Path(directory) / "profiles" / "production.yaml").read_text())

    def test_ui_install_rejects_plaintext_credentials(self):
        source = """
            apiVersion: cisre.io/v1alpha1
            kind: HarnessPlugin
            metadata: {name: team.unsafe-secret, version: 1.0.0}
            spec:
              provides: [inventory.database]
              permissions: [service:provide]
              config: {api_token: plaintext-must-not-be-written}
        """
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED": "true",
        }):
            manager = HarnessPackageManager(HarnessPluginRuntime(runtime_id="secret-test"), directory)
            with self.assertRaisesRegex(Exception, "must not contain credentials"):
                manager.install_source(textwrap.dedent(source))
            self.assertFalse((Path(directory) / "packages").exists())

    def test_ui_install_accepts_kubernetes_secret_reference(self):
        source = """
            apiVersion: cisre.io/v1alpha1
            kind: HarnessPlugin
            metadata: {name: team.safe-secret-ref, version: 1.0.0}
            spec:
              provides: [inventory.database]
              permissions: [service:provide]
              config:
                credential_ref: {name: database-provider, key: api-token, namespace: platform}
        """
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "HARNESS_PACKAGE_RUNTIME_WRITE_ENABLED": "true",
        }):
            manager = HarnessPackageManager(HarnessPluginRuntime(runtime_id="secret-ref-test"), directory)
            result = manager.install_source(textwrap.dedent(source))
            self.assertEqual(result["status"], "installed")

    def test_out_of_tree_provider_and_consumer_load_without_core_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_yaml(root / "packages" / "inventory.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessPlugin
                metadata:
                  name: team.inventory-provider
                  version: 2.1.0
                spec:
                  provides:
                    - name: inventory.reader
                      version: 2.1.0
                  permissions: [service:provide, inventory:read]
            """)
            write_yaml(root / "packages" / "planner.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessPlugin
                metadata:
                  name: team.risk-planner
                  version: 1.0.0
                spec:
                  requires:
                    - name: inventory.reader
                      constraint: ">=2.0,<3"
                  provides: [risk.planner]
                  permissions: [service:provide, ops:propose]
            """)
            write_yaml(root / "profiles" / "team.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessProfile
                metadata: {name: team}
                spec:
                  plugins: [team.risk-planner, team.inventory-provider]
            """)
            runtime = HarnessPluginRuntime(runtime_id="package-test")
            manager = HarnessPackageManager(runtime, root)
            result = manager.refresh("team")

            self.assertEqual(result["active_profile"], "team")
            self.assertEqual(runtime.resolve("inventory.reader")["plugin_id"], "team.inventory-provider")
            self.assertEqual(runtime.resolve("risk.planner")["plugin_id"], "team.risk-planner")
            self.assertTrue(all(item["package_status"] == "active" for item in result["packages"]))

    def test_profile_patch_swaps_provider_by_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("alpha", "beta"):
                write_yaml(root / "packages" / f"{name}.yaml", f"""
                    apiVersion: cisre.io/v1alpha1
                    kind: HarnessPlugin
                    metadata:
                      name: team.{name}
                      version: 1.0.0
                    spec:
                      provides: [model.provider]
                      permissions: [service:provide, model:invoke]
                """)
            write_yaml(root / "profiles" / "team.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessProfile
                metadata: {name: team}
                spec:
                  plugins: [team.alpha, team.beta]
                  patches:
                    - {id: team.alpha, priority: 10}
                    - {id: team.beta, priority: 90}
            """)
            runtime = HarnessPluginRuntime(runtime_id="swap-test")
            manager = HarnessPackageManager(runtime, root)
            manager.refresh("team")
            self.assertEqual(runtime.resolve("model.provider")["plugin_id"], "team.beta")

    def test_unsigned_privileged_plugin_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_yaml(root / "packages" / "unsafe.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessPlugin
                metadata:
                  name: team.unsafe
                  version: 1.0.0
                spec:
                  provides: [tool.executor]
                  permissions: [service:provide, subprocess:execute, kubernetes:mutate]
            """)
            write_yaml(root / "profiles" / "team.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessProfile
                metadata: {name: team}
                spec: {plugins: [team.unsafe]}
            """)
            runtime = HarnessPluginRuntime(runtime_id="policy-test")
            manager = HarnessPackageManager(runtime, root)
            result = manager.refresh("team")
            package = result["packages"][0]
            self.assertEqual(package["package_status"], "policy_denied")
            self.assertIn("kubernetes:mutate", package["denied_permissions"])
            self.assertFalse(runtime.has_service("tool.executor"))

    def test_incompatible_service_version_stays_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_yaml(root / "packages" / "provider.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessPlugin
                metadata: {name: team.provider, version: 1.4.0}
                spec:
                  provides: [{name: evidence.reader, version: 1.4.0}]
                  permissions: [service:provide]
            """)
            write_yaml(root / "packages" / "consumer.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessPlugin
                metadata: {name: team.consumer, version: 1.0.0}
                spec:
                  requires: [{name: evidence.reader, constraint: ">=2.0"}]
                  provides: [planner]
                  permissions: [service:provide]
            """)
            write_yaml(root / "profiles" / "team.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessProfile
                metadata: {name: team}
                spec: {plugins: [team.provider, team.consumer]}
            """)
            manager = HarnessPackageManager(HarnessPluginRuntime(runtime_id="version-test"), root)
            result = manager.refresh("team")
            consumer = next(item for item in result["packages"] if item["id"] == "team.consumer")
            self.assertEqual(consumer["package_status"], "pending_dependencies")
            self.assertIn("evidence.reader>=2.0", consumer["package_error"])

    def test_signed_remote_provider_registers_only_declared_read_only_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = root / "packages" / "remote.yaml"
            write_yaml(package_path, """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessPlugin
                metadata: {name: team.remote-inventory, version: 1.0.0}
                spec:
                  provides: [inventory.remote]
                  permissions: [service:provide, inventory:read, network:egress]
                  runtime:
                    type: remote
                    endpoint: http://127.0.0.1:19090/invoke
                    operations:
                      - {id: discover, read_only: true}
                      - {id: mutate, read_only: false}
            """)
            write_yaml(root / "profiles" / "team.yaml", """
                apiVersion: cisre.io/v1alpha1
                kind: HarnessProfile
                metadata: {name: team}
                spec: {plugins: [team.remote-inventory]}
            """)
            digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
            with patch.dict("os.environ", {
                "HARNESS_TRUSTED_PLUGIN_DIGESTS": digest,
                "HARNESS_TRUSTED_PLUGIN_PERMISSIONS": "network:egress",
                "HARNESS_PLUGIN_DEVELOPMENT_MODE": "true",
            }):
                runtime = HarnessPluginRuntime(runtime_id="remote-test")
                manager = HarnessPackageManager(runtime, root)
                manager.refresh("team")
            provider = runtime.resolve("inventory.remote")
            self.assertIsInstance(provider, RemoteServiceProvider)
            self.assertEqual(provider.operations, ("discover",))

            class FakeResponse:
                content = b'{"resources":[{"id":"db-1"}]}'

                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"resources": [{"id": "db-1"}], "pass" + "word": "must-" + "redact"}

            class FakeClient:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                async def post(self, endpoint, *, json):
                    self.assertion = (endpoint, json)
                    return FakeResponse()

            with patch("backend.app.services.harness_packages.httpx.AsyncClient", return_value=FakeClient()):
                result = asyncio.run(provider.invoke("discover", {"scope": "all"}))
            self.assertEqual(result["resources"][0]["id"], "db-1")
            self.assertEqual(result["password"], "[REDACTED]")
            with self.assertRaisesRegex(Exception, "not declared read-only"):
                asyncio.run(provider.invoke("mutate", {}))


class HarnessEventStoreTests(unittest.TestCase):
    def test_persistence_redaction_replay_fork_and_hash_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = HarnessEventStore(path)
            store.append(
                "job-1",
                "session/created",
                phase="evidence",
                status="pending",
                data={"api_token": "do-not-store", "message": "Bearer abcdefghijklmnop"},
                target={"resource": "Deployment/api"},
            )
            store.append("job-1", "tools/result", phase="change", status="completed", tool="patch_workload")
            child = store.fork("job-1", at_seq=2, actor="tester")

            restored = HarnessEventStore(path)
            events = restored.events("job-1")
            self.assertEqual(events[0]["data"]["api_token"], "[REDACTED]")
            self.assertIn("[REDACTED]", events[0]["data"]["message"])
            self.assertTrue(restored.verify_chain("job-1")["valid"])
            self.assertEqual(restored.replay("job-1")["current_phase"], "change")
            self.assertEqual(restored.children("job-1")[0]["session_id"], child["session_id"])
            self.assertEqual(restored.resume(child["session_id"])["projection"]["status"], "running")
            self.assertGreaterEqual(len(path.read_text(encoding="utf-8").splitlines()), 5)

    def test_file_is_jsonl_not_mutable_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = HarnessEventStore(path)
            store.append("one", "first")
            store.append("one", "second")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["seq"] for row in rows], [1, 2])
            self.assertEqual(rows[1]["previous_hash"], rows[0]["hash"])

    def test_branch_delete_is_audited_tombstone_and_root_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            store = HarnessEventStore(path)
            store.append("root", "session/created", status="paused")
            child = store.fork("root", actor="tester")

            result = store.delete_branch(child["session_id"], actor="tester")

            self.assertEqual(result["status"], "deleted")
            self.assertTrue(result["audit_retained"])
            self.assertEqual(store.children("root"), [])
            self.assertIsNone(store.session(child["session_id"], include_deleted=False))
            self.assertEqual(store.session(child["session_id"])["status"], "deleted")
            self.assertEqual(store.events(child["session_id"])[-1]["type"], "session/deleted")
            self.assertTrue(store.verify_chain(child["session_id"])["valid"])
            with self.assertRaisesRegex(ValueError, "root sessions"):
                store.delete_branch("root", actor="tester")

    def test_running_or_parent_branch_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HarnessEventStore(Path(directory) / "events.jsonl")
            store.append("root", "session/created", status="paused")
            child = store.fork("root", actor="tester")["session_id"]
            grandchild = store.fork(child, actor="tester")["session_id"]

            with self.assertRaisesRegex(ValueError, "child sessions"):
                store.delete_branch(child)
            store.resume(grandchild)
            with self.assertRaisesRegex(RuntimeError, "running branches"):
                store.delete_branch(grandchild)

    def test_agent_trace_projects_context_llm_skill_tool_and_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HarnessEventStore(Path(directory) / "events.jsonl")
            store.append("job", "evidence/collected", stage="evidence_done", message="ERROR logs attached")
            store.append("job", "diagnosis/planning", stage="llm_planning_done", data={"model_profile_id": "primary"})
            store.append("job", "skills/selected", stage="skill_selected", plugin_id="skill-volume-recovery")
            store.append("job", "tools/result", stage="change_done", tool="patch_workload", status="completed")
            store.append("job", "recovery/complete", stage="recovered", status="completed")

            trace = store.trace("job")

            self.assertEqual(trace["trace_schema"], "cisre.agent-trace/v1")
            self.assertEqual([span["kind"] for span in trace["spans"]], ["context", "think", "skill", "tool", "verify"])
            self.assertEqual(trace["summary"]["model_calls"], 1)
            self.assertEqual(trace["summary"]["tools"]["patch_workload"], 1)
            self.assertEqual(trace["summary"]["turns"], 1)
            self.assertEqual(trace["summary"]["calls"], 2)
            self.assertGreaterEqual(trace["summary"]["duration_ms"], 0)
            self.assertEqual(trace["spans"][0]["lane"], "Input")
            self.assertEqual(trace["spans"][1]["lane"], "Model")
            self.assertEqual(trace["spans"][3]["lane"], "Tools")
            self.assertIn("offset_ms", trace["spans"][0])
            self.assertTrue(trace["integrity"]["valid"])


if __name__ == "__main__":
    unittest.main()
