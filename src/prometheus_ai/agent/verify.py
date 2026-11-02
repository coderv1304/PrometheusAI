"""Verification after an approved write.

A verification is a set of named checks over a bounded window. Failure never
triggers an automatic second write: it escalates with a recommended rollback,
because every write needs a fresh approval.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Protocol

import httpx

from prometheus_ai.config import Settings
from prometheus_ai.models import (
    CheckResult,
    DryRunResult,
    ExecutionResult,
    Incident,
    Verification,
)


class Verifier(Protocol):
    async def verify(self, incident: Incident, execution: ExecutionResult) -> Verification: ...


class SyntheticVerifier:
    """Demo verifier. Mirrors an injected fault clearing after a successful action."""

    async def verify(self, incident: Incident, execution: ExecutionResult) -> Verification:
        forced_failure = incident.labels.get("demo_verify") == "fail"
        if execution.status not in {"executed", "dry_run"}:
            return Verification(
                success=False,
                summary=f"execution status was {execution.status}",
                checks=[
                    CheckResult(name="execution", passed=False, detail=execution.status),
                ],
            )
        if forced_failure:
            return Verification(
                success=False,
                summary="synthetic fault did not clear within the verify window",
                checks=[CheckResult(name="alert_resolved", passed=False, detail="ALERTS still firing")],
            )
        return Verification(
            success=True,
            summary="alert resolved and deployment reached its ready state (synthetic)",
            checks=[
                CheckResult(name="alert_resolved", passed=True, detail="no firing series"),
                CheckResult(name="deployment_ready", passed=True, detail="1/1 ready"),
            ],
        )


class LiveVerifier:
    def __init__(self, settings: Settings, *, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self.kubectl = shutil.which("kubectl") or "kubectl"

    async def verify(self, incident: Incident, execution: ExecutionResult) -> Verification:
        checks: list[CheckResult] = []

        alert_query = (
            f'ALERTS{{alertname="{incident.alertname}",namespace="{incident.namespace}"}}'
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.settings.prometheus_url.rstrip('/')}/api/v1/query",
                    params={"query": alert_query},
                )
                response.raise_for_status()
                payload = response.json()
            series = payload.get("data", {}).get("result", [])
            firing = [
                s
                for s in series
                if s.get("value") and str(s["value"][1]).lower() not in {"0", "0.0"}
            ]
            checks.append(
                CheckResult(
                    name="alert_resolved",
                    passed=not firing,
                    detail=f"{len(firing)} firing series for {incident.alertname}",
                )
            )
        except httpx.HTTPError as exc:
            checks.append(
                CheckResult(name="alert_resolved", passed=False, detail=f"prometheus: {exc}")
            )

        argv = [
            self.kubectl,
            "rollout",
            "status",
            f"deployment/{incident.service}",
            "-n",
            incident.namespace,
            f"--timeout={int(self.timeout)}s",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout + 5)
            checks.append(
                CheckResult(
                    name="deployment_ready",
                    passed=proc.returncode == 0,
                    detail=(stdout or stderr).decode(errors="replace")[-300:].strip(),
                )
            )
        except (OSError, TimeoutError) as exc:
            checks.append(
                CheckResult(name="deployment_ready", passed=False, detail=str(exc))
            )

        success = all(c.passed for c in checks)
        return Verification(
            success=success,
            summary=(
                "alert cleared and rollout is ready"
                if success
                else "verification failed; no automatic second write, escalate instead"
            ),
            checks=checks,
            window_seconds=int(self.timeout),
        )


class SyntheticExecutor:
    """No-op executor used only in synthetic demo mode; never reported as a real write."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def preview(self, *, action) -> DryRunResult:
        return DryRunResult(
            action_hash=action.action_hash,
            ok=True,
            command=["synthetic", action.action, action.target_name, "--dry-run=server"],
            stdout="synthetic dry run; no API server was contacted",
        )

    async def execute(self, *, incident_id: str, action) -> ExecutionResult:
        self.calls.append(f"{incident_id}:{action.action_hash[:8]}")
        return ExecutionResult(
            action_hash=action.action_hash,
            status="executed",
            command=["synthetic", action.action, action.target_name],
            stdout="synthetic demo execution; no cluster write occurred",
            idempotency_key=f"synthetic:{incident_id}:{action.action_hash[:8]}",
        )
