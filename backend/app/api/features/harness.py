"""Harness composition, event sourcing and recovery inspection endpoints."""
from __future__ import annotations

import asyncio
import json
import os
import re

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.app.services.harness_events import HARNESS_EVENT_STORE
from backend.app.services.harness_packages import HARNESS_PACKAGE_MANAGER, HarnessPackageError
from backend.app.services.harness_plugins import harness_capabilities_payload
from backend.app.kernel import scalability_profile


class ProfileActivationRequest(BaseModel):
    profile: str = Field(min_length=2, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]+$")


class SessionForkRequest(BaseModel):
    at_seq: int | None = Field(default=None, ge=1)


class ServiceInvokeRequest(BaseModel):
    operation: str = Field(min_length=2, max_length=128, pattern=r"^[a-zA-Z0-9._/-]+$")
    payload: dict = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list, max_length=8)


class PluginManifestRequest(BaseModel):
    manifest: str = Field(min_length=20, max_length=512 * 1024)
    add_to_active_profile: bool = True


class PluginGenerateRequest(BaseModel):
    goal: str = Field(min_length=12, max_length=6000)
    domain: str = Field(default="infrastructure", min_length=2, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    model_profile_id: str = Field(default="", max_length=128)
    include_agent_loop: bool = True
    orchestration_mode: str = Field(default="plan-execute", pattern=r"^(react|plan-execute|sequential|parallel)$")


def _manifest_from_model_output(content: str) -> tuple[str, str]:
    text = str(content or "").strip()
    explanation = ""
    if text.startswith("{"):
        payload = json.loads(text)
        text = str(payload.get("manifest") or payload.get("yaml") or "").strip()
        explanation = str(payload.get("explanation") or "")[:1200]
    fence = re.match(r"^```(?:yaml|yml)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    if not text:
        raise ValueError("model did not return a plugin manifest")
    return text, explanation


def _actor(request: Request) -> str:
    return str(
        request.headers.get("x-auth-request-user")
        or request.headers.get("x-operator")
        or (request.client.host if request.client else "operator")
    )[:160]


def _plugin_detail(plugin_id: str) -> dict:
    payload = harness_capabilities_payload()
    runtime_plugin = next(
        (item for item in payload.get("plugins") or [] if str(item.get("id") or "") == plugin_id),
        None,
    )
    composition = payload.get("composition") or {}
    package = next(
        (item for item in composition.get("packages") or [] if str(item.get("id") or "") == plugin_id),
        None,
    )
    if not runtime_plugin and not package:
        raise KeyError(f"plugin not found: {plugin_id}")

    plugin = {**(runtime_plugin or {}), **(package or {})}
    provides = [str(item) for item in plugin.get("provides") or []]
    requires = [str(item) for item in plugin.get("requires") or []]
    events = [str(item) for item in plugin.get("events") or []]
    event_modes = plugin.get("event_modes") or {}
    scope = str(plugin.get("scope") or "global")
    priority = int(plugin.get("priority") or 0)
    runtime_type = str(plugin.get("runtime_type") or "builtin")
    domains = [str(item) for item in plugin.get("domains") or []]
    if not domains:
        ui_group = str((plugin.get("ui") or {}).get("group") or "")
        domains = [ui_group.replace("_", "-")] if ui_group else ["common"]
    category = str(plugin.get("category") or ("shared" if domains == ["common"] else "domain"))
    agents = [str(item) for item in plugin.get("agents") or (["all"] if domains == ["common"] else domains)]
    conditions: list[str] = []
    if provides:
        conditions.append(
            f"当运行时请求服务 {', '.join(provides)}，且 scope 与 {scope} 匹配时，进入 Provider 候选。"
        )
        conditions.append("同一服务存在多个 Provider 时，先匹配最近作用域，再选择 priority 更高的插件。")
    if events:
        conditions.extend(
            f"事件 {event} 发布时，以 {event_modes.get(event) or '运行时声明模式'} 调用。"
            for event in events
        )
    if not conditions:
        conditions.append("插件仅参与生命周期或组合配置，不会被业务路径主动调用。")
    if requires:
        conditions.append(f"仅当依赖服务 {', '.join(requires)} 全部满足版本约束后才会激活。")

    requested = [str(item) for item in plugin.get("requested_permissions") or plugin.get("permissions") or []]
    granted = [str(item) for item in plugin.get("granted_permissions") or requested]
    denied = [str(item) for item in plugin.get("denied_permissions") or []]
    if runtime_type == "builtin":
        boundary = [
            "内置受信插件运行在控制面，但任何生产变更仍必须经过 typed action、爆炸半径、人工审批、写后回读和恢复验证。",
            "模型输出不能直接成为执行结果，也不能自行宣布恢复成功。",
        ]
    elif runtime_type == "remote":
        boundary = [
            "远程插件运行在独立进程；控制面只允许调用 manifest 中标记为 read_only 的操作。",
            "网络目标必须命中允许列表或集群内 Service；响应大小、超时和敏感字段均受限制。",
            "外置插件不能获得 kubernetes:mutate、ops:execute 或 secrets:read。",
        ]
    else:
        boundary = [
            "声明式插件不在 API 进程内执行任意代码，只注册服务合同、事件订阅和 UI 元数据。",
            "外置插件只能提出运维动作；真实写操作由受控执行器在人工审批后完成。",
            "明文 Token、密码、API Key 和私钥在安装前会被拒绝。",
        ]

    recent_events = HARNESS_EVENT_STORE.events_for_plugin(plugin_id, limit=30)
    return {
        "plugin": plugin,
        "classification": {
            "category": category,
            "domains": domains,
            "agents": agents,
            "shared": category == "shared" or "common" in domains,
        },
        "invocation": {
            "conditions": conditions,
            "provides": provides,
            "requires": requires,
            "events": [{"name": event, "mode": event_modes.get(event) or "runtime-declared"} for event in events],
            "scope": scope,
            "priority": priority,
            "active": str(plugin.get("status") or plugin.get("package_status") or "") == "active",
            "missing_dependencies": plugin.get("missing_dependencies") or [],
        },
        "security": {
            "runtime_type": runtime_type,
            "requested_permissions": requested,
            "granted_permissions": granted,
            "denied_permissions": denied,
            "boundaries": boundary,
            "mutation_authority": runtime_type == "builtin" and plugin_id in {
                "cisre.kubernetes-executor", "cisre.approval-gate", "cisre.execution-target-guard"
            },
            "human_approval_required": True,
        },
        "recent_invocations": recent_events,
        "recent_invocation_count": len(recent_events),
    }


def build_router() -> APIRouter:
    router = APIRouter(tags=["Harness Runtime"])

    @router.get("/api/harness/runtime")
    async def runtime():
        return {"status": "ok", "harness": harness_capabilities_payload()}

    @router.get("/api/harness/scalability")
    async def scalability():
        return {"status": "ok", "scalability": scalability_profile()}

    @router.get("/api/harness/profiles")
    async def profiles():
        value = HARNESS_PACKAGE_MANAGER.diagnostics()
        return {
            "active_profile": value["active_profile"],
            "runtime_write_enabled": value["runtime_write_enabled"],
            "package_write_enabled": value["package_write_enabled"],
            "profiles": value["profiles"],
            "bundles": value["bundles"],
            "packages": value["packages"],
            "last_error": value["last_error"],
        }

    @router.post("/api/harness/plugins/validate")
    async def validate_plugin(body: PluginManifestRequest):
        try:
            package = HARNESS_PACKAGE_MANAGER.validate_source(body.manifest)
        except HarnessPackageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "valid", "package": package}

    @router.post("/api/harness/plugins/install")
    async def install_plugin(body: PluginManifestRequest, request: Request):
        try:
            result = HARNESS_PACKAGE_MANAGER.install_source(
                body.manifest,
                add_to_active_profile=body.add_to_active_profile,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (HarnessPackageError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        plugin_id = str(result.get("package", {}).get("id") or "plugin")
        HARNESS_EVENT_STORE.append(
            "harness-runtime",
            "plugin/installed",
            stage="plugin_installed",
            phase="runtime",
            status="completed",
            message=f"Plugin {plugin_id} installed and reconciled",
            actor=_actor(request),
            plugin_id="cisre.plugin-loader",
            data={"installed_plugin": plugin_id, "active_profile": HARNESS_PACKAGE_MANAGER.active_profile},
        )
        return result

    @router.post("/api/harness/plugins/generate")
    async def generate_plugin(body: PluginGenerateRequest, request: Request):
        """Use the configured compatible model to author a safe declarative plugin."""
        prompt = f"""你是 CISRE 插件架构师。根据团队需求生成一个可安装的 HarnessPlugin。

强制要求：
- 只返回 JSON 对象：{{"manifest":"完整 YAML 字符串","explanation":"中文说明"}}。
- apiVersion 必须是 cisre.io/v1alpha1，kind 必须是 HarnessPlugin。
- metadata.name 使用小写团队命名，例如 team.database-provider；版本使用 SemVer。
- spec 必须声明 scope、priority、provides、requires、events、permissions、runtime、config、ui。
- spec 必须声明 category（shared 或 domain）、domains 和 agents；公共能力使用 domains:[common]、agents:[all]，领域插件使用对应资源域。
- 外置插件只能 inventory:read、evidence:read、events:publish、ops:propose、service:provide、ui:contribute；不得申请 secrets:read、subprocess:execute、kubernetes:mutate、ops:execute。
- runtime.type 使用 declarative。凭据只能在 config 中使用 credential_ref，不得出现明文 Token、密码、API Key、URL 凭据。
- config.agent_loop 写明 mode={body.orchestration_mode if body.include_agent_loop else 'disabled'}、max_steps=12、delegates=[evidence,planner,verifier]；这只是声明式编排，真实写操作仍由 CISRE 人工审批执行器完成。
- provides/requires 使用带 name/version 或 constraint 的对象；事件使用 name/mode 对象。

领域：{body.domain}
团队需求：
{body.goal}
"""
        session_id = "plugin-authoring"
        HARNESS_EVENT_STORE.append(
            session_id, "context/injected", phase="authoring", stage="plugin_goal_ready",
            status="completed", message="Plugin authoring context prepared",
            actor=_actor(request), plugin_id="cisre.context-compaction",
            data={"domain": body.domain, "goal_length": len(body.goal), "agent_loop": body.include_agent_loop},
        )
        try:
            def _call_model():
                from agents.llm_client import get_llm
                return get_llm(
                    temperature=0.1,
                    max_tokens=int(os.getenv("HARNESS_PLUGIN_GENERATION_MAX_TOKENS", "2200")),
                    profile_id=body.model_profile_id or None,
                ).invoke(prompt, response_format={"type": "json_object"})

            response = await asyncio.wait_for(
                asyncio.to_thread(_call_model),
                timeout=float(os.getenv("HARNESS_PLUGIN_GENERATION_TIMEOUT_SECONDS", "45")),
            )
            manifest, explanation = _manifest_from_model_output(getattr(response, "content", "") or "")
            package = HARNESS_PACKAGE_MANAGER.validate_source(manifest)
            metadata = getattr(response, "response_metadata", {}) or {}
            HARNESS_EVENT_STORE.append(
                session_id, "llm/response", phase="authoring", stage="plugin_manifest_generated",
                status="completed", message=f"Model generated validated plugin {package.get('id')}",
                actor=_actor(request), plugin_id="cisre.deepseek-planner",
                data={
                    "model_profile_id": body.model_profile_id or metadata.get("model_profile_id") or "active",
                    "token_usage": metadata.get("token_usage") or {},
                    "plugin_id": package.get("id"),
                },
            )
            return {"status": "generated", "manifest": manifest, "package": package, "explanation": explanation}
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="plugin generation model timed out") from exc
        except (ValueError, json.JSONDecodeError, HarnessPackageError) as exc:
            raise HTTPException(status_code=422, detail=f"generated manifest rejected: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"plugin generation unavailable: {type(exc).__name__}: {exc}") from exc

    @router.get("/api/harness/plugins/{plugin_id}")
    async def plugin_detail(plugin_id: str):
        try:
            return _plugin_detail(plugin_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/harness/profiles/activate")
    async def activate_profile(body: ProfileActivationRequest, request: Request):
        try:
            result = HARNESS_PACKAGE_MANAGER.activate(body.profile)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except HarnessPackageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        HARNESS_EVENT_STORE.append(
            "harness-runtime",
            "profile/activated",
            stage="profile_activated",
            phase="runtime",
            status="completed",
            message=f"Profile {body.profile} activated",
            actor=_actor(request),
            plugin_id="cisre.plugin-loader",
            data={"profile": body.profile},
        )
        return {"status": "ok", "composition": result}

    @router.post("/api/harness/plugins/reload")
    async def reload_plugins(request: Request):
        try:
            result = HARNESS_PACKAGE_MANAGER.refresh()
        except HarnessPackageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        HARNESS_EVENT_STORE.append(
            "harness-runtime",
            "plugins/reloaded",
            stage="plugins_reloaded",
            phase="runtime",
            status="completed",
            message="Plugin packages reloaded",
            actor=_actor(request),
            plugin_id="cisre.plugin-loader",
        )
        return {"status": "ok", "composition": result}

    @router.post("/api/harness/services/{service_name}/invoke")
    async def invoke_service(service_name: str, body: ServiceInvokeRequest, request: Request):
        try:
            result = await HARNESS_PACKAGE_MANAGER.invoke(
                service_name,
                body.operation,
                body.payload,
                scopes=tuple(str(item)[:160] for item in body.scopes),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except HarnessPackageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        HARNESS_EVENT_STORE.append(
            "harness-runtime",
            "service/invoked",
            stage="service_invoked",
            phase="runtime",
            status="completed",
            message=f"Read-only service {service_name}.{body.operation} invoked",
            actor=_actor(request),
            tool=body.operation,
            data={"service": service_name, "scopes": body.scopes},
        )
        return {"status": "ok", "service": service_name, "operation": body.operation, "result": result}

    @router.get("/api/harness/sessions")
    async def sessions(limit: int = Query(default=100, ge=1, le=500)):
        return {"sessions": HARNESS_EVENT_STORE.sessions(limit=limit), "store": HARNESS_EVENT_STORE.diagnostics()}

    @router.get("/api/harness/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2000),
    ):
        events = HARNESS_EVENT_STORE.events(session_id, after_seq=after_seq, limit=limit)
        if not events and after_seq == 0:
            raise HTTPException(status_code=404, detail="Harness session not found")
        return {
            "session_id": session_id,
            "session": HARNESS_EVENT_STORE.session(session_id),
            "events": events,
            "trace": HARNESS_EVENT_STORE.trace(session_id, limit=limit),
            "integrity": HARNESS_EVENT_STORE.verify_chain(session_id),
            "children": HARNESS_EVENT_STORE.children(session_id),
        }

    @router.get("/api/harness/sessions/{session_id}/trace")
    async def session_trace(session_id: str, limit: int = Query(default=1000, ge=1, le=2000)):
        trace = HARNESS_EVENT_STORE.trace(session_id, limit=limit)
        if not trace.get("spans"):
            raise HTTPException(status_code=404, detail="Harness session not found")
        return trace

    @router.get("/api/harness/sessions/{session_id}/replay")
    async def replay_session(session_id: str, until_seq: int | None = Query(default=None, ge=1)):
        projection = HARNESS_EVENT_STORE.replay(session_id, until_seq=until_seq)
        if not projection.get("checkpoint_seq"):
            raise HTTPException(status_code=404, detail="Harness session not found")
        return projection

    @router.post("/api/harness/sessions/{session_id}/fork")
    async def fork_session(session_id: str, body: SessionForkRequest, request: Request):
        try:
            return HARNESS_EVENT_STORE.fork(session_id, at_seq=body.at_seq, actor=_actor(request))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/harness/sessions/{session_id}/resume")
    async def resume_session(session_id: str, request: Request):
        try:
            return HARNESS_EVENT_STORE.resume(session_id, actor=_actor(request))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/api/harness/sessions/{session_id}")
    async def delete_session_branch(session_id: str, request: Request):
        try:
            return HARNESS_EVENT_STORE.delete_branch(session_id, actor=_actor(request))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
