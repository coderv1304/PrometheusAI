"""FastAPI application: webhook intake, approval API and the operator console."""

from __future__ import annotations

import json
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import markdown as markdown_lib
from fastapi import FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from prometheus_ai import __version__
from prometheus_ai.agent.graph import create_checkpointer
from prometheus_ai.config import Settings, get_settings
from prometheus_ai.models import AuditEvent, DryRunResult, Incident, IncidentStatus
from prometheus_ai.services.alerts import IngestResult
from prometheus_ai.services.bootstrap import Services, build_services
from prometheus_ai.util import iso, new_id, utcnow

BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
EVAL_LATEST = Path("evals/out/latest.json")

CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Baseline browser hardening applied to every response."""

    def __init__(self, app, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
                headers.setdefault("Content-Security-Policy", CSP)
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestBodyLimitMiddleware:
    """Rejects oversized request bodies before they reach a parser."""

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return

        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    # Drop the connection before the application sees the body.
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)

    async def _reject(self, send) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


STATUS_TONE = {
    "received": "muted",
    "investigating": "sapphire",
    "awaiting_approval": "amber",
    "approved": "emerald",
    "rejected": "ruby",
    "executing": "sapphire",
    "verifying": "sapphire",
    "resolved": "emerald",
    "escalated": "ruby",
    "failed": "ruby",
}
STATUS_ICON = {
    "received": "○",
    "investigating": "◐",
    "awaiting_approval": "◆",
    "approved": "✓",
    "rejected": "✕",
    "executing": "▶",
    "verifying": "◑",
    "resolved": "●",
    "escalated": "▲",
    "failed": "✕",
}


def _status_label(value: object) -> str:
    return str(value).replace("_", " ").title()


def _short_time(value: object) -> str:
    text = iso(value) if hasattr(value, "isoformat") else str(value)
    return text[11:19] if len(text) >= 19 else text


def _money(value: float | None) -> str:
    return f"${(value or 0):.4f}"


TEMPLATES.env.filters["status_label"] = _status_label
TEMPLATES.env.filters["short_time"] = _short_time
TEMPLATES.env.filters["money"] = _money
TEMPLATES.env.filters["markdown"] = lambda text: markdown_lib.markdown(
    text or "", extensions=["tables", "fenced_code", "sane_lists"]
)
TEMPLATES.env.globals["status_tone"] = lambda v: STATUS_TONE.get(str(v), "muted")
TEMPLATES.env.globals["status_icon"] = lambda v: STATUS_ICON.get(str(v), "○")


def _steps(incident: Incident) -> list[dict[str, str]]:
    """Presentation-only stepper state derived from recorded facts."""

    def state(done: bool, failed: bool = False) -> str:
        if failed:
            return "fail"
        return "done" if done else "pending"

    approval_state = "active" if incident.status == "awaiting_approval" else state(
        incident.approval is not None,
        bool(incident.approval and incident.approval.decision == "reject"),
    )
    policy_failed = bool(incident.policy_decision and not incident.policy_decision.allowed)
    return [
        {
            "name": "Triage",
            "state": state(incident.status != "received"),
            "meta": f"{incident.severity} · {incident.namespace}/{incident.service}",
        },
        {
            "name": "Hypothesis",
            "state": state(incident.hypothesis is not None),
            "meta": (
                f"{incident.hypothesis.category} · confidence "
                f"{incident.hypothesis.confidence:.0%}"
                if incident.hypothesis
                else "waiting for evidence"
            ),
        },
        {
            "name": "Evidence gathering",
            "state": state(bool(incident.evidence)),
            "meta": f"{incident.tool_calls} tool call(s), budget 6",
        },
        {
            "name": "Proposal",
            "state": state(incident.proposal is not None),
            "meta": (
                f"{incident.proposal.action} → {incident.proposal.target_name}"
                if incident.proposal
                else "no proposal yet"
            ),
        },
        {
            "name": "Pre-approval dry run",
            "state": state(
                bool(incident.dry_run and incident.dry_run.ok),
                bool(incident.dry_run and not incident.dry_run.ok),
            ),
            "meta": (
                "passed" if incident.dry_run and incident.dry_run.ok
                else "failed" if incident.dry_run
                else "not reached"
            ),
        },
        {
            "name": "Human approval",
            "state": approval_state,
            "meta": (
                f"{incident.approval.decision} by {incident.approval.approver}"
                if incident.approval
                else "blocks all writes"
            ),
        },
        {
            "name": "Policy check",
            "state": state(incident.policy_decision is not None, policy_failed),
            "meta": incident.policy_decision.code if incident.policy_decision else "not reached",
        },
        {
            "name": "Execute",
            "state": state(
                incident.execution is not None,
                bool(incident.execution and incident.execution.status == "failed"),
            ),
            "meta": incident.execution.status if incident.execution else "not reached",
        },
        {
            "name": "Verify",
            "state": state(
                bool(incident.verification and incident.verification.success),
                bool(incident.verification and not incident.verification.success),
            ),
            "meta": incident.verification.summary if incident.verification else "not reached",
        },
        {
            "name": "Postmortem",
            "state": state(bool(incident.postmortem_md)),
            "meta": "written to the incident record" if incident.postmortem_md else "not reached",
        },
    ]


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str = Field(min_length=2, max_length=120)
    reason: str = Field(default="", max_length=500)


def create_app(
    settings: Settings | None = None,
    *,
    services: Services | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            if services is None:
                checkpointer = await create_checkpointer(settings, stack)
                app.state.services = await build_services(settings, checkpointer=checkpointer)
            else:
                app.state.services = services
            app.state.stack = stack
            yield
        finally:
            current: Services | None = getattr(app.state, "services", None)
            if current is not None and services is None:
                await current.close()
            await stack.aclose()

    app = FastAPI(
        title="PrometheusAI",
        version=__version__,
        description="Approval-gated incident response for Kubernetes (demo scope).",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware, max_bytes=settings.max_request_bytes
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=not settings.is_dev)
    if services is not None:
        app.state.services = services
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def svc(request: Request) -> Services:
        runtime: Services | None = getattr(request.app.state, "services", None)
        if runtime is None:
            raise HTTPException(status_code=503, detail="runtime not initialised")
        return runtime

    def require_token(request: Request, token: str | None) -> None:
        expected = svc(request).settings.approver_token
        if token is None or not secrets.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="invalid approval token")

    def require_webhook_token(request: Request, token: str | None) -> None:
        settings_now = svc(request).settings
        expected = settings_now.webhook_token
        if not expected:
            # Dev convenience only; the settings validator refuses to start
            # outside dev without a webhook token.
            if settings_now.is_dev:
                return
            raise HTTPException(status_code=503, detail="webhook authentication is not configured")
        if token is None or not secrets.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="invalid webhook token")

    # ---------------------------------------------------------------- intake
    @app.post("/webhooks/alertmanager", status_code=status.HTTP_202_ACCEPTED)
    async def alertmanager_webhook(
        payload: dict,
        request: Request,
        x_webhook_token: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        scheme, _, bearer = (authorization or "").partition(" ")
        presented = x_webhook_token or (
            bearer if scheme.lower() == "bearer" else None
        )
        require_webhook_token(request, presented)
        if not isinstance(payload.get("alerts"), list):
            raise HTTPException(status_code=422, detail="expected an Alertmanager payload")
        if len(payload["alerts"]) > 100:
            raise HTTPException(status_code=413, detail="too many alerts in one webhook")
        result: IngestResult = await svc(request).alerts.ingest(payload)
        return result.__dict__

    @app.get("/api/incidents")
    async def list_incidents(
        request: Request,
        limit: int = 50,
        status_filter: IncidentStatus | None = None,
    ) -> list[dict]:
        incidents = await svc(request).repo.list_incidents(
            limit=min(max(limit, 1), 200), status=status_filter
        )
        return [i.model_dump(mode="json") for i in incidents]

    @app.get("/api/incidents/{incident_id}")
    async def get_incident(incident_id: str, request: Request) -> dict:
        incident = await svc(request).repo.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return incident.model_dump(mode="json")

    @app.get("/api/incidents/{incident_id}/audit")
    async def get_audit(incident_id: str, request: Request) -> list[dict]:
        return [
            event.model_dump(mode="json")
            for event in await svc(request).repo.list_audit(incident_id)
        ]

    @app.post("/api/incidents/{incident_id}/preflight")
    async def preflight(
        incident_id: str,
        request: Request,
        x_approval_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        """Re-run the server-side dry run on demand. Never writes to the cluster."""
        require_token(request, x_approval_token)
        runtime = svc(request)
        incident = await runtime.repo.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        if incident.proposal is None:
            raise HTTPException(status_code=409, detail="incident has no proposal")
        result: DryRunResult = await runtime.policy.dry_run(
            action=incident.proposal.to_action(), incident_id=incident_id
        )
        await runtime.repo.save_incident(
            incident.model_copy(update={"dry_run": result, "updated_at": utcnow()})
        )
        await runtime.repo.add_audit(
            AuditEvent(
                id=new_id("aud"),
                incident_id=incident_id,
                node="preflight",
                actor="human",
                action="dry_run",
                decision="pass" if result.ok else "deny",
                output_hash=result.action_hash,
                detail=(result.error or result.stdout)[:500],
            )
        )
        return result.model_dump(mode="json")

    # -------------------------------------------------------------- approvals
    @app.post("/api/incidents/{incident_id}/decision")
    async def decide(
        incident_id: str,
        body: DecisionRequest,
        request: Request,
        x_approval_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        require_token(request, x_approval_token)
        return await _decide(svc(request), incident_id, body)

    @app.post("/ui/incidents/{incident_id}/decision")
    async def decide_form(
        incident_id: str,
        request: Request,
        decision: Annotated[str, Form()],
        approver: Annotated[str, Form()],
        token: Annotated[str, Form()],
        reason: Annotated[str, Form()] = "",
    ):
        try:
            require_token(request, token)
            if decision not in {"approve", "reject"}:
                raise HTTPException(status_code=422, detail="decision must be approve or reject")
            await _decide(
                svc(request),
                incident_id,
                DecisionRequest(decision=decision, approver=approver, reason=reason),
            )
        except HTTPException as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "partials/decision_error.html",
                {"message": str(exc.detail), "incident_id": incident_id},
                status_code=exc.status_code,
            )
        return RedirectResponse(url=f"/incidents/{incident_id}", status_code=303)

    async def _decide(runtime: Services, incident_id: str, body: DecisionRequest) -> dict:
        incident = await runtime.repo.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        if incident.status != IncidentStatus.AWAITING_APPROVAL:
            raise HTTPException(
                status_code=409,
                detail=f"incident is {incident.status}, not awaiting approval",
            )
        if incident.approval is not None:
            raise HTTPException(status_code=409, detail="a decision is already recorded")
        if incident.dry_run is None or not incident.dry_run.ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    "a passing pre-approval dry run is required before a decision "
                    "can be recorded; run POST /api/incidents/{id}/preflight"
                ),
            )
        try:
            await runtime.runner.resolve(
                incident_id,
                decision=body.decision,
                approver=body.approver,
                reason=body.reason,
            )
        except Exception as exc:  # noqa: BLE001 - surface as a conflict, keep the audit trail
            raise HTTPException(
                status_code=409,
                detail=f"could not resume the investigation: {type(exc).__name__}: {exc}",
            ) from exc
        updated = await runtime.repo.get_incident(incident_id)
        return updated.model_dump(mode="json") if updated else {}

    @app.post("/api/admin/kill-switch")
    async def kill_switch(
        request: Request,
        enabled: bool = True,
        x_approval_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        require_token(request, x_approval_token)
        await svc(request).policy.set_kill_switch(enabled)
        return {"kill_switch": enabled}

    # ------------------------------------------------------------------ ops
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict:
        runtime = getattr(request.app.state, "services", None)
        return {
            "ok": runtime is not None,
            "model": runtime.model.name if runtime else None,
            "synthetic": runtime.synthetic if runtime else None,
            "policy_mode": runtime.settings.policy_mode if runtime else None,
            "dry_run_before_approval": True,
        }

    @app.get("/version")
    async def version() -> dict:
        from prometheus_ai.versioning import collect_versions

        return collect_versions()

    @app.get("/metrics", response_class=Response)
    async def metrics(request: Request) -> Response:
        runtime = svc(request)
        incidents = await runtime.repo.list_incidents(limit=500)
        counts: dict[str, int] = {}
        denied = 0
        for incident in incidents:
            counts[str(incident.status)] = counts.get(str(incident.status), 0) + 1
            if incident.policy_decision and not incident.policy_decision.allowed:
                denied += 1
        lines = [
            "# HELP prometheus_ai_incidents_total Incidents by status.",
            "# TYPE prometheus_ai_incidents_total gauge",
        ]
        for state, count in sorted(counts.items()):
            lines.append(f'prometheus_ai_incidents_total{{status="{state}"}} {count}')
        lines += [
            "# HELP prometheus_ai_policy_denials_total Incidents whose proposed write was denied.",
            "# TYPE prometheus_ai_policy_denials_total gauge",
            f"prometheus_ai_policy_denials_total {denied}",
            "# HELP prometheus_ai_synthetic_mode 1 when the runtime is the synthetic demo.",
            "# TYPE prometheus_ai_synthetic_mode gauge",
            f"prometheus_ai_synthetic_mode {1 if runtime.synthetic else 0}",
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # ------------------------------------------------------------------- UI
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        runtime = svc(request)
        incidents = await runtime.repo.list_incidents(limit=50)
        counts: dict[str, int] = {}
        for incident in incidents:
            counts[str(incident.status)] = counts.get(str(incident.status), 0) + 1
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "incidents": incidents,
                "counts": counts,
                "settings": runtime.settings,
                "synthetic": runtime.synthetic,
                "model": runtime.model.name,
                "eval": _latest_eval(),
            },
        )

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    async def incident_console(incident_id: str, request: Request):
        runtime = svc(request)
        incident = await runtime.repo.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        audit = await runtime.repo.list_audit(incident_id)
        return TEMPLATES.TemplateResponse(
            request,
            "incident.html",
            {
                "incident": incident,
                "audit": audit,
                "steps": _steps(incident),
                "settings": runtime.settings,
                "synthetic": runtime.synthetic,
                "model": runtime.model.name,
            },
        )

    @app.get("/incidents/{incident_id}/postmortem", response_class=HTMLResponse)
    async def postmortem_view(incident_id: str, request: Request):
        runtime = svc(request)
        incident = await runtime.repo.get_incident(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident not found")
        return TEMPLATES.TemplateResponse(
            request,
            "postmortem.html",
            {
                "incident": incident,
                "settings": runtime.settings,
                "synthetic": runtime.synthetic,
            },
        )

    @app.get("/evals", response_class=HTMLResponse)
    async def evals_view(request: Request):
        runtime = svc(request)
        return TEMPLATES.TemplateResponse(
            request,
            "evals.html",
            {
                "settings": runtime.settings,
                "synthetic": runtime.synthetic,
                "eval": _latest_eval(),
            },
        )

    return app


def _latest_eval() -> dict | None:
    if not EVAL_LATEST.exists():
        return None
    try:
        return json.loads(EVAL_LATEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


app = create_app()
