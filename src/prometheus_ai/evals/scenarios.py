"""Evaluation scenarios: YAML in, typed objects out."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from prometheus_ai.models import EvidenceRequest, RootCause


class ProposalSpec(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["rollout_restart", "scale", "rollout_undo"]
    namespace: str = "demo"
    target_kind: Literal["deployment"] = "deployment"
    target_name: str = "api"
    parameters: dict = Field(default_factory=dict)
    rationale: str = Field(min_length=10, max_length=3000)
    confidence: float = Field(ge=0.0, le=1.0)
    rollback: str = Field(min_length=5, max_length=1000)


class Scenario(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    fault: str
    namespace: str = "demo"
    service: str = "api"
    severity: Literal["critical", "warning", "info"] = "critical"
    alertname: str
    summary: str
    expected_root_cause: RootCause
    acceptable_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    expected_status: Literal["resolved", "escalated"]
    decision: Literal["approve", "reject"] = "approve"
    demo_verify: Literal["pass", "fail"] = "pass"
    hypothesis: RootCause
    hypothesis_confidence: float = 0.9
    hypothesis_reasoning: str = "Evidence points at the injected fault."
    proposal: ProposalSpec
    queries: list[EvidenceRequest] = Field(
        default_factory=lambda: [
            EvidenceRequest(tool="prometheus_query", query='up{namespace="demo"}'),
            EvidenceRequest(tool="loki_logs", query='{namespace="demo"}'),
            EvidenceRequest(tool="kubectl_read", query="describe deployment", params={"op": "describe"}),
        ]
    )
    notes: str = ""

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("scenario id must be a slug")
        return value


def load_scenarios(directory: str | Path) -> list[Scenario]:
    directory = Path(directory)
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        scenario = Scenario.model_validate(data)
        if scenario.id != path.stem:
            raise ValueError(f"{path.name}: id must match the filename ({scenario.id!r})")
        scenarios.append(scenario)
    if not scenarios:
        raise FileNotFoundError(f"no scenario YAML files in {directory}")
    return scenarios
