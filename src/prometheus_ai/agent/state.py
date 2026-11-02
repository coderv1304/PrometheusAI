"""Typed graph state. Everything is JSON-serialisable for the checkpointer."""

from __future__ import annotations

from typing import Annotated, TypedDict


def _append(left: list, right: list | None) -> list:
    return [*left, *(right or [])]


class IncidentState(TypedDict, total=False):
    incident_id: str
    alert: dict
    evidence: Annotated[list[dict], _append]
    hypothesis: dict | None
    proposal: dict | None
    approval: dict | None
    policy: dict | None
    execution: dict | None
    verification: dict | None
    postmortem: str | None
    status: str
    rounds: int
    new_calls: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error: str
    synthetic: bool
