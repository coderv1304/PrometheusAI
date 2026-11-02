from __future__ import annotations

import httpx
import pytest

from prometheus_ai.api.app import create_app
from prometheus_ai.config import Settings
from tests.conftest import APPROVER, WEBHOOK, alert_payload, ingest


@pytest.mark.asyncio
async def test_security_headers_are_present(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers  # dev/test only


@pytest.mark.asyncio
async def test_webhook_requires_a_token_when_configured(client: httpx.AsyncClient) -> None:
    payload = alert_payload(fingerprint="no-token")
    missing = await client.post("/webhooks/alertmanager", json=payload)
    wrong = await client.post(
        "/webhooks/alertmanager", json=payload, headers={"X-Webhook-Token": "wrong"}
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_webhook_accepts_header_or_bearer_token(client: httpx.AsyncClient) -> None:
    header = await client.post(
        "/webhooks/alertmanager",
        json=alert_payload(fingerprint="header-token"),
        headers={"X-Webhook-Token": WEBHOOK},
    )
    bearer = await client.post(
        "/webhooks/alertmanager",
        json=alert_payload(fingerprint="bearer-token"),
        headers={"Authorization": f"Bearer {WEBHOOK}"},
    )
    assert header.status_code == 202
    assert bearer.status_code == 202


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_before_parsing(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/webhooks/alertmanager",
        content=b"x" * 8192,
        headers={"Content-Type": "application/json", "X-Webhook-Token": WEBHOOK},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_webhook_rejects_alert_floods(client: httpx.AsyncClient) -> None:
    payload = {"alerts": [{"status": "firing"} for _ in range(101)]}
    response = await client.post(
        "/webhooks/alertmanager",
        json=payload,
        headers={"X-Webhook-Token": WEBHOOK},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_approval_endpoint_requires_the_approver_token(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/incidents/inc_missing/decision",
        json={"decision": "approve", "approver": "tester", "reason": "test"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_preflight_endpoint_requires_the_approver_token(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/incidents/inc_missing/preflight")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_kill_switch_requires_the_approver_token(client: httpx.AsyncClient, services) -> None:
    response = await client.post("/api/admin/kill-switch", params={"enabled": "true"})
    assert response.status_code == 401
    assert services.engine.kill_switch is False


@pytest.mark.asyncio
async def test_kill_switch_engages_with_a_valid_token(client: httpx.AsyncClient, services) -> None:
    response = await client.post(
        "/api/admin/kill-switch",
        params={"enabled": "true"},
        headers={"X-Approval-Token": APPROVER},
    )
    assert response.status_code == 200
    assert services.engine.kill_switch is True


@pytest.mark.asyncio
async def test_docs_can_be_disabled(services, settings: Settings) -> None:
    locked = settings.model_copy(update={"enable_docs": False})
    app = create_app(locked, services=services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


@pytest.mark.asyncio
async def test_decision_is_refused_without_a_passing_dry_run(
    client: httpx.AsyncClient, services
) -> None:
    incident_id = await ingest(client, services, fingerprint="no-dry-run")
    incident = await services.repo.get_incident(incident_id)
    assert incident is not None
    tampered = incident.model_copy(update={"dry_run": None})
    await services.repo.save_incident(tampered)

    response = await client.post(
        f"/api/incidents/{incident_id}/decision",
        json={"decision": "approve", "approver": "tester", "reason": "test"},
        headers={"X-Approval-Token": APPROVER},
    )
    assert response.status_code == 409
    assert "dry run" in response.text


@pytest.mark.asyncio
async def test_preflight_endpoint_returns_a_typed_result(
    client: httpx.AsyncClient, services
) -> None:
    incident_id = await ingest(client, services, fingerprint="preflight")
    response = await client.post(
        f"/api/incidents/{incident_id}/preflight",
        headers={"X-Approval-Token": APPROVER},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["action_hash"]
    assert body["command"][-1] == "--dry-run=server"
