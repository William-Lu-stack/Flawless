import asyncio
import copy
import httpx
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.app.domain.slo import evaluate_error_budget
from backend.app.api.reliability import ReliabilityDependencies, _validate_release_manifest, build_reliability_router
from backend.app.main import _score_benchmark_answer
from backend.app import main as server
from backend.app.services.ops_execution import StageTimeoutError, run_with_heartbeat
from backend.app.services.reliability_store import ReliabilityStore
from backend.app.services.external_traffic import build_external_traffic_payload


class ErrorBudgetTests(unittest.TestCase):
    def test_kubernetes_patch_redaction_keeps_deep_approval_values_visible(self):
        patch = {
            "pending_approval": {
                "change": {
                    "patch": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [{
                                        "name": "grafana",
                                        "securityContext": {
                                            "runAsUser": 10001,
                                            "runAsGroup": 10001,
                                            "runAsNonRoot": True,
                                        },
                                    }],
                                },
                            },
                        },
                    },
                },
            },
        }
        redacted = server._redact_sensitive(patch)
        security_context = redacted["pending_approval"]["change"]["patch"]["spec"]["template"]["spec"]["containers"][0]["securityContext"]
        self.assertEqual(security_context["runAsUser"], 10001)
        self.assertEqual(security_context["runAsGroup"], 10001)
        self.assertTrue(security_context["runAsNonRoot"])

    def test_deepseek_structured_json_disables_thinking_by_default(self):
        from agents import llm_client

        response = MagicMock()
        response.json.return_value = {
            "model": "deepseek-v4-flash",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"status":"ok"}'},
            }],
            "usage": {},
        }
        response.status_code = 200
        with patch.object(llm_client._gateway_client, "post", return_value=response) as post:
            model = llm_client.GatewayChatModel(
                model_name="deepseek-v4-flash",
                base_url="https://model.example/v1",
                auth_type="none",
            )
            result = model.invoke(
                "只返回 JSON",
                response_format={"type": "json_object"},
            )
        self.assertEqual(result.content, '{"status":"ok"}')
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_deepseek_vllm_structured_json_uses_vllm_thinking_control(self):
        from agents import llm_client

        response = MagicMock(status_code=200)
        response.json.return_value = {
            "model": "deepseek-v4-flash",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"status":"ok"}'},
            }],
            "usage": {},
        }
        with patch.object(llm_client._gateway_client, "post", return_value=response) as post:
            model = llm_client.GatewayChatModel(
                model_name="deepseek-v4-flash",
                provider_name="internal-vllm",
                base_url="https://model.example/engines/vllm",
                auth_type="none",
            )
            model.invoke(
                "只返回 JSON",
                response_format={"type": "json_object"},
            )
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("thinking", payload)
        self.assertEqual(
            payload["chat_template_kwargs"],
            {"enable_thinking": False},
        )

    def test_unsupported_structured_thinking_control_retries_without_extension(self):
        from agents import llm_client

        rejected = MagicMock(status_code=400)
        accepted = MagicMock(status_code=200)
        accepted.json.return_value = {
            "model": "deepseek-v4-flash",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"status":"ok"}'},
            }],
            "usage": {},
        }
        with patch.object(
            llm_client._gateway_client,
            "post",
            side_effect=[rejected, accepted],
        ) as post:
            model = llm_client.GatewayChatModel(
                model_name="deepseek-v4-flash",
                base_url="https://compatible.example/v1",
                auth_type="none",
            )
            result = model.invoke(
                "只返回 JSON",
                response_format={"type": "json_object"},
            )
        self.assertEqual(result.content, '{"status":"ok"}')
        self.assertEqual(post.call_count, 2)
        retry_payload = post.call_args_list[1].kwargs["json"]
        self.assertNotIn("thinking", retry_payload)
        self.assertNotIn("chat_template_kwargs", retry_payload)

    def test_default_production_slo_is_99_9(self):
        budget = evaluate_error_budget({"service": "svc", "window_days": 30})
        self.assertEqual(budget["target_percent"], 99.9)
        self.assertAlmostEqual(budget["allowed_downtime_minutes"], 43.2)

    def test_workload_target_prefers_unhealthy_pod_for_evidence(self):
        pods = [
            {
                "name": "api-healthy-aaa",
                "workload_name": "api",
                "workload_kind": "Deployment",
                "ready": True,
                "phase": "Running",
                "restart_count": 0,
                "containers": [{"name": "api", "ready": True, "state": "running"}],
            },
            {
                "name": "api-bad-bbb",
                "workload_name": "api",
                "workload_kind": "Deployment",
                "ready": False,
                "phase": "Running",
                "restart_count": 9,
                "containers": [{"name": "api", "ready": False, "state": "waiting", "reason": "CrashLoopBackOff"}],
            },
        ]
        selected, matching = server._select_representative_pod(
            pods,
            workload_name="api",
            workload_type="Deployment",
        )
        self.assertEqual(selected["name"], "api-bad-bbb")
        self.assertEqual(len(matching), 2)

    def test_image_pull_rpc_error_is_not_misclassified_as_crashloop(self):
        category, severity, _reason = server._classify_pod_issue({
            "phase": "Pending",
            "ready": False,
            "containers": [{
                "state": "waiting",
                "reason": "ErrImagePull",
                "state_detail": {"message": "rpc error: pull access denied"},
            }],
        }, [])
        self.assertEqual(category, "image_pull")
        self.assertEqual(severity, "P1")

    def test_init_container_failure_is_part_of_pod_diagnosis(self):
        pod = server._normalize_k8s_pod({
            "metadata": {"name": "api-abc", "namespace": "prod"},
            "spec": {
                "containers": [{"name": "api"}],
                "initContainers": [{"name": "prepare", "securityContext": {"runAsUser": 10001}}],
            },
            "status": {
                "phase": "Pending",
                "containerStatuses": [{"name": "api", "ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}],
                "initContainerStatuses": [{
                    "name": "prepare",
                    "ready": False,
                    "restartCount": 3,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "mkdir /data/cache: permission denied"}},
                }],
            },
        })
        init = next(item for item in pod["containers"] if item["name"] == "prepare")
        self.assertTrue(init["is_init"])
        self.assertIn("permission denied", init["state_detail"]["message"])
        self.assertEqual(server._classify_pod_issue(pod, [])[0], "crashloop")

    def test_storage_admin_steps_do_not_guess_a_group(self):
        plan = {
            "evidence": {
                "pod": {
                    "security_context": {"fsGroup": 1000, "runAsUser": 472, "runAsGroup": 472},
                    "containers": [{
                        "name": "grafana",
                        "security_context": {"runAsUser": 472, "runAsGroup": 472},
                        "volume_mounts": [{"name": "data", "mount_path": "/var/lib/grafana"}],
                    }],
                }
            }
        }
        steps = server._storage_admin_steps(plan)
        self.assertFalse(any("建议属组" in step or "472:472" in step for step in steps))

    def test_storage_permission_followup_starts_with_nonroot_group_patch(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "volume permission denied",
            "evidence": {
                "state_text": "mkdir: can't create directory '/var/lib/grafana/plugins': Permission denied",
                "pod": {
                    "containers": [{
                        "name": "grafana",
                        "security_context": {"runAsUser": 472, "runAsGroup": 472},
                        "volume_mounts": [{"name": "data", "mount_path": "/var/lib/grafana"}],
                    }],
                },
            },
            "changes": [{"type": "patch_workload", "workload_type": "Deployment", "workload_name": "grafana"}],
        }
        followups = server._derive_followup_plans(plan, "storage volume permission denied")
        self.assertEqual(len(followups[0]["changes"]), 1)
        self.assertEqual(followups[0]["source"], "executable_volume_permission_skill")
        self.assertEqual(
            followups[0]["skill_runtime"]["handler_id"],
            "volume-write-permission-recovery",
        )
        self.assertEqual(followups[0]["permission_recovery_stage"], "nonroot_group")
        patch = followups[0]["changes"][0]["patch"]
        self.assertEqual(patch["spec"]["template"]["spec"]["securityContext"]["fsGroup"], 472)
        self.assertTrue(patch["spec"]["template"]["spec"]["containers"][0]["securityContext"]["runAsNonRoot"])

    def test_permission_recovery_escalates_to_bounded_init_then_failing_container_root(self):
        pod = {
            "name": "grafana-abc",
            "containers": [{
                "name": "grafana",
                "security_context": {"runAsUser": 472, "runAsGroup": 472},
                "volume_mounts": [{
                    "name": "data",
                    "mount_path": "/var/lib/grafana",
                    "sub_path": "grafana",
                }],
            }],
        }
        nonroot_change = {
            "type": "patch_workload_runtime_security",
            "patch": {"spec": {"template": {"spec": {
                "securityContext": {"fsGroup": 472},
                "containers": [{
                    "name": "grafana",
                    "securityContext": {"runAsUser": 472, "runAsGroup": 472, "runAsNonRoot": True},
                }],
            }}}},
        }
        base = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "volume permission denied",
            "evidence": {"state_text": "permission denied", "pod": pod},
            "_runtime_evidence": {
                "pod_name": "grafana-abc",
                "pod": pod,
                "logs": {"grafana": {"current": "mkdir /var/lib/grafana/plugins: permission denied"}},
            },
            "_last_failure": {"attempted_changes": [nonroot_change]},
        }
        with patch.dict(os.environ, {"NODE_EXEC_IMAGE": "busybox:1.36"}):
            init_plan = server._permission_recovery_followup(base)
        self.assertEqual(init_plan["permission_recovery_stage"], "init_owner")
        init = init_plan["changes"][0]["patch"]["spec"]["template"]["spec"]["initContainers"][0]
        self.assertEqual(init["securityContext"]["runAsUser"], 0)
        self.assertEqual(init["volumeMounts"][0]["mountPath"], "/var/lib/grafana")
        self.assertEqual(init["volumeMounts"][0]["subPath"], "grafana")
        self.assertIn("chown -R 472:472", init["args"][0])
        self.assertEqual(
            init_plan["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["runAsUser"],
            472,
        )

        escalated = {
            **base,
            "_last_failure": {
                "attempted_changes": [nonroot_change, init_plan["changes"][0]],
            },
        }
        root_plan = server._permission_recovery_followup(escalated)
        self.assertEqual(root_plan["permission_recovery_stage"], "root")
        root_spec = root_plan["changes"][0]["patch"]["spec"]["template"]["spec"]
        self.assertEqual(root_spec["securityContext"]["fsGroup"], 0)
        self.assertEqual(len(root_spec["containers"]), 1)
        self.assertEqual(root_spec["containers"][0]["name"], "grafana")
        self.assertEqual(root_spec["containers"][0]["securityContext"]["runAsUser"], 0)

    def test_root_fallback_materializes_complete_pod_and_container_security_context(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "permission_recovery_stage": "root",
            "_runtime_evidence": {
                "pod": {
                    "containers": [{
                        "name": "grafana",
                        "security_context": {"runAsUser": 472, "runAsNonRoot": True},
                    }],
                },
                "logs": {"grafana": {"current": "unable to open database file"}},
            },
            "changes": [{
                "type": "patch_workload",
                "workload_type": "Deployment",
                "workload_name": "grafana",
                "container_name": "grafana",
                "patch": {
                    "spec": {
                        "template": {
                            "spec": {
                                "securityContext": {"runAsUser": 0},
                                "containers": [{
                                    "name": "grafana",
                                    "securityContext": {"runAsUser": 0},
                                }],
                            },
                        },
                    },
                },
            }],
        }
        valid, reason = server._enforce_root_security_context_plan(plan)
        self.assertTrue(valid, reason)
        change = plan["changes"][0]
        complete, missing = server._root_security_context_patch_is_complete(plan, change)
        self.assertTrue(complete, missing)
        spec = change["patch"]["spec"]["template"]["spec"]
        self.assertEqual(spec["securityContext"]["runAsUser"], 0)
        self.assertEqual(spec["securityContext"]["runAsGroup"], 0)
        self.assertEqual(spec["securityContext"]["fsGroup"], 0)
        self.assertEqual(spec["securityContext"]["supplementalGroups"], [0])
        self.assertIs(spec["securityContext"]["runAsNonRoot"], False)
        container_sc = spec["containers"][0]["securityContext"]
        self.assertEqual(container_sc["runAsUser"], 0)
        self.assertEqual(container_sc["runAsGroup"], 0)
        self.assertIs(container_sc["runAsNonRoot"], False)
        self.assertIs(container_sc["allowPrivilegeEscalation"], False)

    def test_resumed_permission_lineage_escalates_after_executed_nonroot_stages(self):
        pod = {
            "name": "grafana-abc",
            "containers": [{
                "name": "grafana",
                "security_context": {"runAsUser": 472, "runAsGroup": 472},
                "volume_mounts": [{"name": "data", "mount_path": "/var/lib/grafana"}],
            }],
        }
        resumed = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "unable to open database file",
            "_runtime_evidence": {
                "pod": pod,
                "logs": {"grafana": {"current": "GF_PATHS_DATA=/var/lib/grafana is not writable"}},
            },
            "_attempted_permission_recovery_stages": ["nonroot_group", "init_owner"],
        }
        root_plan = server._permission_recovery_followup(resumed)
        self.assertEqual(root_plan["permission_recovery_stage"], "root")
        root_sc = root_plan["changes"][0]["patch"]["spec"]["template"]["spec"]["securityContext"]
        self.assertEqual(root_sc["runAsUser"], 0)
        self.assertEqual(root_sc["runAsGroup"], 0)
        self.assertEqual(root_sc["fsGroup"], 0)
        self.assertIs(root_sc["runAsNonRoot"], False)

    def test_accepted_nonroot_stage_is_visible_to_same_process_replan(self):
        plan = {
            "namespace": "k8s-agent",
            "target": "Deployment/k8s-agent-grafana",
            "summary": "GF_PATHS_DATA=/var/lib/grafana is not writable",
            "_runtime_evidence": {
                "logs": {"grafana": {"current": (
                    "GF_PATHS_DATA=/var/lib/grafana is not writable\n"
                    "Error: unable to open database file (14)"
                )}},
                "pod": {
                    "name": "grafana-bad",
                    "security_context": {"runAsUser": 10001, "runAsGroup": 10001},
                    "containers": [{
                        "name": "grafana",
                        "security_context": {"runAsUser": 10001, "runAsGroup": 10001},
                        "volume_mounts": [{"name": "data", "mount_path": "/var/lib/grafana"}],
                    }],
                },
            },
        }
        accepted_change = {
            "type": "patch_workload_runtime_security",
            "namespace": "k8s-agent",
            "workload_type": "Deployment",
            "workload_name": "k8s-agent-grafana",
            "container_name": "grafana",
            "permission_recovery_stage": "nonroot_group",
            "patch": {"spec": {"template": {"spec": {
                "securityContext": {
                    "runAsUser": 10001,
                    "runAsGroup": 10001,
                    "runAsNonRoot": True,
                    "fsGroup": 10001,
                    "supplementalGroups": [10001],
                },
                "containers": [{
                    "name": "grafana",
                    "securityContext": {
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "runAsNonRoot": True,
                    },
                }],
            }}}},
        }

        server._record_permission_stage_attempt(
            plan,
            accepted_change,
            outcome="patched",
        )
        next_plan = server._permission_recovery_followup(plan)

        self.assertEqual(next_plan["permission_recovery_stage"], "root")
        self.assertEqual(
            plan["_permission_stage_receipts"][0]["stage"],
            "nonroot_group",
        )
        root_sc = next_plan["changes"][0]["patch"]["spec"]["template"]["spec"]
        self.assertEqual(root_sc["securityContext"]["runAsUser"], 0)
        self.assertEqual(root_sc["securityContext"]["runAsGroup"], 0)
        self.assertEqual(root_sc["securityContext"]["fsGroup"], 0)
        self.assertIs(root_sc["securityContext"]["runAsNonRoot"], False)

    def test_declared_workload_patch_must_match_live_yaml_before_recovery(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "changes": [{
                "type": "patch_workload_runtime_security",
                "workload_type": "Deployment",
                "workload_name": "grafana",
                "patch": {"spec": {"template": {"spec": {
                    "securityContext": {"fsGroup": 0, "runAsUser": 0},
                    "containers": [{
                        "name": "grafana",
                        "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
                    }],
                }}}},
            }],
        }
        stale_workload = {
            "spec": {"template": {"spec": {
                "securityContext": {"fsGroup": 472, "runAsUser": 472},
                "containers": [{
                    "name": "grafana",
                    "securityContext": {"runAsUser": 472, "runAsNonRoot": True},
                }],
            }}},
        }
        applied, proof = server._live_workload_postcondition(plan, stale_workload)
        self.assertFalse(applied)
        self.assertIn("fsGroup", proof)
        recovered_workload = {
            "spec": {"template": {"spec": {
                "securityContext": {"fsGroup": 0, "runAsUser": 0},
                "containers": [{
                    "name": "grafana",
                    "image": "example.invalid/grafana:latest",
                    "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
                }],
            }}},
        }
        applied, proof = server._live_workload_postcondition(plan, recovered_workload)
        self.assertTrue(applied, proof)

    def test_natural_language_write_recovery_criteria_use_fresh_logs(self):
        criteria = server._assess_recovery_criteria(
            {
                "namespace": "default",
                "target": "Deployment/writer",
                "success_criteria": [
                    "new_pod_ready",
                    "file_create_succeeds",
                    "permission_error_absent",
                ],
                "changes": [],
            },
            {"recovered": True},
            {
                "pod": {"name": "writer-new", "ready": True, "restart_count": 0},
                "events": [],
                "logs": {"writer": {"current": "FILE_CREATE_OK database-ready"}},
                "workload": {
                    "metadata": {"generation": 3},
                    "spec": {"replicas": 1},
                    "status": {
                        "observedGeneration": 3,
                        "updatedReplicas": 1,
                        "readyReplicas": 1,
                    },
                },
            },
        )
        self.assertTrue(criteria["passed"], criteria)
        self.assertEqual(criteria["not_evaluated"], [])

    def test_99_percent_slo_has_one_percent_budget(self):
        budget = evaluate_error_budget({
            "id": "svc",
            "service": "svc",
            "target_percent": 99,
            "window_days": 30,
            "observed_minutes": 43200,
            "downtime_minutes": 216,
        })
        self.assertEqual(budget["error_budget_percent"], 1.0)
        self.assertEqual(budget["allowed_downtime_minutes"], 432.0)
        self.assertAlmostEqual(budget["consumed_ratio"], 0.5)
        self.assertFalse(budget["freeze_changes"])

    def test_exhausted_budget_freezes_changes(self):
        budget = evaluate_error_budget({
            "service": "svc",
            "target_percent": 99,
            "window_days": 30,
            "observed_minutes": 43200,
            "downtime_minutes": 500,
        })
        self.assertEqual(budget["state"], "exhausted")
        self.assertTrue(budget["freeze_changes"])

    def test_store_persists_objective_and_release_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reliability.json"
            store = ReliabilityStore(str(path))
            saved = store.upsert_objective({"service": "checkout", "target_percent": 99.9, "window_days": 30})
            release = store.add_release({"service": "checkout", "status": "awaiting_approval"})
            restored = ReliabilityStore(str(path))
            self.assertEqual(restored.objectives()[0]["id"], saved["id"])
            self.assertEqual(restored.release(release["id"])["status"], "awaiting_approval")

    def test_store_falls_back_when_primary_path_is_not_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            fallback = Path(directory) / "fallback" / "reliability.json"
            old = os.environ.get("RELIABILITY_STORE_FALLBACK_PATH")
            os.environ["RELIABILITY_STORE_FALLBACK_PATH"] = str(fallback)
            try:
                store = ReliabilityStore("/proc/flawless/reliability.json")
                release = store.add_release({"service": "checkout", "status": "awaiting_approval"})
                self.assertEqual(store.path, fallback)
                self.assertEqual(ReliabilityStore(str(fallback)).release(release["id"])["service"], "checkout")
            finally:
                if old is None:
                    os.environ.pop("RELIABILITY_STORE_FALLBACK_PATH", None)
                else:
                    os.environ["RELIABILITY_STORE_FALLBACK_PATH"] = old

    def test_store_uses_emergency_path_when_primary_and_fallback_are_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            emergency = Path(directory) / "emergency" / "reliability.json"
            old_fallback = os.environ.get("RELIABILITY_STORE_FALLBACK_PATH")
            old_emergency = os.environ.get("RELIABILITY_STORE_EMERGENCY_PATH")
            os.environ["RELIABILITY_STORE_FALLBACK_PATH"] = "/proc/flawless-fallback/reliability.json"
            os.environ["RELIABILITY_STORE_EMERGENCY_PATH"] = str(emergency)
            try:
                store = ReliabilityStore("/proc/flawless-primary/reliability.json")
                store.add_release({"service": "checkout", "status": "awaiting_approval"})
                self.assertEqual(store.path, emergency)
                self.assertFalse(store.storage_status()["durable"])
            finally:
                if old_fallback is None:
                    os.environ.pop("RELIABILITY_STORE_FALLBACK_PATH", None)
                else:
                    os.environ["RELIABILITY_STORE_FALLBACK_PATH"] = old_fallback
                if old_emergency is None:
                    os.environ.pop("RELIABILITY_STORE_EMERGENCY_PATH", None)
                else:
                    os.environ["RELIABILITY_STORE_EMERGENCY_PATH"] = old_emergency


class ObservableExecutionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _rollout_pod(name: str, *, replica_set: str = "api-new") -> dict:
        return {
            "metadata": {
                "name": name,
                "namespace": "apps",
                "ownerReferences": [{"kind": "ReplicaSet", "name": replica_set}],
            },
            "spec": {"containers": [{"name": "api", "securityContext": {}}]},
            "status": {
                "phase": "Running",
                "containerStatuses": [{
                    "name": "api",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {}},
                }],
            },
        }

    async def test_managed_kubeconfig_reselects_new_pod_after_rollout(self):
        class KubernetesNotFound(Exception):
            status = 404

        new_pod = self._rollout_pod("api-new-abc")
        plan = {
            "cluster_id": "managed-1",
            "source": "kubeconfig",
            "namespace": "apps",
            "target": "Deployment/api",
            "pod_name": "api-old-xyz",
            "changes": [{
                "namespace": "apps",
                "workload_type": "Deployment",
                "workload_name": "api",
            }],
        }
        registry = MagicMock()
        registry.pod_priority_evidence.side_effect = [
            KubernetesNotFound("(404) pods api-old-xyz not found"),
            {"raw_pod": new_pod, "logs": {"api": {"current": "RECOVERED"}}},
        ]
        registry.namespace_pods.return_value = {
            "replicasets": [{
                "metadata": {
                    "name": "api-new",
                    "ownerReferences": [{"kind": "Deployment", "name": "api"}],
                },
            }],
            "pods": [new_pod],
        }
        with patch.object(server, "CLUSTER_REGISTRY", registry), patch.object(
            server, "_ops_cluster_transport", return_value="kubeconfig"
        ):
            evidence = await server._collect_plan_priority_evidence(plan)
        self.assertEqual(evidence["pod_name"], "api-new-abc")
        self.assertEqual(evidence["superseded_pod_name"], "api-old-xyz")
        self.assertTrue(evidence["pod_lineage"]["reselected_after_rollout"])
        self.assertEqual(evidence["logs"]["api"]["current"], "RECOVERED")
        self.assertEqual(plan["pod_name"], "api-new-abc")

    async def test_managed_kubeconfig_does_not_hide_forbidden_log_access(self):
        class KubernetesForbidden(Exception):
            status = 403

        plan = {
            "cluster_id": "managed-1",
            "source": "kubeconfig",
            "namespace": "apps",
            "target": "Deployment/api",
            "pod_name": "api-current-abc",
            "changes": [{
                "namespace": "apps",
                "workload_type": "Deployment",
                "workload_name": "api",
            }],
        }
        registry = MagicMock()
        registry.pod_priority_evidence.side_effect = KubernetesForbidden("Forbidden")
        with patch.object(server, "CLUSTER_REGISTRY", registry), patch.object(
            server, "_ops_cluster_transport", return_value="kubeconfig"
        ):
            with self.assertRaises(KubernetesForbidden):
                await server._collect_plan_priority_evidence(plan)
        registry.namespace_pods.assert_not_called()

    async def test_rancher_reselects_new_pod_after_rollout(self):
        request = httpx.Request("GET", "https://rancher.example/k8s/clusters/c-1/pods/old")
        response = httpx.Response(404, request=request)
        not_found = httpx.HTTPStatusError("Pod not found", request=request, response=response)
        new_pod = self._rollout_pod("api-new-rancher")
        plan = {
            "cluster_id": "c-1",
            "source": "rancher",
            "namespace": "apps",
            "target": "Deployment/api",
            "pod_name": "api-old-rancher",
            "changes": [{
                "namespace": "apps",
                "workload_type": "Deployment",
                "workload_name": "api",
            }],
        }
        rancher_get = AsyncMock(side_effect=[
            not_found,
            {"items": [{
                "metadata": {
                    "name": "api-new",
                    "ownerReferences": [{"kind": "Deployment", "name": "api"}],
                },
            }]},
            {"items": [new_pod]},
            new_pod,
        ])
        with patch.object(server, "_ops_cluster_transport", return_value="rancher"), patch.object(
            server, "_rancher_k8s_get", rancher_get
        ), patch.object(
            server,
            "_collect_rancher_pod_logs",
            AsyncMock(return_value={"api": {"current": "RECOVERED"}}),
        ):
            evidence = await server._collect_plan_priority_evidence(plan)
        self.assertEqual(evidence["pod_name"], "api-new-rancher")
        self.assertEqual(evidence["superseded_pod_name"], "api-old-rancher")
        self.assertEqual(evidence["transport"], "rancher")
        self.assertEqual(plan["pod_name"], "api-new-rancher")

    async def test_rancher_log_collection_uses_pod_state_before_unstarted_container_log(self):
        pod = {
            "containers": [{
                "name": "grafana",
                "state": "waiting",
                "reason": "ContainerCreating",
                "state_detail": {"message": "containers with unready status"},
                "restart_count": 0,
            }],
        }
        rancher_get = AsyncMock()
        with patch.object(server, "_rancher_k8s_get", rancher_get):
            logs = await server._collect_rancher_pod_logs(
                "cluster-a", "monitoring", "grafana-new", pod,
            )
        rancher_get.assert_not_called()
        self.assertIn("容器尚未启动", logs["grafana"]["current_error"])
        self.assertIn("ContainerCreating", logs["grafana"]["current_error"])
        self.assertEqual(logs["grafana"]["container_state"]["state"], "waiting")

    async def test_rancher_log_http_error_keeps_kubernetes_status_message(self):
        request = httpx.Request("GET", "https://rancher.example/k8s/clusters/cluster-a/log")
        response = httpx.Response(
            400,
            request=request,
            json={"kind": "Status", "message": "container grafana is waiting to start: ContainerCreating"},
        )
        error = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)
        with patch.object(server, "_rancher_k8s_get", AsyncMock(side_effect=error)):
            logs = await server._collect_rancher_pod_logs(
                "cluster-a",
                "monitoring",
                "grafana-new",
                {"containers": [{"name": "grafana", "state": "running", "restart_count": 0}]},
            )
        self.assertEqual(
            logs["grafana"]["current_error"],
            "HTTP 400: container grafana is waiting to start: ContainerCreating",
        )

    async def test_rancher_uses_sibling_crashloop_log_when_selected_pod_has_not_started(self):
        def raw_pod(name: str, phase: str, state: dict, restart_count: int) -> dict:
            return {
                "metadata": {
                    "name": name,
                    "namespace": "monitoring",
                    "ownerReferences": [{"kind": "Deployment", "name": "grafana"}],
                },
                "spec": {"containers": [{"name": "grafana"}]},
                "status": {
                    "phase": phase,
                    "containerStatuses": [{
                        "name": "grafana",
                        "ready": False,
                        "restartCount": restart_count,
                        "state": state,
                    }],
                },
            }

        pending = raw_pod(
            "grafana-new", "Pending", {"waiting": {"reason": "ContainerCreating"}}, 0,
        )
        crashloop = raw_pod(
            "grafana-old", "Running", {"waiting": {"reason": "CrashLoopBackOff"}}, 12,
        )
        rancher_get = AsyncMock(side_effect=[
            pending,
            {"items": []},
            {"items": [pending, crashloop]},
            crashloop,
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "grafana", "namespace": "monitoring"},
                "spec": {
                    "template": {
                        "spec": {
                            "securityContext": {"runAsUser": 472, "runAsGroup": 472},
                            "containers": [{"name": "grafana"}],
                        },
                    },
                },
            },
        ])
        collect_logs = AsyncMock(side_effect=[
            {"grafana": {"current": "", "current_error": "container has not started"}},
            {"grafana": {"current": "GF_PATHS_DATA is not writable", "current_error": ""}},
        ])
        plan = {
            "cluster_id": "cluster-a",
            "source": "rancher",
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "pod_name": "grafana-new",
            "changes": [{
                "namespace": "monitoring",
                "workload_type": "Deployment",
                "workload_name": "grafana",
            }],
        }
        with patch.object(server, "_ops_cluster_transport", return_value="rancher"), patch.object(
            server, "_rancher_k8s_get", rancher_get,
        ), patch.object(server, "_collect_rancher_pod_logs", collect_logs):
            evidence = await server._collect_plan_priority_evidence(plan)
        self.assertEqual(evidence["pod_name"], "grafana-old")
        self.assertEqual(evidence["logs"]["grafana"]["current"], "GF_PATHS_DATA is not writable")
        self.assertTrue(evidence["log_fallback"]["used"])
        self.assertEqual(evidence["log_fallback"]["unavailable_pod_name"], "grafana-new")
        self.assertTrue(evidence["pod_lineage"]["reselected_for_logs"])
        self.assertEqual(evidence["workload"]["kind"], "Deployment")
        self.assertFalse(evidence["workload_error"])

    def test_actionable_direct_evidence_enters_diagnosis_without_global_enrichment(self):
        evidence = {
            "pod": {
                "name": "grafana-old",
                "security_context": {"runAsUser": 472, "runAsGroup": 472},
                "containers": [{"name": "grafana", "restart_count": 12}],
            },
            "logs": {
                "grafana": {
                    "current": "Error: unable to open database file",
                    "current_error": "",
                },
            },
            "workload": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "grafana"},
                "spec": {"template": {"spec": {"containers": [{"name": "grafana"}]}}},
            },
        }
        triage = server.triage_kubernetes_logs(evidence["logs"])
        self.assertTrue(server._priority_evidence_can_start_diagnosis(evidence, triage))
        self.assertTrue(
            server._priority_evidence_can_start_diagnosis(
                {"pod": evidence["pod"], "logs": evidence["logs"], "workload": {}},
                triage,
            )
        )

        pending_pvc = {
            "pod": evidence["pod"],
            "logs": {},
            "workload": evidence["workload"],
        }
        self.assertFalse(
            server._priority_evidence_can_start_diagnosis(
                pending_pvc,
                server.triage_kubernetes_logs({}),
            )
        )

    async def test_actionable_logs_do_not_wait_for_unrelated_deep_collectors(self):
        evidence = {
            "namespace": "monitoring",
            "pod_name": "grafana-old",
            "pod": {
                "name": "grafana-old",
                "security_context": {"runAsUser": 472, "runAsGroup": 472},
                "containers": [{"name": "grafana", "restart_count": 12}],
            },
            "logs": {
                "grafana": {
                    "current": "Error: unable to open database file",
                    "current_error": "",
                },
            },
            "workload": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "grafana", "namespace": "monitoring"},
                "spec": {"template": {"spec": {"containers": [{"name": "grafana"}]}}},
            },
        }
        events = []

        async def progress(stage, _message, **_extra):
            events.append(stage)

        deep_collector = AsyncMock(side_effect=AssertionError("deep collector must be deferred"))
        plan = {
            "title": "diagnose grafana",
            "target": "Deployment/grafana",
            "namespace": "monitoring",
            "cluster_id": "cluster-a",
            "source": "rancher",
            "summary": "unable to open database file",
            "steps": [],
            "changes": [],
        }
        with patch.object(server, "_ops_release_gate", return_value={"allowed": True}), patch.object(
            server, "_collect_plan_priority_evidence", AsyncMock(return_value=evidence),
        ), patch.object(
            server, "_collect_plan_deep_evidence", deep_collector,
        ), patch.object(
            server, "_attach_operator_skills_to_plan", side_effect=lambda current, *_args, **_kwargs: current,
        ), patch.object(
            server,
            "_probe_plan_recovery",
            AsyncMock(return_value={"status": "failed", "recovered": False, "message": "still failing"}),
        ), patch.object(
            server, "_evidence_based_replan", AsyncMock(return_value=[]),
        ):
            result = await server._execute_ops_plan_once(
                plan,
                progress=progress,
                summarize=False,
            )

        deep_collector.assert_not_awaited()
        self.assertIn("collecting_evidence_done", events)
        self.assertIn("diagnosing", events)
        self.assertIn("root_cause_diagnosing", events)
        self.assertIn("diagnosis_done", events)
        self.assertEqual(result["status"], "diagnostic_completed")

    async def test_http_deep_diagnosis_reaches_permission_change_approval(self):
        """Exercise the browser's real POST -> background job -> GET polling path.

        A shallow chat plan initially matches the generic CrashLoop Skill. Live
        logs then prove a writable-path fault and generate the executable volume
        permission Skill. The second evidence pass must retain that executable
        Skill instead of being displaced by the generic router and looping at
        ``collecting_evidence_done`` forever.
        """
        evidence = {
            "namespace": "monitoring",
            "pod_name": "grafana-abc",
            "pod": {
                "name": "grafana-abc",
                "namespace": "monitoring",
                "ready": False,
                "phase": "Running",
                "restart_count": 5,
                "workload": {"kind": "Deployment", "name": "grafana"},
                "security_context": {
                    "runAsUser": 472,
                    "runAsGroup": 472,
                    "runAsNonRoot": True,
                    "fsGroup": 472,
                    "supplementalGroups": [472],
                    "fsGroupChangePolicy": "OnRootMismatch",
                },
                "containers": [{
                    "name": "grafana",
                    "ready": False,
                    "state": "waiting",
                    "reason": "CrashLoopBackOff",
                    "security_context": {
                        "runAsUser": 472,
                        "runAsGroup": 472,
                        "runAsNonRoot": True,
                    },
                    "volume_mounts": [{"name": "data", "mount_path": "/var/lib/grafana"}],
                }],
            },
            "events": [],
            "logs": {
                "grafana": {
                    "current": (
                        "GF_PATHS_DATA=/var/lib/grafana is not writable\n"
                        "Error: unable to open database file"
                    ),
                },
            },
            "workload": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "grafana", "namespace": "monitoring"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "securityContext": {
                                "runAsUser": 472,
                                "runAsGroup": 472,
                                "runAsNonRoot": True,
                                "fsGroup": 472,
                                "supplementalGroups": [472],
                                "fsGroupChangePolicy": "OnRootMismatch",
                            },
                            "containers": [{
                                "name": "grafana",
                                "securityContext": {
                                    "runAsUser": 472,
                                    "runAsGroup": 472,
                                    "runAsNonRoot": True,
                                },
                                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/grafana"}],
                            }],
                            "volumes": [{
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": "grafana-data"},
                            }],
                        },
                    },
                },
                "status": {"readyReplicas": 0},
            },
            "storage": [{"pvc": "grafana-data", "pvc_phase": "Bound", "pv": "grafana-pv"}],
            "services": [],
        }
        plan = {
            "title": "深度诊断 Grafana",
            "target": "Deployment/grafana",
            "namespace": "monitoring",
            "cluster_id": "c-test",
            "cluster": "test",
            "source": "rancher",
            "summary": "Pod CrashLoopBackOff，读取日志定位根因",
            "steps": [
                {"title": "读取 ERROR 日志", "probe": "current_logs"},
                {"title": "核对安全上下文", "probe": "pod_security_context"},
                {"title": "追踪 CMDB 依赖链", "probe": "dependency_topology", "optional": True},
            ],
            "changes": [],
        }
        # Reproduce the production timing bug: the first collection only knows
        # CrashLoopBackOff, while the targeted Skill refresh obtains logs and
        # the owning Deployment on the second pass.
        shallow_evidence = {
            "namespace": "monitoring",
            "pod_name": "grafana-abc",
            "pod": {
                "name": "grafana-abc",
                "namespace": "monitoring",
                "ready": False,
                "containers": [{
                    "name": "grafana",
                    "reason": "CrashLoopBackOff",
                    "restart_count": 5,
                }],
            },
            "logs": {},
        }
        simulated_cluster = {
            "workload_security": copy.deepcopy(
                evidence["workload"]["spec"]["template"]["spec"]["securityContext"]
            ),
            "container_security": copy.deepcopy(
                evidence["workload"]["spec"]["template"]["spec"]["containers"][0]["securityContext"]
            ),
            "new_pod_logs": evidence["logs"]["grafana"]["current"],
            "new_pod_ready": False,
        }

        async def apply_simulated_change(change, _plan):
            patched_spec = (((change.get("patch") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
            simulated_cluster["workload_security"] = copy.deepcopy(
                patched_spec.get("securityContext") or {}
            )
            simulated_cluster["container_security"] = copy.deepcopy(
                ((patched_spec.get("containers") or [{}])[0]).get("securityContext") or {}
            )
            simulated_cluster["new_pod_logs"] = "FILE_CREATE_OK database-ready"
            simulated_cluster["new_pod_ready"] = True
            return {
                "status": "completed",
                "change": copy.deepcopy(change),
                "result": {"accepted": True, "operation": "patched"},
            }

        async def verify_simulated_recovery(_plan, _results, _cancel_event):
            self.assertEqual(simulated_cluster["workload_security"]["runAsUser"], 0)
            self.assertEqual(simulated_cluster["workload_security"]["runAsGroup"], 0)
            self.assertEqual(simulated_cluster["workload_security"]["fsGroup"], 0)
            self.assertEqual(simulated_cluster["workload_security"]["supplementalGroups"], [0])
            self.assertIs(simulated_cluster["workload_security"]["runAsNonRoot"], False)
            self.assertEqual(simulated_cluster["container_security"]["runAsUser"], 0)
            self.assertIs(simulated_cluster["container_security"]["runAsNonRoot"], False)
            self.assertTrue(simulated_cluster["new_pod_ready"])
            self.assertNotIn("not writable", simulated_cluster["new_pod_logs"])
            self.assertNotIn("unable to open database", simulated_cluster["new_pod_logs"])
            return {
                "status": "completed",
                "recovered": True,
                "message": "新 Pod 已 Ready，错误日志消失，重启次数稳定。",
                "proof": simulated_cluster["new_pod_logs"],
            }
        job_id = ""
        transport = httpx.ASGITransport(app=server.app)
        with patch.object(server, "_ops_release_gate", return_value={"allowed": True}), patch.object(
            server,
            "_collect_plan_priority_evidence",
            AsyncMock(side_effect=[shallow_evidence, evidence]),
        ), patch.object(
            server,
            "_collect_plan_deep_evidence",
            AsyncMock(side_effect=[shallow_evidence, evidence]),
        ), patch.object(
            server,
            "_probe_plan_recovery",
            AsyncMock(return_value={"status": "completed", "recovered": False, "message": "still failing"}),
        ), patch.object(
            server,
            "_execute_change",
            AsyncMock(side_effect=apply_simulated_change),
        ), patch.object(
            server,
            "_verify_plan_recovery",
            AsyncMock(side_effect=verify_simulated_recovery),
        ), patch.object(
            server,
            "_llm_ops_summary",
            AsyncMock(return_value={
                "source": "test",
                "content": "权限变更已执行并验证恢复。",
                "followup_plans": [],
            }),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/ops/jobs", json={
                    "plan": plan,
                    "confirm": True,
                    "autonomous": False,
                    "operator_force_execute": True,
                })
                self.assertEqual(response.status_code, 200, response.text)
                job_id = response.json()["id"]
                job = response.json()
                for _ in range(150):
                    await asyncio.sleep(0.02)
                    job_response = await client.get(f"/api/ops/jobs/{job_id}")
                    self.assertEqual(job_response.status_code, 200, job_response.text)
                    job = job_response.json()
                    if job.get("status") in {"awaiting_approval", "failed", "completed"}:
                        break
                self.assertEqual(job["status"], "awaiting_approval", job)
                self.assertEqual(job["stage"], "awaiting_change_approval")
                self.assertEqual(
                    (job.get("pending_approval") or {}).get("action"),
                    "patch_workload_runtime_security",
                )
                pending_spec = ((((
                    (job.get("pending_approval") or {}).get("patch") or {}
                ).get("spec") or {}).get("template") or {}).get("spec") or {})
                self.assertEqual(
                    (pending_spec.get("securityContext") or {}).get("runAsUser"),
                    0,
                )
                self.assertIs(
                    (pending_spec.get("securityContext") or {}).get("runAsNonRoot"),
                    False,
                )
                self.assertEqual(
                    (pending_spec.get("securityContext") or {}).get("fsGroup"),
                    0,
                )
                stages = [event.get("stage") for event in job.get("events") or []]
                self.assertIn("diagnosing", stages)
                self.assertIn("diagnosis_done", stages)
                self.assertIn("skill_evidence_refreshed", stages)
                self.assertIn("root_cause_diagnosed", stages)
                self.assertIn("awaiting_change_approval", stages)
                pending = job["pending_approval"]
                approval_response = await client.post(
                    f"/api/ops/jobs/{job_id}/approve-step",
                    json={
                        "change_index": pending["change_index"],
                        "approval_id": pending["approval_id"],
                        "change_fingerprint": pending["change_fingerprint"],
                        "confirm": True,
                        "comment": "HTTP E2E confirms the exact generated patch",
                    },
                )
                self.assertEqual(approval_response.status_code, 200, approval_response.text)
                for _ in range(150):
                    await asyncio.sleep(0.02)
                    job = (await client.get(f"/api/ops/jobs/{job_id}")).json()
                    if job.get("status") in {"completed", "failed", "cancelled"}:
                        break
                self.assertEqual(job["status"], "completed")
                self.assertEqual(job["stage"], "recovered")
                self.assertTrue(((job.get("result") or {}).get("verification") or {}).get("recovered"))
                stages = [event.get("stage") for event in job.get("events") or []]
                for expected_stage in (
                    "change_approved",
                    "change_start",
                    "change_done",
                    "verifying",
                    "verification_done",
                    "recovered",
                ):
                    self.assertIn(expected_stage, stages)
        if job_id:
            cancel_event = server.OPS_JOB_CANCEL_EVENTS.get(job_id)
            if cancel_event:
                cancel_event.set()
            task = server.OPS_JOB_TASKS.get(job_id)
            if task:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            server.OPS_JOBS.pop(job_id, None)

    def _request(self):
        return Request({
            "type": "http",
            "method": "POST",
            "path": "/api/ops/jobs/ops-test/approve-step",
            "headers": [(b"x-auth-request-user", b"unit-test")],
            "client": ("127.0.0.1", 12345),
        })

    async def test_stage_emits_heartbeat(self):
        heartbeats = []

        async def slow_result():
            await asyncio.sleep(0.08)
            return "ok"

        result = await run_with_heartbeat(
            slow_result(),
            stage="probe",
            timeout_seconds=1,
            heartbeat_seconds=0.02,
            on_heartbeat=lambda elapsed, remaining: self._record(heartbeats, elapsed, remaining),
        )
        self.assertEqual(result, "ok")
        self.assertGreaterEqual(len(heartbeats), 1)

    async def test_stage_hard_timeout(self):
        async def never_finishes():
            await asyncio.sleep(5)

        with self.assertRaises(StageTimeoutError):
            await run_with_heartbeat(
                never_finishes(),
                stage="probe",
                timeout_seconds=0.05,
                heartbeat_seconds=0.01,
            )

    async def test_stage_timeout_is_not_defeated_by_cancellation_resistant_child(self):
        async def resists_one_cancel():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)

        started = time.monotonic()
        with self.assertRaises(StageTimeoutError):
            await run_with_heartbeat(
                resists_one_cancel(),
                stage="resistant",
                timeout_seconds=0.05,
                heartbeat_seconds=0.01,
                cleanup_grace_seconds=0.01,
            )
        self.assertLess(time.monotonic() - started, 0.15)

    async def test_slow_heartbeat_sink_cannot_block_stage_completion(self):
        async def work():
            await asyncio.sleep(0.06)
            return "recovered"

        async def blocked_sink(_elapsed, _remaining):
            await asyncio.sleep(5)

        started = time.monotonic()
        result = await run_with_heartbeat(
            work(),
            stage="repair",
            timeout_seconds=0.3,
            heartbeat_seconds=0.01,
            heartbeat_timeout_seconds=0.01,
            cleanup_grace_seconds=0.01,
            on_heartbeat=blocked_sink,
        )
        self.assertEqual(result, "recovered")
        self.assertLess(time.monotonic() - started, 0.2)

    async def test_permission_evidence_bypasses_generic_runbook_and_llm(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "CrashLoopBackOff",
            "_runtime_evidence": {
                "logs": {
                    "grafana": {
                        "current": (
                            "GF_PATHS_DATA='/var/lib/grafana' is not writable\n"
                            "failed to open database file /var/lib/grafana/grafana.db"
                        )
                    }
                },
                "pod": {
                    "name": "grafana-bad",
                    "security_context": {"runAsUser": 472, "runAsGroup": 472},
                    "containers": [{
                        "name": "grafana",
                        "security_context": {"runAsUser": 472, "runAsGroup": 472},
                        "volume_mounts": [{"name": "data", "mount_path": "/var/lib/grafana"}],
                    }],
                },
                "workload": {
                    "kind": "Deployment",
                    "metadata": {"name": "grafana", "namespace": "monitoring"},
                    "spec": {"template": {"spec": {
                        "securityContext": {"runAsUser": 472, "runAsGroup": 472},
                    }}},
                },
            },
        }
        generic = {
            "runbook_id": "generic_crashloop",
            "reason": "container repeatedly exits",
            "hypotheses": [{"confidence": 0.81}],
            "changes": [],
            "steps": [],
        }
        materialized = {
            **plan,
            "selected_skill_id": "volume-write-permission-recovery",
            "permission_recovery_stage": "nonroot_group",
            "changes": [{"type": "patch_workload_runtime_security"}],
        }
        with patch.object(server, "build_remediation_plan", return_value=generic), patch.object(
            server,
            "_materialize_executable_skill",
            return_value=materialized,
        ) as skill_runtime, patch(
            "agents.llm_client.get_llm",
        ) as llm_factory:
            replans = await server._evidence_based_replan(plan, [], set(), include_llm=True)
        self.assertEqual(replans[0]["selected_skill_id"], "volume-write-permission-recovery")
        self.assertEqual(plan["_runtime_replan"]["runbook_id"], "storage_permission")
        skill_runtime.assert_called_once()
        llm_factory.assert_not_called()

    async def test_llm_return_and_skill_router_are_independently_observable(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "container cannot open its database file",
            "_runtime_evidence": {
                "logs": {"grafana": {"current": "database initialization failed"}},
                "workload": {"kind": "Deployment", "metadata": {"name": "grafana"}},
            },
        }
        engine_plan = {
            "runbook_id": "generic_crashloop",
            "reason": "container repeatedly exits",
            "hypotheses": [{"confidence": 0.51}],
            "changes": [],
            "steps": [],
        }
        llm_payload = {
            "root_cause": "runtime configuration mismatch",
            "selected_skill_id": "workload-runtime-recovery",
            "secondary_skill_ids": [],
            "skill_dependencies": [],
            "reason": "live evidence supports one workload patch",
            "changes": [{"type": "patch_workload"}],
        }
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=json.dumps(llm_payload))
        normalized_change = {
            "type": "patch_workload",
            "namespace": "monitoring",
            "workload_type": "Deployment",
            "workload_name": "grafana",
            "patch": {"spec": {"template": {"metadata": {"annotations": {"repair": "approved"}}}}},
            "risk": "high",
        }
        stages = []

        async def progress(stage, _message, **_extra):
            stages.append(stage)

        def attach_skill(candidate, _signal, **_kwargs):
            candidate["selected_skill_id"] = "workload-runtime-recovery"
            return candidate

        with patch.object(server, "build_remediation_plan", return_value=engine_plan), patch(
            "agents.llm_client.get_llm",
            return_value=llm,
        ), patch.object(
            server,
            "_normalize_planner_change",
            return_value=(normalized_change, ""),
        ), patch.object(
            server,
            "_attach_operator_skills_to_plan",
            side_effect=attach_skill,
        ):
            replans = await server._evidence_based_replan(
                plan,
                [],
                set(),
                include_llm=True,
                progress=progress,
            )

        self.assertEqual(replans[0]["selected_skill_id"], "workload-runtime-recovery")
        self.assertLess(stages.index("llm_planning"), stages.index("llm_planning_done"))
        self.assertLess(stages.index("llm_planning_done"), stages.index("skill_router_processing"))
        self.assertLess(stages.index("skill_router_processing"), stages.index("skill_router_done"))

    async def test_returned_llm_cannot_be_hidden_by_stuck_skill_router(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "container cannot open its database file",
            "_runtime_evidence": {"logs": {"grafana": {"current": "database initialization failed"}}},
        }
        engine_plan = {
            "runbook_id": "generic_crashloop",
            "reason": "container repeatedly exits",
            "hypotheses": [{"confidence": 0.51}],
            "changes": [],
            "steps": [],
        }
        llm_payload = {
            "root_cause": "runtime configuration mismatch",
            "selected_skill_id": "workload-runtime-recovery",
            "changes": [{"type": "patch_workload"}],
        }
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content=json.dumps(llm_payload))
        stages = []

        async def progress(stage, _message, **_extra):
            stages.append(stage)

        def blocked_router(candidate, _signal, **_kwargs):
            time.sleep(0.2)
            return candidate

        with patch.object(server, "build_remediation_plan", return_value=engine_plan), patch(
            "agents.llm_client.get_llm",
            return_value=llm,
        ), patch.object(
            server,
            "_normalize_planner_change",
            return_value=({"type": "patch_workload", "risk": "high"}, ""),
        ), patch.object(
            server,
            "_attach_operator_skills_to_plan",
            side_effect=blocked_router,
        ), patch.dict(
            os.environ,
            {"OPS_SKILL_ROUTER_TIMEOUT_SECONDS": "0.05"},
        ):
            started = time.monotonic()
            replans = await server._evidence_based_replan(
                plan,
                [],
                set(),
                include_llm=True,
                progress=progress,
            )

        self.assertEqual(replans, [])
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertIn("llm_planning_done", stages)
        self.assertIn("skill_router_timeout", stages)

    async def test_ops_progress_does_not_wait_for_record_store_io(self):
        job_id = "ops-nonblocking-store"
        server.OPS_JOBS[job_id] = {
            "id": job_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "events": [],
        }

        def slow_persist(_snapshot=None):
            time.sleep(0.2)

        old_task = server.OPS_JOB_PERSIST_TASK
        old_dirty = server.OPS_JOB_PERSIST_DIRTY
        server.OPS_JOB_PERSIST_TASK = None
        server.OPS_JOB_PERSIST_DIRTY = False
        try:
            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                server,
                "OPS_JOB_STORE_PATH",
                Path(temp_dir) / "ops-jobs.json",
            ), patch.object(server, "_persist_ops_jobs", side_effect=slow_persist), patch.dict(
                os.environ,
                {"OPS_JOB_STORE_DEBOUNCE_SECONDS": "0.01"},
            ):
                started = time.monotonic()
                await server._append_ops_job_event(job_id, "diagnosing", "根因诊断")
                self.assertLess(time.monotonic() - started, 0.05)
                task = server.OPS_JOB_PERSIST_TASK
                if task:
                    await asyncio.wait_for(task, timeout=1)
        finally:
            server.OPS_JOBS.pop(job_id, None)
            server.OPS_JOB_PERSIST_TASK = old_task
            server.OPS_JOB_PERSIST_DIRTY = old_dirty

    async def test_job_poll_resumes_orphaned_running_diagnosis(self):
        job_id = "ops-orphaned-root-cause"
        server.OPS_JOBS[job_id] = {
            "id": job_id,
            "status": "running",
            "stage": "root_cause_diagnosing",
            "autonomous": False,
            "events": [],
            "plan": {
                "namespace": "monitoring",
                "target": "Deployment/grafana",
                "steps": [{"id": "current_logs", "title": "read logs"}],
                "changes": [],
                "high_risk_confirmed": True,
            },
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        started = asyncio.Event()

        async def recovered_worker(_job_id, recovered_plan, _autonomous, cancel_event):
            self.assertFalse(recovered_plan["high_risk_confirmed"])
            started.set()
            await cancel_event.wait()

        try:
            with patch.object(server, "_run_ops_job", side_effect=recovered_worker):
                public = await server.get_ops_job(job_id)
                await asyncio.wait_for(started.wait(), timeout=0.2)
            self.assertEqual(public["status"], "resume_pending")
            self.assertEqual(public["stage"], "resume_pending")
            self.assertIn(job_id, server.OPS_JOB_TASKS)
            self.assertIn(job_id, server.OPS_JOB_CANCEL_EVENTS)
        finally:
            event = server.OPS_JOB_CANCEL_EVENTS.pop(job_id, None)
            if event:
                event.set()
            task = server.OPS_JOB_TASKS.pop(job_id, None)
            if task:
                await asyncio.wait_for(task, timeout=0.2)
            server.OPS_JOBS.pop(job_id, None)

    async def test_sre_diagnosis_timeout_falls_back_without_blocking_event_loop(self):
        from agents import sre_graph

        class BlockingLLM:
            model_name = "blocked-model"
            profile_id = "timeout-test"

            def invoke(self, _prompt, **_kwargs):
                time.sleep(0.2)
                raise RuntimeError("late model failure")

        state = {
            "alert": {
                "alert_name": "KubePodCrashLooping",
                "namespace": "monitoring",
                "deployment": "grafana",
            },
            "k8s_context": {
                "pods": {"pods": []},
                "events": {"events": []},
                "logs": {
                    "grafana": {
                        "current": (
                            "GF_PATHS_DATA='/var/lib/grafana' is not writable; "
                            "unable to open database file"
                        )
                    }
                },
                "pod": {
                    "containers": [{
                        "name": "grafana",
                        "security_context": {"runAsUser": 472, "runAsNonRoot": True},
                        "volume_mounts": [{"mount_path": "/var/lib/grafana"}],
                    }]
                },
            },
        }
        with patch.object(sre_graph, "get_llm", return_value=BlockingLLM()), patch.dict(
            os.environ,
            {"SRE_DIAGNOSIS_TIMEOUT_SECONDS": "0.05"},
        ):
            started = time.monotonic()
            result = await sre_graph.diagnose(state)
        self.assertLess(time.monotonic() - started, 0.15)
        diagnosis = result["diagnosis"]
        self.assertEqual(diagnosis["diagnosis_metadata"]["source"], "fallback")
        self.assertIn("UID/GID", diagnosis["root_cause"])

    async def test_recovery_verification_stops_immediately_on_terminal_failure(self):
        terminal = {
            "status": "completed",
            "recovered": False,
            "message": "still failing",
            "terminal_unresolved": [{"name": "grafana-abc", "category": "storage_config", "phase": "Running"}],
        }
        old_grace = os.environ.get("OPS_VERIFY_INITIAL_GRACE_SECONDS")
        os.environ["OPS_VERIFY_INITIAL_GRACE_SECONDS"] = "0"
        try:
            with patch.object(server, "_probe_plan_recovery", AsyncMock(return_value=terminal)) as probe:
                result = await asyncio.wait_for(
                    server._verify_plan_recovery({"changes": [{"type": "patch_workload"}]}, [{"status": "completed"}]),
                    timeout=0.05,
                )
            self.assertEqual(result["status"], "needs_followup")
            self.assertEqual(probe.await_count, 1)
        finally:
            if old_grace is None:
                os.environ.pop("OPS_VERIFY_INITIAL_GRACE_SECONDS", None)
            else:
                os.environ["OPS_VERIFY_INITIAL_GRACE_SECONDS"] = old_grace

    async def test_recovery_probe_ignores_superseded_broken_replicaset(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "changes": [{"type": "patch_workload_runtime_security"}],
        }
        pods = {"pods": [
            {
                "name": "grafana-old",
                "namespace": "monitoring",
                "workload_kind": "Deployment",
                "workload_name": "grafana",
                "labels": {"pod-template-hash": "old"},
                "created_at": "2026-08-04T01:00:00Z",
                "phase": "Running",
                "ready": False,
                "restart_count": 12,
                "containers": [{"name": "grafana", "reason": "CrashLoopBackOff"}],
            },
            {
                "name": "grafana-new",
                "namespace": "monitoring",
                "workload_kind": "Deployment",
                "workload_name": "grafana",
                "labels": {"pod-template-hash": "new"},
                "created_at": "2026-08-04T01:01:00Z",
                "phase": "Running",
                "ready": True,
                "restart_count": 0,
                "containers": [{"name": "grafana", "ready": True, "state": "running"}],
            },
        ]}
        with patch.object(server, "_call_mcp_tool", AsyncMock(return_value=pods)):
            result = await server._probe_plan_recovery(plan, [{"status": "completed"}])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["recovered_pods"], ["grafana-new"])
        self.assertEqual(result["superseded_pods_ignored"], ["grafana-old"])

    async def test_recovery_probe_does_not_hide_broken_new_replicaset(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "changes": [{"type": "patch_workload_runtime_security"}],
        }
        pods = {"pods": [
            {
                "name": "grafana-old",
                "namespace": "monitoring",
                "workload_kind": "Deployment",
                "workload_name": "grafana",
                "labels": {"pod-template-hash": "old"},
                "created_at": "2026-08-04T01:00:00Z",
                "phase": "Running",
                "ready": True,
                "containers": [{"name": "grafana", "ready": True, "state": "running"}],
            },
            {
                "name": "grafana-new",
                "namespace": "monitoring",
                "workload_kind": "Deployment",
                "workload_name": "grafana",
                "labels": {"pod-template-hash": "new"},
                "created_at": "2026-08-04T01:01:00Z",
                "phase": "Running",
                "ready": False,
                "restart_count": 3,
                "containers": [{"name": "grafana", "reason": "CrashLoopBackOff"}],
            },
        ]}
        with patch.object(server, "_call_mcp_tool", AsyncMock(return_value=pods)):
            result = await server._probe_plan_recovery(plan, [{"status": "completed"}])
        self.assertFalse(result["recovered"])
        self.assertEqual(result["unresolved"][0]["name"], "grafana-new")

    async def test_recovery_verification_closes_from_new_pod_priority_evidence(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "changes": [{"type": "patch_workload_runtime_security"}],
            "success_criteria": [
                "pod_ready",
                "restart_count_stable",
                "events_no_new_backoff",
                "write_errors_absent",
            ],
            "evidence": {"pod": {"restart_count": 12}},
        }
        probe = {
            "status": "completed",
            "recovered": True,
            "recovered_pods": ["grafana-new"],
            "message": "ready",
        }
        fresh = {
            "pod": {
                "name": "grafana-new",
                "ready": True,
                "phase": "Running",
                "restart_count": 0,
            },
            "logs": {"grafana": {"current": "Grafana is ready", "previous": ""}},
            "events": [],
            "workload": {
                "metadata": {"generation": 2},
                "spec": {"replicas": 1},
                "status": {
                    "observedGeneration": 2,
                    "updatedReplicas": 1,
                    "readyReplicas": 1,
                },
            },
        }
        with patch.dict(os.environ, {
            "OPS_VERIFY_INITIAL_GRACE_SECONDS": "0",
            "OPS_RECOVERY_STABILITY_SECONDS": "0",
        }), patch.object(
            server, "_probe_plan_recovery", AsyncMock(return_value=probe),
        ), patch.object(
            server, "_collect_plan_priority_evidence", AsyncMock(return_value=fresh),
        ) as priority, patch.object(
            server, "_collect_plan_deep_evidence", AsyncMock(),
        ) as deep:
            result = await server._verify_plan_recovery(plan, [{"status": "completed"}])
        self.assertTrue(result["recovered"])
        self.assertTrue(result["criteria"]["passed"])
        priority.assert_awaited_once()
        deep.assert_not_awaited()

    async def test_configmap_match_does_not_close_incident_while_workload_pod_is_broken(self):
        plan = {
            "namespace": "prod",
            "target": "Deployment/orders",
            "changes": [{
                "type": "create_configmap",
                "namespace": "prod",
                "configmap_name": "orders-runtime",
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "orders-runtime", "namespace": "prod"},
                    "data": {"CACHE_DIR": "/data/cache"},
                },
            }],
        }
        resource_state = {
            "kind": "ConfigMap",
            "name": "orders-runtime",
            "namespace": "prod",
            "data_keys": ["CACHE_DIR"],
        }
        broken_pods = {"pods": [{
            "name": "orders-abc",
            "namespace": "prod",
            "workload_kind": "Deployment",
            "workload_name": "orders",
            "ready": False,
            "phase": "Running",
            "restart_count": 8,
            "containers": [{
                "name": "orders",
                "ready": False,
                "state": "waiting",
                "reason": "CrashLoopBackOff",
                "state_detail": {"message": "mkdir /data/cache: permission denied"},
            }],
        }]}
        with patch.object(server, "_call_mcp_tool", AsyncMock(side_effect=[resource_state, broken_pods])):
            verification = await server._probe_plan_recovery(plan, [{"status": "completed"}])
        self.assertFalse(verification["recovered"])
        self.assertTrue(verification["resource_verification"]["recovered"])
        self.assertEqual(verification["unresolved"][0]["category"], "crashloop")

    async def test_resource_postcondition_without_workload_is_not_business_recovery(self):
        plan = {
            "namespace": "prod",
            "target": "ConfigMap/orders-runtime",
            "changes": [{
                "type": "create_configmap",
                "namespace": "prod",
                "configmap_name": "orders-runtime",
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "orders-runtime", "namespace": "prod"},
                    "data": {"CACHE_DIR": "/data/cache"},
                },
            }],
        }
        with patch.object(server, "_call_mcp_tool", AsyncMock(return_value={
            "kind": "ConfigMap",
            "name": "orders-runtime",
            "namespace": "prod",
            "data_keys": ["CACHE_DIR"],
        })):
            verification = await server._probe_plan_recovery(plan, [{"status": "completed"}])
        self.assertIsNone(verification["recovered"])
        self.assertEqual(verification["status"], "unknown")

    async def test_approved_rancher_pod_exec_uses_temporary_kubeconfig_transport(self):
        configuration = object()
        executor_result = {"exit_code": 0, "stdout": "uid=10001", "stderr": ""}
        change = {
            "type": "exec_pod",
            "namespace": "prod",
            "pod_name": "orders-api-abc",
            "container_name": "app",
            "command": "id",
            "timeout_seconds": 30,
            "human_approved": True,
            "operator_confirmed": True,
        }
        plan = {
            "cluster_id": "c-prod",
            "cluster": "prod",
            "source": "rancher",
            "namespace": "prod",
            "target": "Deployment/orders-api",
            "_operator": "unit-test",
        }
        with patch.object(server, "_rancher_execution_configuration", AsyncMock(return_value=configuration)) as config_call, patch.object(
            server.CLUSTER_REGISTRY,
            "exec_pod_with_configuration",
            return_value=executor_result,
        ) as exec_call:
            result = await server._execute_change(change, plan)
        self.assertEqual(result["status"], "completed")
        config_call.assert_awaited_once_with("c-prod")
        self.assertIs(exec_call.call_args.args[0], configuration)
        self.assertEqual(exec_call.call_args.kwargs["pod_name"], "orders-api-abc")

    async def test_workload_change_completes_only_after_exact_live_readback(self):
        change = {
            "type": "scale_out",
            "namespace": "prod",
            "workload_type": "Deployment",
            "workload_name": "orders-api",
            "replicas": 2,
            "human_approved": True,
            "operator_confirmed": True,
        }
        plan = {
            "cluster_id": "local",
            "namespace": "prod",
            "target": "Deployment/orders-api",
            "_operator": "unit-test",
        }
        before = {
            "metadata": {"uid": "uid-1", "resourceVersion": "41", "generation": 3},
            "spec": {"replicas": 1},
        }
        after = {
            "metadata": {"uid": "uid-1", "resourceVersion": "42", "generation": 4},
            "spec": {"replicas": 2},
            "status": {"observedGeneration": 3},
        }
        with patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]), patch.object(
            server,
            "_read_live_workload_after_change",
            AsyncMock(side_effect=[(before, "local_mcp"), (after, "local_mcp")]),
        ), patch.object(
            server,
            "_call_mcp_tool",
            AsyncMock(return_value={"metadata": {"uid": "uid-1", "resourceVersion": "42"}}),
        ) as patch_call:
            result = await server._execute_change(change, plan)
        self.assertEqual(result["status"], "completed")
        receipt = result["result"]["mutation_postcondition"]
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["before_resource_version"], "41")
        self.assertEqual(receipt["resource_version"], "42")
        self.assertEqual(
            patch_call.await_args.args[1]["patch"],
            {"spec": {"replicas": 2}},
        )

    async def test_rancher_display_name_is_bound_to_opaque_id_before_approval(self):
        plan = {
            "cluster_id": "production-east",
            "cluster": "production-east",
            "source": "sre_chat",
            "namespace": "prod",
            "target": "Deployment/orders-api",
        }
        changes = [{
            "type": "patch_workload",
            "namespace": "prod",
            "workload_type": "Deployment",
            "workload_name": "orders-api",
            "patch": {"spec": {"template": {"metadata": {"annotations": {"fix": "approved"}}}}},
        }]
        with patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]), patch.object(
            server, "_rancher_enabled", return_value=True,
        ), patch.object(
            server,
            "_rancher_clusters",
            AsyncMock(return_value=[{"id": "c-m-12345", "name": "production-east"}]),
        ):
            binding = await server._bind_ops_execution_target(plan, changes)
        self.assertEqual(binding["cluster_id"], "c-m-12345")
        self.assertEqual(binding["transport"], "rancher")
        self.assertEqual(plan["cluster_id"], "c-m-12345")
        self.assertEqual(changes[0]["cluster_id"], "c-m-12345")

    async def test_workload_change_fails_when_generation_does_not_advance(self):
        change = {
            "type": "scale_out",
            "namespace": "prod",
            "workload_type": "Deployment",
            "workload_name": "orders-api",
            "replicas": 2,
            "human_approved": True,
            "operator_confirmed": True,
        }
        plan = {"cluster_id": "local", "namespace": "prod", "target": "Deployment/orders-api"}
        before = {
            "metadata": {"uid": "uid-1", "resourceVersion": "41", "generation": 3},
            "spec": {"replicas": 1},
        }
        after = {
            "metadata": {"uid": "uid-1", "resourceVersion": "42", "generation": 3},
            "spec": {"replicas": 2},
        }
        with patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]), patch.object(
            server,
            "_read_live_workload_after_change",
            AsyncMock(side_effect=[(before, "local"), (after, "local")]),
        ), patch.object(
            server,
            "_call_mcp_tool",
            AsyncMock(return_value={"metadata": {"uid": "uid-1", "resourceVersion": "42"}}),
        ):
            result = await server._execute_change(change, plan)
        self.assertEqual(result["status"], "failed")
        self.assertIn("generation", result["result"]["mutation_postcondition"]["error"])

    async def test_workload_change_fails_when_api_accepts_but_live_object_does_not_change(self):
        change = {
            "type": "scale_out",
            "namespace": "prod",
            "workload_type": "Deployment",
            "workload_name": "orders-api",
            "replicas": 2,
            "human_approved": True,
            "operator_confirmed": True,
        }
        plan = {"cluster_id": "local", "namespace": "prod", "target": "Deployment/orders-api"}
        unchanged = {
            "metadata": {"uid": "uid-1", "resourceVersion": "41", "generation": 3},
            "spec": {"replicas": 1},
        }
        with patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]), patch.object(
            server,
            "_read_live_workload_after_change",
            AsyncMock(side_effect=[(unchanged, "local_mcp"), (unchanged, "local_mcp")]),
        ), patch.object(
            server,
            "_call_mcp_tool",
            AsyncMock(return_value={"metadata": {"uid": "uid-1", "resourceVersion": "41"}}),
        ):
            result = await server._execute_change(change, plan)
        self.assertEqual(result["status"], "failed")
        receipt = result["result"]["mutation_postcondition"]
        self.assertFalse(receipt["verified"])
        self.assertIn("spec.replicas", receipt["mismatches"][0])

    async def test_matching_workload_patch_is_idempotent_and_not_sent_twice(self):
        change = {
            "type": "scale_out",
            "namespace": "prod",
            "workload_type": "Deployment",
            "workload_name": "orders-api",
            "replicas": 2,
            "human_approved": True,
            "operator_confirmed": True,
        }
        plan = {"cluster_id": "local", "namespace": "prod", "target": "Deployment/orders-api"}
        live = {
            "metadata": {"uid": "uid-1", "resourceVersion": "42", "generation": 4},
            "spec": {"replicas": 2},
        }
        with patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]), patch.object(
            server,
            "_read_live_workload_after_change",
            AsyncMock(side_effect=[(live, "local_mcp"), (live, "local_mcp")]),
        ), patch.object(server, "_call_mcp_tool", AsyncMock()) as patch_call:
            result = await server._execute_change(change, plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["operation"], "already_applied")
        self.assertTrue(result["result"]["mutation_postcondition"]["already_applied"])
        patch_call.assert_not_awaited()

    def test_recovery_criteria_require_workload_rollout_evidence(self):
        criteria = server._assess_recovery_criteria(
            {"namespace": "prod", "target": "Deployment/orders", "success_criteria": ["pod_ready"]},
            {"recovered": True},
            {"pod": {"name": "orders-abc", "ready": True}, "events": [], "logs": {}, "workload": {}},
        )
        self.assertIn("rollout_complete", criteria["mandatory"])
        self.assertIn("rollout_complete", criteria["not_evaluated"])
        self.assertFalse(criteria["passed"])

    def test_recovery_uses_current_logs_and_keeps_previous_only_for_diagnosis(self):
        plan = {
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "changes": [{
                "type": "patch_workload_runtime_security",
                "patch": {"spec": {"template": {"spec": {"securityContext": {"fsGroup": 10001}}}}},
            }],
            "evidence": {"pod": {"restart_count": 8}},
        }
        evidence = {
            "pod": {"name": "grafana-new", "ready": True, "restart_count": 0},
            "events": [],
            "logs": {"grafana": {
                "current": "logger=server level=info msg=HTTP Server Listen",
                "previous": "GF_PATHS_DATA is not writable; unable to open database file",
            }},
            "workload": {
                "metadata": {"generation": 4},
                "spec": {
                    "replicas": 1,
                    "template": {"spec": {"securityContext": {"fsGroup": 10001}}},
                },
                "status": {
                    "observedGeneration": 4,
                    "updatedReplicas": 1,
                    "readyReplicas": 1,
                    "availableReplicas": 1,
                    "unavailableReplicas": 0,
                },
            },
        }
        criteria = server._assess_recovery_criteria(plan, {"recovered": True}, evidence)
        self.assertTrue(criteria["passed"], criteria)
        write_check = next(
            item for item in criteria["evaluations"]
            if item["criterion"] == "write_errors_absent"
        )
        self.assertTrue(write_check["passed"])

    async def test_recovery_does_not_accept_pre_mutation_healthy_pod(self):
        plan = {
            "namespace": "prod",
            "target": "Deployment/orders-api",
            "changes": [{
                "type": "patch_workload",
                "patch": {"spec": {"template": {"metadata": {"annotations": {"fix": "approved"}}}}},
                "_mutation_started_at": "2026-08-14T10:00:00+00:00",
                "_mutation_postcondition": {"verified": True, "generation": 4, "already_applied": False},
            }],
        }
        pods = {"pods": [{
            "name": "orders-old",
            "namespace": "prod",
            "workload_kind": "Deployment",
            "workload_name": "orders-api",
            "labels": {"pod-template-hash": "old"},
            "created_at": "2026-08-14T09:00:00Z",
            "phase": "Running",
            "ready": True,
            "containers": [{"name": "app", "ready": True, "state": "running"}],
        }]}
        with patch.object(server, "_call_mcp_tool", AsyncMock(return_value=pods)):
            result = await server._probe_plan_recovery(plan, [{"status": "completed"}])
        self.assertIsNone(result["recovered"])
        self.assertEqual(result["status"], "progressing")
        self.assertEqual(result["pre_mutation_pods_ignored"], ["orders-old"])

    async def _record(self, target, elapsed, remaining):
        target.append((elapsed, remaining))

    async def test_diagnosis_only_job_keeps_rechecking_until_recovered(self):
        job_id = "ops-test-terminal"
        server.OPS_JOBS[job_id] = {
            "id": job_id, "status": "running", "stage": "starting", "events": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        first_result = {
            "status": "diagnostic_completed", "executed": False, "steps": [], "results": [],
            "verification": {"recovered": None, "message": "证据不足"},
            "alternative_plans": [], "message": "证据不足",
        }
        recovered_result = {
            "status": "completed", "executed": False, "steps": [], "results": [],
            "verification": {"recovered": True, "message": "实时复检确认恢复"},
            "alternative_plans": [], "message": "恢复",
        }
        try:
            runner = AsyncMock(side_effect=[first_result, recovered_result])
            with patch.object(server, "_execute_ops_plan_once", runner), patch.object(
                server, "_wait_for_continuous_recheck", AsyncMock(return_value=True)
            ), patch.object(
                server, "_llm_ops_summary", AsyncMock(return_value={"source": "test", "content": "证据不足", "followup_plans": []})
            ):
                await server._run_ops_job(job_id, {"title": "diagnosis", "steps": [{"title": "read evidence"}], "changes": []}, False, asyncio.Event())
            self.assertEqual(runner.await_count, 2)
            self.assertEqual(server.OPS_JOBS[job_id]["status"], "completed")
            self.assertEqual(server.OPS_JOBS[job_id]["stage"], "recovered")
        finally:
            server.OPS_JOBS.pop(job_id, None)

    def test_continuous_diagnostic_cycle_invalidates_old_approvals(self):
        plan = {
            "title": "approved mutation",
            "namespace": "prod",
            "target": "Deployment/orders",
            "high_risk_confirmed": True,
            "operator_force_execute": True,
            "changes": [{
                "type": "patch_workload",
                "namespace": "prod",
                "workload_type": "Deployment",
                "workload_name": "orders",
                "human_approved": True,
                "operator_confirmed": True,
                "approval_receipt": {"approval_id": "stale"},
            }],
        }
        result = {
            "verification": {"recovered": False, "message": "still broken"},
            "continuation_context": {"lineage_id": "incident-1", "attempt_count": 1},
        }
        next_plan = server._continuous_diagnostic_plan(plan, result, cycle=2)
        self.assertEqual(next_plan["changes"], [])
        self.assertFalse(next_plan["high_risk_confirmed"])
        self.assertFalse(next_plan["operator_force_execute"])
        self.assertEqual(next_plan["source"], "continuous_recovery_loop")

    async def test_autonomous_job_continues_diagnostic_followup_instead_of_stopping(self):
        job_id = "ops-test-diagnostic-followup"
        server.OPS_JOBS[job_id] = {
            "id": job_id, "status": "running", "stage": "starting", "events": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        first_result = {
            "status": "diagnostic_completed",
            "executed": False,
            "steps": [],
            "results": [],
            "verification": {"recovered": None, "message": "需要继续取证"},
            "alternative_plans": [{
                "id": "deep-dive-logs",
                "title": "继续取证：重建后读取 previous logs",
                "summary": "当前没有变更，但需要继续读取新 Pod 的失败证据。",
                "steps": [{"id": "previous-logs", "title": "读取 previous logs"}],
                "changes": [],
                "source": "evidence_replan",
            }],
            "message": "证据不足，需要继续取证",
        }
        second_result = {
            "status": "completed",
            "executed": False,
            "steps": [],
            "results": [],
            "verification": {"recovered": True, "message": "目标已恢复"},
            "alternative_plans": [],
            "message": "完成",
        }
        try:
            runner = AsyncMock(side_effect=[first_result, second_result])
            with patch.object(server, "_execute_ops_plan_once", runner), patch.object(
                server, "_llm_ops_summary", AsyncMock(return_value={"source": "test", "content": "完成", "followup_plans": []})
            ):
                await server._run_ops_job(
                    job_id,
                    {"title": "initial diagnosis", "steps": [{"title": "查看事件"}], "changes": []},
                    True,
                    asyncio.Event(),
                )
            self.assertEqual(runner.await_count, 2)
            self.assertEqual(server.OPS_JOBS[job_id]["stage"], "recovered")
            self.assertEqual(server.OPS_JOBS[job_id]["status"], "completed")
        finally:
            server.OPS_JOBS.pop(job_id, None)

    async def test_non_autonomous_failed_change_switches_to_different_strategy_in_same_job(self):
        job_id = "ops-test-change-followup"
        server.OPS_JOBS[job_id] = {
            "id": job_id, "status": "running", "stage": "starting", "events": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        next_plan = {
            "id": "alternative-runtime-security",
            "title": "改用受控 initContainer 修复目录属主",
            "target": "Deployment/orders-api",
            "namespace": "prod",
            "steps": [{"title": "核对新的失败 Pod"}],
            "changes": [{
                "type": "patch_workload_runtime_security",
                "namespace": "prod",
                "workload_type": "Deployment",
                "workload_name": "orders-api",
                "patch": {"spec": {"template": {"spec": {"initContainers": [{"name": "prepare-volume"}]}}}},
            }],
            "stepwise_confirmation": True,
        }
        first_result = {
            "status": "failed",
            "executed": False,
            "results": [{"status": "failed", "result": {"error": "patch validation did not converge"}}],
            "verification": {"recovered": False, "message": "new pod still fails"},
            "alternative_plans": [next_plan],
        }
        second_result = {
            "status": "completed", "executed": True, "results": [],
            "verification": {"recovered": True, "message": "target recovered"},
            "alternative_plans": [],
        }
        initial = {
            "title": "先尝试 fsGroup",
            "target": "Deployment/orders-api",
            "namespace": "prod",
            "changes": [{
                "type": "patch_workload",
                "namespace": "prod",
                "workload_type": "Deployment",
                "workload_name": "orders-api",
                "patch": {"spec": {"template": {"spec": {"securityContext": {"fsGroup": 1000}}}}},
            }],
        }
        try:
            runner = AsyncMock(side_effect=[first_result, second_result])
            with patch.object(server, "_execute_ops_plan_once", runner), patch.object(
                server, "_llm_ops_summary", AsyncMock(return_value={"source": "test", "content": "恢复", "followup_plans": []})
            ):
                await server._run_ops_job(job_id, initial, False, asyncio.Event())
            self.assertEqual(runner.await_count, 2)
            self.assertEqual(server.OPS_JOBS[job_id]["status"], "completed")
            self.assertEqual(server.OPS_JOBS[job_id]["stage"], "recovered")
            self.assertEqual(len(server.OPS_JOBS[job_id]["history"]), 2)
        finally:
            server.OPS_JOBS.pop(job_id, None)

    async def test_permission_failure_keeps_operator_steps_and_resumes_after_channel_fix(self):
        job_id = "ops-test-permission-boundary"
        server.OPS_JOBS[job_id] = {
            "id": job_id, "status": "running", "stage": "starting", "events": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        failed_result = {
            "status": "failed",
            "executed": False,
            "results": [{
                "status": "failed",
                "result": {
                    "error": "HTTP 403 Forbidden",
                    "permission_guidance": {"do_this": ["绑定所需 ClusterRole 后重新执行。"]},
                },
            }],
            "verification": {"recovered": False, "message": "change API rejected"},
            "alternative_plans": [{"title": "不应自动执行", "steps": [{"title": "retry"}], "changes": []}],
        }
        recovered_result = {
            "status": "completed",
            "executed": False,
            "results": [],
            "verification": {"recovered": True, "message": "权限修复后实时复检确认目标恢复"},
            "alternative_plans": [],
        }
        try:
            runner = AsyncMock(side_effect=[failed_result, recovered_result])
            with patch.object(server, "_execute_ops_plan_once", runner), patch.object(
                server, "_wait_for_continuous_recheck", AsyncMock(return_value=True)
            ), patch.object(
                server, "_llm_ops_summary", AsyncMock(return_value={"source": "test", "content": "权限阻断", "followup_plans": []})
            ):
                await server._run_ops_job(
                    job_id,
                    {"title": "patch", "target": "Deployment/orders", "changes": [{"type": "patch_workload"}]},
                    True,
                    asyncio.Event(),
                )
            self.assertEqual(runner.await_count, 2)
            self.assertEqual(server.OPS_JOBS[job_id]["status"], "completed")
            self.assertEqual(server.OPS_JOBS[job_id]["stage"], "recovered")
            first_attempt = server.OPS_JOBS[job_id]["history"][0]["result"]
            self.assertIn("绑定所需 ClusterRole", first_attempt["operator_steps"][0])
        finally:
            server.OPS_JOBS.pop(job_id, None)

    def test_namespace_guard_blocks_mutation_outside_allowlist(self):
        with patch.dict(os.environ, {"ALLOWED_NAMESPACES": "platform,observability"}):
            result = server._ops_namespace_guard({
                "namespace": "business-prod",
                "changes": [{"type": "patch_workload"}],
            })
        self.assertFalse(result["allowed"])
        self.assertIn("business-prod", result["reason"])
        self.assertIn("ALLOWED_NAMESPACES", result["operator_steps"][0])

    async def test_local_mutation_preflight_detects_service_account_rbac(self):
        plan = {
            "cluster_id": "local",
            "namespace": "business-prod",
            "target": "Deployment/orders",
        }
        changes = [{
            "type": "patch_workload",
            "namespace": "business-prod",
            "workload_type": "Deployment",
            "workload_name": "orders",
            "patch": {"spec": {"template": {"spec": {"securityContext": {"runAsUser": 0}}}}},
        }]
        denied = {
            "namespace": "business-prod",
            "verb": "patch",
            "group": "apps",
            "resource": "deployments",
            "name": "orders",
            "allowed": False,
            "denied": False,
            "reason": "",
            "evaluation_error": "",
        }
        with patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]), patch.object(
            server, "_call_mcp_tool", AsyncMock(return_value=denied)
        ) as call:
            result = await server._ops_mutation_access_preflight(plan, changes)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("ServiceAccount", result["reason"])
        call.assert_awaited_once_with("check_access", {
            "namespace": "business-prod",
            "verb": "patch",
            "group": "apps",
            "resource": "deployments",
            "name": "orders",
        })

    async def test_local_mutation_preflight_allows_approved_rbac(self):
        plan = {
            "cluster_id": "local",
            "namespace": "business-prod",
            "target": "Deployment/orders",
        }
        changes = [{
            "type": "patch_workload_runtime_security",
            "namespace": "business-prod",
            "workload_type": "Deployment",
            "workload_name": "orders",
        }]
        with patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]), patch.object(
            server,
            "_call_mcp_tool",
            AsyncMock(return_value={
                "namespace": "business-prod",
                "verb": "patch",
                "group": "apps",
                "resource": "deployments",
                "name": "orders",
                "allowed": True,
            }),
        ):
            result = await server._ops_mutation_access_preflight(plan, changes)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "allowed")

    def test_workload_permission_denied_is_not_misclassified_as_rbac_blocker(self):
        result = {
            "status": "failed",
            "results": [{
                "status": "failed",
                "result": {"error": "patch validation did not converge"},
            }],
            "verification": {
                "recovered": False,
                "message": "new pod is still failing",
                "unresolved": [{
                    "name": "orders-api-next",
                    "logs": "mkdir: can't create directory '/data/cache': Permission denied",
                }],
            },
        }
        self.assertFalse(server._operator_blocking_execution_failure(result))

    def test_executor_timeout_is_treated_as_indeterminate_execution_boundary(self):
        result = {
            "status": "failed",
            "results": [{
                "status": "failed",
                "result": {"error": "Kubernetes change timed out", "timeout": True},
            }],
        }
        self.assertTrue(server._operator_blocking_execution_failure(result))

    def test_strategy_fingerprint_ignores_reworded_reason_but_keeps_patch_difference(self):
        base = {
            "changes": [{
                "type": "patch_workload", "namespace": "prod", "workload_name": "orders",
                "patch": {"spec": {"template": {"spec": {"securityContext": {"fsGroup": 1000}}}}},
                "reason": "第一次说明",
            }],
        }
        reworded = {"changes": [{**base["changes"][0], "reason": "模型换了一种说法"}]}
        different = {"changes": [{
            **base["changes"][0],
            "patch": {"spec": {"template": {"spec": {"securityContext": {"fsGroup": 2000}}}}},
        }]}
        self.assertEqual(server._change_fingerprint(base), server._change_fingerprint(reworded))
        self.assertNotEqual(server._change_fingerprint(base), server._change_fingerprint(different))

    def test_manual_followup_keeps_failed_strategy_lineage_across_jobs(self):
        failed_plan = {
            "title": "fsGroup 修复",
            "target": "Deployment/orders",
            "changes": [{
                "type": "patch_workload", "namespace": "prod", "workload_name": "orders",
                "patch": {"spec": {"template": {"spec": {"securityContext": {"fsGroup": 1000}}}}},
            }],
        }
        failed_fingerprint = server._change_fingerprint(failed_plan)
        failed_change_fingerprint = server._change_item_fingerprint(failed_plan["changes"][0])
        followup = {
            "title": "改用 initContainer 修复目录属主",
            "target": "Deployment/orders",
            "changes": [{
                "type": "patch_workload_runtime_security", "namespace": "prod", "workload_name": "orders",
                "patch": {"spec": {"template": {"spec": {"initContainers": [{"name": "prepare-volume"}]}}}},
            }],
        }
        result = {
            "status": "unresolved",
            "verification": {"recovered": False, "message": "new pod still reports permission denied"},
            "alternative_plans": [followup],
        }
        history = [{
            "attempt": 1,
            "strategy": failed_plan["title"],
            "fingerprint": failed_fingerprint,
            "actions": ["patch_workload"],
            "change_fingerprints": [failed_change_fingerprint],
            "result": result,
        }]
        attached = server._attach_ops_continuation_context(
            "ops-parent", failed_plan, result, {failed_fingerprint}, history,
        )
        prepared = server._apply_ops_continuation_context(attached["alternative_plans"][0])
        self.assertEqual(prepared["_lineage_id"], "ops-parent")
        self.assertIn(failed_fingerprint, prepared["_attempted_strategy_fingerprints"])
        self.assertIn(failed_change_fingerprint, prepared["_attempted_change_fingerprints"])
        self.assertIn("permission denied", prepared["_last_failure"]["outcome"])
        self.assertEqual(prepared["_prior_attempts"][0]["strategy"], "fsGroup 修复")

    def test_lineage_attempted_strategy_cannot_be_selected_again(self):
        repeated = {
            "title": "same patch, reworded",
            "target": "Deployment/orders",
            "steps": [{"title": "retry"}],
            "changes": [{
                "type": "patch_workload", "namespace": "prod", "workload_name": "orders",
                "patch": {"spec": {"template": {"spec": {"securityContext": {"fsGroup": 1000}}}}},
                "reason": "new wording only",
            }],
        }
        attempted = {server._change_fingerprint(repeated)}
        self.assertIsNone(server._select_next_ops_plan([repeated], attempted, autonomous=False))

    async def test_step_approval_survives_audit_sink_failure(self):
        job_id = "ops-test-approval"
        approval_event = asyncio.Event()
        approval_id = "approve-unit-test-approval"
        change_fingerprint = "a" * 64
        server.OPS_JOBS[job_id] = {
            "id": job_id,
            "status": "awaiting_approval",
            "stage": "awaiting_change_approval",
            "message": "等待确认",
            "pending_approval": {
                "approval_id": approval_id,
                "change_fingerprint": change_fingerprint,
                "change_index": 1,
                "changes_total": 1,
                "action": "create_pvc",
                "target": "PVC/data-missing",
            },
            "events": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        server.OPS_JOB_STEP_APPROVAL_EVENTS[job_id] = approval_event
        try:
            with patch.object(server, "_audit_event", side_effect=OSError("audit sink down")):
                result = await server.approve_ops_job_step(
                    job_id,
                    server.OpsStepApprovalRequest(
                        change_index=1,
                        approval_id=approval_id,
                        change_fingerprint=change_fingerprint,
                        confirm=True,
                        comment="确认执行",
                    ),
                    self._request(),
                )
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["stage"], "change_approval_received")
            self.assertTrue(approval_event.is_set())
            self.assertTrue(any(event.get("stage") == "audit_warning" for event in server.OPS_JOBS[job_id]["events"]))
        finally:
            server.OPS_JOBS.pop(job_id, None)
            server.OPS_JOB_STEP_APPROVAL_EVENTS.pop(job_id, None)

    async def test_step_approval_rejects_stale_id_without_consuming_current_approval(self):
        job_id = "ops-test-stale-approval"
        approval_event = asyncio.Event()
        expected_id = "approve-current-unit-test"
        expected_fingerprint = "b" * 64
        server.OPS_JOBS[job_id] = {
            "id": job_id,
            "status": "awaiting_approval",
            "stage": "awaiting_change_approval",
            "pending_approval": {
                "approval_id": expected_id,
                "change_fingerprint": expected_fingerprint,
                "change_index": 1,
                "changes_total": 1,
                "action": "patch_workload",
                "target": "Deployment/orders",
            },
            "events": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        server.OPS_JOB_STEP_APPROVAL_EVENTS[job_id] = approval_event
        try:
            with self.assertRaises(HTTPException) as context:
                await server.approve_ops_job_step(
                    job_id,
                    server.OpsStepApprovalRequest(
                        change_index=1,
                        approval_id="approve-stale-unit-test",
                        change_fingerprint=expected_fingerprint,
                        confirm=True,
                    ),
                    self._request(),
                )
            self.assertEqual(context.exception.status_code, 409)
            self.assertFalse(server.OPS_JOBS[job_id]["pending_approval"].get("consumed", False))
            self.assertFalse(approval_event.is_set())
        finally:
            server.OPS_JOBS.pop(job_id, None)
            server.OPS_JOB_STEP_APPROVAL_EVENTS.pop(job_id, None)

    async def test_live_pvc_evidence_cancels_stale_recreate_and_requires_new_approval(self):
        plan = {
            "title": "generic recovery",
            "cluster": "nonprod",
            "cluster_id": "c-nonprod",
            "source": "rancher",
            "namespace": "k8s-agent",
            "target": "Deployment/k8s-agent-loki",
            "pod_name": "k8s-agent-loki-abc",
            "summary": "FailedScheduling",
            "operator_force_execute": True,
            "high_risk_confirmed": True,
            "steps": [{"id": "events", "title": "查看事件"}],
            "changes": [{
                "type": "recreate_pod",
                "namespace": "k8s-agent",
                "pod_name": "k8s-agent-loki-abc",
                "workload_type": "Deployment",
                "workload_name": "k8s-agent-loki",
            }],
        }
        evidence = {
            "pod": {
                "name": "k8s-agent-loki-abc",
                "namespace": "k8s-agent",
                "workload": {"kind": "Deployment", "name": "k8s-agent-loki"},
            },
            "events": [{
                "type": "Warning",
                "reason": "FailedScheduling",
                "message": "0/10 nodes are available: pod has unbound immediate PersistentVolumeClaims.",
            }],
            "storage": [{
                "pvc": "loki-data",
                "pvc_phase": "Pending",
                "requested": "10Gi",
                "access_modes": ["ReadWriteMany"],
                "storage_class": "nfs-static",
                "storage_class_provisioner": "kubernetes.io/no-provisioner",
            }],
            "logs": {},
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "k8s-agent-loki", "generation": 4},
                "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "loki"}], "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "loki-data"}}]}}},
                "status": {"observedGeneration": 4, "readyReplicas": 0},
            },
            "services": [],
        }
        old_server = os.environ.get("AUTO_OPS_STATIC_PV_NFS_SERVER")
        old_path = os.environ.get("AUTO_OPS_STATIC_PV_NFS_BASE_PATH")
        os.environ["AUTO_OPS_STATIC_PV_NFS_SERVER"] = "192.0.2.10"
        os.environ["AUTO_OPS_STATIC_PV_NFS_BASE_PATH"] = "/exports"
        try:
            with patch.dict(os.environ, {"ALLOWED_NAMESPACES": "k8s-agent"}), patch.object(
                server, "_collect_plan_priority_evidence", AsyncMock(return_value=evidence)
            ), patch.object(
                server, "_collect_plan_deep_evidence", AsyncMock(return_value=evidence)
            ), patch.object(server, "_execute_change", AsyncMock()) as execute_change:
                result = await server._execute_ops_plan_once(plan, summarize=False)
            self.assertEqual(result["status"], "planned")
            self.assertFalse(result["executed"])
            execute_change.assert_not_awaited()
            replacement = result["alternative_plans"][0]
            self.assertEqual(replacement["changes"][0]["type"], "create_pv")
            self.assertTrue(replacement["requires_high_risk_confirmation"])
            self.assertEqual(replacement["cluster_id"], "c-nonprod")
            self.assertEqual(replacement["source"], "rancher")
        finally:
            if old_server is None:
                os.environ.pop("AUTO_OPS_STATIC_PV_NFS_SERVER", None)
            else:
                os.environ["AUTO_OPS_STATIC_PV_NFS_SERVER"] = old_server
            if old_path is None:
                os.environ.pop("AUTO_OPS_STATIC_PV_NFS_BASE_PATH", None)
            else:
                os.environ["AUTO_OPS_STATIC_PV_NFS_BASE_PATH"] = old_path

    async def test_managed_topology_without_cmdb_still_fuses_beyla_flows(self):
        managed = {
            "status": "ok",
            "source": "kubeconfig",
            "nodes": [{
                "id": "cluster-a:apps:Deployment/api",
                "name": "api",
                "type": "workload",
                "cluster": "cluster-a",
                "namespace": "apps",
            }],
            "edges": [],
            "summary": {},
        }
        observed = [{
            "source_system": "ebpf_beyla",
            "observed": True,
            "direction": "egress",
            "source": {
                "id": "cluster-a:apps:Deployment/api",
                "cluster": "cluster-a",
                "cluster_id": "cluster-a",
                "namespace": "apps",
                "kind": "Deployment",
                "name": "api",
            },
            "destination": {
                "id": "external:db.example",
                "type": "external_ip",
                "kind": "External",
                "name": "db.example",
                "address": "db.example",
            },
            "protocol": "tcp",
            "port": 5432,
            "bytes": 1024,
            "confidence": 0.98,
            "evidence": ["network_flow"],
        }]
        with (
            patch.dict(server.SERVICES, {"cmdb": ""}),
            patch.object(server, "_managed_topology_payload", AsyncMock(return_value=managed)),
            patch.object(
                server,
                "_fetch_configured_observed_flows",
                AsyncMock(return_value=(observed, [{"id": "ebpf_beyla", "status": "connected", "flows": 1}])),
            ),
            patch.dict(os.environ, {"EBPF_TOPOLOGY_FUSION_ENABLED": "true"}),
        ):
            payload = await server.cmdb_topology()
        self.assertEqual(payload["summary"]["ebpf_observed_edges"], 1)
        self.assertTrue(any(edge.get("source_system") == "ebpf_beyla" for edge in payload["edges"]))
        self.assertEqual(payload["diagnostics"]["ebpf_flow_status"][0]["status"], "connected")

    def test_beyla_loki_parser_accepts_json_wrapped_logs_and_alloy_label_aliases(self):
        line = json.dumps({
            "message": (
                "network_flow: k8s.src.namespace=apps k8s.src.owner.name=api "
                "k8s.src.owner.type=Deployment k8s.dst.namespace=data "
                "k8s.dst.owner.name=postgres k8s.dst.owner.type=StatefulSet "
                "src.address=192.0.2.2 dst.address=192.0.2.3 dst.port=5432 transport=TCP"
            ),
        })
        payload = {
            "data": {"result": [{
                "stream": {
                    "namespace_name": "observability",
                    "pod_name": "beyla-node-a",
                    "container_name": "beyla",
                },
                "values": [["1", line]],
            }]},
        }
        lines, raw_lines, labels = server._beyla_loki_payload_lines(payload)
        self.assertEqual(raw_lines, 1)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("network_flow:"))
        self.assertEqual(labels[0]["namespace_name"], "observability")
        parsed = server._parse_beyla_network_flow_line(lines[0], cluster_hint="cluster-a")
        self.assertEqual(parsed["source"]["name"], "api")
        self.assertEqual(parsed["destination"]["name"], "postgres")

    async def test_beyla_loki_query_falls_back_to_alloy_label_names(self):
        flow_line = (
            "network_flow: k8s.src.namespace=apps k8s.src.owner.name=api "
            "k8s.src.owner.type=Deployment k8s.dst.namespace=data "
            "k8s.dst.owner.name=postgres k8s.dst.owner.type=StatefulSet "
            "src.address=192.0.2.2 dst.address=192.0.2.3 dst.port=5432 transport=TCP"
        )

        class Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class LokiClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, _url, *, params):
                if "namespace_name" in params["query"]:
                    return Response({"data": {"result": [{
                        "stream": {
                            "namespace_name": "observability",
                            "pod_name": "beyla-node-a",
                            "container_name": "beyla",
                        },
                        "values": [["1", flow_line]],
                    }]}})
                return Response({"data": {"result": []}})

        request = server.ExternalTrafficFlowRequest(
            cluster="cluster-a",
            cluster_id="cluster-a",
            namespace="all",
            workload="",
            window="5m",
            source="observed",
            include_static_inference=False,
            include_cmdb=False,
        )
        with (
            patch.dict(server.SERVICES, {"loki": "http://loki.example"}),
            patch.object(server, "_client", return_value=LokiClient()),
            patch.dict(os.environ, {
                "BEYLA_LOKI_FLOW_ENABLED": "true",
                "BEYLA_LOKI_NAMESPACE": "observability",
                "BEYLA_LOKI_POD_SELECTOR": "beyla.*",
            }),
        ):
            flows, statuses = await server._fetch_beyla_loki_flows(request)
        self.assertEqual(len(flows), 1)
        self.assertEqual(statuses[0]["status"], "connected")
        self.assertIn("namespace_name", statuses[0]["effective_query"])
        self.assertTrue(statuses[0]["query_fallback_used"])
        self.assertGreaterEqual(len(statuses[0]["query_attempts"]), 2)

    async def test_managed_topology_fuses_beyla_when_configured_cmdb_is_down(self):
        managed = {
            "status": "ok",
            "source": "rancher",
            "message": "Kubernetes topology.",
            "nodes": [{
                "id": "cluster-a:apps:Deployment/api",
                "name": "api",
                "type": "workload",
                "cluster": "cluster-a",
                "namespace": "apps",
            }],
            "edges": [],
            "summary": {},
        }
        observed = [{
            "source_system": "ebpf_beyla",
            "observed": True,
            "direction": "egress",
            "source": {
                "id": "cluster-a:apps:Deployment/api",
                "cluster": "cluster-a",
                "cluster_id": "cluster-a",
                "namespace": "apps",
                "kind": "Deployment",
                "name": "api",
            },
            "destination": {
                "id": "external:db.example",
                "type": "external_ip",
                "kind": "External",
                "name": "db.example",
                "address": "db.example",
            },
            "protocol": "tcp",
            "port": 5432,
            "bytes": 2048,
            "confidence": 0.98,
            "evidence": ["network_flow"],
        }]

        class BrokenClient:
            async def __aenter__(self):
                raise RuntimeError("cmdb unavailable")

            async def __aexit__(self, *_args):
                return False

        with (
            patch.dict(server.SERVICES, {"cmdb": "http://cmdb.invalid"}),
            patch.object(server, "_managed_topology_payload", AsyncMock(return_value=managed)),
            patch.object(server, "_client", return_value=BrokenClient()),
            patch.object(
                server,
                "_fetch_configured_observed_flows",
                AsyncMock(return_value=(observed, [{"id": "ebpf_beyla", "status": "connected", "flows": 1}])),
            ),
            patch.dict(os.environ, {"EBPF_TOPOLOGY_FUSION_ENABLED": "true"}),
        ):
            payload = await server.cmdb_topology()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["summary"]["ebpf_observed_edges"], 1)
        self.assertEqual(payload["diagnostics"]["ebpf_flow_status"][0]["status"], "connected")

    async def test_emergency_restart_translates_to_restart_action(self):
        enqueue = AsyncMock(return_value={"id": "ops-emergency", "status": "queued"})
        release = {
            "id": "rel-emergency", "service": "checkout", "cluster": "local", "namespace": "prod",
            "workload_kind": "Deployment", "workload_name": "checkout", "release_mode": "existing",
            "change_channel": "emergency_recovery", "emergency_action": "restart_component",
            "emergency_reason": "组件故障，需要恢复服务", "approved_by": "operator",
        }
        with patch.object(server, "_enqueue_ops_job", enqueue):
            await server._submit_release_job(release, "operator")
        plan = enqueue.await_args.args[0]
        self.assertEqual(plan["change_class"], "emergency_recovery")
        self.assertEqual(plan["changes"][0]["type"], "restart")

    async def test_emergency_rollback_translates_to_image_patch(self):
        enqueue = AsyncMock(return_value={"id": "ops-rollback", "status": "queued"})
        release = {
            "id": "rel-rollback", "service": "checkout", "cluster": "local", "namespace": "prod",
            "workload_kind": "Deployment", "workload_name": "checkout", "release_mode": "existing",
            "change_channel": "emergency_recovery", "emergency_action": "rollback",
            "container_name": "app", "image": "registry.local/checkout:v1.2.2", "approved_by": "operator",
        }
        with patch.object(server, "_enqueue_ops_job", enqueue):
            await server._submit_release_job(release, "operator")
        change = enqueue.await_args.args[0]["changes"][0]
        self.assertEqual(change["type"], "patch_workload")
        self.assertEqual(change["patch"]["spec"]["template"]["spec"]["containers"][0]["image"], "registry.local/checkout:v1.2.2")

    async def test_emergency_restore_config_translates_to_expected_patch(self):
        enqueue = AsyncMock(return_value={"id": "ops-restore", "status": "queued"})
        patch_body = {"spec": {"template": {"metadata": {"annotations": {"restored": "true"}}}}}
        release = {
            "id": "rel-restore", "service": "checkout", "cluster": "local", "namespace": "prod",
            "workload_kind": "Deployment", "workload_name": "checkout", "release_mode": "existing",
            "change_channel": "emergency_recovery", "emergency_action": "restore_config",
            "patch": patch_body, "approved_by": "operator",
        }
        with patch.object(server, "_enqueue_ops_job", enqueue):
            await server._submit_release_job(release, "operator")
        change = enqueue.await_args.args[0]["changes"][0]
        self.assertEqual(change["type"], "patch_workload")
        self.assertEqual(change["patch"], patch_body)


class ReleaseAndBenchmarkTests(unittest.TestCase):
    def test_skill_memory_does_not_prepend_execution_steps(self):
        plan = {"steps": [{"id": "ai-step", "title": "AI 动态诊断"}], "changes": []}
        skill = {
            "id": "storage-skill",
            "name": "存储权限专家",
            "category": "storage",
            "summary": "处理目录权限",
            "risk": "medium",
            "success_criteria": ["Pod Ready"],
            "allowed_actions": ["patch_workload"],
            "enabled": True,
        }
        with patch.object(server.OPS_SKILL_REGISTRY, "match", return_value={"matches": [{"skill": skill, "confidence": 0.9, "score": 0.9}]}), patch.object(
            server.OPS_SKILL_REGISTRY,
            "steps_from_matches",
            return_value=[{"id": "skill-step", "title": "Skill 建议步骤"}],
        ):
            enriched = server._attach_operator_skills_to_plan(plan, {"question": "permission denied"})
        self.assertEqual(enriched["steps"][0]["id"], "ai-step")
        self.assertEqual(enriched["skill_suggested_steps"][0]["id"], "skill-step")

    def test_external_traffic_filters_observed_ebpf_flows_by_workload(self):
        payload = build_external_traffic_payload(
            [],
            observed_flows=[
                {
                    "source_system": "ebpf_beyla",
                    "direction": "egress",
                    "source": {"cluster": "c-prod", "namespace": "pay", "kind": "Deployment", "name": "orders-api", "pod": "orders-api-1"},
                    "destination": {"type": "external_domain", "name": "elk.example.local", "address": "elk.example.local", "port": 443},
                    "evidence": ["beyla network_flow orders-api -> elk.example.local"],
                },
                {
                    "source_system": "ebpf_beyla",
                    "direction": "egress",
                    "source": {"cluster": "c-prod", "namespace": "pay", "kind": "Deployment", "name": "billing-api", "pod": "billing-api-1"},
                    "destination": {"type": "external_domain", "name": "kafka.example.local", "address": "kafka.example.local", "port": 9092},
                    "evidence": ["beyla network_flow billing-api -> kafka.example.local"],
                },
            ],
            scope={"cluster": "c-prod", "namespace": "pay", "workload": "orders-api"},
        )
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["summary"]["ebpf_observed"], 1)
        self.assertEqual(payload["flows"][0]["source"]["name"], "orders-api")

    def test_chat_selected_fourth_workload_does_not_fall_back_to_ranked_first(self):
        pods = [
            {"name": "first-api-a", "workload_name": "first-api", "ready": False},
            {"name": "second-api-a", "workload_name": "second-api", "ready": False},
            {"name": "third-agent-a", "workload_name": "third-agent", "ready": False},
            {"name": "chosen-daemon-x", "workload_name": "chosen-daemon", "ready": False},
        ]
        req = server.ChatRequest(
            message="修复这个 DaemonSet",
            cluster_id="c-prod",
            namespace="logging",
            deployment="chosen-daemon",
            workload_type="DaemonSet",
        )
        selected = server._select_chat_target_pod(pods, req)
        self.assertEqual(selected["name"], "chosen-daemon-x")

    def test_chat_target_binding_rejects_cross_workload_action(self):
        req = server.ChatRequest(
            message="修复我选择的对象",
            cluster="nonprod",
            cluster_id="c-nonprod",
            namespace="prod",
            deployment="orders-api",
            workload_type="Deployment",
            target_id="c-nonprod|prod|Deployment|orders-api",
        )
        data = {
            "answer": "candidate",
            "raw": {
                "alert": {},
                "diagnosis": {
                    "proposed_changes": [],
                    "remediation_plan": {
                        "changes": [{
                            "type": "restart",
                            "namespace": "prod",
                            "workload_type": "Deployment",
                            "workload_name": "ranked-first-api",
                        }],
                    },
                },
                "decision": {"proposed_changes": []},
            },
        }
        bound = server._enforce_chat_target_binding(req, data)
        raw = bound["raw"]
        self.assertEqual(raw["alert"]["workload_name"], "orders-api")
        self.assertEqual(raw["diagnosis"]["remediation_plan"]["target"], "Deployment/orders-api")
        self.assertEqual(raw["diagnosis"]["remediation_plan"]["changes"], [])
        self.assertEqual(raw["target_binding"]["rejected_cross_target_actions"][0]["target"], "ranked-first-api")

    def test_chat_skill_plan_binds_cross_cluster_evidence_to_concrete_target(self):
        req = server.ChatRequest(message="检查所有集群最严重的异常", cluster="all", cluster_id="all", namespace="all")
        data = {
            "answer": "candidate",
            "raw": {
                "k8s_context": {
                    "source": "rancher",
                    "cluster": "prod-a",
                    "cluster_id": "c-prod-a",
                    "pod": {
                        "name": "orders-api-abc",
                        "namespace": "prod",
                        "workload_kind": "Deployment",
                        "workload_name": "orders-api",
                        "containers": [{"name": "app", "reason": "CrashLoopBackOff"}],
                        "security_context": {},
                    },
                    "events": {"events": [{"reason": "BackOff", "message": "restarting"}]},
                    "logs": {"app": {"previous": "startup failed"}},
                    "workload": {"metadata": {"name": "orders-api", "generation": 4}},
                    "diagnostics": {"storage": [], "services": []},
                },
                "diagnosis": {
                    "root_cause": "startup failure",
                    "remediation_plan": {"target": "Deployment/unknown", "changes": []},
                },
            },
        }
        attached = server._attach_operator_skills_to_chat(req, data)
        plan = attached["raw"]["diagnosis"]["remediation_plan"]
        self.assertEqual(plan["cluster_id"], "c-prod-a")
        self.assertEqual(plan["source"], "rancher")
        self.assertEqual(plan["namespace"], "prod")
        self.assertEqual(plan["target"], "Deployment/orders-api")
        self.assertEqual(plan["pod_name"], "orders-api-abc")

    def test_dependency_failure_chat_fallback_never_invents_a_business_root_cause(self):
        response = server._fallback_diagnosis_response(server.ChatRequest(
            message="为什么启动失败",
            cluster_id="c-prod",
            namespace="prod",
            deployment="orders-api",
        ))
        diagnosis = response["raw"]["diagnosis"]
        self.assertEqual(diagnosis["confidence"], 0)
        self.assertEqual(diagnosis["proposed_changes"], [])
        self.assertEqual(diagnosis["remediation_plan"]["changes"], [])
        self.assertNotIn("镜像拉取失败", response["answer"])

    def test_release_manifest_is_validated_and_normalized(self):
        manifest, report = _validate_release_manifest(
            """apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: prod
spec:
  replicas: 2
  selector:
    matchLabels: {app: checkout}
  template:
    metadata:
      labels: {app: checkout}
    spec:
      automountServiceAccountToken: false
      containers:
        - name: app
          image: registry.local/checkout:v1.2.3
          securityContext:
            allowPrivilegeEscalation: false
""",
            {"release_mode": "new", "namespace": "prod", "workload_kind": "Deployment", "workload_name": ""},
        )
        self.assertEqual(manifest["metadata"]["name"], "checkout")
        self.assertTrue(report["immutable_images"])
        self.assertEqual(len(report["digest"]), 16)

    def test_frontier_sre_score_explains_every_dimension(self):
        score = _score_benchmark_answer(
            "根因候选：OOMKilled。证据来自 previous logs、Events 和 Deployment spec。先人工审批并 dry-run，"
            "patch resources 后 rollout；持续观察 Pod Ready、restart_count、P95 和错误率 15 分钟。未恢复则回滚并重新取证依赖拓扑。",
            {"findings": [{"name": "checkout", "namespace": "prod", "category": "crashloop"}]},
            1800,
            {"total_tokens": 1200},
        )
        self.assertEqual(sum(item["weight"] for item in score["criteria"]), 100)
        self.assertEqual(len(score["criteria"]), 6)
        self.assertIn(score["grade"], {"S", "A", "B", "C", "D"})
        self.assertTrue(all("evidence" in item and "missing" in item for item in score["criteria"]))

    def test_emergency_recovery_can_bypass_budget_freeze_with_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReliabilityStore(str(Path(directory) / "reliability.json"))
            store.upsert_objective({
                "service": "checkout", "target_percent": 99.9, "window_days": 30,
                "observed_minutes": 43200, "downtime_minutes": 120,
            })

            async def submit(release, actor):
                return {"id": "ops-emergency", "status": "queued", "actor": actor}

            app = FastAPI()
            app.include_router(build_reliability_router(ReliabilityDependencies(
                store=store,
                gate_evaluator=lambda *args: {"verdict": "pass", "risk": {"risk_score": 0.2}},
                submit_release=submit,
            )))
            client = TestClient(app)
            response = client.post("/api/releases", json={
                "service": "checkout", "cluster": "local", "namespace": "prod",
                "workload_kind": "Deployment", "workload_name": "checkout",
                "release_mode": "existing", "change_channel": "emergency_recovery",
                "emergency_action": "restart_component",
                "emergency_reason": "当前组件故障导致业务不可用，需要受控重启恢复服务",
            })
            self.assertEqual(response.status_code, 200, response.text)
            release = response.json()["release"]
            self.assertEqual(release["status"], "awaiting_approval")
            self.assertTrue(release["emergency_audit"]["budget_freeze_bypassed"])
            approved = client.post(f"/api/releases/{release['id']}/approve", json={"confirm": True, "comment": "已复核影响范围和回退条件"})
            self.assertEqual(approved.status_code, 200, approved.text)
            executed = client.post(f"/api/releases/{release['id']}/execute")
            self.assertEqual(executed.status_code, 200, executed.text)

    def test_standard_release_returns_operator_readable_report(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReliabilityStore(str(Path(directory) / "reliability.json"))
            store.upsert_objective({"service": "checkout", "target_percent": 99.9, "window_days": 30})

            app = FastAPI()
            app.include_router(build_reliability_router(ReliabilityDependencies(
                store=store,
                gate_evaluator=lambda *args: {
                    "verdict": "pass",
                    "reason": "候选策略位于错误预算安全包络内。",
                    "risk": {"diff_risk": 0.12, "amplification_factor": 0.35},
                    "selected_strategy": {"first_ratio": 0.01, "step_ratio": 0.02, "max_ratio": 0.1, "observation_window_min": 20, "batches": 5},
                    "candidate_strategies": [{"within_envelope": True}],
                    "safety_envelope": {"budget_cost_limit": 0.02},
                    "blast_radius": {
                        "impact_level": "low",
                        "amplification_factor": 0.35,
                        "blast_radius": {"impacted_services": [], "impacted_pods": [], "related_dependencies": [], "critical_paths": []},
                    },
                    "algorithm": {"name": "SemanticGrayReleaseGate"},
                },
                submit_release=lambda release, actor: {"id": "ops-release", "status": "queued"},
            )))
            client = TestClient(app)
            response = client.post("/api/releases", json={
                "service": "checkout", "cluster": "local", "namespace": "prod",
                "workload_kind": "Deployment", "workload_name": "checkout",
                "release_mode": "existing", "change_channel": "standard",
                "container_name": "app", "image": "registry.local/checkout:v1.2.3",
                "change_summary": "升级 checkout 到 v1.2.3",
            })
            self.assertEqual(response.status_code, 200, response.text)
            report = response.json()["release"]["report"]
            self.assertIn("灰度", report["allowed_scope"])
            self.assertIn("镜像", report["image_check"])
            self.assertGreaterEqual(len(report["evidence"]), 4)

    def test_standard_release_blocks_high_risk_image_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReliabilityStore(str(Path(directory) / "reliability.json"))
            store.upsert_objective({"service": "checkout", "target_percent": 99.9, "window_days": 30})
            app = FastAPI()
            app.include_router(build_reliability_router(ReliabilityDependencies(
                store=store,
                gate_evaluator=lambda *args: {"verdict": "pass", "risk": {"diff_risk": 0.1}, "reason": "灰度策略可行"},
                submit_release=lambda release, actor: {"id": "ops-release", "status": "queued"},
            )))
            client = TestClient(app)
            with patch("backend.app.api.reliability._scan_release_images", AsyncMock(return_value={
                "status": "ok",
                "summary": "发现 1 个 high 漏洞。",
                "risk_level": "high",
                "high": 1,
                "images": ["registry.local/checkout:v1.2.3"],
            })):
                response = client.post("/api/releases", json={
                    "service": "checkout", "cluster": "local", "namespace": "prod",
                    "workload_kind": "Deployment", "workload_name": "checkout",
                    "release_mode": "existing", "change_channel": "standard",
                    "container_name": "app", "image": "registry.local/checkout:v1.2.3",
                    "change_summary": "升级 checkout 到 v1.2.3",
                })
            self.assertEqual(response.status_code, 200, response.text)
            release = response.json()["release"]
            self.assertEqual(release["status"], "blocked")
            self.assertEqual(release["gate"]["action"], "block_image_risk")
            self.assertEqual(release["report"]["image_scan"]["risk_level"], "high")


if __name__ == "__main__":
    unittest.main()
