"""Harness composition, event sourcing and recovery inspection endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.app.services.harness_events import HARNESS_EVENT_STORE
from backend.app.services.harness_packages import HARNESS_PACKAGE_MANAGER, HarnessPackageError
from backend.app.services.harness_plugins import harness_capabilities_payload


class ProfileActivationRequest(BaseModel):
    profile: str = Field(min_length=2, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]+$")


class SessionForkRequest(BaseModel):
    at_seq: int | None = Field(default=None, ge=1)


class ServiceInvokeRequest(BaseModel):
    operation: str = Field(min_length=2, max_length=128, pattern=r"^[a-zA-Z0-9._/-]+$")
    payload: dict = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list, max_length=8)


def _actor(request: Request) -> str:
    return str(
        request.headers.get("x-auth-request-user")
        or request.headers.get("x-operator")
        or (request.client.host if request.client else "operator")
    )[:160]


def build_router() -> APIRouter:
    router = APIRouter(tags=["Harness Runtime"])

    @router.get("/api/harness/runtime")
    async def runtime():
        return {"status": "ok", "harness": harness_capabilities_payload()}

    @router.get("/api/harness/profiles")
    async def profiles():
        value = HARNESS_PACKAGE_MANAGER.diagnostics()
        return {
            "active_profile": value["active_profile"],
            "runtime_write_enabled": value["runtime_write_enabled"],
            "profiles": value["profiles"],
            "bundles": value["bundles"],
            "packages": value["packages"],
            "last_error": value["last_error"],
        }

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
            "events": events,
            "integrity": HARNESS_EVENT_STORE.verify_chain(session_id),
            "children": HARNESS_EVENT_STORE.children(session_id),
        }

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

    return router
