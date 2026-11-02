# Threat model

Scope: the PrometheusAI demo deployment described in `deploy/k8s/`. It runs an
investigating agent and a separate policy service in a local kind cluster. It
is **not** a production incident-response system, and the threat model does not
claim to cover a multi-tenant production estate.

## Assets

| Asset | Why it matters |
| --- | --- |
| Kubernetes write credential | The policy service's ServiceAccount can patch three demo deployments. Theft means arbitrary changes to those workloads. |
| Approval token | Authorises a human decision on a proposal. Theft bypasses the human gate. |
| Webhook token | Authorises alert intake. Theft allows incident flooding or spoofed alerts. |
| Policy token | Agent-to-policy shared secret. Theft allows direct policy calls from inside the cluster. |
| Audit log | The record of what was proposed, denied, approved and executed. Tampering hides actions. |
| Model context | Logs, events and runbook text. May contain credentials or attacker-controlled text. |
| Proposals and hashes | The binding between a human decision and the action that will run. |
| Images and SBOMs | Supply-chain integrity of the agent and policy service. |

## Actors and trust boundaries

```
Alertmanager --(webhook token)--> Agent API
Operator     --(approval token)--> Agent API
Agent        --(policy token)----> Policy service
Policy       --(write RBAC)------> Kubernetes API (demo deployments only)
Agent        --(read RBAC)-------> Kubernetes API (demo reads only)
Agent        --(no credentials)--> Model (Ollama local or Bedrock)
Workloads    --(logs/metrics)----> Prometheus/Loki --> Agent
```

Boundaries and assumptions:

1. **The model is untrusted.** Its only output is a schema-validated proposal.
   It cannot call the policy service, hold a token, or execute a command.
2. **Logs, events, annotations and runbooks are untrusted data.** They can
   contain prompt injection. The policy engine never reads them for a decision.
3. **The agent is less trusted than the policy service.** It has no write RBAC
   and, in HTTP policy mode, cannot construct a kubectl write executor.
4. **The policy service is the only writer.** It is a separate namespace,
   ServiceAccount and process with a separate token.
5. **The human is trusted to decide, not to bypass policy.** An approval is
   bound to the action hash and is re-checked by the policy engine.
6. **The cluster control plane is trusted** for the demo. RBAC and admission
   are enforced by Kubernetes, not by the application.

## Threats and mitigations

| ID | Threat | Mitigation | Residual risk |
| --- | --- | --- | --- |
| T-01 | Agent writes without approval | Graph interrupt before `policy_check`; action hash binding; API refuses a decision without a passing dry run | A compromised policy service could ignore its own contract |
| T-02 | Model proposes a destructive action | Deterministic allowlist (`rollout_restart`, `scale`, `rollout_undo`), namespace scoping, replica bounds `[1,5]`, live target lookup | A bug in `policy/rules.py` is a policy bypass; tests cover the rules |
| T-03 | Model is steered by injected logs | Logs are evidence only; injection detection is recorded; policy evaluates the proposal independently; the malicious-scale fixture is denied | Novel injection that stays inside the allowlist still needs human review |
| T-04 | Approval replay or swap | Approval carries the action hash and an expiry; mismatch or expiry denies | A human can still approve a bad but allowed action |
| T-05 | Duplicate execution after a crash | Durable SQLite ledger keyed by `sha256(incident_id + action_hash)`; duplicate executions are skipped | Ledger is local to the policy pod; a lost volume can lose dedupe state |
| T-06 | Hallucinated target | Live `kubectl get` inventory check before approval and again before execution; failed server-side dry run escalates | RBAC must allow the inventory lookup; a stale cache would weaken it (none is used) |
| T-07 | Credential leakage to a hosted model | Redaction before every model call; audit input redaction; no secrets in prompts | Redaction is pattern-based and can miss unusual formats |
| T-08 | Secret committed to git | `.gitignore`, gitleaks in CI, secret template only, production fail-fast on default tokens | A developer can still force-add a secret |
| T-09 | Lateral movement from the agent | Default-deny NetworkPolicy; egress only to policy, Postgres, Prometheus, Loki, DNS and the API; restricted PSS | Calico must be installed; kindnet ignores NetworkPolicy |
| T-10 | Agent pod compromise yields write power | Agent SA has read-only RBAC; policy token is in the agent's secret, so a compromised agent can call the policy API | The policy service still enforces allowlist and approval, so the blast radius is one allowed action per valid approval |
| T-11 | Policy pod compromise | Separate namespace/SA; write Role scoped to three named deployments; no create/delete; no cluster-wide roles | Compromise can patch the demo deployments, which is the intended capability |
| T-12 | Container escape / privilege escalation | Non-root UID 10001, read-only root, no privilege escalation, all capabilities dropped, RuntimeDefault seccomp, no hostPath/hostNetwork | Kernel and runtime vulnerabilities are out of scope |
| T-13 | Audit tampering | Append-only DB trigger rejects UPDATE/DELETE; events are written by the agent and policy paths | A database superuser can disable the trigger |
| T-14 | Alert flooding / resource exhaustion | Webhook body limit, 100-alert cap, dedupe, per-incident write rate limit, tool-call budget | A valid token holder can still create many incidents |
| T-15 | Supply-chain compromise | Pinned dependencies (`uv.lock`), pinned images, Trivy/Syft/Cosign in CI, Dependabot | The demo uses tags; deploy by digest and verify signatures |
| T-16 | Kill switch is off | Kill switch is explicit and persists for the process lifetime; `preflight` denies when engaged | It is in-memory per policy process; restarting clears it |

## Out of scope

- Multi-tenant isolation, cross-cluster remediation and production SLOs.
- Protecting a real company's supply chain, registry or CI identity.
- Model weights, prompt confidentiality and provider-side logging.
- Denial of service against the Kubernetes control plane.
- Physical access, insider threats with cluster-admin, and compromised nodes.

## Accepted demo risks

- The demo uses a self-signed/HTTP-only edge; TLS termination is expected in
  front of the API. HSTS is only emitted outside dev.
- The policy service and agent share a cluster; the credential split is a
  process/RBAC boundary, not a separate trust domain.
- Postgres is a single replica with `sslmode=disable` on the in-cluster hop.
- The SQLite idempotency ledger lives on an `emptyDir`, so it does not survive
  a pod replacement. A production design would use Postgres with a unique key.
