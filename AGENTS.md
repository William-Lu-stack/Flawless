# CISRE repository instructions

These rules apply to human contributors and AI coding agents working in this repository.

## Read before changing architecture

1. `docs/TEAM_ARCHITECTURE_AND_EXTENSION_GUIDE_ZH.md`
2. `backend/app/adapters/README.md` for database, VM, storage, middleware or cloud work
3. `docs/DEEPSEEK_HARNESS_INTEGRATION_ZH.md` for planner/harness work

## Stable boundaries

- Keep Kubernetes remediation compatible with Rancher, uploaded kubeconfig and local ServiceAccount transports.
- Never let an LLM response, Adapter, browser or frontend execute arbitrary shell, SQL or HTTP mutations.
- Every mutation must remain: typed action -> policy/risk gate -> human approval -> executor -> same-target readback -> recovery verification -> record.
- Infrastructure Adapters are read-only. Put product-specific code under `backend/app/adapters/<domain>/`.
- Additive changes are allowed in v1 contracts. Breaking schema or semantic changes require a parallel v2.
- Do not put credentials, internal URLs, private IPs, company names or personal data in source, fixtures, docs, logs or commits.

## Module placement

- API route declarations: `backend/app/api/features/`
- Request/response schemas: `backend/app/api/schemas/` or the existing versioned schema module
- Orchestration and reusable business services: `backend/app/services/`
- Product/provider integrations: `backend/app/adapters/<domain>/`
- Agent reasoning only: `agents/`
- Kubernetes typed execution: `mcp_servers/`
- UI pages/components: `frontend/modern/src/`
- Kubernetes packaging: `manifests/`, `charts/`, `deploy/`
- Cross-module behavior tests: `tests/`

Do not add new provider branches to `backend/app/application.py`. Treat it as the legacy composition root and move new behavior behind a service or feature router.

## Required workflow

- Preserve unrelated working-tree changes.
- Start with a contract test; implement behind the stable interface; add failure/timeout and redaction coverage.
- Re-read the real target after a mutation. An API 2xx or model statement is not recovery proof.
- Run `python -m pytest tests` and `cd frontend/modern && npm run build` before handoff.
- Update the team architecture guide when adding a domain, action type, contract version or top-level module.
