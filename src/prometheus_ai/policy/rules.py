"""Policy rules: the safety boundary, expressed as plain code.

No model output reaches this module as a decision. The model may only *propose*;
every rule below is evaluated against the live cluster and the approval record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from prometheus_ai.models import ActionRequest, Approval, CheckResult
from prometheus_ai.util import is_safe_slug

# v1 write allowlist. Any action or parameter not listed here is denied.
ACTION_PARAMETERS: dict[str, set[str] | None] = {
    "rollout_restart": set(),
    "rollout_undo": set(),
    "scale": {"replicas"},
}

REPLICA_MIN = 1
REPLICA_MAX = 5


class PolicyViolation(Exception):
    pass


@dataclass(frozen=True)
class RuleContext:
    allowed_namespaces: frozenset[str]
    allowed_actions: frozenset[str]


def check_action_allowlisted(
    action: ActionRequest, ctx: RuleContext
) -> CheckResult:
    if action.action not in ctx.allowed_actions or action.action not in ACTION_PARAMETERS:
        return CheckResult(
            name="action_allowlisted",
            passed=False,
            detail=f"{action.action!r} is not in the v1 write allowlist",
        )
    return CheckResult(
        name="action_allowlisted", passed=True, detail=f"{action.action} is allowlisted"
    )


def check_target_kind(action: ActionRequest) -> CheckResult:
    if action.target_kind != "deployment":
        return CheckResult(
            name="target_kind",
            passed=False,
            detail=f"target kind {action.target_kind!r} denied; only deployments are writable",
        )
    return CheckResult(name="target_kind", passed=True, detail="deployment")


def check_namespace(action: ActionRequest, ctx: RuleContext) -> CheckResult:
    if action.namespace not in ctx.allowed_namespaces:
        return CheckResult(
            name="namespace",
            passed=False,
            detail=f"namespace {action.namespace!r} is outside {sorted(ctx.allowed_namespaces)}",
        )
    return CheckResult(name="namespace", passed=True, detail=action.namespace)


def check_target_name(action: ActionRequest) -> CheckResult:
    if not is_safe_slug(action.target_name):
        return CheckResult(
            name="target_name",
            passed=False,
            detail="target name is not a valid DNS-1123 label",
        )
    return CheckResult(name="target_name", passed=True, detail=action.target_name)


def check_parameters(action: ActionRequest) -> CheckResult:
    expected = ACTION_PARAMETERS.get(action.action)
    if expected is None:
        return CheckResult(name="parameters", passed=False, detail="unknown action")
    unknown = set(action.parameters) - expected
    if unknown:
        return CheckResult(
            name="parameters",
            passed=False,
            detail=f"unexpected parameters for {action.action}: {sorted(unknown)}",
        )
    if action.action == "scale":
        if "replicas" not in action.parameters:
            return CheckResult(name="parameters", passed=False, detail="scale requires replicas")
        replicas = action.parameters["replicas"]
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            return CheckResult(
                name="parameters", passed=False, detail="replicas must be an integer"
            )
        if not REPLICA_MIN <= replicas <= REPLICA_MAX:
            return CheckResult(
                name="parameters",
                passed=False,
                detail=f"replicas {replicas} outside [{REPLICA_MIN}, {REPLICA_MAX}]",
            )
        return CheckResult(
            name="parameters", passed=True, detail=f"replicas={replicas}"
        )
    return CheckResult(name="parameters", passed=True, detail="no parameters required")


def check_live_resource(action: ActionRequest, exists: bool) -> CheckResult:
    if not exists:
        return CheckResult(
            name="resource_exists",
            passed=False,
            detail=(
                f"deployment {action.namespace}/{action.target_name} does not exist "
                "(live lookup; hallucinated targets are rejected)"
            ),
        )
    return CheckResult(name="resource_exists", passed=True, detail="deployment exists")


def check_approval(action: ActionRequest, approval: Approval | None, now) -> tuple[CheckResult, bool]:
    """Return the check plus whether it was an expiry failure (for clearer codes)."""
    if approval is None:
        return (
            CheckResult(
                name="approval",
                passed=False,
                detail="no approval record exists for this action",
            ),
            False,
        )
    if approval.decision != "approve":
        return (
            CheckResult(name="approval", passed=False, detail="approval was rejected"),
            False,
        )
    if approval.action_hash != action.action_hash:
        return (
            CheckResult(
                name="approval",
                passed=False,
                detail="approval hash does not match the current action",
            ),
            False,
        )
    if approval.expires_at is not None and approval.expires_at <= now:
        return (
            CheckResult(name="approval", passed=False, detail="approval has expired"),
            True,
        )
    if not approval.approver.strip():
        return (
            CheckResult(name="approval", passed=False, detail="approval has no approver identity"),
            False,
        )
    return (
        CheckResult(name="approval", passed=True, detail=f"approved by {approval.approver}"),
        False,
    )


class InputBudget:
    """Guard against oversized model/tool input in the same spirit as output checks."""

    MAX_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")

    def __init__(self, max_bytes: int = 64_000) -> None:
        self.max_bytes = max_bytes
