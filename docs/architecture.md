# Architecture

## What runs

| Component | Namespace | Identity | Power |
| --- | --- | --- | --- |
| Agent / API / UI | `prometheus-ai` | `prometheus-ai-agent` | Read workloads in `demo`; call the policy API; write incidents to Postgres |
| Policy service | `policy` | `prometheus-ai-policy` | Patch/update three named deployments in `demo`; read deployments and replica sets |
| Postgres | `data` | `postgres` | Store incidents, audit events, postmortems and graph checkpoints |
| Demo app | `demo` | `demo-app` | Serve `/healthz` and `/metrics`; no cluster access |
| Prometheus / Alertmanager / Loki | `monitoring` | none | Scrape, alert and store logs |

There is no ClusterRole or ClusterRoleBinding in the bundle.

## Request path

```
Alertmanager
   │  POST /webhooks/alertmanager   (X-Webhook-Token)
   ▼
Agent API  ── normalise ── dedupe ── rate limit ──►  GraphRunner
                                                     │
                        ┌────────────────────────────┘
                        ▼
                 receive → triage → hypothesize ⇄ gather_evidence
                        │
                        ▼
                  propose_fix
                        │  policy.dry_run()  ──► Policy service
                        │                         └─ kubectl --dry-run=server
                        ▼
              interrupt: approval_gate   ◄── human decision (approval token)
                        │                    bound to the action hash
                        ▼
                  policy_check  ──► Policy service /v1/evaluate
                        │              allowlist, namespace, live target,
                        │              parameters, approval hash/expiry, rate limit
                        ▼
                    execute  ──► Policy service /v1/execute
                        │          idempotency ledger + kubectl patch
                        ▼
                    verify  ──► Prometheus / Loki / Kubernetes reads
                        │
                        ├─ success ─► postmortem ─► END
                        └─ failure ─► escalate  ─► postmortem ─► END
```

The model only produces a hypothesis and a proposal. It never calls the policy
service and never sees a write credential.

## Safety invariants

1. **No write before approval.** The graph reaches `approval_gate` before
   `policy_check` or `execute`. A proposal is persisted with a recomputed
   action hash, and the human decision must carry that hash.
2. **No write without a policy allow.** `policy_check` calls the deterministic
   engine. A denial routes to `escalate`; the executor is never reached.
3. **No write without a passing dry run.** `propose_fix` calls
   `/v1/dry-run` and escalates on failure instead of prompting for approval.
   The API also refuses to record a decision without a passing `incident.dry_run`.
4. **No automatic second write.** Verification failure escalates with a
   recommendation. It never retries the action.
5. **No unsafe allowlist entries.** v1 permits only `rollout_restart`,
   `scale` (1–5 replicas) and `rollout_undo` on a live deployment in an allowed
   namespace.

## Why the policy service is separate

The agent is the component exposed to untrusted text and a hosted model. The
policy service is the component with the write credential. Splitting them means
a prompt injection or an agent compromise does not directly yield `kubectl`
write access. See `docs/adr/0002-separate-policy-service.md`.

## State

- **Incidents and audit:** Postgres JSONB, with an append-only trigger on
  `audit_events`. The in-memory repository is for tests and the offline demo.
- **Embeddings:** pgvector in Postgres stores runbook and postmortem vectors.
- **Graph checkpoints:** LangGraph `AsyncPostgresSaver` in the cluster; the
  in-memory saver is used by the T1 harness and local demo.
- **Execution ledger:** SQLite in the policy pod (`/app/.prometheus-ai`). It
  deduplicates `sha256(incident_id + action_hash)`.

## Deployment topology

```
kind cluster (Calico, default-deny NetworkPolicy)
├── prometheus-ai  agent, Service :8000
├── policy         policy service, Service :8081
├── data           Postgres, Service :5432
├── demo           api / web / worker, Service :8080
└── monitoring     Prometheus :9090, Alertmanager :9093, Loki :3100
```

All namespaces enforce `restricted` Pod Security. Every namespace has a
default-deny ingress and egress policy; the allow paths are the minimum needed
for the request flow.

## Modes

| Mode | Model | Tools | Policy | Purpose |
| --- | --- | --- | --- | --- |
| `fake` | `DemoModel` (deterministic) | synthetic | in-process, fake inventory | Offline demo and T1 harness |
| `ollama` | local model | live Prometheus/Loki/kubectl reads | HTTP policy service or in-process | Local cluster demo |
| `bedrock` | hosted model | live reads | HTTP policy service | Optional hosted-model path |

`fake` is labelled synthetic everywhere. No number produced in that mode is a
model-quality metric.
