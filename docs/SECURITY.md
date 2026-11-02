# Security controls and verification

This document maps each control to the code or manifest that implements it and
to a command that demonstrates it. Run the commands from the repository root.

## 1. Approval gate and sealed approval

Implementation: `src/prometheus_ai/agent/graph.py`,
`src/prometheus_ai/agent/nodes.py`, `src/prometheus_ai/api/app.py`.

Invariants:

- The graph interrupts before `policy_check`.
- `propose_fix` recomputes `action_hash` from validated fields; a model-supplied
  hash is discarded.
- A resume payload must carry the matching hash. A mismatch is turned into a
  rejection and escalated.
- The API refuses a decision when `incident.dry_run` is missing or failed.

Verify:

```bash
uv run pytest tests/test_graph_security.py -q
```

## 2. Deterministic policy engine

Implementation: `src/prometheus_ai/policy/rules.py`,
`src/prometheus_ai/policy/engine.py`.

Checks, in order: kill switch, allowlisted action, target kind, allowed
namespace, safe target name, live existence, parameter bounds, approval
presence/hash/expiry, per-incident write rate.

Verify:

```bash
uv run pytest tests/test_policy.py -q
```

## 3. Credential split and least privilege

Implementation: `deploy/k8s/base/01-serviceaccounts-rbac.yaml`,
`src/prometheus_ai/services/bootstrap.py`.

- Agent SA: `get/list/watch` on pods, logs, events, services, endpoints and
  workload kinds in `demo`. No write verbs.
- Policy SA: `get/patch/update` on `deployments` and `deployments/scale` with
  `resourceNames: [api, web, worker]`. No create, delete or deletecollection.
- No ClusterRole or ClusterRoleBinding.

Verify:

```bash
uv run pytest tests/test_manifests.py -q
kubectl auth can-i --list --as=system:serviceaccount:prometheus-ai:prometheus-ai-agent -n demo
kubectl auth can-i patch deployments --as=system:serviceaccount:policy:prometheus-ai-policy -n demo
kubectl auth can-i delete deployments --as=system:serviceaccount:policy:prometheus-ai-policy -n demo
```

Expected: the last command returns `no`.

## 4. Pre-approval server-side dry run

Implementation: `policy/executor.py` (`preview`), `agent/nodes.py`
(`propose_fix`), `policy/server.py` (`/v1/dry-run`), `api/app.py`
(`/api/incidents/{id}/preflight`).

The dry run uses `kubectl --dry-run=server`, never writes to the ledger, and
escalates on failure. A hallucinated target never reaches a human approval
prompt.

Verify:

```bash
uv run pytest tests/test_policy.py -q -k preview
uv run pytest tests/test_graph_security.py -q -k dry_run
```

## 5. Kill switch

Implementation: `policy/engine.py`, `policy/server.py`, `api/app.py`, `cli.py`.

Verify (with the service running):

```bash
prometheus-ai kill-switch on  --url http://127.0.0.1:8000
curl -s http://127.0.0.1:8000/api/admin/kill-switch
prometheus-ai kill-switch off --url http://127.0.0.1:8000
```

## 6. Redaction before model calls and audit

Implementation: `src/prometheus_ai/llm/redact.py`,
`src/prometheus_ai/agent/nodes.py`.

Patterns cover private keys, JWTs, AWS/GitHub/Slack tokens, bearer tokens,
credential assignments, connection strings, emails and long hex blobs. Audit
inputs and details are redacted unless `PROMETHEUS_AI_AUDIT_INPUT_REDACTION=false`.

Verify:

```bash
uv run pytest tests/test_redaction.py -q
```

## 7. NetworkPolicy and Pod Security

Implementation: `deploy/k8s/base/00-namespaces.yaml`,
`deploy/k8s/base/02-networkpolicies.yaml`.

- All five namespaces enforce `restricted` PSS.
- Every namespace has `default-deny` for ingress and egress.
- Allow paths: agent → policy/Postgres/Prometheus/Loki/DNS/API; policy →
  API/DNS; monitoring → demo and agent webhook; demo → Loki/DNS.

Verify after `kind` + Calico:

```bash
kubectl get ns -L pod-security.kubernetes.io/enforce
kubectl get networkpolicy -A
kubectl run probe --rm -it --image=busybox:1.36 --restart=Never \
  -n prometheus-ai -- wget -qO- --timeout=3 http://prometheus-ai-policy.policy:8081/healthz
```

The probe should fail unless it is labelled as the agent, because the policy
namespace only admits the agent pod.

## 8. Container hardening

Implementation: `Dockerfile`, `demo/Dockerfile`, manifests.

All workloads run as UID 10001 or a non-root image UID, with
`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, all
capabilities dropped, and `RuntimeDefault` seccomp. Writable paths are explicit
`emptyDir` mounts. No hostPath, hostNetwork or privileged containers.

Verify:

```bash
uv run pytest tests/test_manifests.py -q
docker run --rm --read-only --user 10001:10001 --tmpfs /tmp \
  --tmpfs /app/.prometheus-ai prometheusai:0.1.0 version
```

## 9. HTTP hardening

Implementation: `src/prometheus_ai/api/app.py`.

- Webhook token (`X-Webhook-Token` or bearer) outside dev.
- Approval token on every decision, preflight and kill-switch call.
- Body-size limit and a 100-alert cap.
- CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy`, `Cross-Origin-Opener-Policy`, `Cache-Control: no-store`,
  and HSTS outside dev.
- API docs disabled with `PROMETHEUS_AI_ENABLE_DOCS=false`.

Verify:

```bash
uv run pytest tests/test_api_security.py -q
curl -sI http://127.0.0.1:8000/healthz
```

## 10. Audit integrity

Implementation: `src/prometheus_ai/storage/postgres.py`.

`audit_events` has a `BEFORE UPDATE OR DELETE` trigger that raises. The
repository only inserts. Incident deletion while audit rows exist is blocked by
the foreign key and the trigger.

Verify after deploying Postgres:

```bash
kubectl -n data exec -it statefulset/prometheus-ai-postgres -- \
  psql -U prometheus_ai -d prometheus_ai -c \
  "UPDATE audit_events SET decision='tampered' WHERE id=(SELECT id FROM audit_events LIMIT 1);"
```

Expected: `ERROR: audit_events is append-only`.

## 11. Supply chain

Implementation: `.github/workflows/security.yml`, `Dockerfile`,
`docs/VERSIONS.md`.

- gitleaks (history), Trivy (vuln/secret/misconfig), Syft SBOM, Cosign keyless
  signature on `main`, dependency review on PRs, Dependabot.
- kubectl is downloaded from `dl.k8s.io` and its checksum is verified at build.
- Deploy by digest, not tag, for a real environment.

Verify locally:

```bash
uv run pytest tests/test_manifests.py -q
uv run prometheus-ai versions
trivy fs --scanners vuln,secret,misconfig .
syft . -o spdx-json > sbom.spdx.json
cosign verify ghcr.io/OWNER/prometheus-ai@sha256:...
```

## 12. Configuration fail-fast

Implementation: `src/prometheus_ai/config.py`.

Outside `dev`/`test`/`local`, startup fails if the approver token is the
development default, the webhook token is empty, or HTTP policy mode uses the
development policy token.

Verify:

```bash
uv run pytest tests/test_config.py -q
PROMETHEUS_AI_ENV=prod uv run prometheus-ai serve
```

Expected: a validation error naming the insecure settings.

## Known limitations

- Tokens are static shared secrets, not OIDC/mTLS identities.
- The policy token is available to the agent process, so an agent compromise
  can call the policy API. The policy engine still requires a valid approval.
- Rate limiting is per process and per incident, not global.
- Redaction is regex-based.
- The SQLite execution ledger is not replicated.
- This is a single-cluster demo; see `docs/THREAT_MODEL.md` for scope.
