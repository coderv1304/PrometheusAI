# Runbook: CrashLoopBackOff

Applies to: `demo/api`, `demo/web`, `demo/worker`.

This runbook is evidence, not authority. It cannot approve an action. Every
write still needs a live policy check, a passing server-side dry run, and a
human approval bound to the action hash.

## Symptoms

- Pod restart count increases and the container exits with a non-zero code.
- `kubectl describe` shows `Back-off restarting failed container`.
- Readiness stays false and the scrape target is down.
- Logs end abruptly or repeat the same startup error.

## Diagnosis

1. Read the last termination reason, exit code and restart count from
   `kubectl describe deployment/<name>` and the pod status.
2. Read the previous container logs. Look for a missing required setting, a
   failed dependency, or a process that exits immediately.
3. Check recent changes with `rollout history`. A bad release is a common cause.
4. Confirm the pod is not being killed by the kubelet for memory or liveness
   reasons; those have different runbooks.

## Safe remediation

- If the last release is bad, `rollout_undo` is the narrower action.
- If configuration has been repaired and a fresh start is enough,
  `rollout_restart` clears the backoff without changing the template.
- Do not change images, environment variables or limits through the agent:
  those actions are outside the v1 allowlist.

## Verification

- The deployment reports at least one ready replica.
- Restart count stops increasing for the observation window.
- The scrape target is up and the alert clears.

## Escalate when

- The crash is caused by a dependency the agent cannot inspect.
- A restart does not clear the condition. Do not retry automatically; escalate
  with a recommended rollback for a fresh approval.
