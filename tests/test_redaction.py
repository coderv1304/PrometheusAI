from __future__ import annotations

from types import SimpleNamespace

import pytest

from prometheus_ai.agent.nodes import _audit
from prometheus_ai.llm.redact import redact, redact_value
from prometheus_ai.storage.base import MemoryRepository


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("token=supersecretvalue123", "credential_assignment"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "bearer"),
        ("aws AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
        ("postgres://user:pass@db:5432/app", "connection_string"),
        ("contact ops@example.com", "email"),
    ],
)
def test_secret_patterns_are_redacted(text: str, kind: str) -> None:
    result = redact(text)
    assert kind in result.counts
    assert "REDACTED" in result.text


def test_private_key_blocks_are_removed() -> None:
    block = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
    result = redact(f"before {block} after")
    assert "MIIEvQIBADANBg" not in result.text
    assert result.counts["private_key"] == 1


def test_redact_value_walks_nested_structures() -> None:
    payload = {
        "query": "token=abcdef123456",
        "nested": ["password=hunter2hunter2", {"ok": "plain"}],
    }
    cleaned = redact_value(payload)
    assert "abcdef123456" not in str(cleaned)
    assert "hunter2hunter2" not in str(cleaned)
    assert cleaned["nested"][1]["ok"] == "plain"


@pytest.mark.asyncio
async def test_audit_events_are_redacted_before_persistence() -> None:
    repo = MemoryRepository()
    settings = SimpleNamespace(audit_input_redaction=True)
    deps = SimpleNamespace(settings=settings, repo=repo)

    await _audit(
        deps,
        "inc_test",
        node="gather_evidence",
        action="tool_call",
        input={"query": "token=abcdef123456", "namespace": "demo"},
        detail="authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    )

    [event] = await repo.list_audit("inc_test")
    rendered = f"{event.input} {event.detail}"
    assert "abcdef123456" not in rendered
    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
    assert event.input["namespace"] == "demo"


@pytest.mark.asyncio
async def test_redaction_can_be_disabled_for_offline_debugging() -> None:
    repo = MemoryRepository()
    deps = SimpleNamespace(settings=SimpleNamespace(audit_input_redaction=False), repo=repo)
    await _audit(
        deps,
        "inc_test",
        node="test",
        action="test",
        input={"query": "token=abcdef123456"},
    )
    [event] = await repo.list_audit("inc_test")
    assert event.input["query"] == "token=abcdef123456"
