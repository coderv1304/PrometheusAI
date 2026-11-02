# ADR-0001: Human approval is a graph interrupt bound to an action hash

- Status: accepted
- Date: 2026-11-02

## Context

The agent proposes remediation. A model-driven proposal is not a decision, and
a button that merely resumes a workflow can be replayed or swapped for a
different action. We need a gate that is structural, not a UI convention.

## Decision

The LangGraph workflow interrupts at `approval_gate` before any policy check or
execution. A resume payload must contain `decision`, `approver`, `reason` and
`action_hash`. The hash is recomputed by `propose_fix` from the validated
proposal fields; a model-supplied hash is discarded. A mismatch is converted
into a rejection, the incident escalates, and no write occurs.

The API also refuses to record a decision unless the incident has a passing
`dry_run`.

## Consequences

- A stale or forged approval cannot authorise a different action.
- The checkpoint is the source of truth; a restart cannot skip the gate.
- Approval has an expiry and is single-use per incident.
- A human can still approve an allowed but unwise action; policy cannot fix a
  bad judgement call, only bound it.
