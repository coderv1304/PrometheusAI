"""Domain models.

These are the contracts between the receiver, agent, policy engine, API and UI.
Write proposals are strict (`extra="forbid"`): a model that invents a field is
a schema failure, not something we silently ignore.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from prometheus_ai.util import iso, sha256_json, utcnow

Severity = Literal["critical", "warning", "info"]
ActionName = Literal["rollout_restart", "scale", "rollout_undo"]


class IncidentStatus(StrEnum):
    RECEIVED = "received"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"


class RootCause(StrEnum):
    CRASHLOOP = "crashloop"
    OOM = "oom"
    BAD_IMAGE = "bad_image"
    LATENCY = "latency"
    BROKEN_CONFIG = "broken_config"
    UNKNOWN = "unknown"


class EvidenceSource(StrEnum):
    METRICS = "metrics"
    LOGS = "logs"
    KUBERNETES = "kubernetes"
    RUNBOOK = "runbook"


class Alert(BaseModel):
    fingerprint: str
    alertname: str
    status: str = "firing"
    severity: Severity = "warning"
    namespace: str = "demo"
    service: str = "unknown"
    summary: str = ""
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(default_factory=utcnow)

    @field_validator("namespace", "service")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class EvidenceRequest(BaseModel):
    """A bounded, typed request the model may make for more evidence."""

    model_config = ConfigDict(extra="forbid")

    tool: Literal["prometheus_query", "loki_logs", "kubectl_read", "runbook_search"]
    query: str = Field(min_length=1, max_length=2000)
    params: dict[str, str] = Field(default_factory=dict)


class Evidence(BaseModel):
    id: str
    source: EvidenceSource
    query: str
    summary: str
    detail: str
    untrusted: bool = False
    tool_call_id: str
    collected_at: datetime = Field(default_factory=utcnow)
    raw_hash: str = ""

    @property
    def as_untrusted_block(self) -> str:
        marker = "UNTRUSTED" if self.untrusted else "TRUSTED"
        return (
            f"<<<{marker}_EVIDENCE id={self.id} source={self.source}>>>\n"
            f"{self.detail}\n"
            f"<<<END_{marker}_EVIDENCE id={self.id}>>>"
        )


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=3, max_length=2000)
    next_queries: list[EvidenceRequest] = Field(default_factory=list, max_length=2)


class ActionRequest(BaseModel):
    """A proposed cluster mutation. Only the policy engine may execute one."""

    model_config = ConfigDict(extra="forbid")

    action: ActionName
    namespace: str
    target_kind: Literal["deployment"] = "deployment"
    target_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_name", "namespace")
    @classmethod
    def _slug(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    def hash_payload(self) -> dict[str, Any]:
        """Fields covered by the approval hash. Rationale is deliberately excluded."""
        return {
            "action": self.action,
            "namespace": self.namespace,
            "target_kind": self.target_kind,
            "target_name": self.target_name,
            "parameters": self.parameters,
        }

    @property
    def action_hash(self) -> str:
        return sha256_json(self.hash_payload())


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionName
    namespace: str
    target_kind: Literal["deployment"] = "deployment"
    target_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=10, max_length=3000)
    confidence: float = Field(ge=0.0, le=1.0)
    rollback: str = Field(min_length=5, max_length=1000)
    action_hash: str = ""

    @classmethod
    def from_action(
        cls,
        action: ActionRequest,
        *,
        rationale: str,
        confidence: float,
        rollback: str,
    ) -> Proposal:
        return cls(
            **action.hash_payload(),
            rationale=rationale,
            confidence=confidence,
            rollback=rollback,
            action_hash=action.action_hash,
        )

    def to_action(self) -> ActionRequest:
        return ActionRequest(**self.hash_payload())

    def hash_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "namespace": self.namespace,
            "target_kind": self.target_kind,
            "target_name": self.target_name,
            "parameters": self.parameters,
        }

    def with_hash(self) -> Proposal:
        return self.model_copy(update={"action_hash": self.to_action().action_hash})


class Approval(BaseModel):
    action_hash: str
    approver: str
    decision: Literal["approve", "reject"]
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str


class PolicyDecision(BaseModel):
    allowed: bool
    code: str
    reason: str
    action_hash: str
    checks: list[CheckResult] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)


class ExecutionResult(BaseModel):
    action_hash: str
    status: Literal["executed", "dry_run", "skipped_duplicate", "failed", "denied"]
    command: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    idempotency_key: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class DryRunResult(BaseModel):
    """Result of a server-side dry run (`kubectl --dry-run=server`).

    This is a preflight, not an execution: nothing is persisted by the ledger and
    nothing reaches the cluster. A failed dry run blocks the approval prompt.
    """

    action_hash: str
    ok: bool
    command: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    checked_at: datetime = Field(default_factory=utcnow)

    @property
    def summary(self) -> str:
        return "server dry run passed" if self.ok else f"server dry run failed: {self.error}"


class Verification(BaseModel):
    success: bool
    checks: list[CheckResult] = Field(default_factory=list)
    summary: str
    window_seconds: int = 180


class Postmortem(BaseModel):
    incident_id: str
    markdown: str
    root_cause_category: RootCause
    created_at: datetime = Field(default_factory=utcnow)


class AuditEvent(BaseModel):
    id: str
    incident_id: str
    ts: datetime = Field(default_factory=utcnow)
    node: str
    actor: Literal["agent", "human", "system", "policy"]
    tool: str | None = None
    action: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    output_hash: str = ""
    decision: str = ""
    detail: str = ""

    def as_line(self) -> str:
        return f"{iso(self.ts)} {self.node} actor={self.actor} action={self.action} decision={self.decision}"


class Incident(BaseModel):
    id: str
    fingerprint: str
    alertname: str
    namespace: str
    service: str
    severity: Severity
    status: IncidentStatus = IncidentStatus.RECEIVED
    summary: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    hypothesis: Hypothesis | None = None
    proposal: Proposal | None = None
    approval: Approval | None = None
    policy_decision: PolicyDecision | None = None
    dry_run: DryRunResult | None = None
    execution: ExecutionResult | None = None
    verification: Verification | None = None
    postmortem_md: str = ""
    tool_calls: int = 0
    attempts: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model_backend: str = "fake"
    error: str = ""

    def touch(self, status: IncidentStatus | None = None) -> Incident:
        return self.model_copy(
            update={
                "status": status or self.status,
                "updated_at": utcnow(),
            }
        )
