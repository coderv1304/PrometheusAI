# ADR-0004: Run a server-side dry run before asking for approval

- Status: accepted
- Date: 2026-11-02

## Context

A human should not be asked to approve an action that cannot run. A proposal may
name a deployment that does not exist, use an invalid parameter or hit a
denied namespace. Catching that after approval wastes the human's attention and
makes the audit trail noisier.

## Decision

`propose_fix` calls the policy service's `/v1/dry-run`, which first runs the
approval-independent policy checks and then
`kubectl --dry-run=server`. The preview never writes to the execution ledger.
A failed preview escalates immediately; no approval prompt is shown. The API
also refuses a decision when the stored dry run is missing or failed, and
exposes `POST /api/incidents/{id}/preflight` to re-run it on demand.

## Consequences

- Hallucinated targets and invalid parameters are caught before a human sees
  them.
- The dry run needs the same read access as the real action; a failure to reach
  the API server escalates rather than guessing.
- A successful dry run is not a guarantee that the later write succeeds, so the
  real action and verification still run under the full policy check.
