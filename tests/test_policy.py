from __future__ import annotations

import subprocess
from datetime import timedelta

import pytest

from prometheus_ai.models import ActionRequest, Approval, DryRunResult
from prometheus_ai.policy.client import InProcessPolicyClient
from prometheus_ai.policy.engine import PolicyEngine
from prometheus_ai.policy.executor import InMemoryLedger, KubectlExecutor
from prometheus_ai.policy.inventory import FakeInventory
from prometheus_ai.util import utcnow


def make_engine() -> PolicyEngine:
    inventory = FakeInventory()
    inventory.add("demo", "api", 1)
    return PolicyEngine(
        allowed_namespaces={"demo"},
        allowed_actions={"rollout_restart", "scale", "rollout_undo"},
        inventory=inventory,
        max_writes_per_hour=3,
    )


def action(**overrides) -> ActionRequest:
    payload = {
        "action": "rollout_restart",
        "namespace": "demo",
        "target_kind": "deployment",
        "target_name": "api",
        "parameters": {},
    }
    payload.update(overrides)
    return ActionRequest.model_validate(payload)


def approval_for(request: ActionRequest, **overrides) -> Approval:
    payload = {
        "action_hash": request.action_hash,
        "approver": "on-call",
        "decision": "approve",
        "reason": "test",
        "expires_at": utcnow() + timedelta(minutes=10),
    }
    payload.update(overrides)
    return Approval.model_validate(payload)


def test_allowlisted_action_passes_with_live_target_and_approval() -> None:
    request = action()
    decision = make_engine().evaluate(request, approval_for(request), incident_id="inc_1")
    assert decision.allowed is True
    assert decision.code == "allowed"


def test_missing_approval_denies() -> None:
    decision = make_engine().evaluate(action(), None, incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "approval_missing"


def test_hash_mismatch_denies() -> None:
    other = action(target_name="web")
    decision = make_engine().evaluate(action(), approval_for(other), incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "approval_missing"


def test_expired_approval_denies() -> None:
    request = action()
    approval = approval_for(request, expires_at=utcnow() - timedelta(minutes=1))
    decision = make_engine().evaluate(request, approval, incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "approval_expired"


def test_namespace_outside_allowlist_denies() -> None:
    request = action(namespace="kube-system")
    decision = make_engine().evaluate(request, approval_for(request), incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "namespace_denied"


def test_unknown_target_denies() -> None:
    request = action(target_name="ghost")
    decision = make_engine().evaluate(request, approval_for(request), incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "resource_not_found"


@pytest.mark.parametrize("replicas", [0, 6, 50, -1])
def test_scale_out_of_bounds_denies(replicas: int) -> None:
    request = action(action="scale", parameters={"replicas": replicas})
    decision = make_engine().evaluate(request, approval_for(request), incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "parameters_out_of_bounds"


def test_kill_switch_denies_everything() -> None:
    request = action()
    engine = make_engine()
    engine.kill_switch = True
    decision = engine.evaluate(request, approval_for(request), incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "kill_switch_engaged"


def test_preflight_does_not_require_an_approval() -> None:
    decision = make_engine().preflight(action())
    assert decision.allowed is True


def test_preflight_still_denies_bad_namespace() -> None:
    decision = make_engine().preflight(action(namespace="kube-system"))
    assert decision.allowed is False
    assert decision.code == "namespace_denied"


def test_preflight_still_respects_kill_switch() -> None:
    engine = make_engine()
    engine.kill_switch = True
    decision = engine.preflight(action())
    assert decision.allowed is False
    assert decision.code == "kill_switch_engaged"


def test_rate_limit_denies_the_fourth_write_for_one_incident() -> None:
    engine = make_engine()
    request = action()
    approval = approval_for(request)
    for _ in range(3):
        engine.record_write("inc_1")
    decision = engine.evaluate(request, approval, incident_id="inc_1")
    assert decision.allowed is False
    assert decision.code == "rate_limited"


class _Runner:
    def __init__(self, *, returncode: int = 0, stdout: str = "ok", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    async def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.mark.asyncio
async def test_preview_is_server_side_and_never_records_the_ledger() -> None:
    ledger = InMemoryLedger()
    executor = KubectlExecutor(ledger, dry_run=False, kubectl="kubectl")
    runner = _Runner()
    result = await executor.preview(action=action(), runner=runner)

    assert result.ok is True
    assert result.command[-1] == "--dry-run=server"
    assert ledger.already_executed(executor.idempotency_key("inc_1", action().action_hash)) is False


@pytest.mark.asyncio
async def test_preview_failure_is_reported() -> None:
    executor = KubectlExecutor(InMemoryLedger(), dry_run=False, kubectl="kubectl")
    runner = _Runner(returncode=1, stderr="Error from server: not found")
    result = await executor.preview(action=action(), runner=runner)
    assert result.ok is False
    assert "not found" in result.error


@pytest.mark.asyncio
async def test_duplicate_execution_is_skipped_by_the_ledger() -> None:
    ledger = InMemoryLedger()
    executor = KubectlExecutor(ledger, dry_run=False, kubectl="kubectl")
    runner = _Runner()
    request = action()
    first = await executor.execute(incident_id="inc_1", action=request, runner=runner)
    second = await executor.execute(incident_id="inc_1", action=request, runner=runner)

    assert first.status == "executed"
    assert second.status == "skipped_duplicate"
    assert len(runner.calls) == 2  # one dry run + one real write; duplicate stops early


@pytest.mark.asyncio
async def test_inprocess_client_denies_preflight_before_touching_kubectl() -> None:
    executor = KubectlExecutor(InMemoryLedger(), dry_run=False, kubectl="kubectl")
    client = InProcessPolicyClient(make_engine(), executor)
    result: DryRunResult = await client.dry_run(
        action=action(namespace="kube-system"), incident_id="inc_1"
    )
    assert result.ok is False
    assert "namespace_denied" in result.error
