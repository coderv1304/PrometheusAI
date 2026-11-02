# ADR-0003: Policy is deterministic code, not a prompt

- Status: accepted
- Date: 2026-11-02

## Context

Asking a model whether an action is safe is circular: the model produced the
action and can be steered by the same text that produced it. A policy prompt is
also hard to test exhaustively.

## Decision

The allowlist, namespace scoping, target-kind check, live-resource lookup,
parameter bounds, approval hash/expiry check and write-rate limit are plain
Python in `policy/rules.py` and `policy/engine.py`. The model never supplies a
decision. Every check returns a `CheckResult`, and a denial names the failing
check and a stable code.

v1 allows only:

- `rollout_restart` on a live deployment in an allowed namespace,
- `rollout_undo` on the same,
- `scale` with `replicas` in `[1, 5]`.

## Consequences

- The same input always yields the same decision.
- The rule set is unit-tested and small enough to review.
- Adding an action requires a code change, tests and a review, which is the
  intended friction.
- The engine cannot reason about novel actions; it denies them. That is the
  correct failure mode for this system.
