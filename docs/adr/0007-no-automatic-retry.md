# ADR-0007: Verification failure escalates; it never retries automatically

- Status: accepted
- Date: 2026-11-02

## Context

An approved action may not fix the incident. The tempting behaviour is to try
the next action immediately. That turns one human decision into an unbounded
sequence of writes, and it is exactly the failure mode the approval gate exists
to prevent.

## Decision

`verify` records named checks over a bounded window. If verification fails, the
incident moves to `escalated` with a recommended next step (usually a rollout
undo). It does not execute a second action. A new action requires a new
proposal, a new dry run, a new policy check and a new approval.

## Consequences

- An incident can stay unresolved while a human is unavailable. That is
  acceptable: availability does not outrank the write gate.
- The postmortem records the failed verification and the recommendation.
- The T1 fixtures `crashloop-verify-fail` and `latency-verify-fail` assert this
  path and fail if a second write is ever executed.
