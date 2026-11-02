from __future__ import annotations

import httpx
import pytest
from langgraph.types import Command

from prometheus_ai.models import Incident, IncidentStatus
from prometheus_ai.util import new_id
from tests.conftest import APPROVER, ingest


@pytest.mark.asyncio
async def test_nothing_is_written_before_approval(client: httpx.AsyncClient, services) -> None:
    incident_id = await ingest(client, services, fingerprint="gate-1")
    incident = await services.repo.get_incident(incident_id)

    assert incident is not None
    assert incident.status == IncidentStatus.AWAITING_APPROVAL
    assert incident.proposal is not None
    assert incident.dry_run is not None and incident.dry_run.ok is True
    assert incident.approval is None
    assert incident.execution is None
    assert services.policy.executor.calls == []

    audit = await services.repo.list_audit(incident_id)
    assert any(event.action == "dry_run" and event.decision == "pass" for event in audit)
    assert not any(event.node == "execute" for event in audit)


@pytest.mark.asyncio
async def test_approval_resumes_the_graph_once_and_resolves(
    client: httpx.AsyncClient, services
) -> None:
    incident_id = await ingest(client, services, fingerprint="approve-1")
    response = await client.post(
        f"/api/incidents/{incident_id}/decision",
        json={"decision": "approve", "approver": "asha", "reason": "narrowest safe action"},
        headers={"X-Approval-Token": APPROVER},
    )
    assert response.status_code == 200, response.text
    incident = await services.repo.get_incident(incident_id)

    assert incident is not None
    assert incident.status == IncidentStatus.RESOLVED
    assert incident.approval is not None
    assert incident.approval.decision == "approve"
    assert incident.approval.action_hash == incident.proposal.action_hash
    assert incident.policy_decision is not None and incident.policy_decision.allowed
    assert incident.execution is not None and incident.execution.status == "executed"
    assert incident.verification is not None and incident.verification.success
    assert incident.postmortem_md
    assert len(services.policy.executor.calls) == 1

    # A second decision cannot execute the action again.
    again = await client.post(
        f"/api/incidents/{incident_id}/decision",
        json={"decision": "approve", "approver": "asha", "reason": "again"},
        headers={"X-Approval-Token": APPROVER},
    )
    assert again.status_code == 409
    assert len(services.policy.executor.calls) == 1


@pytest.mark.asyncio
async def test_rejection_reason_is_persisted_and_escalates(
    client: httpx.AsyncClient, services
) -> None:
    incident_id = await ingest(client, services, fingerprint="reject-1")
    response = await client.post(
        f"/api/incidents/{incident_id}/decision",
        json={"decision": "reject", "approver": "asha", "reason": "runbook does not match"},
        headers={"X-Approval-Token": APPROVER},
    )
    assert response.status_code == 200, response.text
    incident = await services.repo.get_incident(incident_id)

    assert incident is not None
    assert incident.status == IncidentStatus.ESCALATED
    assert incident.approval is not None and incident.approval.decision == "reject"
    assert "rejected by asha" in incident.error
    assert "runbook does not match" in incident.error
    assert incident.execution is None
    assert services.policy.executor.calls == []


@pytest.mark.asyncio
async def test_forged_approval_hash_is_rejected(client: httpx.AsyncClient, services) -> None:
    incident_id = await ingest(client, services, fingerprint="forged-hash")
    # Bypass the API and resume the graph with a hash that does not match.
    await services.runner.graph.ainvoke(
        Command(
            resume={
                "decision": "approve",
                "approver": "mallory",
                "reason": "trust me",
                "action_hash": "0" * 64,
            }
        ),
        {"configurable": {"thread_id": incident_id}},
    )
    incident = await services.repo.get_incident(incident_id)

    assert incident is not None
    assert incident.status == IncidentStatus.ESCALATED
    assert incident.approval is not None and incident.approval.decision == "reject"
    assert "hash did not match" in incident.error
    assert incident.execution is None
    assert services.policy.executor.calls == []


@pytest.mark.asyncio
async def test_failed_dry_run_escalates_instead_of_prompting_for_approval(
    client: httpx.AsyncClient, services
) -> None:
    incident = Incident(
        id=new_id("inc"),
        fingerprint="ghost",
        alertname="DemoCrashLooping",
        namespace="demo",
        service="ghost-api",
        severity="critical",
        summary="synthetic fault on a deployment that does not exist",
        labels={"demo_fault": "crashloop"},
    )
    await services.repo.save_incident(incident)
    await services.runner.start(incident)
    updated = await services.repo.get_incident(incident.id)

    assert updated is not None
    assert updated.status == IncidentStatus.ESCALATED
    assert updated.dry_run is not None and updated.dry_run.ok is False
    assert "resource_not_found" in updated.error
    assert updated.approval is None
    assert services.policy.executor.calls == []
