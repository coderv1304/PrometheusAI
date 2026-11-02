"""Deterministic policy engine.

The engine is intentionally boring: ordered checks, no LLM, no heuristics.
Every check is recorded so a denial can be explained to a human afterwards.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Protocol

from prometheus_ai.models import (
    ActionRequest,
    Approval,
    CheckResult,
    PolicyDecision,
)
from prometheus_ai.policy.rules import (
    RuleContext,
    check_action_allowlisted,
    check_approval,
    check_live_resource,
    check_namespace,
    check_parameters,
    check_target_kind,
    check_target_name,
)
from prometheus_ai.util import utcnow


class ClusterInventory(Protocol):
    def deployment_exists(self, namespace: str, name: str) -> bool: ...


class PolicyEngine:
    def __init__(
        self,
        *,
        allowed_namespaces: set[str] | frozenset[str],
        allowed_actions: set[str] | frozenset[str],
        inventory: ClusterInventory,
        max_writes_per_hour: int = 3,
        clock=utcnow,
    ) -> None:
        self._ctx = RuleContext(
            allowed_namespaces=frozenset(allowed_namespaces),
            allowed_actions=frozenset(allowed_actions),
        )
        self._inventory = inventory
        self._max_writes = max_writes_per_hour
        self._clock = clock
        self._writes: dict[str, deque[datetime]] = defaultdict(deque)
        self.kill_switch = False

    @property
    def rule_context(self) -> RuleContext:
        return self._ctx

    def evaluate(
        self,
        action: ActionRequest,
        approval: Approval | None,
        *,
        incident_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision:
        now = now or self._clock()
        checks: list[CheckResult] = []

        if self.kill_switch:
            checks.append(
                CheckResult(
                    name="kill_switch",
                    passed=False,
                    detail="global kill switch is engaged; all writes are halted",
                )
            )
            # Still describe the rest of the policy for the operator, but the
            # decision is already denied.
            checks.append(check_action_allowlisted(action, self._ctx))
            return self._decide(action, checks, "kill_switch")

        checks.append(check_action_allowlisted(action, self._ctx))
        checks.append(check_target_kind(action))
        checks.append(check_namespace(action, self._ctx))
        checks.append(check_target_name(action))

        if checks[-2].passed and checks[-1].passed:
            exists = self._inventory.deployment_exists(action.namespace, action.target_name)
            checks.append(check_live_resource(action, exists))
        else:
            checks.append(
                CheckResult(
                    name="resource_exists",
                    passed=False,
                    detail="skipped: namespace or target invalid",
                )
            )

        checks.append(check_parameters(action))
        approval_check, expired = check_approval(action, approval, now)
        checks.append(approval_check)

        window_start = now - timedelta(hours=1)
        recent = [ts for ts in self._writes[incident_id] if ts > window_start]
        within_limit = len(recent) < self._max_writes
        checks.append(
            CheckResult(
                name="rate_limit",
                passed=within_limit,
                detail=f"{len(recent)}/{self._max_writes} writes in the last hour for this incident",
            )
        )

        return self._decide(
            action,
            checks,
            "approval_expired" if expired else None,
        )

    def preflight(self, action: ActionRequest) -> PolicyDecision:
        """Checks that do not depend on an approval or the write rate limit.

        Used for the pre-approval server-side dry run. A model may ask for a
        preview, but it still cannot preview an action or namespace outside the
        allowlist, and the kill switch still denies it.
        """
        checks: list[CheckResult] = []
        if self.kill_switch:
            checks.append(
                CheckResult(
                    name="kill_switch",
                    passed=False,
                    detail="global kill switch is engaged; all writes are halted",
                )
            )
            checks.append(check_action_allowlisted(action, self._ctx))
            return self._decide(action, checks, "kill_switch")

        checks.append(check_action_allowlisted(action, self._ctx))
        checks.append(check_target_kind(action))
        checks.append(check_namespace(action, self._ctx))
        checks.append(check_target_name(action))
        if checks[-2].passed and checks[-1].passed:
            exists = self._inventory.deployment_exists(action.namespace, action.target_name)
            checks.append(check_live_resource(action, exists))
        else:
            checks.append(
                CheckResult(
                    name="resource_exists",
                    passed=False,
                    detail="skipped: namespace or target invalid",
                )
            )
        checks.append(check_parameters(action))
        return self._decide(action, checks, None)

    def _decide(
        self, action: ActionRequest, checks: list[CheckResult], override_code: str | None
    ) -> PolicyDecision:
        failing = [c for c in checks if not c.passed]
        if not failing:
            return PolicyDecision(
                allowed=True,
                code="allowed",
                reason="all policy checks passed",
                action_hash=action.action_hash,
                checks=checks,
            )
        code = (
            _CODE_BY_CHECK.get(override_code, override_code)
            if override_code
            else _CODE_BY_CHECK.get(failing[0].name, "policy_denied")
        )
        return PolicyDecision(
            allowed=False,
            code=code,
            reason=failing[0].detail,
            action_hash=action.action_hash,
            checks=checks,
        )

    def record_write(self, incident_id: str, *, at: datetime | None = None) -> None:
        self._writes[incident_id].append(at or self._clock())

    def writes_last_hour(self, incident_id: str) -> int:
        window_start = self._clock() - timedelta(hours=1)
        return sum(1 for ts in self._writes[incident_id] if ts > window_start)

    def reset_rate_limits(self) -> None:
        self._writes.clear()


_CODE_BY_CHECK = {
    "kill_switch": "kill_switch_engaged",
    "action_allowlisted": "action_not_allowlisted",
    "target_kind": "target_kind_denied",
    "namespace": "namespace_denied",
    "target_name": "invalid_target_name",
    "resource_exists": "resource_not_found",
    "parameters": "parameters_out_of_bounds",
    "approval": "approval_missing",
    "rate_limit": "rate_limited",
}
