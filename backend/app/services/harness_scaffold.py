"""Generate a self-contained, reviewable CISRE team plugin project.

The archive is intentionally a source scaffold, not executable code loaded
inside the control plane.  Teams own the isolated Provider and Skills while
CISRE keeps mutation authority, approval and recovery verification.
"""
from __future__ import annotations

import io
import textwrap
import zipfile
from typing import Any


def _block(value: str) -> str:
    return textwrap.dedent(value).strip() + "\n"


def build_plugin_project(manifest_source: str, package: dict[str, Any]) -> tuple[str, bytes]:
    plugin_id = str(package.get("id") or "team.plugin")
    version = str(package.get("version") or "1.0.0")
    domains = [str(item) for item in package.get("domains") or ["common"]]
    domain = domains[0].replace("virtual-machine", "virtual_machine")
    project_root = f"{plugin_id}/"
    image = f"registry.example.com/platform-plugins/{plugin_id}:{version}"
    skill_id = f"{domain.replace('_', '-')}-operations"

    files = {
        "manifest.yaml": manifest_source.strip() + "\n",
        "README.md": _block(f"""
            # {plugin_id}

            CISRE `{domain}` 领域插件项目。它提供事实、证据、恢复验证和 Skill，
            不直接持有生产变更权限。

            ## 本地开发

            1. 在 `provider/app.py` 中实现 `discover`、`collect_evidence`、`verify`。
            2. 在 `skills/{skill_id}/SKILL.md` 中写清触发证据、动作提案、回滚和恢复判据。
            3. 运行 `python -m pytest tests`。
            4. 构建并推送 `{image}`，再应用 `deploy/deployment.yaml`。
            5. 将 `manifest.yaml` 的 runtime 改为远程 Provider Service 地址，在 CISRE
               “平台能力 → 插件中心”校验并安装。

            真实变更必须由插件返回 typed proposal，进入 CISRE 的策略、爆炸半径、
            人工逐项审批、领域执行器、同目标回读和恢复验证链路。
        """),
        "provider/app.py": _block(f''' 
            """Isolated read-only Provider for {plugin_id}."""
            from typing import Any

            from fastapi import FastAPI, HTTPException
            from pydantic import BaseModel, Field


            app = FastAPI(title="{plugin_id}", docs_url=None, redoc_url=None)
            READ_ONLY_OPERATIONS = {{"discover", "collect_evidence", "verify", "propose"}}


            class InvokeRequest(BaseModel):
                service: str = Field(min_length=2, max_length=160)
                operation: str = Field(min_length=2, max_length=128)
                payload: dict[str, Any] = Field(default_factory=dict)


            @app.get("/health")
            async def health() -> dict[str, Any]:
                return {{"status": "up", "plugin": "{plugin_id}", "read_only": True}}


            @app.post("/invoke")
            async def invoke(request: InvokeRequest) -> dict[str, Any]:
                if request.operation not in READ_ONLY_OPERATIONS:
                    raise HTTPException(status_code=403, detail="operation is outside the read-only contract")
                # Replace this placeholder with a vendor client. Credentials
                # must come from a workload identity or Secret reference and
                # must never be returned in evidence.
                return {{
                    "contract_version": "cisre.infrastructure.evidence/v1",
                    "plugin": "{plugin_id}",
                    "domain": "{domain}",
                    "operation": request.operation,
                    "implemented": False,
                    "read_only": True,
                    "facts": {{}},
                    "signals": [],
                    "proposals": [],
                }}
        '''),
        "provider/requirements.txt": "fastapi>=0.115,<1\nuvicorn[standard]>=0.34,<1\npydantic>=2.10,<3\n",
        "provider/Dockerfile": _block("""
            FROM python:3.13-slim
            WORKDIR /app
            COPY requirements.txt ./
            RUN pip install --no-cache-dir -r requirements.txt
            COPY app.py ./
            RUN useradd --system --uid 10001 --home /tmp plugin
            USER 10001:10001
            EXPOSE 8080
            CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
        """),
        "deploy/deployment.yaml": _block(f"""
            apiVersion: apps/v1
            kind: Deployment
            metadata:
              name: {plugin_id.replace('.', '-')}
              namespace: cisre-plugins
              labels:
                app.kubernetes.io/name: {plugin_id.replace('.', '-')}
                app.kubernetes.io/part-of: cisre
            spec:
              replicas: 2
              selector:
                matchLabels:
                  app.kubernetes.io/name: {plugin_id.replace('.', '-')}
              template:
                metadata:
                  labels:
                    app.kubernetes.io/name: {plugin_id.replace('.', '-')}
                spec:
                  automountServiceAccountToken: false
                  securityContext:
                    runAsNonRoot: true
                    seccompProfile: {{type: RuntimeDefault}}
                  containers:
                    - name: provider
                      image: {image}
                      ports: [{{name: http, containerPort: 8080}}]
                      securityContext:
                        allowPrivilegeEscalation: false
                        readOnlyRootFilesystem: true
                        capabilities: {{drop: ["ALL"]}}
                      readinessProbe:
                        httpGet: {{path: /health, port: http}}
                      resources:
                        requests: {{cpu: 50m, memory: 96Mi}}
                        limits: {{cpu: 500m, memory: 256Mi}}
            ---
            apiVersion: v1
            kind: Service
            metadata:
              name: {plugin_id.replace('.', '-')}
              namespace: cisre-plugins
            spec:
              selector:
                app.kubernetes.io/name: {plugin_id.replace('.', '-')}
              ports: [{{name: http, port: 8080, targetPort: http}}]
        """),
        f"skills/{skill_id}/SKILL.md": _block(f"""
            ---
            name: {skill_id}
            description: Diagnose and propose bounded recovery for {domain} resources.
            ---

            # {domain} operations

            ## Trigger contract

            Use this Skill only when fresh `{domain}` evidence matches the declared
            failure signature. Rank one primary Skill first; add a secondary Skill
            only when its dependency and gate evidence are explicit.

            ## Required evidence

            - Stable resource identity and environment.
            - Current health, error and dependency evidence with timestamps.
            - Recent change and blast-radius context.
            - The exact postcondition that can prove recovery.

            ## Action contract

            Return typed proposals only. Include target, parameters, reason, risk,
            blast radius, rollback and recovery criteria. Never execute shell, SQL,
            API or infrastructure mutations from this Provider.

            ## Completion contract

            CISRE closes the incident only after human approval, executor receipt,
            same-target readback and fresh verification of every mandatory criterion.
        """),
        "tests/test_provider_contract.py": _block("""
            from fastapi.testclient import TestClient
            from provider.app import app


            def test_provider_is_read_only():
                client = TestClient(app)
                health = client.get("/health").json()
                assert health["read_only"] is True
                denied = client.post("/invoke", json={
                    "service": "evidence.domain",
                    "operation": "mutate",
                    "payload": {},
                })
                assert denied.status_code == 403


            def test_evidence_response_is_explicitly_unimplemented_until_team_wires_provider():
                client = TestClient(app)
                result = client.post("/invoke", json={
                    "service": "evidence.domain",
                    "operation": "collect_evidence",
                    "payload": {"resource_id": "example"},
                }).json()
                assert result["read_only"] is True
                assert result["implemented"] is False
        """),
        "docs/SECURITY_BOUNDARY.md": _block("""
            # 安全边界

            - Provider 默认无生产写权限、无宿主机 Shell、无 Kubernetes mutation authority。
            - 凭据仅通过工作负载身份或 Secret 引用注入，不进入 Manifest、模型上下文、事件和日志。
            - 插件只能提供库存、证据、验证和 typed proposal。
            - 任何真实变更都必须经过 CISRE 策略与爆炸半径评估、人工审批、受控执行器、
              同目标写后回读、恢复验证和不可篡改审计。
            - 验证 API 返回、模型结论或旧实例健康都不能单独证明恢复。
        """),
        "pyproject.toml": _block("""
            [tool.pytest.ini_options]
            pythonpath = ["."]
            testpaths = ["tests"]
        """),
        "provider/__init__.py": "",
    }

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(project_root + name)
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return f"{plugin_id}-{version}.zip", output.getvalue()
