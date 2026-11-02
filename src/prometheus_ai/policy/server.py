"""Standalone policy service.

This process is the *only* component allowed to hold a Kubernetes write
credential (see ADR-4 and NFR-10). The agent talks to it over HTTP with a shared
token; the agent's own ServiceAccount has no write RBAC at all.

Endpoints:

    GET  /healthz          liveness (no auth)
    GET  /readyz           readiness (no auth)
    GET  /v1/rules         the effective allowlist (auth)
    POST /v1/evaluate      policy decision, no write (auth)
    POST /v1/dry-run       server-side dry run, no write (auth)
    POST /v1/execute       evaluate then execute once (auth)
    POST /v1/kill-switch   engage/release the global write halt (auth)

Failures are closed: unknown action, bad payload, missing approval or an
unreachable cluster all produce a denial, never an execution.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from prometheus_ai.config import Settings, get_settings
from prometheus_ai.models import (
    ActionRequest,
    Approval,
    DryRunResult,
    ExecutionResult,
    PolicyDecision,
)
from prometheus_ai.policy.engine import PolicyEngine
from prometheus_ai.policy.executor import KubectlExecutor
from prometheus_ai.services.bootstrap import build_inprocess_engine

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class EvaluateRequest(BaseModel):
    incident_id: str = Field(min_length=3, max_length=120)
    action: ActionRequest
    approval: Approval | None = None


class ExecuteRequest(BaseModel):
    incident_id: str = Field(min_length=3, max_length=120)
    action: ActionRequest
    approval: Approval | None = None


class DryRunRequest(BaseModel):
    incident_id: str = Field(min_length=3, max_length=120)
    action: ActionRequest


class ExecuteResponse(BaseModel):
    decision: PolicyDecision
    execution: ExecutionResult | None = None


class KillSwitchRequest(BaseModel):
    enabled: bool


class RulesResponse(BaseModel):
    namespaces: list[str]
    actions: list[str]
    max_writes_per_hour: int
    kill_switch: bool
    dry_run: bool


def create_policy_app(
    settings: Settings | None = None,
    *,
    engine: PolicyEngine | None = None,
    executor: KubectlExecutor | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if engine is None or executor is None:
        engine, executor = build_inprocess_engine(settings)

    max_writes = settings.max_writes_per_hour

    app = FastAPI(
        title="PrometheusAI policy service",
        version="0.1.0",
        description=(
            "Deterministic write gate. Holds the only Kubernetes write credential; "
            "the investigating agent has none."
        ),
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    app.state.engine = engine
    app.state.executor = executor
    app.state.settings = settings

    def require_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = settings.policy_token
        scheme, _, presented = (authorization or "").partition(" ")
        # Empty token means the operator forgot to configure auth: fail closed.
        if (
            not expected
            or scheme.lower() != "bearer"
            or not secrets.compare_digest(presented, expected)
        ):
            raise HTTPException(status_code=401, detail="invalid policy token")

    auth = Depends(require_token)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        state: PolicyEngine = request.app.state.engine
        return {
            "ok": True,
            "kill_switch": state.kill_switch,
            "dry_run": settings.dry_run,
        }

    @app.get("/readyz")
    async def readyz() -> dict:
        return {
            "ok": True,
            "dry_run": settings.dry_run,
            "policy_mode": settings.policy_mode,
            "inventory_backend": settings.inventory_backend,
        }

    @app.get("/v1/rules", response_model=RulesResponse)
    async def rules(request: Request, _: None = auth) -> RulesResponse:
        state: PolicyEngine = request.app.state.engine
        ctx = state.rule_context
        return RulesResponse(
            namespaces=sorted(ctx.allowed_namespaces),
            actions=sorted(ctx.allowed_actions),
            max_writes_per_hour=max_writes,
            kill_switch=state.kill_switch,
            dry_run=settings.dry_run,
        )

    @app.post("/v1/evaluate", response_model=PolicyDecision)
    async def evaluate(
        body: EvaluateRequest, request: Request, _: None = auth
    ) -> PolicyDecision:
        state: PolicyEngine = request.app.state.engine
        return state.evaluate(
            body.action, body.approval, incident_id=body.incident_id
        )

    @app.post("/v1/dry-run", response_model=DryRunResult)
    async def dry_run(
        body: DryRunRequest, request: Request, _: None = auth
    ) -> DryRunResult:
        state: PolicyEngine = request.app.state.engine
        runner: KubectlExecutor = request.app.state.executor
        decision = state.preflight(body.action)
        if not decision.allowed:
            return DryRunResult(
                action_hash=body.action.action_hash,
                ok=False,
                error=f"preflight denied ({decision.code}): {decision.reason}",
            )
        return await runner.preview(action=body.action)

    @app.post("/v1/execute", response_model=ExecuteResponse)
    async def execute(
        body: ExecuteRequest, request: Request, _: None = auth
    ) -> ExecuteResponse:
        state: PolicyEngine = request.app.state.engine
        runner: KubectlExecutor = request.app.state.executor
        decision = state.evaluate(
            body.action, body.approval, incident_id=body.incident_id
        )
        if not decision.allowed:
            return ExecuteResponse(decision=decision, execution=None)
        result = await runner.execute(incident_id=body.incident_id, action=body.action)
        if result.status == "executed":
            state.record_write(body.incident_id)
        return ExecuteResponse(decision=decision, execution=result)

    @app.post("/v1/kill-switch")
    async def kill_switch(
        body: KillSwitchRequest, request: Request, _: None = auth
    ) -> dict:
        state: PolicyEngine = request.app.state.engine
        state.kill_switch = body.enabled
        return {"kill_switch": state.kill_switch}

    return app


app = create_policy_app()
