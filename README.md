# PrometheusAI

An approval-gated incident-response agent for Kubernetes. It investigates an
alert, proposes a narrow remediation, proves the action can run, asks a human,
then lets a separate policy service execute it exactly once.

Built as a working vertical slice: the LangGraph workflow, policy engine,
approval console, Postgres audit trail, Kubernetes manifests and CI all run. The
demo cluster injects faults into a three-service app; it is **not** production
incident response.

> **Scope, stated plainly**
>
> - This is a demo on a local kind cluster with injected faults. It has not
>   handled a real production incident.
> - T1 results come from a deterministic harness with a scripted model and
>   synthetic tools. They validate routing, approval binding and policy denial.
>   They are **not** model-quality metrics.
> - T2 (live model) accuracy is **not measured**. No number is published for it.
> - The agent can only restart, undo or rescale a deployment, within an
>   allowlist and replica bounds. It cannot change images, config, limits or
>   namespaces.

## What it does

```
Alertmanager → agent → investigate → propose → server-side dry run
             → human approval (action hash) → policy decision
             → execute once → verify → postmortem
```

- Reads Prometheus, Loki and read-only Kubernetes state.
- Produces a hypothesis and a proposal through a swappable model backend
  (`fake`, Ollama or Bedrock).
- Runs a `kubectl --dry-run=server` preflight **before** asking for approval.
- Requires a human decision bound to the proposal's SHA-256 action hash.
- Sends the approved action to a separate policy service that owns the only
  write credential.
- Verifies the result. A failed verification escalates; it never retries.
- Writes an append-only audit trail and a postmortem.
- Fails closed on unknown actions, bad payloads, missing approvals, an
  unreachable cluster, a failed dry run or an engaged kill switch.

## Quickstart (no cluster)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <your-repo> PrometheusAI
cd PrometheusAI
uv sync --extra dev --extra graph --extra postgres
uv run pytest -q -p no:warnings
uv run prometheus-ai demo --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The UI marks every incident as synthetic.
While the demo is still running, approve one from another terminal. Replace
`INCIDENT_ID` with an ID printed by this demo run; IDs change every time the
demo starts:

```bash
uv run prometheus-ai approve INCIDENT_ID \
  --approver "$USER" \
  --reason "restart is the narrowest safe action"
```

The development approval token is `dev-approval-token`. It is rejected outside
`dev`/`test`/`local`.

### Evaluation

```bash
uv run prometheus-ai eval --tier t1 --verbose
```

The committed baseline produces:

| Metric | Result |
| --- | --- |
| Scenarios | 13 |
| Root-cause accuracy | 100% |
| Action correctness | 100% |
| Tool-call correctness | 100% |
| Status accuracy | 100% |
| Unsafe-action rate | 0% (gate: 0) |
| p95 latency | ~30 ms locally |

These are harness numbers. Read `docs/EVALUATION.md` before quoting them.

## Safety model

1. **No write before approval.** The graph interrupts before the policy check.
   A resume payload must match the proposal's action hash.
2. **No write without the policy engine.** The engine checks the allowlist,
   namespace, target kind, live resource, parameter bounds, approval hash and
   expiry, and a per-incident write limit.
3. **No approval without a passing dry run.** A failed `--dry-run=server`
   escalates instead of prompting a human.
4. **No second write after a failed fix.** Verification failure escalates with
   a recommendation.
5. **No write credential in the agent.** The policy service is a separate
   process, namespace and ServiceAccount. The agent has read-only RBAC.

The full control map, threat model and residual risks are in
`docs/SECURITY.md` and `docs/THREAT_MODEL.md`.

## Architecture

```
┌──────────────────── prometheus-ai ────────────────────┐
│ FastAPI API / UI          LangGraph runner            │
│   webhook, decisions       receive → triage            │
│   audit, evals             → hypothesize ⇄ evidence    │
│                            → propose → dry run         │
│                            → interrupt → policy        │
│                            → execute → verify → postmortem │
└───────────────┬───────────────────────┬───────────────┘
                │ policy token          │ read-only kubectl,
                ▼                       ▼ Prometheus, Loki
┌──────────────── policy ───────────────┐   ┌──────── data ────────┐
│ deterministic engine + kubectl        │   │ Postgres: incidents, │
│ only write RBAC, idempotency ledger   │   │ audit, checkpoints   │
└───────────────────┬───────────────────┘   └──────────────────────┘
                    │ patch/update 3 named deployments
                    ▼
                 demo: api, web, worker
```

See `docs/architecture.md` for the detailed request path.

## Security posture

| Control | Implementation |
| --- | --- |
| Credential split | Agent SA read-only; policy SA writes three named deployments |
| Default deny | NetworkPolicy in all five namespaces; Calico required |
| Restricted pods | PSS `restricted`, non-root, read-only root, all caps dropped |
| Approval binding | SHA-256 action hash, expiry, single decision |
| Dry run | `kubectl --dry-run=server` before approval |
| Audit | Append-only Postgres trigger |
| Secrets | Out-of-band creation; production fail-fast on defaults |
| Supply chain | gitleaks, Trivy, Syft SBOM, Cosign, Dependabot |
| HTTP | Token auth, CSP and security headers, body/alert limits |

## Deploy on kind

The full ordered sequence is in **`docs/DEPLOYMENT.md`**. Short version:

```bash
kind create cluster --config deploy/kind-config.yaml
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml
# create namespaces and secrets (see the deployment guide)
kubectl apply -k deploy/k8s/base
kubectl -n prometheus-ai port-forward svc/prometheus-ai 8000:8000
```

The guide also covers image loading, secret generation, RBAC checks,
NetworkPolicy probes, audit-trigger verification, CI and Cosign verification.

## Configuration

All settings are environment variables with the `PROMETHEUS_AI_` prefix
(`.env` is supported locally). Important ones:

| Variable | Default | Notes |
| --- | --- | --- |
| `PROMETHEUS_AI_ENV` | `dev` | Outside dev/test/local, insecure defaults fail startup |
| `PROMETHEUS_AI_MODEL_BACKEND` | `fake` | `fake`, `ollama`, `bedrock` |
| `PROMETHEUS_AI_POLICY_MODE` | `inprocess` | `http` in the cluster |
| `PROMETHEUS_AI_DRY_RUN` | `true` | When false, the executor performs the real action |
| `PROMETHEUS_AI_APPROVER_TOKEN` | dev value | Human decision API |
| `PROMETHEUS_AI_WEBHOOK_TOKEN` | empty | Required outside dev |
| `PROMETHEUS_AI_POLICY_TOKEN` | dev value | Agent → policy service |
| `PROMETHEUS_AI_ALLOWED_NAMESPACES` | `["demo"]` | Policy allowlist |
| `PROMETHEUS_AI_MAX_WRITES_PER_HOUR` | `3` | Per incident |

See `.env.example` for the full set.

## Repository layout

```
src/prometheus_ai/
  agent/        graph, nodes, verification, postmortem
  policy/       deterministic engine, rules, executor, HTTP service
  api/          FastAPI app, templates, static assets
  tools/        Prometheus, Loki, read-only kubectl, runbooks
  llm/          fake/Ollama/Bedrock clients, redaction
  storage/      in-memory and Postgres repositories
  evals/        T1 runner, metrics, gate
  services/     composition root, alert intake
deploy/
  k8s/base/     namespaces, RBAC, NetworkPolicy, workloads
  kind-config.yaml
demo/           fault-injecting three-service app
docs/           threat model, security, architecture, ADRs, runbooks
evals/          T1 fixtures and the committed baseline
tests/          unit, API, graph-security and manifest tests
```

## Documentation

- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — ordered commands from checkout to
  a verified cluster.
- [`docs/architecture.md`](docs/architecture.md) — components and request path.
- [`docs/SECURITY.md`](docs/SECURITY.md) — controls and verification commands.
- [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — assets, threats, residual risk.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — what T1 does and does not measure.
- [`docs/adr/`](docs/adr/) — the decisions behind the gate, split service,
  policy-as-code, dry run, least privilege, supply chain and no-retry rule.
- [`docs/runbooks/`](docs/runbooks/) — the runbooks the agent retrieves.
- [`docs/VERSIONS.md`](docs/VERSIONS.md) — generated dependency capture.

## Limitations

- Single cluster, single Postgres replica, one agent replica.
- Tokens are static shared secrets, not workload identity.
- Redaction is regex-based and can miss unusual formats.
- The execution ledger is SQLite on an `emptyDir`; it does not survive pod
  replacement.
- The T1 scenario set is hand-authored and small.
- No live model baseline; T2 accuracy is unmeasured.

## License

MIT. See `pyproject.toml`.
