# Deployment

This is the complete, ordered path from a clean checkout to a running kind
cluster with Calico, secrets, the demo stack and the agent. Commands are shown
for **PowerShell 7** on Windows; Git Bash equivalents are noted where quoting
differs.

The demo is intentionally single-cluster and single-replica. It is not a
production deployment guide.

## 0. Prerequisites

| Tool | Why |
| --- | --- |
| `uv` | Python environment and locked installs |
| Python 3.12+ | Local tests (the image uses 3.12) |
| Docker Desktop | Build images, run kind nodes |
| `kind` | Local cluster |
| `kubectl` | Apply and inspect manifests |
| `git`, `gh` | Version control and CI |
| `gitleaks`, `trivy`, `syft`, `cosign` | Local supply-chain checks (optional but recommended) |

Check:

```powershell
uv --version
docker version
kind version
kubectl version --client
gh --version
```

If a tool is missing, install it with `winget` or `choco`, or follow its
upstream instructions. Pin versions in your own environment; the commands below
assume current stable releases.

## 1. Install, lint, test, evaluate

```powershell
cd C:\Users\varun\OneDrive\Desktop\PrometheusAI
uv sync --extra dev --extra graph --extra postgres
uv run ruff check src tests scripts
uv run pytest -q -p no:warnings
uv run prometheus-ai eval --tier t1 --update-baseline --verbose
uv run prometheus-ai eval --tier t1
uv run prometheus-ai versions
```

Expected: 64+ tests pass, the T1 gate passes with 0 unsafe actions, and
`docs/VERSIONS.md` is regenerated for your machine.

## 2. Run the offline demo (no cluster)

```powershell
$env:PROMETHEUS_AI_MODEL_BACKEND = "fake"
uv run prometheus-ai demo --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The UI labels every incident as synthetic.
Approve one from another terminal:

```powershell
uv run prometheus-ai approve INCIDENT_ID --approver varun --reason "narrowest safe action"
```

The default development approval token is `dev-approval-token`. It is rejected
outside `dev`/`test`/`local`.

Stop with `Ctrl+C`.

## 3. Local security scans

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
gitleaks detect --source . --config .gitleaks.toml --redact --verbose
trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL .
docker build -t prometheusai:0.1.0 .
docker build -t prometheusai-demo:0.1.0 ./demo
trivy image --severity HIGH,CRITICAL prometheusai:0.1.0
syft prometheusai:0.1.0 -o spdx-json > artifacts/sbom-agent.spdx.json
```

Address findings before deploying. `ignore-unfixed` is used in CI; do not
silently waive a fixable HIGH or CRITICAL.

## 4. Render and validate the manifests

```powershell
kubectl kustomize deploy/k8s/base > artifacts/rendered.yaml
docker run --rm -v "${PWD}:/work" ghcr.io/yannh/kubeconform:v0.6.7 -strict -summary /work/artifacts/rendered.yaml
```

## 5. Create the kind cluster with Calico

kind's default CNI (kindnet) does not enforce NetworkPolicy. The kind config
disables it; install Calico before applying the app.

```powershell
kind create cluster --config deploy/kind-config.yaml
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml
kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s
kubectl get nodes -o wide
```

Pin the Calico release you have verified. The exact version is less important
than the fact that a policy-enforcing CNI is installed. Confirm:

```powershell
kubectl get pods -n kube-system -l k8s-app=calico-node
```

## 6. Load the images

```powershell
kind load docker-image prometheusai:0.1.0 --name prometheus-ai
kind load docker-image prometheusai-demo:0.1.0 --name prometheus-ai
```

`imagePullPolicy: IfNotPresent` is already set, so the loaded images are used.

## 7. Create namespaces and secrets

Namespaces must exist before namespaced secrets.

```powershell
kubectl apply -f deploy/k8s/base/00-namespaces.yaml

function New-Secret {
  param([int]$Bytes = 32)
  $buffer = New-Object byte[] $Bytes
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($buffer)
  $rng.Dispose()
  ($buffer | ForEach-Object { $_.ToString("x2") }) -join ""
}

$APPROVER_TOKEN = New-Secret
$WEBHOOK_TOKEN  = New-Secret
$POLICY_TOKEN   = New-Secret
$PG_PASSWORD    = New-Secret

kubectl -n prometheus-ai create secret generic prometheus-ai-secrets `
  --from-literal=approver-token=$APPROVER_TOKEN `
  --from-literal=webhook-token=$WEBHOOK_TOKEN `
  --from-literal=database-url="postgresql://prometheus_ai:$PG_PASSWORD@prometheus-ai-postgres.data.svc.cluster.local:5432/prometheus_ai"

kubectl -n data create secret generic prometheus-ai-secrets `
  --from-literal=postgres-password=$PG_PASSWORD

kubectl -n policy create secret generic policy-secrets `
  --from-literal=policy-token=$POLICY_TOKEN `
  --from-literal=approver-token=$APPROVER_TOKEN `
  --from-literal=webhook-token=$WEBHOOK_TOKEN

kubectl -n monitoring create secret generic monitoring-secrets `
  --from-literal=webhook-token=$WEBHOOK_TOKEN
```

Keep `$APPROVER_TOKEN` for the approval commands below. Do not commit these
values or paste them into a shell history you share.

> Git Bash: `export APPROVER_TOKEN=...` and use `kubectl ... --from-literal=approver-token="$APPROVER_TOKEN"`.

## 8. Apply in dependency order

The agent waits for Postgres, the policy service and the runbooks ConfigMap.
Apply in this order so the restarts are short:

```powershell
kubectl apply -f deploy/k8s/base/01-serviceaccounts-rbac.yaml
kubectl apply -f deploy/k8s/base/02-networkpolicies.yaml
kubectl apply -f deploy/k8s/base/03-postgres.yaml
kubectl -n data rollout status statefulset/prometheus-ai-postgres --timeout=180s

kubectl apply -f deploy/k8s/base/04-policy-service.yaml
kubectl -n policy rollout status deployment/prometheus-ai-policy --timeout=180s

kubectl apply -f deploy/k8s/base/07-monitoring.yaml
kubectl -n monitoring rollout status deployment/prometheus --timeout=180s
kubectl -n monitoring rollout status deployment/alertmanager --timeout=180s
kubectl -n monitoring rollout status deployment/loki --timeout=180s

kubectl apply -f deploy/k8s/base/06-demo-app.yaml
kubectl -n demo rollout status deployment/api --timeout=180s
kubectl -n demo rollout status deployment/web --timeout=180s
kubectl -n demo rollout status deployment/worker --timeout=180s

kubectl apply -f deploy/k8s/base/08-runbooks.yaml
kubectl apply -f deploy/k8s/base/05-agent.yaml
kubectl -n prometheus-ai rollout status deployment/prometheus-ai --timeout=300s
```

Then confirm the full bundle is identical to what you applied:

```powershell
kubectl apply -k deploy/k8s/base --dry-run=server
```

## 9. Open the console

```powershell
kubectl -n prometheus-ai port-forward svc/prometheus-ai 8000:8000
```

Open <http://127.0.0.1:8000>. The agent is running with `MODEL_BACKEND=ollama`
from `agent-config`; point `PROMETHEUS_AI_OLLAMA_URL` at a reachable Ollama if
you want live proposals. Without it, the investigation will fail closed and
escalate; no write occurs.

## 10. Inject a demo fault

In Git Bash (or `bash` inside WSL/Git Bash):

```bash
bash ./demo/fault.sh api crashloop
```

PowerShell equivalent:

```powershell
kubectl -n demo set env deployment/api FAULT=crashloop
kubectl -n demo rollout status deployment/api --timeout=20s
```

Wait for the alert to fire, then approve in the UI or with the CLI:

```powershell
uv run prometheus-ai approve INCIDENT_ID --approver varun `
  --reason "restart is the narrowest safe action" --token $APPROVER_TOKEN
```

Reset:

```powershell
kubectl -n demo set env deployment/api FAULT=none
kubectl -n demo rollout restart deployment/api
kubectl -n demo rollout status deployment/api --timeout=120s
```

## 11. Verify the security posture

```powershell
# Pod Security
kubectl get ns -L pod-security.kubernetes.io/enforce

# Default deny plus allow paths
kubectl get networkpolicy -A

# The agent can read, but not write
kubectl auth can-i get pods --as=system:serviceaccount:prometheus-ai:prometheus-ai-agent -n demo
kubectl auth can-i patch deployments --as=system:serviceaccount:prometheus-ai:prometheus-ai-agent -n demo

# The policy service can patch, but not delete
kubectl auth can-i patch deployments --as=system:serviceaccount:policy:prometheus-ai-policy -n demo
kubectl auth can-i delete deployments --as=system:serviceaccount:policy:prometheus-ai-policy -n demo
```

Expected: agent `get` yes / `patch` no; policy `patch` yes / `delete` no.

```powershell
# The demo app cannot reach the policy service
kubectl -n demo exec deploy/api -- python -c "import urllib.request;print(urllib.request.urlopen('http://prometheus-ai-policy.policy:8081/healthz',timeout=3).read())"

# The agent can (healthz is unauthenticated)
kubectl -n prometheus-ai exec deploy/prometheus-ai -- python -c "import urllib.request;print(urllib.request.urlopen('http://prometheus-ai-policy.policy:8081/healthz',timeout=3).read())"
```

The first should time out or be refused; the second should print the health
payload.

```powershell
# Audit is append-only
kubectl -n data exec -it statefulset/prometheus-ai-postgres -- psql -U prometheus_ai -d prometheus_ai -c "UPDATE audit_events SET decision='tampered' WHERE id=(SELECT id FROM audit_events LIMIT 1);"
```

Expected: `ERROR: audit_events is append-only`.

```powershell
# No cluster-wide bindings
kubectl get clusterrolebinding -o wide
```

## 12. Kill switch

```powershell
kubectl -n prometheus-ai port-forward svc/prometheus-ai 8000:8000
uv run prometheus-ai kill-switch on --token $APPROVER_TOKEN
uv run prometheus-ai kill-switch off --token $APPROVER_TOKEN
```

When engaged, every preflight and policy evaluation denies with
`kill_switch_engaged`.

## 13. Commit and run CI

```powershell
gitleaks detect --source . --config .gitleaks.toml --redact
git init
git add .
git commit -m "PrometheusAI: approval-gated Kubernetes incident response"
gh repo create PrometheusAI --private --source . --remote origin --push
```

The push triggers `ci.yml` and `security.yml`. On `main`, the security workflow
builds, scans, pushes and keyless-signs the image.

```powershell
gh run list --limit 5
gh run watch
```

## 14. Verify the signature

After the `publish` job completes:

```powershell
$IMAGE = "ghcr.io/OWNER/prometheus-ai"
$DIGEST = kubectl get deployment -n prometheus-ai prometheus-ai -o jsonpath="{.spec.template.spec.containers[0].image}"
cosign verify "$IMAGE@$DIGEST" `
  --certificate-oidc-issuer https://token.actions.githubusercontent.com `
  --certificate-identity-regexp "https://github.com/OWNER/PrometheusAI/.github/workflows/security.yml@refs/heads/main"
```

Use the digest printed by the workflow, not a tag.

## 15. Cleanup

```powershell
kind delete cluster --name prometheus-ai
```

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Pods stay `Pending` | CNI not installed | Install Calico, wait for `calico-node` |
| NetworkPolicy appears ignored | Calico not running | `kubectl get pods -n kube-system -l k8s-app=calico-node` |
| Agent `CreateContainerConfigError` | Missing secret key | `kubectl -n prometheus-ai describe deployment prometheus-ai` |
| Agent cannot reach policy | Wrong policy token or NetworkPolicy | Check `policy-secrets` and the policy allow rule |
| Policy logs `resource_not_found` | Live inventory cannot see the deployment | Check the policy Role and the demo deployment name |
| Decision returns 409 "dry run" | No passing preflight | Call `POST /api/incidents/{id}/preflight` first |
| Approvals fail with 401 | Wrong approval token | Use `$APPROVER_TOKEN` from step 7 |
