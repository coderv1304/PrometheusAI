"""Shared fixtures.

Everything runs against the synthetic backend: tests must never need a cluster
or a hosted model. The live adapters are covered by manifest/config tests and by
the documented manual verification commands.
"""

from __future__ import annotations

import httpx
import pytest

from prometheus_ai.config import Settings, reset_settings_cache

APPROVER = "test-approval-token"
WEBHOOK = "test-webhook-token"
POLICY = "test-policy-token"


@pytest.fixture
def settings() -> Settings:
    reset_settings_cache()
    return Settings(
        _env_file=None,
        env="test",
        model_backend="fake",
        approver_token=APPROVER,
        webhook_token=WEBHOOK,
        policy_token=POLICY,
        max_request_bytes=4096,
        max_tool_calls=6,
        max_rehypothesis=2,
        node_timeout_seconds=20.0,
    )


@pytest.fixture
async def services(settings: Settings):
    from prometheus_ai.services.bootstrap import build_services

    runtime = await build_services(settings)
    try:
        yield runtime
    finally:
        await runtime.close()


@pytest.fixture
async def client(settings: Settings, services):
    from prometheus_ai.api.app import create_app

    app = create_app(settings, services=services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def alert_payload(
    *,
    alertname: str = "DemoCrashLooping",
    service: str = "api",
    namespace: str = "demo",
    fault: str = "crashloop",
    fingerprint: str = "test-fingerprint",
) -> dict:
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": fingerprint,
                "labels": {
                    "alertname": alertname,
                    "namespace": namespace,
                    "service": service,
                    "severity": "critical",
                    "demo_fault": fault,
                },
                "annotations": {"summary": f"synthetic {fault} fault on {service}"},
            }
        ],
    }


async def ingest(client: httpx.AsyncClient, services, **kwargs) -> str:
    response = await client.post(
        "/webhooks/alertmanager",
        json=alert_payload(**kwargs),
        headers={"X-Webhook-Token": WEBHOOK},
    )
    assert response.status_code == 202, response.text
    created = response.json()["created"]
    assert created, response.json()
    await services.manager.wait_all()
    return created[0]
