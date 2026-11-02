# ADR-0005: Read-only investigation, default-deny networking, restricted pods

- Status: accepted
- Date: 2026-11-02

## Context

The demo needed a credible least-privilege posture, not just a policy module.
Kubernetes defaults are permissive: pods can reach each other, containers often
run as root, and a broad ServiceAccount is easy to add by accident.

## Decision

- The agent reads only the resource kinds it needs in `demo`; it has no write
  verbs and no ClusterRole.
- The policy service writes only `deployments` and `deployments/scale` with
  `resourceNames: [api, web, worker]`; no create, delete or deletecollection.
- Every namespace is labelled `pod-security.kubernetes.io/enforce=restricted`.
- Every namespace has a default-deny NetworkPolicy for ingress and egress, with
  explicit allow paths for the request flow.
- All containers are non-root, read-only, drop `ALL` capabilities, disable
  privilege escalation and use `RuntimeDefault` seccomp.
- kind runs with its default CNI disabled; Calico is installed because kindnet
  does not enforce NetworkPolicy.

## Consequences

- The bundle depends on a policy-enforcing CNI. Without Calico the network
  controls are inert, so the deployment guide installs it first.
- Adding a dependency means adding a NetworkPolicy and a volume mount, which is
  visible in review.
- The demo cannot be run on a CNI that ignores policy without weakening the
  claim; the documentation says so.
