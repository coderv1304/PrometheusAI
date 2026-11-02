# Security

We take a "deny by default, prove it's safe" approach. If something is not
explicitly allowed, it must not happen.

## Scope
- This repository contains a **demo** approval-gated incident-response agent.
- T1 results are from a deterministic harness with scripted fixtures; they are
  not model-quality metrics.
- Real cluster access requires the manifests and configuration described below.

## Security controls
| Control | Where | Purpose |
| --- | --- | --- |
| Approval gate | `agent/graph.py`, `api/app.py` | No write before a human approval bound to the action hash. |
| Deterministic policy engine | `policy/engine.py`, `policy/rules.py` | Allowlist, namespace scoping, live resource checks, parameter bounds, rate limits. |
| Credential split | `policy/server.py`, `deploy/k8s/base/*` | Only the policy service holds a write credential. The agent SA has no write RBAC. |
| Pre-approval dry run | `policy/executor.py`, `agent/nodes.py`, `api/app.py` | `kubectl --dry-run=server` before the approval prompt; failure escalates. |
| Kill switch | `policy/engine.py`, `api/app.py`, `cli.py` | Global halt of all writes. |
| Sealed approval | `models.py`, `agent/nodes.py` | Approval action hash must match the proposed action; mismatch escalates. |
| Redaction | `llm/redact.py`, `agent/nodes.py` | Secrets and PII are redacted before model calls and before audit persistence. |
| Append-only audit | `storage/postgres.py` | DB trigger prevents UPDATE/DELETE of audit events. |
| NetworkPolicy + PSS | `deploy/k8s/base/02-networkpolicies.yaml`, namespaces | Default deny, restricted Pod Security, least privilege. |
| Webhook/auth hardening | `api/app.py` | Token auth, security headers, body size limit, flood limit. |
| Non-root/read-only | Dockerfiles, manifests | Containers run as non-root with read-only roots and dropped capabilities. |

## Reporting a vulnerability
Please do **not** open a public issue with exploit details. Email
[varun@example.invalid] with a description, reproduction steps and affected
versions. We aim to respond within 48 hours and to coordinate disclosure.

## Secrets management
- Never commit `.env`, tokens or kubeconfig files.
- Use `deploy/k8s/base/secrets.example.yaml` as a template; create secrets
  out of band with `kubectl create secret`.
- Rotate credentials on suspicion of compromise.
- In production the app fails fast if default/dev tokens are used.

## Supply chain
- Images should be built in CI, scanned (Trivy), SBOM'd (Syft), signed (Cosign)
  and deployed by digest.
- Use `prometheus-ai versions` to regenerate `docs/VERSIONS.md` after changes.
- Keep `uv.lock` up to date and review dependency updates (Dependabot).
