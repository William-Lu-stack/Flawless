import base64
import asyncio
import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request

from agents import effectiveness
from agents.remediation_engine import action_catalog_payload
from backend.app import main as server
from backend.app.schemas.chat import ChatRiskRankRequest
from backend.app.schemas.operations import OpsJobCreateRequest, OpsSkillDefinition
from backend.app.services.agent_skill_packages import write_package
from backend.app.services.ops_skill_registry import (
    OpsSkillRegistry,
    approved_script_catalog,
    skill_option_catalog,
)
from backend.app.services.ops_skill_runtime import classify_volume_write_failure
from backend.app.services.log_evidence import triage_kubernetes_logs


class OpsSkillCatalogTests(unittest.TestCase):
    def test_first_upgraded_boot_replaces_stale_builtin_crashloop_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ops-skills"
            legacy = Path(directory) / "ops-skills.json"
            legacy.write_text(
                json.dumps({
                    "skills": [{
                        "id": "skill-crashloop-root-cause",
                        "name": "旧版 CrashLoop 根因分流",
                        "category": "runtime",
                        "summary": "旧版补采后会停在当前 Skill。",
                        "symptoms": ["CrashLoopBackOff"],
                        "diagnostic_steps": ["采集证据后停止"],
                        "allowed_actions": ["patch_workload"],
                        "evidence_required": ["last_state"],
                        "builtin": True,
                        "execution_ready": True,
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            registry = OpsSkillRegistry(root, legacy_path=legacy)
            payload = registry.list()
            skill = next(
                item
                for item in payload["skills"]
                if item["id"] == "skill-crashloop-root-cause"
            )

            self.assertEqual(skill["version"], "2.1.0")
            self.assertEqual(skill["skill_type"], "router")
            self.assertTrue(skill["routing_only"])
            self.assertTrue(skill["handoff_required"])
            self.assertFalse(skill["execution_ready"])
            self.assertEqual(skill["allowed_actions"], [])
            self.assertEqual(
                skill["evidence_required"],
                ["last_state", "workload_spec"],
            )
            self.assertTrue(
                any(
                    item == "builtin-policy-upgraded:skill-crashloop-root-cause:legacy"
                    for item in payload["load_errors"]
                )
            )
            persisted = (
                root
                / "skill-crashloop-root-cause"
                / "references"
                / "ops-policy.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("routing_only: true", persisted)
            self.assertIn("handoff_required: true", persisted)
            second_boot = OpsSkillRegistry(root, legacy_path=legacy).list()
            self.assertFalse(
                any(
                    item.startswith("builtin-policy-upgraded:")
                    for item in second_boot["load_errors"]
                )
            )

    def test_persisted_package_cannot_shadow_builtin_policy_by_clearing_builtin_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ops-skills"
            write_package(root, {
                "id": "skill-crashloop-root-cause",
                "version": "1.0.0",
                "name": "同 ID 的旧导入包",
                "summary": "试图把路由 Skill 变成终态执行 Skill。",
                "category": "runtime",
                "symptoms": ["CrashLoopBackOff"],
                "evidence_required": ["last_state"],
                "diagnostic_steps": ["补采后停止"],
                "allowed_actions": ["patch_workload"],
                "execution_ready": True,
                "runtime_handler": "untrusted_handler",
                "routing_only": False,
                "handoff_required": False,
                "builtin": False,
                "enabled": True,
            })

            payload = OpsSkillRegistry(root).list()
            skill = next(
                item
                for item in payload["skills"]
                if item["id"] == "skill-crashloop-root-cause"
            )

            self.assertEqual(skill["version"], "2.1.0")
            self.assertEqual(skill["runtime_handler"], "")
            self.assertTrue(skill["builtin"])
            self.assertTrue(skill["routing_only"])
            self.assertTrue(skill["handoff_required"])
            self.assertFalse(skill["execution_ready"])
            self.assertEqual(skill["allowed_actions"], [])
            self.assertIn(
                "builtin-policy-upgraded:skill-crashloop-root-cause:package",
                payload["load_errors"],
            )

    def test_log_triage_prioritizes_semantic_write_errors_over_info_noise(self):
        triage = triage_kubernetes_logs({
            "grafana": {
                "current": "\n".join([
                    "logger=settings level=info msg=\"Starting Grafana\"",
                    "logger=settings level=info msg=\"Config loaded\"",
                    "GF_PATHS_DATA='/var/lib/grafana' is not writable.",
                    "logger=sqlstore level=info msg=\"Connecting to DB\"",
                    "Error: unable to open database file (14)",
                    "logger=cleanup level=warn msg=\"retrying startup\"",
                ]),
            },
        })
        self.assertTrue(triage["actionable"])
        self.assertGreaterEqual(triage["error_count"], 2)
        self.assertEqual(triage["warning_count"], 1)
        excerpt = triage["priority"][0]["excerpt"]
        self.assertIn("not writable", excerpt)
        self.assertIn("unable to open database", excerpt)

    def test_log_triage_falls_back_to_tail_when_no_error_or_warning(self):
        triage = triage_kubernetes_logs({"app": {"current": "booting\nready\nserving"}})
        self.assertFalse(triage["actionable"])
        self.assertEqual(triage["priority"], [])
        self.assertIn("serving", triage["fallback"][0]["excerpt"])

    def test_change_audit_receipt_keeps_identity_without_full_resource(self):
        receipt = server._change_result_audit_receipt({
            "operation": "created",
            "resource": {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {
                    "name": "data-pv",
                    "uid": "resource-uid",
                    "resourceVersion": "42",
                    "managedFields": [{"large": "internal-structure"}],
                },
                "spec": {"hostPath": {"path": "/internal/path"}},
                "status": {"phase": "Available"},
            },
        })
        self.assertEqual(receipt["operation"], "created")
        self.assertEqual(receipt["kind"], "PersistentVolume")
        self.assertEqual(receipt["resource"]["name"], "data-pv")
        self.assertEqual(receipt["resource_status"]["phase"], "Available")
        self.assertNotIn("spec", receipt)
        self.assertNotIn("managedFields", json.dumps(receipt))

    def test_explicit_grafana_data_path_error_ranks_root_but_starts_nonroot(self):
        logs = "\n".join([
            "GF_PATHS_DATA='/var/lib/grafana' is not writable.",
            "logger=sqlstore msg=\"Connecting to DB\" dbtype=sqlite3",
            "Error: unable to open database file (14)",
        ])
        evidence = {
            "logs": {"grafana": {"current": logs}},
            "events": [],
            "pod": {
                "name": "grafana-abc",
                "security_context": {
                    "runAsUser": 472,
                    "runAsGroup": 472,
                    "runAsNonRoot": True,
                    "fsGroup": 472,
                },
                "containers": [{
                    "name": "grafana",
                    "security_context": {
                        "runAsUser": 472,
                        "runAsGroup": 472,
                        "runAsNonRoot": True,
                    },
                    "volume_mounts": [{
                        "name": "data",
                        "mount_path": "/var/lib/grafana",
                    }],
                }],
            },
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "grafana"},
                "spec": {"template": {"spec": {
                    "securityContext": {
                        "runAsUser": 472,
                        "runAsGroup": 472,
                        "runAsNonRoot": True,
                        "fsGroup": 472,
                    },
                    "containers": [{"name": "grafana"}],
                }}},
            },
            "storage": [{"pvc": "grafana-data", "pvc_phase": "Bound"}],
        }
        plan = {
            "_skill_incident_id": "grafana-write-path",
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "Grafana cannot start",
            "evidence": evidence,
            "changes": [],
        }
        hypothesis = classify_volume_write_failure(plan)
        self.assertEqual(hypothesis["signal_class"], "explicit_mounted_path_not_writable")
        self.assertEqual(hypothesis["candidate_path"], "/var/lib/grafana")
        self.assertTrue(hypothesis["path_on_mount"])
        self.assertEqual(hypothesis["recommended_strategy"], "root_workload_security_context")

        with patch.object(server, "successful_remediation_hint", return_value={}):
            attached = server._attach_operator_skills_to_plan(
                plan,
                {
                    "question": "Grafana data path is not writable",
                    "diagnosis": {
                        "skill_routing": {
                            "primary_skill_id": "skill-volume-permission-recovery",
                            "strategy_id": "root_workload_security_context",
                        },
                    },
                    "evidence": evidence,
                    "plan": plan,
                },
                preferred_skill_ids=["skill-volume-permission-recovery"],
            )
        self.assertEqual(attached["permission_recovery_stage"], "nonroot_group")
        self.assertEqual(
            attached["permission_strategy_decision"]["source"],
            "deepseek_candidate_reasoning+server_evidence_guard",
        )
        spec = attached["changes"][0]["patch"]["spec"]["template"]["spec"]
        self.assertEqual(spec["securityContext"]["runAsUser"], 472)
        self.assertEqual(spec["securityContext"]["runAsGroup"], 472)
        self.assertEqual(spec["securityContext"]["fsGroup"], 472)
        self.assertEqual(spec["securityContext"]["supplementalGroups"], [472])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertEqual(spec["containers"][0]["securityContext"]["runAsUser"], 472)
        self.assertEqual(spec["containers"][0]["securityContext"]["runAsGroup"], 472)
        self.assertTrue(spec["containers"][0]["securityContext"]["runAsNonRoot"])

    def test_live_complete_nonroot_context_is_not_reproposed_and_escalates_to_root(self):
        security = {
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "runAsNonRoot": True,
            "fsGroup": 10001,
            "supplementalGroups": [10001],
            "fsGroupChangePolicy": "OnRootMismatch",
        }
        evidence = {
            "logs": {"grafana": {"current": "\n".join([
                "GF_PATHS_DATA='/var/lib/grafana' is not writable.",
                "Error: unable to open database file",
            ])}},
            "pod": {
                "name": "grafana-abc",
                "security_context": security,
                "containers": [{
                    "name": "grafana",
                    "security_context": {
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "runAsNonRoot": True,
                    },
                    "volume_mounts": [{
                        "name": "data",
                        "mount_path": "/var/lib/grafana",
                    }],
                }],
            },
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "grafana"},
                "spec": {"template": {"spec": {
                    "securityContext": security,
                    "containers": [{
                        "name": "grafana",
                        "securityContext": {
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "runAsNonRoot": True,
                        },
                    }],
                }}},
            },
            "storage": [{"pvc": "grafana-data", "pvc_phase": "Bound"}],
        }
        plan = {
            "_skill_incident_id": "grafana-live-nonroot-noop",
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "Grafana data path is not writable",
            "evidence": evidence,
            "_runtime_evidence": evidence,
            "changes": [],
        }
        attached = server._attach_operator_skills_to_plan(
            plan,
            {
                "question": plan["summary"],
                "diagnosis": {
                    "skill_routing": {
                        "primary_skill_id": "skill-volume-permission-recovery",
                        "strategy_id": "root_workload_security_context",
                    },
                },
                "evidence": evidence,
                "plan": plan,
            },
            preferred_skill_ids=["skill-volume-permission-recovery"],
        )
        self.assertEqual(attached["permission_recovery_stage"], "root")
        self.assertTrue(
            attached["permission_strategy_decision"]["live_nonroot_patch_is_noop"]
        )
        spec = attached["changes"][0]["patch"]["spec"]["template"]["spec"]
        self.assertEqual(spec["securityContext"]["runAsUser"], 0)
        self.assertEqual(spec["securityContext"]["runAsGroup"], 0)
        self.assertEqual(spec["securityContext"]["fsGroup"], 0)
        self.assertEqual(spec["securityContext"]["supplementalGroups"], [0])
        self.assertIs(spec["securityContext"]["runAsNonRoot"], False)
        container = spec["containers"][0]["securityContext"]
        self.assertEqual(container["runAsUser"], 0)
        self.assertEqual(container["runAsGroup"], 0)
        self.assertIs(container["runAsNonRoot"], False)

    def test_live_workload_nonroot_contract_overrides_stale_rollout_pod(self):
        """A retained old CrashLoop Pod must not make the same patch look new."""
        workload_security = {
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "runAsNonRoot": True,
            "fsGroup": 10001,
            "supplementalGroups": [10001],
            "fsGroupChangePolicy": "OnRootMismatch",
        }
        evidence = {
            "logs": {"grafana": {"current": (
                "GF_PATHS_DATA='/var/lib/grafana' is not writable.\n"
                "Error: unable to open database file (14)"
            )}},
            # During a broken rollout the originally selected Pod can still
            # carry the old, incomplete security context.
            "pod": {
                "name": "grafana-old",
                "security_context": {"fsGroup": 10001},
                "containers": [{
                    "name": "grafana",
                    "security_context": {
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "runAsNonRoot": True,
                    },
                    "volume_mounts": [{
                        "name": "data",
                        "mount_path": "/var/lib/grafana",
                    }],
                }],
            },
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "grafana", "generation": 8},
                "spec": {"template": {"spec": {
                    "securityContext": workload_security,
                    "containers": [{
                        "name": "grafana",
                        "securityContext": {
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "runAsNonRoot": True,
                            "allowPrivilegeEscalation": False,
                        },
                        "volumeMounts": [{
                            "name": "data",
                            "mountPath": "/var/lib/grafana",
                        }],
                    }],
                }}},
            },
        }
        plan = {
            "namespace": "k8s-agent",
            "target": "Deployment/k8s-agent-grafana",
            "summary": "Grafana CrashLoopBackOff",
            "evidence": evidence,
            "_runtime_evidence": evidence,
            "changes": [],
        }

        attached = server._attach_operator_skills_to_plan(
            plan,
            {
                "question": plan["summary"],
                "diagnosis": {"root_cause": "mounted Grafana path is not writable"},
                "evidence": evidence,
                "plan": plan,
            },
            preferred_skill_ids=["skill-volume-permission-recovery"],
        )

        self.assertEqual(attached["permission_recovery_stage"], "root")
        self.assertIn(
            "nonroot_group",
            attached["_attempted_permission_recovery_stages"],
        )
        patch_spec = attached["changes"][0]["patch"]["spec"]["template"]["spec"]
        self.assertEqual(patch_spec["securityContext"]["runAsUser"], 0)
        self.assertEqual(patch_spec["securityContext"]["runAsGroup"], 0)
        self.assertEqual(patch_spec["securityContext"]["fsGroup"], 0)
        self.assertIs(patch_spec["securityContext"]["runAsNonRoot"], False)

    def test_live_root_contract_is_not_repeated_when_storage_still_fails(self):
        root_pod_security = copy.deepcopy(server.ROOT_POD_SECURITY_CONTEXT)
        root_container_security = copy.deepcopy(server.ROOT_CONTAINER_SECURITY_CONTEXT)
        evidence = {
            "logs": {"grafana": {"current": (
                "GF_PATHS_DATA='/var/lib/grafana' is not writable.\n"
                "Error: unable to open database file (14)"
            )}},
            "pod": {
                "name": "grafana-root",
                "security_context": root_pod_security,
                "containers": [{
                    "name": "grafana",
                    "security_context": root_container_security,
                    "volume_mounts": [{"name": "data", "mount_path": "/var/lib/grafana"}],
                }],
            },
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "grafana"},
                "spec": {"template": {"spec": {
                    "securityContext": root_pod_security,
                    "containers": [{
                        "name": "grafana",
                        "securityContext": root_container_security,
                        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/grafana"}],
                    }],
                }}},
            },
        }
        plan = {
            "namespace": "k8s-agent",
            "target": "Deployment/k8s-agent-grafana",
            "summary": "Grafana data path is not writable",
            "_runtime_evidence": evidence,
            "evidence": evidence,
        }

        followup = server._permission_recovery_followup(plan)

        self.assertEqual(followup["source"], "storage_admin_required")
        self.assertEqual(followup["changes"], [])
        self.assertIn("root", plan["_attempted_permission_recovery_stages"])
        self.assertTrue(plan["permission_strategy_decision"]["live_root_patch_is_noop"])

    def test_capacity_evidence_prevents_direct_root_strategy(self):
        plan = {
            "summary": "data path failure",
            "evidence": {
                "logs": {"db": {"current": (
                    "DATA_PATH='/var/lib/app' is not writable; "
                    "unable to open database file; no space left on device"
                )}},
                "pod": {
                    "security_context": {"runAsUser": 10001, "runAsNonRoot": True},
                    "containers": [{
                        "name": "db",
                        "security_context": {"runAsUser": 10001, "runAsNonRoot": True},
                        "volume_mounts": [{"name": "data", "mount_path": "/var/lib/app"}],
                    }],
                },
            },
        }
        hypothesis = classify_volume_write_failure(plan)
        self.assertNotEqual(hypothesis["recommended_strategy"], "root_workload_security_context")
        root = next(
            item for item in hypothesis["strategy_candidates"]
            if item["strategy_id"] == "root_workload_security_context"
        )
        self.assertIn("no space left on device", root["contradicting_evidence"])

    def test_verified_root_recovery_is_reused_as_strategy_hint_only(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "EFFECTIVENESS_STORE_PATH": str(Path(directory) / "effectiveness.json"),
                "EFFECTIVENESS_STORE_FALLBACK_PATH": str(Path(directory) / "fallback.json"),
            }):
                effectiveness._STORE_LOADED_FROM = ""
                effectiveness.INSPECTION_RUNS.clear()
                effectiveness.REMEDIATION_OUTCOMES.clear()
                recovered_plan = {
                    "id": "plan-1",
                    "target": "Deployment/metrics",
                    "namespace": "monitoring",
                    "selected_skill_id": "skill-volume-permission-recovery",
                    "permission_recovery_stage": "root",
                    "evidence": {
                        "logs": {"metrics": {"current": (
                            "DATA_PATH='/var/lib/metrics' is not writable; "
                            "unable to open database file"
                        )}},
                    },
                    "changes": [{
                        "type": "patch_workload_runtime_security",
                        "workload_type": "Deployment",
                        "workload_name": "metrics",
                        "patch": {"spec": {"template": {"spec": {
                            "securityContext": {"runAsUser": 0, "runAsGroup": 0, "fsGroup": 0},
                        }}}},
                    }],
                }
                effectiveness.record_remediation(recovered_plan, {
                    "status": "completed",
                    "results": [{"status": "success"}],
                    "verification": {"recovered": True},
                })
                hint = effectiveness.successful_remediation_hint(
                    {
                        "target": "Deployment/metrics",
                        "evidence": recovered_plan["evidence"],
                    },
                    "skill-volume-permission-recovery",
                )
                self.assertEqual(hint["strategy_id"], "root_workload_security_context")
                self.assertGreaterEqual(hint["confidence"], 0.86)
                self.assertNotIn("patch", hint)
                effectiveness._STORE_LOADED_FROM = ""
                effectiveness.INSPECTION_RUNS.clear()
                effectiveness.REMEDIATION_OUTCOMES.clear()

    def test_database_open_error_is_correlated_as_write_path_hypothesis(self):
        plan = {
            "summary": "CrashLoopBackOff",
            "evidence": {
                "logs": {"api": {"previous": "sqlite3.OperationalError: unable to open database file"}},
                "pod": {
                    "security_context": {"runAsUser": 10001, "runAsGroup": 10001},
                    "containers": [{
                        "name": "api",
                        "security_context": {"runAsUser": 10001, "runAsGroup": 10001},
                        "volume_mounts": [{"name": "data", "mount_path": "/var/lib/app"}],
                    }],
                },
                "workload": {
                    "kind": "Deployment",
                    "spec": {"template": {"spec": {"securityContext": {"runAsUser": 10001}}}},
                },
            },
        }
        hypothesis = classify_volume_write_failure(plan)
        self.assertTrue(hypothesis["detected"])
        self.assertEqual(hypothesis["signal_class"], "indirect_write_path_error")
        self.assertGreaterEqual(hypothesis["confidence"], 0.72)
        self.assertIn("container_volume_mount", hypothesis["corroboration"])

    def test_executable_permission_skill_materializes_same_runtime_plan(self):
        evidence = {
            "logs": {"api": {"previous": "sqlite3.OperationalError: unable to open database file"}},
            "events": [],
            "pod": {
                "name": "api-abc",
                "security_context": {},
                "containers": [{
                    "name": "api",
                    "security_context": {"runAsUser": 10001, "runAsGroup": 10001},
                    "volume_mounts": [{"name": "data", "mount_path": "/var/lib/app"}],
                }],
            },
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "api"},
                "spec": {"template": {"spec": {"containers": [{"name": "api"}]}}},
            },
            "storage": [{"pvc": "api-data", "pvc_phase": "Bound"}],
        }
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "summary": "unable to open database file",
            "evidence": evidence,
            "changes": [],
        }
        attached = server._attach_operator_skills_to_plan(
            plan,
            {"question": plan["summary"], "evidence": evidence, "plan": plan},
            preferred_skill_ids=["skill-volume-permission-recovery"],
        )
        self.assertEqual(attached["selected_skill_id"], "skill-volume-permission-recovery")
        self.assertEqual(attached["change_source"], "executable_skill")
        self.assertEqual(attached["permission_recovery_stage"], "nonroot_group")
        self.assertEqual(attached["skill_runtime"]["handler_id"], "volume-write-permission-recovery")
        self.assertEqual(attached["changes"][0]["selection_source"], "executable_skill_handler")

    def test_chat_and_inspection_use_identical_executable_skill_core(self):
        evidence = {
            "logs": {"api": {"current": "sqlite3.OperationalError: unable to open database file"}},
            "events": [],
            "pod": {
                "name": "api-abc",
                "security_context": {},
                "containers": [{
                    "name": "api",
                    "security_context": {"runAsUser": 10001, "runAsGroup": 10001},
                    "volume_mounts": [{"name": "data", "mount_path": "/var/lib/app"}],
                }],
            },
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "api"},
                "spec": {"template": {"spec": {"containers": [{"name": "api"}]}}},
            },
            "storage": [{"pvc": "api-data", "pvc_phase": "Bound"}],
        }
        outputs = []
        for surface in ("sre_chat", "ai_inspection"):
            plan = {
                "_skill_incident_id": f"{surface}-incident",
                "source_surface": surface,
                "namespace": "default",
                "target": "Deployment/api",
                "summary": "unable to open database file",
                "evidence": evidence,
                "changes": [],
            }
            outputs.append(server._attach_operator_skills_to_plan(
                plan,
                {"question": plan["summary"], "evidence": evidence, "plan": plan},
                preferred_skill_ids=["skill-volume-permission-recovery"],
            ))
        self.assertEqual(outputs[0]["skill_runtime"], outputs[1]["skill_runtime"])
        self.assertEqual(outputs[0]["permission_recovery_stage"], outputs[1]["permission_recovery_stage"])
        self.assertEqual(outputs[0]["changes"][0]["patch"], outputs[1]["changes"][0]["patch"])

    def test_skill_route_metrics_are_idempotent_for_same_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            plan = {"_skill_incident_id": "incident-1"}
            with patch.object(server, "OPS_SKILL_REGISTRY", registry):
                server._record_skill_route_once(plan, "skill-volume-permission-recovery", "matched")
                server._record_skill_route_once(plan, "skill-volume-permission-recovery", "matched")
                server._record_skill_route_once(plan, "skill-volume-permission-recovery", "selected")
                server._record_skill_route_once(plan, "skill-volume-permission-recovery", "selected")
            row = next(
                item for item in registry.usage_stats()["skills"]
                if item["skill_id"] == "skill-volume-permission-recovery"
            )
            self.assertEqual(row["matched"], 1)
            self.assertEqual(row["selected"], 1)

    def test_skill_effectiveness_uses_distinct_incidents(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            skill_id = "skill-volume-permission-recovery"
            registry.record_incident(skill_id, "incident-a", "handled")
            registry.record_incident(skill_id, "incident-a", "handled")
            registry.record_incident(skill_id, "incident-a", "resolved")
            registry.record_incident(skill_id, "incident-b", "handled")
            row = next(item for item in registry.usage_stats()["skills"] if item["skill_id"] == skill_id)
            self.assertEqual(row["incidents_handled"], 2)
            self.assertEqual(row["incidents_resolved"], 1)
            self.assertEqual(row["success_rate"], 0.5)

    def test_secret_values_are_removed_from_all_public_payloads(self):
        encoded_fixture = base64.b64encode("-".join(("fixture", "value")).encode("utf-8")).decode("ascii")
        value = server._redact_sensitive({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "registry-auth"},
            "data": {"password": encoded_fixture},
            "stringData": {"username": "operator"},
        })
        self.assertEqual(value["data"], "[REDACTED]")
        self.assertEqual(value["stringData"], "[REDACTED]")

    def test_candidate_lifecycle_is_persisted_until_operator_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            registry = OpsSkillRegistry(root)
            candidate = registry.upsert({
                "id": "candidate-permission-recovery",
                "name": "候选权限恢复",
                "summary": "根据已验证恢复生成，等待人工审核。",
                "symptoms": ["permission denied"],
                "applies_to": ["Deployment"],
                "evidence_required": ["previous_logs", "workload_spec"],
                "diagnostic_steps": ["核对失败路径和 YAML 差异"],
                "allowed_actions": ["patch_resource"],
                "success_criteria": ["pod_ready"],
                "enabled": False,
                "lifecycle": "candidate",
            }, actor="learning-loop")
            self.assertEqual(candidate["lifecycle"], "candidate")
            reloaded = OpsSkillRegistry(root)
            stored = next(item for item in reloaded.list()["skills"] if item["id"] == candidate["id"])
            self.assertFalse(stored["enabled"])
            self.assertEqual(stored["lifecycle"], "candidate")
            published = reloaded.upsert({**stored, "enabled": True, "lifecycle": "published"}, actor="operator")
            self.assertTrue(published["enabled"])
            self.assertEqual(published["lifecycle"], "published")

    def test_skill_usage_counts_only_execution_as_invocation_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            registry = OpsSkillRegistry(root)
            skill_id = "skill-volume-permission-recovery"
            for event in ("matched", "selected", "approval_requested", "executed", "succeeded", "rolled_back"):
                registry.record_usage(skill_id, event)
            reloaded = OpsSkillRegistry(root)
            row = next(item for item in reloaded.usage_stats()["skills"] if item["skill_id"] == skill_id)
            self.assertEqual(row["matched"], 1)
            self.assertEqual(row["selected"], 1)
            self.assertEqual(row["approval_requested"], 1)
            self.assertEqual(row["executed"], 1)
            self.assertEqual(row["succeeded"], 1)
            self.assertEqual(row["rolled_back"], 1)
            self.assertEqual(row["success_rate"], 1.0)
            self.assertIn("executed 才计为 Skill 调用", reloaded.usage_stats()["definition"])

    def test_skill_match_exposes_required_weighted_score_breakdown(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            match = registry.match({
                "question": "Deployment mkdir /data/cache permission denied",
                "evidence": {
                    "logs": {"api": "mkdir /data/cache: permission denied"},
                    "workload": {"kind": "Deployment"},
                    "storage": [{"pvc_phase": "Bound"}],
                },
            })["matches"][0]
            self.assertEqual(
                set(match["score_breakdown"]),
                {
                    "symptom_log_match",
                    "environment_match",
                    "semantic_similarity",
                    "hypothesis_alignment",
                    "model_skill_prior",
                    "evidence_readiness",
                    "historical_success",
                    "least_privilege",
                    "recency_reliability",
                    "information_gain",
                    "exploration",
                    "lineage_failure_penalty",
                    "broad_diagnostic_penalty",
                    "supporting_role_penalty",
                    "inference_confidence",
                    "contextual_utility",
                    "selection_utility",
                },
            )
            result = registry.match({"question": "permission denied"})
            self.assertEqual(
                result["selection_algorithm"]["id"],
                "contextual_bayesian_utility_v4",
            )
            self.assertIn("Beta 后验成功率", result["policy"])
            self.assertEqual(match["selection_algorithm"], "contextual_bayesian_utility_v4")

    def test_root_cause_hypotheses_drive_skill_ranking_without_keyword_runbook(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            result = registry.match({
                "question": "application startup failed",
                "diagnosis": {
                    "root_cause_candidates": [
                        {
                            "id": "write-path",
                            "hypothesis": (
                                "sqlite database parent directory is not writable because "
                                "container runAsUser and mounted volume ownership mismatch"
                            ),
                            "confidence": 0.91,
                            "supporting_evidence": [
                                "unable to open database file",
                                "volumeMount /var/lib/app",
                                "runAsNonRoot true",
                            ],
                        },
                        {
                            "id": "database-corruption",
                            "hypothesis": "database file corruption",
                            "confidence": 0.35,
                        },
                    ],
                },
                "evidence": {
                    "logs": {"api": {"current": "sqlite3.OperationalError: unable to open database file"}},
                    "events": [],
                    "pod": {
                        "security_context": {"runAsNonRoot": True},
                        "containers": [{"name": "api"}],
                    },
                    "workload": {"kind": "Deployment"},
                    "storage": [{"pvc_phase": "Bound", "storage_class": "nfs"}],
                },
            })
            top = result["matches"][0]
            self.assertEqual(top["skill"]["id"], "skill-volume-permission-recovery")
            self.assertGreaterEqual(top["confidence"], result["execution_threshold"])
            self.assertGreater(top["score_breakdown"]["hypothesis_alignment"], 0.4)
            self.assertTrue(top["matched_hypotheses"])

    def test_model_skill_hint_is_prior_and_executable_continuations_are_not_penalized(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            evidence = {
                "logs": {"api": {"current": "unable to open database file"}},
                "events": [],
                "pod": {
                    "security_context": {"runAsNonRoot": True},
                    "containers": [{"name": "api"}],
                },
                "workload": {"kind": "Deployment"},
                "storage": [{"pvc_phase": "Bound", "storage_class": "nfs"}],
            }
            payload = {
                "question": "application startup failed",
                "diagnosis": {
                    "root_cause_candidates": [{
                        "hypothesis": (
                            "mounted database path cannot be written by the configured "
                            "runAsUser and runAsGroup"
                        ),
                        "confidence": 0.94,
                        "supporting_evidence": ["unable to open database file", "volumeMount"],
                    }],
                    # Deliberately wrong model hint: evidence must still win.
                    "skill_routing": {
                        "primary_skill_id": "skill-service-endpoint-flow",
                    },
                },
                "evidence": evidence,
                "plan": {},
            }
            first = registry.match(payload)
            self.assertEqual(
                first["matches"][0]["skill"]["id"],
                "skill-volume-permission-recovery",
            )

            failed_payload = {
                **payload,
                "plan": {
                    "_attempted_skill_ids": ["skill-service-endpoint-flow"],
                },
            }
            failed = registry.match(failed_payload, top_k=20)
            service_match = next(
                item for item in failed["matches"]
                if item["skill"]["id"] == "skill-service-endpoint-flow"
            )
            self.assertEqual(
                service_match["score_breakdown"]["lineage_failure_penalty"],
                0.0,
            )
            self.assertTrue(service_match["skill"]["continuation_capable"])

            continuation_payload = {
                **payload,
                "plan": {
                    "_attempted_skill_ids": ["skill-volume-permission-recovery"],
                },
            }
            continuation = registry.match(continuation_payload, top_k=20)
            volume_match = next(
                item for item in continuation["matches"]
                if item["skill"]["id"] == "skill-volume-permission-recovery"
            )
            self.assertEqual(
                volume_match["score_breakdown"]["lineage_failure_penalty"],
                0.0,
            )

    def test_prior_route_descriptions_do_not_pollute_fresh_skill_ranking(self):
        evidence = {
            "logs": {"writer": {"current": "unable to open database file"}},
            "events": [],
            "pod": {
                "security_context": {"runAsUser": 10001, "runAsNonRoot": True},
                "containers": [{
                    "name": "writer",
                    "security_context": {"runAsUser": 10001, "runAsNonRoot": True},
                    "volume_mounts": [{"name": "data", "mount_path": "/var/lib/app"}],
                }],
            },
            "workload": {"kind": "Deployment", "metadata": {"name": "writer"}},
            "storage": [{"pvc_phase": "Bound", "storage_class": "nfs"}],
        }
        plan = {
            "_skill_incident_id": "route-pollution",
            "namespace": "default",
            "target": "Deployment/writer",
            "summary": "unable to open database file",
            "evidence": evidence,
            "changes": [],
            "operator_skills": [{
                "id": "skill-crashloop-root-cause",
                "summary": (
                    "CrashLoopBackOff permission denied unable to open database "
                    "file workload_spec pod_security_context current_logs"
                ),
                "confidence": 0.99,
            }],
            "skill_candidates": [{
                "id": "skill-crashloop-root-cause",
                "confidence": 0.99,
            }],
        }
        attached = server._attach_operator_skills_to_plan(
            plan,
            {
                "question": plan["summary"],
                "diagnosis": {
                    "root_cause_candidates": [{
                        "hypothesis": "mounted database path cannot be written by runAsUser",
                        "confidence": 0.92,
                        "supporting_evidence": ["volumeMount", "runAsNonRoot"],
                    }],
                    "skill_routing": {
                        "primary_skill_id": "skill-volume-permission-recovery",
                    },
                },
                "evidence": evidence,
                "plan": plan,
            },
            preferred_skill_ids=["skill-volume-permission-recovery"],
        )
        self.assertEqual(
            attached["selected_skill_id"],
            "skill-volume-permission-recovery",
        )
        self.assertEqual(
            attached["operator_skills"][0]["id"],
            "skill-volume-permission-recovery",
        )

    def test_persisted_builtin_skill_is_upgraded_to_current_security_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            registry = OpsSkillRegistry(root)
            current = next(item for item in registry.list()["skills"] if item["id"] == "skill-storage-pvc-pv")
            registry.upsert({
                **current,
                "builtin": True,
                "evidence_required": ["storage_chain", "events", "storage_class", "node_storage", "csi_status"],
            }, actor="old-release")
            reloaded = OpsSkillRegistry(root)
            upgraded = next(item for item in reloaded.list()["skills"] if item["id"] == "skill-storage-pvc-pv")
            self.assertEqual(upgraded["evidence_required"], ["storage_chain", "events", "workload_spec"])
            self.assertEqual(upgraded["evidence_any_of"], [["pvc_binding", "storage_class"]])
            self.assertEqual(upgraded["runtime_handler"], "pvc-pv-binding-recovery")

    def test_dynamic_skill_authorizes_and_binds_permission_action(self):
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "summary": "mkdir /work/cache permission denied after init container chmod",
            "evidence": {
                "state_text": "mkdir: permission denied",
                "logs": {"api": {"previous": "mkdir /work/cache: permission denied"}},
                "events": [],
                "workload": {"kind": "Deployment", "metadata": {"generation": 3}},
                "pod": {"containers": [{"name": "api", "reason": "CrashLoopBackOff", "restart_count": 2}], "security_context": {}},
                "storage": [{"pvc": "api-data", "pvc_phase": "Bound", "storage_class": "nfs"}],
            },
            "changes": [{
                "type": "patch_resource",
                "api_version": "apps/v1",
                "kind": "Deployment",
                "name": "api",
                "namespace": "default",
                "patch": {"spec": {"template": {"metadata": {"annotations": {"recovery": "approved"}}}}},
            }],
        }
        attached = server._attach_operator_skills_to_plan(plan, {
            "question": plan["summary"],
            "evidence": plan["evidence"],
            "plan": plan,
        })
        self.assertEqual(
            attached["selected_skill_id"],
            "skill-volume-permission-recovery",
            "a generic lower-risk CrashLoop skill must not outrank stronger permission evidence",
        )
        self.assertEqual(attached["change_source"], "executable_skill")
        self.assertEqual(attached["skill_runtime"]["handler_id"], "volume-write-permission-recovery")
        self.assertEqual(attached["changes"][0]["skill_id"], "skill-volume-permission-recovery")
        self.assertTrue(attached["changes"][0]["skill_supported"])

    def test_adaptive_router_activates_only_highest_ranked_skill(self):
        primary = {
            "id": "primary",
            "name": "最高匹配",
            "category": "storage",
            "summary": "permission denied",
            "diagnostic_steps": ["读取日志"],
            "evidence_required": [],
            "allowed_actions": ["restart"],
            "success_criteria": ["pod_ready"],
            "risk": "low",
            "enabled": True,
            "execution_ready": True,
        }
        secondary = {
            **primary,
            "id": "secondary",
            "name": "次高匹配",
            "allowed_actions": ["patch_workload"],
        }
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "api",
            }],
        }
        matches = {
            "matches": [
                {"skill": primary, "confidence": 0.92, "score": 0.92},
                {"skill": secondary, "confidence": 0.90, "score": 0.90},
            ],
            "execution_threshold": 0.70,
        }
        with (
            patch.object(server.OPS_SKILL_REGISTRY, "match", return_value=matches),
            patch.object(server.OPS_SKILL_REGISTRY, "record_usage"),
        ):
            attached = server._attach_operator_skills_to_plan(plan, {"question": "permission denied"})
        self.assertEqual(attached["selected_skill_id"], "primary")
        self.assertEqual(attached["skill_execution_mode"], "adaptive_serial")
        self.assertEqual(len(attached["operator_skills"]), 2)
        self.assertEqual(attached["changes"][0]["skill_id"], "primary")
        self.assertEqual(attached["skill_allowed_actions"], ["restart"])

    def test_low_confidence_skill_runs_diagnostics_before_mutation(self):
        skill = {
            "id": "uncertain",
            "name": "低置信度 Skill",
            "category": "storage",
            "summary": "possible storage issue",
            "diagnostic_steps": ["读取 Events"],
            "evidence_required": [],
            "allowed_actions": ["restart"],
            "success_criteria": ["pod_ready"],
            "risk": "low",
            "enabled": True,
            "execution_ready": True,
        }
        matches = {
            "matches": [{"skill": skill, "confidence": 0.69, "score": 0.69}],
            "execution_threshold": 0.70,
        }
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "api",
            }],
        }
        with (
            patch.object(server.OPS_SKILL_REGISTRY, "match", return_value=matches),
            patch.object(server.OPS_SKILL_REGISTRY, "record_usage"),
        ):
            attached = server._attach_operator_skills_to_plan(plan, {"question": "uncertain"})
        self.assertEqual(attached["decision"], "diagnostic_skill_then_rerank")
        self.assertEqual(attached["changes"], [])
        self.assertEqual(attached["_blocked_low_confidence_changes"][0]["type"], "restart")

    def test_dynamic_skill_blocks_mutation_until_required_evidence_is_collected(self):
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "summary": "mkdir /work/cache permission denied",
            "changes": [{
                "type": "patch_resource",
                "api_version": "apps/v1",
                "kind": "Deployment",
                "name": "api",
                "namespace": "default",
                "patch": {"metadata": {"annotations": {"recovery": "requested"}}},
            }],
        }
        attached = server._attach_operator_skills_to_plan(plan, {
            "question": plan["summary"],
            "evidence": {"state_text": "mkdir: permission denied"},
            "plan": plan,
        })
        self.assertEqual(attached["changes"], [])
        self.assertEqual(attached["decision"], "skill_evidence_collection_in_progress")
        self.assertTrue(any(item["evidence_missing"] for item in attached["operator_skills"]))
        self.assertEqual(attached["steps"][0]["id"], "skill_evidence_refresh")
        self.assertTrue(attached["skill_workflow_state"]["must_continue"])

    def test_crashloop_router_is_non_terminal_and_schedules_active_evidence(self):
        plan = {
            "namespace": "default",
            "target": "Deployment/api",
            "summary": "CrashLoopBackOff",
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "api",
            }],
        }
        attached = server._attach_operator_skills_to_plan(
            plan,
            {
                "question": "CrashLoopBackOff",
                "evidence": {
                    "pod": {
                        "name": "api-x",
                        "containers": [{
                            "name": "api",
                            "reason": "CrashLoopBackOff",
                            "restart_count": 4,
                        }],
                    },
                },
                "plan": plan,
            },
            preferred_skill_ids=["skill-crashloop-root-cause"],
        )
        self.assertEqual(attached["selected_skill_id"], "skill-crashloop-root-cause")
        self.assertEqual(attached["changes"], [])
        self.assertTrue(attached["operator_skills"][0]["routing_only"])
        self.assertEqual(attached["decision"], "skill_router_collecting_evidence")
        self.assertEqual(attached["steps"][0]["id"], "skill_evidence_refresh")
        self.assertIn("workload_spec", attached["steps"][0]["evidence_ids"])

    def test_option_catalog_exposes_multiselect_fields(self):
        catalog = skill_option_catalog()
        self.assertIn("applies_to", catalog)
        self.assertIn("evidence_required", catalog)
        self.assertIn("success_criteria", catalog)
        self.assertIn("script_triggers", catalog)
        self.assertTrue(any(item["id"] == "previous_logs" for item in catalog["evidence_required"]))
        self.assertTrue(any(item["id"] == "ebpf_flows" for item in catalog["evidence_required"]))
        self.assertTrue(any(item["id"] == "telemetry_fresh" for item in catalog["success_criteria"]))
        self.assertTrue(all(item.get("description") for items in catalog.values() for item in items))

    def test_official_recommended_patterns_are_adapted_as_builtin_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            skills = {item["id"]: item for item in registry.list()["skills"]}
        expected = {
            "skill-kubernetes-progressive-inspection",
            "skill-kubernetes-node-inspection",
            "skill-database-progressive-inspection",
            "skill-observability-collector-recovery",
            "skill-observability-query-generation",
            "skill-topology-data-modeling",
        }
        self.assertTrue(expected.issubset(skills))
        self.assertTrue(all(skills[skill_id]["builtin"] for skill_id in expected))
        self.assertTrue(all(skills[skill_id]["progressive_evidence"] for skill_id in expected))
        self.assertTrue(all(not skills[skill_id]["execution_ready"] for skill_id in expected))
        crashloop = skills["skill-crashloop-root-cause"]
        self.assertEqual(crashloop["skill_type"], "router")
        self.assertTrue(crashloop["routing_only"])
        self.assertTrue(crashloop["handoff_required"])
        self.assertTrue(crashloop["workflow_phases"])
        self.assertIn("普通取证失败", crashloop["evidence_failure_policy"])

    def test_progressive_inspection_and_telemetry_skills_rank_for_their_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            inspection = registry.match({
                "question": "对 Kubernetes 集群做升级前巡检，覆盖管控面、节点、工作负载、网络、存储和 GPU",
            }, top_k=3)
            telemetry = registry.match({
                "question": "Beyla Pod 正常但是拓扑没有 eBPF 流量，检查采集链路",
            }, top_k=3)
            permission = registry.match({
                "question": "GF_PATHS_DATA is not writable; unable to open database file; CrashLoopBackOff",
            }, top_k=3)
        self.assertEqual(inspection["matches"][0]["skill"]["id"], "skill-kubernetes-progressive-inspection")
        self.assertEqual(telemetry["matches"][0]["skill"]["id"], "skill-observability-collector-recovery")
        self.assertEqual(permission["matches"][0]["skill"]["id"], "skill-volume-permission-recovery")

    def test_agent_context_loads_one_primary_body_and_keeps_candidates_lightweight(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            context = registry.agent_context({
                "question": "Beyla Pod 正常但是拓扑没有 eBPF 流量，采集链路和数据模型是否正确",
            }, top_k=3)
        self.assertGreaterEqual(len(context), 2)
        self.assertTrue(context[0]["instructions_loaded"])
        self.assertEqual(context[0]["context_role"], "primary")
        self.assertTrue(all(not item["instructions_loaded"] for item in context[1:]))
        self.assertTrue(all(item["context_role"] == "candidate_metadata" for item in context[1:]))

    def test_skill_chain_requires_explicit_cross_domain_dependency(self):
        primary = {
            "skill": {
                "id": "skill-storage-pvc-pv",
                "name": "存储恢复",
                "category": "storage",
            },
        }
        network = {
            "skill": {
                "id": "skill-service-endpoint-flow",
                "name": "网络恢复",
                "category": "network",
            },
        }
        ignored = server._skill_execution_chain(
            primary,
            [primary, network],
            {
                "secondary_skill_ids": ["skill-service-endpoint-flow"],
                "skill_dependencies": [],
            },
        )
        self.assertEqual(ignored["mode"], "primary_only")
        self.assertEqual(ignored["ignored_secondary_skill_ids"], ["skill-service-endpoint-flow"])

        accepted = server._skill_execution_chain(
            primary,
            [primary, network],
            {
                "secondary_skill_ids": ["skill-service-endpoint-flow"],
                "skill_dependencies": [{
                    "from_skill_id": "skill-storage-pvc-pv",
                    "to_skill_id": "skill-service-endpoint-flow",
                    "reason": "PVC 恢复后 Endpoint 仍为空时再检查网络链路",
                    "gate_evidence": ["pvc_bound", "service_endpoints"],
                }],
            },
        )
        self.assertEqual(accepted["mode"], "serial_dependency")
        self.assertEqual([item["skill_id"] for item in accepted["steps"]], [
            "skill-storage-pvc-pv",
            "skill-service-endpoint-flow",
        ])

    def test_progressive_evidence_plan_marks_collected_and_optional_stages(self):
        skill = {
            "progressive_evidence": [
                {
                    "stage": "direct",
                    "evidence": ["current_logs", "workload_spec"],
                    "stop_when": "根因闭合",
                },
                {
                    "stage": "topology",
                    "evidence": ["dependency_topology"],
                    "optional": True,
                    "stop_when": "仅在需要影响面时加载",
                },
            ],
        }
        evidence_plan = server._skill_progressive_evidence_plan(
            skill,
            {"current_logs", "workload_spec"},
        )
        self.assertEqual(evidence_plan[0]["status"], "completed")
        self.assertEqual(evidence_plan[1]["status"], "optional")
        self.assertEqual(evidence_plan[1]["missing"], ["dependency_topology"])

    def test_action_catalog_has_operator_guidance(self):
        actions = {item["id"]: item for item in action_catalog_payload()}
        self.assertIn("patch_workload", actions)
        self.assertIn("Deployment", actions["patch_workload"]["when_to_use"])
        self.assertTrue(actions["patch_workload"]["label"])
        self.assertTrue(actions["patch_workload"]["operator_note"])

    def test_approved_script_catalog_only_reads_metadata(self):
        value = """[{"id":"inspect-pvc","name":"PVC 权限检查","description":"只读检查挂载目录权限","risk":"medium","allowed_targets":["Pod","PVC"],"required_evidence":["previous_logs"]}]"""
        with patch.dict(os.environ, {"OPS_APPROVED_SCRIPTS_JSON": value}):
            scripts = approved_script_catalog()
        self.assertEqual(scripts[0]["id"], "inspect-pvc")
        self.assertNotIn("content", scripts[0])
        self.assertNotIn("command", scripts[0])

    def test_registry_persists_script_trigger_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills.json")
            skill = registry.upsert({
                "name": "PVC 权限复核",
                "summary": "挂载卷后容器因权限问题反复重启。",
                "symptoms": ["permission denied", "CrashLoopBackOff"],
                "applies_to": ["Pod", "PVC"],
                "evidence_required": ["previous_logs", "storage_chain"],
                "diagnostic_steps": ["读取 previous logs", "核对 PVC 和 securityContext"],
                "allowed_actions": ["patch_workload"],
                "success_criteria": ["pod_ready", "restart_count_stable"],
                "script_policy": {
                    "enabled": True,
                    "script_id": "inspect-pvc",
                    "trigger_conditions": ["required_evidence_collected", "root_cause_confirmed", "manual_confirmation"],
                    "trigger_description": "连续 CrashLoop 且日志明确出现 permission denied 时触发。",
                    "timeout_seconds": 60,
                },
            }, actor="tester")
            self.assertTrue(skill["script_policy"]["enabled"])
            self.assertEqual(skill["script_policy"]["script_id"], "inspect-pvc")
            self.assertTrue(skill["script_policy"]["require_confirmation"])
            package_dir = Path(skill["package_path"])
            self.assertTrue((package_dir / "SKILL.md").is_file())
            self.assertTrue((package_dir / "agents" / "openai.yaml").is_file())
            self.assertTrue((package_dir / "references" / "ops-policy.yaml").is_file())

    def test_existing_frontend_payload_becomes_portable_package(self):
        """旧前端字段无需变化，保存后自动生成标准目录包。"""
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "ops-skills")
            skill = registry.upsert({
                "id": "",
                "name": "Service 端点恢复",
                "category": "network",
                "summary": "处理 Service selector 与 Endpoint 不匹配。",
                "symptoms": ["no endpoints", "503"],
                "applies_to": ["Service", "Deployment"],
                "evidence_required": ["service_endpoints", "events"],
                "diagnostic_steps": ["核对 selector 与 Pod label", "验证 EndpointSlice"],
                "allowed_actions": ["patch_service"],
                "success_criteria": ["endpoint_ready", "error_rate_recovered"],
                "risk": "high",
                "owner": "frontend-operator",
                "script_policy": {"enabled": False},
            }, actor="tester")
            package_dir = Path(skill["package_path"])
            content = (package_dir / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(skill["portable"])
            self.assertIn("name:", content)
            self.assertIn("Service 端点恢复", content)
            matched_ids = [item["skill"]["id"] for item in registry.match({"question": "service no endpoints 503"})["matches"]]
            self.assertIn(skill["id"], matched_ids)

    def test_export_and_import_preserve_runtime_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = OpsSkillRegistry(Path(directory) / "source")
            skill = source.upsert({
                "id": "pvc-pending-recovery",
                "name": "PVC Pending 恢复",
                "summary": "定位并恢复 PVC Pending。",
                "symptoms": ["PVC Pending", "FailedMount"],
                "applies_to": ["PVC", "Pod"],
                "evidence_required": ["storage_chain", "events"],
                "diagnostic_steps": ["检查 PVC、PV、StorageClass 和 CSI"],
                "allowed_actions": ["create_pv", "create_pvc"],
                "success_criteria": ["pvc_bound", "pod_ready"],
                "risk": "high",
            }, actor="tester")
            filename, payload = source.export_package(skill["id"])
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
            self.assertIn("pvc-pending-recovery/SKILL.md", names)
            self.assertIn("pvc-pending-recovery/references/ops-policy.yaml", names)

            target = OpsSkillRegistry(Path(directory) / "target")
            imported = target.import_packages(
                filename,
                payload,
                actor="importer",
                supported_actions={"create_pv", "create_pvc"},
            )
            self.assertEqual(imported[0]["id"], "pvc-pending-recovery")
            self.assertTrue(imported[0]["execution_ready"])
            self.assertEqual(imported[0]["allowed_actions"], ["create_pv", "create_pvc"])

    def test_generic_agent_skill_import_is_instruction_only(self):
        skill_md = """---
name: generic-k8s-check
description: Inspect Kubernetes resources and explain observed failures.
---

# Workflow

Collect evidence and explain the result. Do not mutate infrastructure.
"""
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            # 兼容用户直接压缩 Skill 目录内容、ZIP 中没有顶层文件夹的常见情况。
            archive.writestr("SKILL.md", skill_md)
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills")
            imported = registry.import_packages(
                "generic-k8s-check.zip",
                output.getvalue(),
                actor="importer",
                supported_actions={"patch_workload"},
            )
        self.assertFalse(imported[0]["execution_ready"])
        self.assertEqual(imported[0]["allowed_actions"], [])

    def test_legacy_json_is_migrated_without_changing_skill_id(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "skills.json"
            legacy_path.write_text(json.dumps({"skills": [{
                "id": "legacy-crashloop",
                "name": "旧版 CrashLoop Skill",
                "summary": "兼容旧前端保存的数据。",
                "symptoms": ["CrashLoopBackOff"],
                "diagnostic_steps": ["读取 previous logs"],
                "allowed_actions": ["patch_workload"],
                "success_criteria": ["pod_ready"],
            }]}, ensure_ascii=False), encoding="utf-8")
            registry = OpsSkillRegistry(Path(directory) / "packages", legacy_path=legacy_path)
            skills = {item["id"]: item for item in registry.list()["skills"]}
            self.assertIn("legacy-crashloop", skills)
            self.assertTrue((Path(directory) / "packages" / "legacy-crashloop" / "SKILL.md").is_file())

    def test_registry_rejects_script_without_trigger_description(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = OpsSkillRegistry(Path(directory) / "skills.json")
            with self.assertRaisesRegex(ValueError, "具体故障场景"):
                registry.upsert({
                    "name": "无触发说明",
                    "summary": "测试",
                    "diagnostic_steps": ["读取证据"],
                    "allowed_actions": ["patch_workload"],
                    "script_policy": {
                        "enabled": True,
                        "script_id": "inspect-pvc",
                        "trigger_conditions": ["manual_confirmation"],
                        "trigger_description": "太短",
                    },
                }, actor="tester")


class OpsSkillApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_skill_evidence_refresh_recollects_then_closes_contract(self):
        shallow = {
            "pod": {
                "name": "grafana-x",
                "containers": [{
                    "name": "grafana",
                    "reason": "CrashLoopBackOff",
                    "restart_count": 4,
                }],
            },
            "logs": {},
        }
        full = {
            "pod": {
                "name": "grafana-x",
                "containers": [{
                    "name": "grafana",
                    "reason": "CrashLoopBackOff",
                    "restart_count": 4,
                    "security_context": {
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "runAsNonRoot": True,
                    },
                    "volume_mounts": [{
                        "name": "data",
                        "mount_path": "/var/lib/grafana",
                    }],
                }],
                "security_context": {
                    "runAsUser": 10001,
                    "runAsGroup": 10001,
                    "runAsNonRoot": True,
                    "fsGroup": 10001,
                    "supplementalGroups": [10001],
                },
            },
            "logs": {
                "grafana": {
                    "current": (
                        "GF_PATHS_DATA='/var/lib/grafana' is not writable\n"
                        "Error: unable to open database file (14)"
                    ),
                    "previous": "",
                },
            },
            "events": [],
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "grafana"},
                "spec": {
                    "template": {
                        "spec": {
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
                                "volumeMounts": [{
                                    "name": "data",
                                    "mountPath": "/var/lib/grafana",
                                }],
                            }],
                        },
                    },
                },
            },
            "storage": [{"pvc": "grafana", "pvc_phase": "Bound"}],
        }
        plan = {
            "namespace": "default",
            "target": "Deployment/grafana",
            "pod_name": "grafana-x",
            "_runtime_evidence": shallow,
        }
        step = {
            "id": "skill_evidence_refresh",
            "title": "主动补采",
            "evidence_ids": [
                "workload_spec",
                "pod_security_context",
                "current_logs",
            ],
        }
        with patch.object(
            server,
            "_collect_plan_priority_evidence",
            AsyncMock(return_value=full),
        ), patch.object(
            server,
            "_collect_plan_deep_evidence",
            AsyncMock(return_value=full),
        ):
            result = await server._collect_ops_step(step, plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["artifacts"]["remaining_evidence"], [])
        self.assertIn("workload_spec", result["artifacts"]["newly_collected"])
        rerouted = server._attach_operator_skills_to_plan(
            {
                **plan,
                "summary": "CrashLoopBackOff",
                "changes": [],
            },
            {
                "question": "CrashLoopBackOff",
                "evidence": plan["_runtime_evidence"],
                "plan": plan,
            },
            preferred_skill_ids=["skill-crashloop-root-cause"],
        )
        self.assertEqual(
            rerouted["selected_skill_id"],
            "skill-volume-permission-recovery",
        )
        self.assertEqual(rerouted["permission_recovery_stage"], "root")
        root_spec = rerouted["changes"][0]["patch"]["spec"]["template"]["spec"]
        self.assertEqual(root_spec["securityContext"]["runAsUser"], 0)
        self.assertEqual(root_spec["securityContext"]["runAsGroup"], 0)
        self.assertEqual(root_spec["securityContext"]["fsGroup"], 0)
        self.assertFalse(root_spec["securityContext"]["runAsNonRoot"])

    async def test_remote_sre_chat_plan_uses_rancher_transport_by_cluster_identity(self):
        plan = {
            "source": "sre_chat",
            "cluster_id": "c-remote",
            "namespace": "monitoring",
            "target": "Deployment/grafana",
        }
        with (
            patch.object(server.CLUSTER_REGISTRY, "list", return_value=[]),
            patch.dict(os.environ, {
                "RANCHER_URL": "https://rancher.example.invalid",
                "RANCHER_TOKEN": "redacted-test-token",
            }),
        ):
            self.assertEqual(server._ops_cluster_transport(plan), "rancher")

    async def test_priority_logs_survive_enrichment_error_and_generate_root_candidate(self):
        events = []

        async def progress(stage, message, **extra):
            events.append({"stage": stage, "message": message, **extra})

        pod_security = {
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "runAsNonRoot": True,
            "fsGroup": 10001,
            "supplementalGroups": [10001],
            "fsGroupChangePolicy": "OnRootMismatch",
        }
        evidence = {
            "pod_name": "grafana-abc",
            "transport": "rancher",
            "pod": {
                "name": "grafana-abc",
                "phase": "Running",
                "ready": False,
                "security_context": pod_security,
                "containers": [{
                    "name": "grafana",
                    "ready": False,
                    "restart_count": 4,
                    "security_context": {
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "runAsNonRoot": True,
                    },
                    "volume_mounts": [{
                        "name": "data",
                        "mount_path": "/var/lib/grafana",
                    }],
                }],
                "workload_kind": "Deployment",
                "workload_name": "grafana",
            },
            "workload": {
                "kind": "Deployment",
                "metadata": {"name": "grafana"},
                "spec": {"template": {"spec": {
                    "securityContext": pod_security,
                    "containers": [{
                        "name": "grafana",
                        "securityContext": {
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "runAsNonRoot": True,
                        },
                    }],
                }}},
            },
            "logs": {"grafana": {"current": "\n".join([
                "GF_PATHS_DATA='/var/lib/grafana' is not writable.",
                "Error: unable to open database file",
            ])}},
            "storage": [{"pvc": "grafana-data", "pvc_phase": "Bound"}],
        }
        plan = {
            "source": "sre_chat",
            "cluster_id": "c-remote",
            "namespace": "monitoring",
            "target": "Deployment/grafana",
            "summary": "Grafana CrashLoopBackOff",
            "steps": [],
            "changes": [],
        }
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_priority_evidence", new=AsyncMock(return_value=evidence)),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"error": "optional CMDB timed out"})),
            patch.object(server, "_attach_operator_skills_to_plan", side_effect=lambda current, _signal, **_kwargs: current),
            patch.object(server, "_probe_plan_recovery", new=AsyncMock(return_value={"status": "unknown", "recovered": None, "message": "not ready"})),
            patch.object(server, "record_remediation", return_value={"status": "recorded"}),
        ):
            result = await server._execute_ops_plan_once(
                plan,
                summarize=False,
                progress=progress,
            )
        self.assertEqual(result["status"], "planned")
        self.assertNotIn("证据仍不足", result["message"])
        replacement = result["alternative_plans"][0]
        self.assertEqual(replacement["permission_recovery_stage"], "root")
        root_spec = replacement["changes"][0]["patch"]["spec"]["template"]["spec"]
        self.assertEqual(root_spec["securityContext"]["runAsUser"], 0)
        self.assertIs(root_spec["securityContext"]["runAsNonRoot"], False)
        log_event = next(event for event in events if event["stage"] == "pod_logs_collected")
        self.assertIn("not writable", log_event["priority_excerpts"][0]["excerpt"])
        self.assertTrue(any(event["stage"] == "root_cause_diagnosed" for event in events))

    async def test_actionable_log_skips_optional_cmdb_probe(self):
        events = []

        async def progress(stage, message, **extra):
            events.append({"stage": stage, "message": message, **extra})

        plan = {
            "target": "Deployment/grafana",
            "namespace": "monitoring",
            "steps": [{"id": "dependency_topology", "title": "追踪 CMDB 依赖链"}],
            "changes": [],
        }
        evidence = {
            "pod": {"name": "grafana-abc"},
            "events": [],
            "logs": {"grafana": {"current": "GF_PATHS_DATA is not writable\nError: unable to open database file"}},
        }
        collector = AsyncMock(side_effect=RuntimeError("CMDB transport closed"))
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_priority_evidence", new=AsyncMock(return_value=evidence)),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value=evidence)),
            patch.object(server, "_attach_operator_skills_to_plan", side_effect=lambda current, _signal, **_kwargs: current),
            patch.object(server, "_collect_ops_step", collector),
            patch.object(server, "_verify_plan_recovery", new=AsyncMock(return_value={"status": "unknown", "recovered": None, "message": "diagnosis only"})),
            patch.object(server, "record_remediation", return_value={"status": "recorded"}),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False, progress=progress)
        collector.assert_not_awaited()
        self.assertEqual(result["steps"][0]["status"], "skipped")
        self.assertTrue(any(event["stage"] == "log_triage_done" for event in events))
        self.assertFalse(any(event["stage"] == "step_start" for event in events))

    async def test_diagnostic_probe_exception_is_closed_and_flow_continues(self):
        events = []

        async def progress(stage, message, **extra):
            events.append({"stage": stage, "message": message, **extra})

        plan = {
            "target": "Deployment/web",
            "namespace": "default",
            "steps": [{"id": "workload_spec", "title": "读取 Workload YAML"}],
            "changes": [],
        }
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_priority_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": [], "logs": {}})),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": [], "logs": {}})),
            patch.object(server, "_attach_operator_skills_to_plan", side_effect=lambda current, _signal, **_kwargs: current),
            patch.object(server, "_collect_ops_step", new=AsyncMock(side_effect=RuntimeError("probe serialization failed"))),
            patch.object(server, "_verify_plan_recovery", new=AsyncMock(return_value={"status": "unknown", "recovered": None, "message": "diagnosis only"})),
            patch.object(server, "record_remediation", return_value={"status": "recorded"}),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False, progress=progress)
        self.assertEqual(result["steps"][0]["status"], "warning")
        self.assertTrue(any(event["stage"] == "step_failed" for event in events))
        self.assertTrue(any(event["stage"] == "step_done" for event in events))

    async def test_job_request_merges_explicit_high_risk_approval_into_plan(self):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/ops/jobs",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        payload = OpsJobCreateRequest(
            plan={"target": "StatefulSet/log-store", "changes": [{"type": "create_pv"}]},
            confirm=True,
            high_risk_confirmed=True,
            operator_override_reason="已核对存储后端和回滚方式",
            stepwise_confirmation=True,
        )
        with patch.object(server, "_enqueue_ops_job", new=AsyncMock(return_value={"status": "queued"})) as enqueue:
            response = await server.create_ops_job(payload, request)
        submitted = enqueue.await_args.args[0]
        self.assertEqual(response["status"], "queued")
        self.assertTrue(submitted["high_risk_confirmed"])
        self.assertTrue(submitted["stepwise_confirmation"])
        self.assertIn("回滚", submitted["operator_override_reason"])

    async def test_risk_ranking_falls_back_without_changing_real_targets(self):
        request = ChatRiskRankRequest(risks=[
            {"key": "workload:a", "name": "a", "severity": "P2", "score": 210},
            {"key": "workload:b", "name": "b", "severity": "P1", "score": 420},
        ])
        with patch("agents.llm_client.get_llm", side_effect=RuntimeError("model unavailable")):
            response = await server.rank_chat_risks(request)
        self.assertEqual(response["source"], "deterministic_fallback")
        self.assertEqual(response["ordered_keys"], ["workload:b", "workload:a"])
        self.assertEqual(set(response["rationales"]), {"workload:a", "workload:b"})

    async def test_operator_step_approval_happens_before_kubernetes_change(self):
        order = []

        async def approve(index, total, change, target):
            order.append(("approval", index, target))
            return True

        async def execute(change, plan):
            order.append(("execute", change["type"], plan["target"]))
            return {"status": "completed", "change": change, "result": {"accepted": True}}

        plan = {
            "target": "Deployment/web",
            "namespace": "default",
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "web",
            }],
        }
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_priority_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": [], "logs": {}})),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": []})),
            patch.object(server, "_attach_operator_skills_to_plan", side_effect=lambda plan, _signal, **_kwargs: plan),
            patch.object(server, "_execute_change", side_effect=execute),
            patch.object(server, "_verify_plan_recovery", new=AsyncMock(return_value={"status": "verified", "recovered": True, "message": "Ready"})),
            patch.object(server, "record_remediation", return_value={"status": "recorded"}),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False, change_approval=approve)
        self.assertEqual(order[0][0], "approval")
        self.assertEqual(order[1][0], "execute")
        self.assertEqual(result["status"], "completed")

    async def test_change_executor_exception_becomes_structured_failure(self):
        events = []

        async def progress(stage, message, **extra):
            events.append({"stage": stage, "message": message, **extra})

        plan = {
            "target": "Deployment/web",
            "namespace": "default",
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "web",
            }],
        }
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_priority_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": [], "logs": {}})),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"pod": {"name": "web-abc"}, "events": []})),
            patch.object(server, "_attach_operator_skills_to_plan", side_effect=lambda plan, _signal, **_kwargs: plan),
            patch.object(server, "_execute_change", new=AsyncMock(side_effect=RuntimeError("mcp transport closed"))),
            patch.object(server, "_verify_plan_recovery", new=AsyncMock(return_value={"status": "unknown", "recovered": None, "message": "not verified"})),
            patch.object(server, "record_remediation", return_value={"status": "recorded"}),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False, progress=progress)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertIn("mcp transport closed", result["results"][0]["result"]["error"])
        self.assertTrue(any(event["stage"] == "change_exception" for event in events))

    async def test_fresh_evidence_failure_blocks_preapproved_mutation(self):
        plan = {
            "target": "Deployment/web",
            "namespace": "default",
            "high_risk_confirmed": True,
            "operator_force_execute": True,
            "changes": [{
                "type": "restart",
                "namespace": "default",
                "workload_type": "Deployment",
                "workload_name": "web",
                "human_approved": True,
            }],
        }
        execute = AsyncMock()
        with (
            patch.object(server, "_ops_release_gate", return_value={"allowed": True}),
            patch.object(server, "_collect_plan_priority_evidence", new=AsyncMock(return_value={"error": "Rancher timeout"})),
            patch.object(server, "_collect_plan_deep_evidence", new=AsyncMock(return_value={"error": "Rancher timeout"})),
            patch.object(server, "_execute_change", execute),
            patch.object(server, "_probe_plan_recovery", new=AsyncMock(return_value={"status": "unknown", "recovered": None})),
            patch.object(server, "_evidence_based_replan", new=AsyncMock(return_value=[])),
        ):
            result = await server._execute_ops_plan_once(plan, summarize=False)
        execute.assert_not_awaited()
        self.assertFalse(result["executed"])
        self.assertIsNone(result["verification"]["recovered"])
        self.assertEqual(plan["decision"], "fresh_evidence_required")

    async def test_api_rejects_unapproved_script_id(self):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/ops/skills",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        definition = OpsSkillDefinition(
            name="未批准脚本",
            summary="用于验证脚本白名单。",
            diagnostic_steps=["读取日志和 Events"],
            allowed_actions=["patch_workload"],
            script_policy={
                "enabled": True,
                "script_id": "not-approved",
                "trigger_conditions": ["required_evidence_collected", "manual_confirmation"],
                "trigger_description": "证据齐全且运维人员确认后才允许触发。",
            },
        )
        with patch.dict(os.environ, {"OPS_APPROVED_SCRIPTS_JSON": "[]"}):
            with self.assertRaises(HTTPException) as context:
                await server.upsert_ops_skill(definition, request)
        self.assertEqual(context.exception.status_code, 422)
        self.assertIn("企业批准目录", str(context.exception.detail))

    async def test_high_risk_plan_requires_explicit_second_confirmation(self):
        plan = {
            "namespace": "default",
            "target": "PV/static-data",
            "steps": [{"title": "核对存储模板"}],
            "changes": [{
                "type": "create_pv",
                "manifest": {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "static-data"}},
            }],
        }
        with patch.dict(os.environ, {"OPS_MUTATION_ENABLED": "true"}):
            with self.assertRaises(HTTPException) as context:
                await server._enqueue_ops_job(plan, "tester", autonomous=False, confirmed=True)
        self.assertEqual(context.exception.status_code, 409)
        self.assertTrue(context.exception.detail["requires_high_risk_confirmation"])
        self.assertIn("create_pv", context.exception.detail["high_risk_actions"])

    async def test_confirmed_high_risk_plan_reaches_job_queue(self):
        class DeferredTask:
            def done(self):
                return False

            def cancel(self):
                return True

        def defer(coroutine):
            coroutine.close()
            return DeferredTask()

        plan = {
            "namespace": "default",
            "target": "ConfigMap/runtime-config",
            "summary": "ConfigMap runtime-config is missing and blocks Deployment/api startup",
            "evidence": {
                "events": [{"reason": "CreateContainerConfigError", "message": "configmap runtime-config not found"}],
                "workload": {"kind": "Deployment", "metadata": {"name": "api", "generation": 2}},
            },
            "high_risk_confirmed": True,
            "operator_override_reason": "已核对配置模板、影响范围和回滚方式",
            "stepwise_confirmation": True,
            "steps": [{"id": "config_ref_exists", "title": "确认缺失配置引用"}],
            "changes": [{
                "type": "create_configmap",
                "namespace": "default",
                "configmap_name": "runtime-config",
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "runtime-config", "namespace": "default"},
                    "data": {"MODE": "stable"},
                },
            }],
        }
        job_id = ""
        try:
            with (
                patch.dict(os.environ, {"OPS_MUTATION_ENABLED": "true"}),
                patch.object(server.asyncio, "create_task", side_effect=defer),
            ):
                response = await server._enqueue_ops_job(plan, "tester", autonomous=False, confirmed=True)
            job_id = response["id"]
            self.assertEqual(response["status"], "queued")
            self.assertTrue(response["stepwise_confirmation"])
            self.assertTrue(response["execution_readiness"]["ready"])
        finally:
            if job_id:
                server.OPS_JOBS.pop(job_id, None)
                server.OPS_JOB_TASKS.pop(job_id, None)
                server.OPS_JOB_CANCEL_EVENTS.pop(job_id, None)

    async def test_operator_confirmed_high_risk_autonomous_plan_is_queued_stepwise(self):
        class DeferredTask:
            def done(self):
                return False

            def cancel(self):
                return True

        def defer(coroutine):
            coroutine.close()
            return DeferredTask()

        plan = {
            "namespace": "default",
            "target": "ConfigMap/runtime-config",
            "summary": "ConfigMap runtime-config is missing and blocks Deployment/api startup",
            "evidence": {
                "events": [{"reason": "CreateContainerConfigError", "message": "configmap runtime-config not found"}],
                "workload": {"kind": "Deployment", "metadata": {"name": "api", "generation": 2}},
            },
            "high_risk_confirmed": True,
            "operator_force_execute": True,
            "steps": [{"title": "核对配置引用"}],
            "changes": [{
                "type": "create_configmap",
                "namespace": "default",
                "configmap_name": "runtime-config",
                "manifest": {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": "runtime-config", "namespace": "default"},
                    "data": {"MODE": "stable"},
                },
            }],
        }
        job_id = ""
        try:
            with (
                patch.dict(os.environ, {"OPS_MUTATION_ENABLED": "true", "AUTONOMOUS_OPS_ENABLED": "true"}),
                patch.object(server.asyncio, "create_task", side_effect=defer),
            ):
                response = await server._enqueue_ops_job(plan, "tester", autonomous=True, confirmed=True)
            job_id = response["id"]
            self.assertEqual(response["status"], "queued")
            self.assertTrue(response["stepwise_confirmation"])
        finally:
            if job_id:
                server.OPS_JOBS.pop(job_id, None)
                server.OPS_JOB_TASKS.pop(job_id, None)
                server.OPS_JOB_CANCEL_EVENTS.pop(job_id, None)

    async def test_inspection_routes_finding_through_matching_skill(self):
        finding = {
            "id": "finding-pvc",
            "category": "storage_config",
            "severity": "P1",
            "title": "PVC Pending and FailedMount",
            "summary": "Pod volume references a PVC that cannot bind to a PV",
            "cluster": "local-cluster",
            "namespace": "default",
            "name": "api-0",
            "workload": {"kind": "StatefulSet", "name": "api", "replicas": 1, "ready_replicas": 0},
            "evidence": {
                "state_text": "FailedMount no persistent volumes available for this claim",
                "events": [{"reason": "FailedMount", "message": "PVC is Pending"}],
                "pod": {"name": "api-0", "containers": []},
            },
        }
        finding["ops_plan"] = server._ops_plan_from_finding(finding)
        payload = {"findings": [finding], "summary": {"total": 1}}
        with patch.dict(os.environ, {"INSPECTION_SKILL_ROUTER_ENABLED": "false"}):
            routed = await server._route_inspection_findings_with_skills(payload)
        routed_finding = routed["findings"][0]
        self.assertTrue(routed_finding["matched_skills"])
        self.assertEqual(routed["summary"]["skill_routed"], 1)
        self.assertIn("AgentSkillRouter/v2", routed_finding["ops_plan"]["planning_engine"])
        self.assertTrue(any("存储" in item["name"] for item in routed_finding["matched_skills"]))

    async def test_inspection_preview_recollects_evidence_and_locks_target(self):
        finding = {
            "id": "finding-orders",
            "category": "crashloop",
            "severity": "P1",
            "title": "orders CrashLoop",
            "summary": "orders pod is restarting",
            "source": "rancher",
            "cluster": "nonprod",
            "cluster_id": "c-nonprod",
            "namespace": "prod",
            "name": "orders-api-abc",
            "workload": {"kind": "Deployment", "name": "orders-api", "replicas": 2, "ready_replicas": 1},
            "evidence": {"pod": {"name": "orders-api-abc", "containers": []}, "events": []},
        }
        previous = server.LAST_INSPECTION_PAYLOAD
        server.LAST_INSPECTION_PAYLOAD = {"findings": [finding]}
        replacement = {
            "id": "ai-plan",
            "title": "AI plan",
            "namespace": "prod",
            "target": "Deployment/orders-api",
            "steps": [{"id": "previous_logs", "title": "读取上次退出日志"}],
            "changes": [{
                "type": "patch_workload",
                "namespace": "prod",
                "workload_type": "Deployment",
                "workload_name": "orders-api",
                "patch": {"spec": {"replicas": 3}},
                "reason": "live evidence",
            }],
            "root_cause_hypotheses": [{"title": "CrashLoop evidence"}],
            "success_criteria": ["pod_ready"],
        }
        deep = {
            "pod": {
                "name": "orders-api-abc",
                "workload": {"kind": "Deployment", "name": "orders-api"},
                "containers": [{"name": "app", "state": "waiting", "reason": "CrashLoopBackOff", "restart_count": 3}],
                "security_context": {},
            },
            "events": [{"reason": "BackOff", "message": "back-off restarting failed container"}],
            "logs": {"app": {"previous": "startup failed"}},
            "storage": [],
            "services": [{"name": "orders"}],
            "workload": {"metadata": {"name": "orders-api"}},
        }
        try:
            with patch.object(server, "_collect_plan_deep_evidence", AsyncMock(return_value=deep)) as collect, patch.object(
                server, "_evidence_based_replan", AsyncMock(return_value=[replacement])
            ):
                result = await server.preview_ai_inspection_finding(
                    server.InspectionPreviewRequest(finding_id="finding-orders", model_profile_id="primary")
                )
            collect.assert_awaited_once()
            plan = result["plan"]
            self.assertEqual(plan["preview_mode"], "live_evidence_ai")
            self.assertEqual(plan["target"], "Deployment/orders-api")
            self.assertEqual(plan["changes"], [])
            self.assertEqual(plan["selected_skill_id"], "skill-crashloop-root-cause")
            self.assertTrue(plan["skill_workflow_state"]["must_continue"])
            self.assertEqual(plan["evidence_summary"]["events"], 1)
            self.assertEqual(plan["target_binding"], "inspection_finding_id")
        finally:
            server.LAST_INSPECTION_PAYLOAD = previous

    async def test_common_kubernetes_skills_publish_real_runtime_handlers(self):
        handlers = {
            item["skill_id"]: item["handler_id"]
            for item in server.public_runtime_catalog()
        }
        self.assertEqual(handlers["skill-memory-oom-recovery"], "evidence-runbook-oom-recovery")
        self.assertEqual(handlers["skill-probe-slow-start-recovery"], "evidence-runbook-probe-recovery")
        self.assertEqual(handlers["skill-image-pull-runtime-recovery"], "evidence-runbook-image-recovery")
        self.assertEqual(handlers["skill-config-reference-recovery"], "evidence-runbook-config-recovery")
        self.assertEqual(handlers["skill-service-endpoint-flow"], "evidence-runbook-service-recovery")
        self.assertEqual(handlers["skill-rollout-regression-recovery"], "evidence-runbook-rollout-recovery")
        self.assertEqual(handlers["skill-node-pressure-containment"], "evidence-runbook-node-pressure")
        self.assertEqual(handlers["skill-pdb-rollout-deadlock-recovery"], "evidence-runbook-pdb-recovery")
        self.assertEqual(handlers["skill-cpu-capacity-recovery"], "evidence-runbook-cpu-recovery")

    async def test_oom_skill_materializes_bounded_workload_patch(self):
        evidence = {
            "pod": {
                "name": "api-abc",
                "namespace": "prod",
                "workload_kind": "Deployment",
                "workload_name": "api",
                "last_terminated_reason": "OOMKilled",
                "last_exit_code": 137,
                "containers": [{
                    "name": "api",
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                }],
            },
            "events": [{"reason": "OOMKilled", "message": "exit code 137"}],
        }
        plan = {
            "namespace": "prod",
            "target": "Deployment/api",
            "summary": "OOMKilled exit code 137",
            "evidence": evidence,
        }
        materialized = server._materialize_executable_skill(
            plan,
            {
                "question": plan["summary"],
                "diagnosis": {"root_cause": "OOMKilled"},
                "evidence": evidence,
            },
            "skill-memory-oom-recovery",
        )
        self.assertEqual(materialized["runbook_id"], "oom")
        self.assertEqual(materialized["selected_skill_id"], "skill-memory-oom-recovery")
        self.assertEqual(materialized["changes"][0]["type"], "patch_workload")
        memory = materialized["changes"][0]["patch"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
        self.assertNotEqual(memory, "512Mi")

    async def test_mutation_queue_waits_instead_of_rejecting_and_serializes_targets(self):
        server.OPS_EXECUTION_ACTIVE_JOBS.clear()
        server.OPS_EXECUTION_ACTIVE_TARGETS.clear()
        cancel = asyncio.Event()
        with patch.dict(os.environ, {"OPS_MAX_CONCURRENT_JOBS": "1"}):
            self.assertTrue(await server._acquire_ops_execution_lease("job-a", "cluster/prod/deployment/a", cancel))
            waiter = asyncio.create_task(
                server._acquire_ops_execution_lease("job-b", "cluster/prod/deployment/b", cancel)
            )
            await asyncio.sleep(0.02)
            self.assertFalse(waiter.done())
            await server._release_ops_execution_lease("job-a", "cluster/prod/deployment/a")
            self.assertTrue(await asyncio.wait_for(waiter, timeout=1))
            await server._release_ops_execution_lease("job-b", "cluster/prod/deployment/b")
        self.assertEqual(server.OPS_EXECUTION_ACTIVE_JOBS, set())
        self.assertEqual(server.OPS_EXECUTION_ACTIVE_TARGETS, {})

    async def test_waiting_approval_jobs_do_not_block_new_job_admission(self):
        existing_ids = set(server.OPS_JOBS)
        for index in range(8):
            server.OPS_JOBS[f"approval-wait-{index}"] = {
                "id": f"approval-wait-{index}",
                "status": "awaiting_approval",
            }
        plan = {
            "namespace": "prod",
            "target": "Deployment/api",
            "summary": "read-only diagnosis",
            "steps": [{"id": "events", "title": "read events"}],
            "changes": [],
            "evidence": {"events": []},
        }
        try:
            with patch.object(server, "_run_ops_job", AsyncMock(return_value=None)):
                created = await server._enqueue_ops_job(
                    plan,
                    "tester",
                    autonomous=False,
                    confirmed=False,
                )
            self.assertEqual(created["status"], "queued")
            self.assertTrue(created["id"].startswith("ops-"))
            task = server.OPS_JOB_TASKS.pop(created["id"], None)
            if task:
                await task
        finally:
            for job_id in list(server.OPS_JOBS):
                if job_id not in existing_ids:
                    server.OPS_JOBS.pop(job_id, None)
                    server.OPS_JOB_CANCEL_EVENTS.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
