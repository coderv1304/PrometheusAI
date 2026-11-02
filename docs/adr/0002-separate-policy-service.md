# ADR-0002: A separate policy service holds the only write credential

- Status: accepted
- Date: 2026-11-02

## Context

The investigating agent reads untrusted logs, events, annotations and runbooks
and sends text to a model. It is the most exposed component. If the same process
also held `kubectl` write access, a prompt injection or a model bug would sit
next to a write credential.

## Decision

Run a standalone policy service in its own namespace and ServiceAccount. The
agent talks to it over HTTP with a shared token. The agent's ServiceAccount has
read-only RBAC and, in `policy_mode=http`, the agent process does not construct
a kubectl write executor at all. The policy service owns the execution ledger
and the only write Role.

## Consequences

- A compromised agent can call the policy API, but it cannot mint an approval
  and cannot bypass the allowlist.
- Deployment is two processes, not one. Local development can use
  `policy_mode=inprocess` with the same safety logic.
- The policy token is a shared secret, not a workload identity. Replacing it
  with mTLS/SPIFFE is a future improvement.
