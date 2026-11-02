#!/usr/bin/env bash
# Inject a demo fault. This is the *only* supported way to break the demo app.
#
#   ./demo/fault.sh api crashloop
#   ./demo/fault.sh worker oom
#   ./demo/fault.sh web latency
#   ./demo/fault.sh api injection
#   ./demo/fault.sh api broken-config
#   ./demo/fault.sh api bad-image
#   ./demo/fault.sh api none          # restore
#
# The policy service can only patch these same deployments, and only restart,
# undo or rescale them. Fault injection is a separate, human-run action; the
# agent has no route that changes FAULT or image references.
set -euo pipefail

namespace="${NAMESPACE:-demo}"
target="${1:-}"
fault="${2:-}"

usage() {
  cat <<'EOF'
Usage: ./demo/fault.sh <api|web|worker> <fault>

Faults: crashloop | oom | latency | injection | broken-config | bad-image | none
Set NAMESPACE to target a namespace other than demo.
EOF
  exit 2
}

case "$target" in
  api|web|worker) ;;
  *) usage ;;
esac

case "$fault" in
  crashloop|oom|latency|injection|broken-config|none) ;;
  bad-image)
    kubectl -n "$namespace" set image "deployment/$target" \
      "app=prometheusai-demo:does-not-exist"
    kubectl -n "$namespace" rollout status "deployment/$target" --timeout=60s || true
    exit 0
    ;;
  *) usage ;;
esac

wait_for() {
  # During an intentional outage the rollout does not become ready; do not fail
  # the script because of that.
  kubectl -n "$namespace" rollout status "deployment/$target" --timeout=20s || true
}

case "$fault" in
  crashloop)     env_value="crashloop" ;;
  oom)           env_value="oom" ;;
  latency)       env_value="latency" ;;
  injection)     env_value="injection" ;;
  broken-config) env_value="broken_config" ;;
  none)          env_value="none" ;;
esac

kubectl -n "$namespace" set env "deployment/$target" "FAULT=$env_value"
wait_for

if [[ "$fault" == "none" ]]; then
  # A crashlooping pod cannot be patched back to health in place; restart it
  # with the healthy environment.
  kubectl -n "$namespace" rollout restart "deployment/$target"
  kubectl -n "$namespace" rollout status "deployment/$target" --timeout=120s
fi

echo "fault '$fault' applied to $namespace/$target"
