"""Policy client: in-process or remote.

In production (`PROMETHEUS_AI_POLICY_MODE=http`) the agent has no kubectl write
credential. The policy service owns the executor and the only write RBAC. The
in-process client exists so tests and the offline demo can run without a second
process; the safety logic is identical.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from prometheus_ai.models import (
    ActionRequest,
    Approval,
    DryRunResult,
    ExecutionResult,
    PolicyDecision,
)
from prometheus_ai.policy.engine import PolicyEngine
from prometheus_ai.policy.executor import KubectlExecutor


class PolicyClient(Protocol):
    async def evaluate(
        self, *, action: ActionRequest, approval: Approval | None, incident_id: str
    ) -> PolicyDecision: ...

    async def execute(
        self, *, action: ActionRequest, approval: Approval | None, incident_id: str
    ) -> tuple[PolicyDecision, ExecutionResult | None]: ...

    async def dry_run(self, *, action: ActionRequest, incident_id: str) -> DryRunResult: ...

    async def set_kill_switch(self, enabled: bool) -> None: ...


class InProcessPolicyClient:
    def __init__(self, engine: PolicyEngine, executor: KubectlExecutor) -> None:
        self.engine = engine
        self.executor = executor

    async def evaluate(
        self, *, action: ActionRequest, approval: Approval | None, incident_id: str
    ) -> PolicyDecision:
        return self.engine.evaluate(action, approval, incident_id=incident_id)

    async def execute(
        self, *, action: ActionRequest, approval: Approval | None, incident_id: str
    ) -> tuple[PolicyDecision, ExecutionResult | None]:
        decision = self.engine.evaluate(action, approval, incident_id=incident_id)
        if not decision.allowed:
            return decision, None
        result = await self.executor.execute(incident_id=incident_id, action=action)
        if result.status == "executed":
            self.engine.record_write(incident_id)
        return decision, result

    async def dry_run(self, *, action: ActionRequest, incident_id: str) -> DryRunResult:
        decision = self.engine.preflight(action)
        if not decision.allowed:
            return DryRunResult(
                action_hash=action.action_hash,
                ok=False,
                error=f"preflight denied ({decision.code}): {decision.reason}",
            )
        return await self.executor.preview(action=action)

    async def set_kill_switch(self, enabled: bool) -> None:
        self.engine.kill_switch = enabled


class HttpPolicyClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def evaluate(
        self, *, action: ActionRequest, approval: Approval | None, incident_id: str
    ) -> PolicyDecision:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/evaluate",
                headers=self._headers,
                json={
                    "incident_id": incident_id,
                    "action": action.model_dump(mode="json"),
                    "approval": approval.model_dump(mode="json") if approval else None,
                },
            )
            response.raise_for_status()
            return PolicyDecision.model_validate(response.json())

    async def execute(
        self, *, action: ActionRequest, approval: Approval | None, incident_id: str
    ) -> tuple[PolicyDecision, ExecutionResult | None]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/execute",
                headers=self._headers,
                json={
                    "incident_id": incident_id,
                    "action": action.model_dump(mode="json"),
                    "approval": approval.model_dump(mode="json") if approval else None,
                },
            )
            response.raise_for_status()
            payload = response.json()
            decision = PolicyDecision.model_validate(payload["decision"])
            execution = (
                ExecutionResult.model_validate(payload["execution"])
                if payload.get("execution")
                else None
            )
            return decision, execution

    async def dry_run(self, *, action: ActionRequest, incident_id: str) -> DryRunResult:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/dry-run",
                headers=self._headers,
                json={
                    "incident_id": incident_id,
                    "action": action.model_dump(mode="json"),
                },
            )
            response.raise_for_status()
            return DryRunResult.model_validate(response.json())

    async def set_kill_switch(self, enabled: bool) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/kill-switch",
                headers=self._headers,
                json={"enabled": enabled},
            )
            response.raise_for_status()
