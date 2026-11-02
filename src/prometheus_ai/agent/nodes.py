"""Graph nodes. Each node is a small, auditable step with a hard timeout.

The model only ever returns a *proposal*. `policy_check` and `execute` are the
only nodes that can mutate the cluster, and they call the policy engine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from prometheus_ai.agent.postmortem import render_postmortem
from prometheus_ai.agent.state import IncidentState
from prometheus_ai.agent.verify import Verifier
from prometheus_ai.config import Settings
from prometheus_ai.llm.client import ModelClient
from prometheus_ai.llm.redact import injection_signals, redact, redact_value
from prometheus_ai.models import (
    Approval,
    AuditEvent,
    DryRunResult,
    Hypothesis,
    Incident,
    IncidentStatus,
    PolicyDecision,
    Postmortem,
    Proposal,
    RootCause,
)
from prometheus_ai.policy.client import PolicyClient
from prometheus_ai.storage.base import Repository
from prometheus_ai.tools.base import ToolContext, ToolRegistry
from prometheus_ai.util import iso, new_id, sha256_text, truncate, utcnow


@dataclass
class GraphDeps:
    settings: Settings
    repo: Repository
    model: ModelClient
    registry: ToolRegistry
    policy: PolicyClient
    verifier: Verifier
    model_backend: str = "demo-deterministic"


class ModelFailure(Exception):
    pass


class CostCapExceeded(Exception):
    pass


async def _load(deps: GraphDeps, incident_id: str) -> Incident:
    incident = await deps.repo.get_incident(incident_id)
    if incident is None:
        raise ModelFailure(f"incident {incident_id} not found")
    return incident


async def _save(deps: GraphDeps, incident: Incident, **changes) -> Incident:
    if changes:
        incident = incident.model_copy(update=changes)
    incident = incident.touch()
    return await deps.repo.save_incident(incident)


async def _audit(
    deps: GraphDeps,
    incident_id: str,
    *,
    node: str,
    action: str,
    actor: str = "agent",
    tool: str | None = None,
    input: dict | None = None,
    output_hash: str = "",
    decision: str = "",
    detail: str = "",
) -> None:
    if deps.settings.audit_input_redaction:
        audit_input = redact_value(input or {})
        detail = redact(detail).text
    else:
        audit_input = input or {}
    await deps.repo.add_audit(
        AuditEvent(
            id=new_id("aud"),
            incident_id=incident_id,
            node=node,
            actor=actor,  # type: ignore[arg-type]
            tool=tool,
            action=action,
            input=audit_input,
            output_hash=output_hash,
            decision=decision,
            detail=detail,
        )
    )


def _account(incident: Incident, response) -> Incident:
    return incident.model_copy(
        update={
            "tokens_in": incident.tokens_in + response.tokens_in,
            "tokens_out": incident.tokens_out + response.tokens_out,
            "cost_usd": round(incident.cost_usd + response.cost_usd, 6),
            "model_backend": incident.model_backend,
        }
    )


def _check_cost_cap(settings: Settings, incident: Incident) -> None:
    if incident.cost_usd >= settings.cost_cap_usd:
        raise CostCapExceeded(
            f"per-incident cost cap reached (${incident.cost_usd:.4f} >= "
            f"${settings.cost_cap_usd:.2f})"
        )


async def _call_with_repair(deps: GraphDeps, incident: Incident, mode: str):
    """One repair attempt, then escalate. Never loop indefinitely on bad output."""
    last_error = "empty output"
    for attempt in range(2):
        _check_cost_cap(deps.settings, incident)
        try:
            if mode == "triage":
                response = await deps.model.triage(
                    incident, repair_note=None if attempt == 0 else last_error
                )
            else:
                response = await deps.model.propose(
                    incident, repair_note=None if attempt == 0 else last_error
                )
        except Exception as exc:  # noqa: BLE001 - model/network errors become escalation
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        incident = _account(incident, response)
        ok = response.hypothesis if mode == "triage" else response.proposal
        if ok is not None:
            return response, incident, attempt
        last_error = response.error or "empty output"
    raise ModelFailure(f"{mode} output failed after one repair attempt: {last_error}")


async def triage(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    incident = await _save(
        deps,
        incident,
        status=IncidentStatus.INVESTIGATING,
        model_backend=deps.model_backend,
    )
    await _audit(
        deps,
        incident.id,
        node="triage",
        action="start_investigation",
        decision="continue",
        detail=f"alert={incident.alertname} svc={incident.namespace}/{incident.service}",
    )
    return {"status": IncidentStatus.INVESTIGATING.value, "evidence": [], "rounds": 0}


async def hypothesize(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    rounds = int(state.get("rounds", 0))
    if incident.evidence:
        rounds += 1

    if rounds > deps.settings.max_rehypothesis:
        return {
            "rounds": rounds,
            "hypothesis": incident.hypothesis.model_dump(mode="json")
            if incident.hypothesis
            else None,
            "status": incident.status.value,
        }

    try:
        response, incident, attempts = await _call_with_repair(deps, incident, "triage")
    except (ModelFailure, CostCapExceeded) as exc:
        await _save(deps, incident, status=IncidentStatus.ESCALATED, error=str(exc))
        await _audit(
            deps,
            incident.id,
            node="hypothesize",
            action="model_failure",
            decision="escalate",
            detail=str(exc),
        )
        return {"error": str(exc), "status": IncidentStatus.ESCALATED.value}

    hypothesis = response.hypothesis
    assert hypothesis is not None
    # Bound the query list independently of the model.
    hypothesis = hypothesis.model_copy(
        update={"next_queries": hypothesis.next_queries[:2]}
    )
    incident = await _save(deps, incident, hypothesis=hypothesis)
    await _audit(
        deps,
        incident.id,
        node="hypothesize",
        action="set_hypothesis",
        decision=hypothesis.category.value,
        input={"confidence": hypothesis.confidence, "queries": len(hypothesis.next_queries)},
        detail=truncate(hypothesis.reasoning, 300),
    )
    if response.error:
        await _audit(
            deps,
            incident.id,
            node="hypothesize",
            action="repair_attempt",
            decision="recovered" if attempts else "failed",
            detail=truncate(response.error, 300),
        )
    return {
        "hypothesis": hypothesis.model_dump(mode="json"),
        "rounds": rounds,
        "status": incident.status.value,
        "tokens_in": incident.tokens_in,
        "tokens_out": incident.tokens_out,
        "cost_usd": incident.cost_usd,
    }


async def gather_evidence(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    hypothesis = Hypothesis.model_validate(state["hypothesis"]) if state.get("hypothesis") else None
    if hypothesis is None or not hypothesis.next_queries:
        return {"new_calls": 0}

    ctx = ToolContext(
        incident_id=incident.id,
        namespace=incident.namespace,
        service=incident.service,
        labels=dict(incident.labels),
    )
    before = deps.registry.calls_used(incident.id)
    evidence = await deps.registry.collect(hypothesis.next_queries, ctx)
    after = deps.registry.calls_used(incident.id)

    incident = await _save(
        deps,
        incident,
        evidence=[*incident.evidence, *evidence],
        tool_calls=after,
        status=IncidentStatus.INVESTIGATING,
    )
    for item in evidence:
        signals = injection_signals(item.detail)
        await _audit(
            deps,
            incident.id,
            node="gather_evidence",
            action="tool_call",
            tool=item.source.value,
            input={"query": item.query[:500]},
            output_hash=item.raw_hash,
            decision="read" if "tool_error" not in item.summary else "error",
            detail=(
                f"untrusted={item.untrusted}; injection_signals={len(signals)}; "
                f"summary={item.summary[:200]}"
            ),
        )
    return {
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "new_calls": after - before,
        "tool_calls": after,
        "status": incident.status.value,
    }


async def propose_fix(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    if state.get("hypothesis"):
        incident = await _save(
            deps, incident, hypothesis=Hypothesis.model_validate(state["hypothesis"])
        )
    try:
        response, incident, attempts = await _call_with_repair(deps, incident, "propose")
    except (ModelFailure, CostCapExceeded) as exc:
        await _save(deps, incident, status=IncidentStatus.ESCALATED, error=str(exc))
        await _audit(
            deps,
            incident.id,
            node="propose_fix",
            action="model_failure",
            decision="escalate",
            detail=str(exc),
        )
        return {"error": str(exc), "status": IncidentStatus.ESCALATED.value}

    proposal = response.proposal
    assert proposal is not None
    # Never trust a model-supplied hash: recompute from the validated fields.
    proposal = proposal.model_copy(update={"action_hash": proposal.to_action().action_hash})
    action = proposal.to_action()

    # Pre-approval preflight: ask the API server whether the action would be
    # valid. This is a read-equivalent dry run, not a write, and a failure
    # escalates instead of prompting a human to approve something unrunnable.
    try:
        dry_run = await deps.policy.dry_run(action=action, incident_id=incident.id)
    except Exception as exc:  # noqa: BLE001 - fail closed
        dry_run = DryRunResult(
            action_hash=action.action_hash,
            ok=False,
            error=f"dry-run call failed: {type(exc).__name__}: {exc}",
        )
    await _audit(
        deps,
        incident.id,
        node="propose_fix",
        action="dry_run",
        decision="pass" if dry_run.ok else "deny",
        input={"action": action.action, "target": action.target_name},
        output_hash=dry_run.action_hash,
        detail=truncate(dry_run.error or dry_run.stdout, 300),
    )
    if not dry_run.ok:
        reason = f"pre-approval dry run failed: {dry_run.error or 'unknown error'}"
        await _save(
            deps,
            incident,
            proposal=proposal,
            dry_run=dry_run,
            status=IncidentStatus.ESCALATED,
            error=reason,
        )
        return {
            "proposal": proposal.model_dump(mode="json"),
            "status": IncidentStatus.ESCALATED.value,
            "error": reason,
        }

    incident = await _save(
        deps,
        incident,
        proposal=proposal,
        dry_run=dry_run,
        status=IncidentStatus.AWAITING_APPROVAL,
    )
    await _audit(
        deps,
        incident.id,
        node="propose_fix",
        action="propose",
        decision="awaiting_approval",
        input={"action": proposal.action, "target": proposal.target_name},
        output_hash=proposal.action_hash,
        detail=truncate(proposal.rationale, 300),
    )
    if response.error:
        await _audit(
            deps,
            incident.id,
            node="propose_fix",
            action="repair_attempt",
            decision="recovered" if attempts else "failed",
            detail=truncate(response.error, 300),
        )
    return {
        "proposal": proposal.model_dump(mode="json"),
        "status": IncidentStatus.AWAITING_APPROVAL.value,
        "tokens_in": incident.tokens_in,
        "tokens_out": incident.tokens_out,
        "cost_usd": incident.cost_usd,
    }


async def approval_gate(state: IncidentState, deps: GraphDeps) -> dict:
    """Suspend the graph. Resuming requires a decision for this exact hash."""
    from langgraph.types import interrupt

    proposal = Proposal.model_validate(state["proposal"])
    expires_at = utcnow() + timedelta(minutes=deps.settings.approval_ttl_minutes)
    payload = interrupt(
        {
            "kind": "approval_required",
            "incident_id": state["incident_id"],
            "proposal": proposal.model_dump(mode="json"),
            "action_hash": proposal.action_hash,
            "expires_at": iso(expires_at),
        }
    )

    incident = await _load(deps, state["incident_id"])
    decision = "approve" if str(payload.get("decision", "")).lower() == "approve" else "reject"
    approver = str(payload.get("approver") or "unknown").strip()[:120]
    reason = truncate(str(payload.get("reason") or ""), 500)
    returned_hash = str(payload.get("action_hash") or proposal.action_hash)

    if returned_hash != proposal.action_hash:
        decision = "reject"
        reason = "approval hash did not match the proposal"
        approver = approver or "system"

    approval = Approval(
        action_hash=proposal.action_hash,
        approver=approver,
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        expires_at=expires_at,
    )
    status = IncidentStatus.APPROVED if decision == "approve" else IncidentStatus.REJECTED
    changes: dict = {"approval": approval, "status": status}
    if decision == "reject":
        # Carry the human's reason into incident.error so the escalation node
        # and the postmortem do not fall back to a generic message.
        changes["error"] = f"rejected by {approver}: {reason or 'no reason recorded'}"
    await _save(deps, incident, **changes)
    await _audit(
        deps,
        incident.id,
        node="approval_gate",
        actor="human",
        action=f"decision:{decision}",
        decision=decision,
        input={"approver": approver, "action_hash": proposal.action_hash},
        output_hash=proposal.action_hash,
        detail=reason,
    )
    return {"approval": approval.model_dump(mode="json"), "status": status.value}


async def policy_check(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    proposal = Proposal.model_validate(state["proposal"])
    approval = Approval.model_validate(state["approval"]) if state.get("approval") else None
    action = proposal.to_action()

    try:
        decision = await deps.policy.evaluate(
            action=action, approval=approval, incident_id=incident.id
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        decision = PolicyDecision(
            allowed=False,
            code="policy_unavailable",
            reason=f"policy engine unavailable, failing closed: {exc}",
            action_hash=action.action_hash,
        )

    await _save(deps, incident, policy_decision=decision)
    await _audit(
        deps,
        incident.id,
        node="policy_check",
        actor="policy",
        action="evaluate",
        decision=decision.code,
        input={"action": action.action, "target": action.target_name},
        output_hash=decision.action_hash,
        detail=decision.reason,
    )
    if not decision.allowed:
        await _save(deps, incident, status=IncidentStatus.ESCALATED, error=decision.reason)
        await _audit(
            deps,
            incident.id,
            node="policy_check",
            actor="policy",
            action="deny",
            decision=decision.code,
            detail=decision.reason,
        )
        return {
            "policy": decision.model_dump(mode="json"),
            "status": IncidentStatus.ESCALATED.value,
            "error": decision.reason,
        }
    return {"policy": decision.model_dump(mode="json"), "status": incident.status.value}


async def execute(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    proposal = Proposal.model_validate(state["proposal"])
    approval = Approval.model_validate(state["approval"])
    action = proposal.to_action()

    try:
        decision, execution = await deps.policy.execute(
            action=action, approval=approval, incident_id=incident.id
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        await _save(deps, incident, status=IncidentStatus.ESCALATED, error=str(exc))
        await _audit(
            deps,
            incident.id,
            node="execute",
            actor="policy",
            action="execute",
            decision="error",
            detail=str(exc),
        )
        return {"error": str(exc), "status": IncidentStatus.ESCALATED.value}

    if execution is None or not decision.allowed:
        await _save(
            deps,
            incident,
            policy_decision=decision,
            status=IncidentStatus.ESCALATED,
            error=decision.reason,
        )
        return {"status": IncidentStatus.ESCALATED.value, "error": decision.reason}

    await _save(
        deps,
        incident,
        execution=execution,
        policy_decision=decision,
        status=IncidentStatus.VERIFYING,
        attempts=incident.attempts + 1,
    )
    await _audit(
        deps,
        incident.id,
        node="execute",
        actor="policy",
        action="execute",
        decision=execution.status,
        input={"action": action.action, "target": action.target_name},
        output_hash=sha256_text(" ".join(execution.command)),
        detail=truncate(execution.stdout or execution.stderr, 300),
    )
    return {
        "execution": execution.model_dump(mode="json"),
        "status": IncidentStatus.VERIFYING.value,
    }


async def verify(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    execution = incident.execution
    if execution is None:
        return {"status": IncidentStatus.ESCALATED.value, "error": "no execution to verify"}
    try:
        verification = await deps.verifier.verify(incident, execution)
    except Exception as exc:  # noqa: BLE001 - verification failure is an escalation, not a crash
        verification = None
        error = f"verifier error: {exc}"
    else:
        error = "" if verification.success else verification.summary

    if verification and verification.success:
        incident = await _save(
            deps,
            incident,
            verification=verification,
            status=IncidentStatus.RESOLVED,
            resolved_at=utcnow(),
            error="",
        )
        decision = "resolved"
    else:
        incident = await _save(
            deps,
            incident,
            verification=verification,
            status=IncidentStatus.ESCALATED,
            error=error,
        )
        decision = "escalate"

    await _audit(
        deps,
        incident.id,
        node="verify",
        action="verify",
        decision=decision,
        input={"execution": execution.idempotency_key},
        detail=(verification.summary if verification else error),
    )
    return {
        "verification": verification.model_dump(mode="json") if verification else None,
        "status": incident.status.value,
        "error": error,
    }


async def escalate(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    reason = state.get("error") or incident.error or "escalated without a recorded reason"
    await _save(deps, incident, status=IncidentStatus.ESCALATED, error=reason)
    await _audit(
        deps,
        incident.id,
        node="escalate",
        action="escalate",
        decision="human_required",
        detail=truncate(reason, 500),
    )
    return {"status": IncidentStatus.ESCALATED.value, "error": reason}


async def postmortem(state: IncidentState, deps: GraphDeps) -> dict:
    incident = await _load(deps, state["incident_id"])
    audit = await deps.repo.list_audit(incident.id)
    markdown = render_postmortem(incident, audit)
    root = incident.hypothesis.category if incident.hypothesis else RootCause.UNKNOWN
    await deps.repo.save_postmortem(
        Postmortem(incident_id=incident.id, markdown=markdown, root_cause_category=root)
    )
    await _save(deps, incident, postmortem_md=markdown)
    await _audit(
        deps,
        incident.id,
        node="postmortem",
        action="write_postmortem",
        decision="done",
        output_hash=sha256_text(markdown),
        detail=f"{len(audit)} audit events rendered",
    )
    return {"postmortem": markdown, "status": incident.status.value}


async def _with_timeout(coro, seconds: float):
    return await asyncio.wait_for(coro, timeout=seconds)