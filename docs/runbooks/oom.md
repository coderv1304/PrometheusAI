# Runbook: OOMKilled

Applies to: `demo/api`, `demo/web`, `demo/worker`.

This runbook is evidence, not authority. A restart is containment, not a fix
for a leak.

## Symptoms

- Container terminated with reason `OOMKilled`.
- Working set sits at or near the container memory limit.
- Restarts spike, then the pod may run for a while before dying again.
- Latency rises before the kill as the process thrashes.

## Diagnosis

1. Confirm the termination reason and the limit in `kubectl describe`.
2. Compare the working set with the limit over the last hour.
3. Check whether traffic, batch size or a cache grew before the kill.
4. Look for a leak signal: memory rises monotonically across restarts.

## Safe remediation

- `rollout_restart` is containment: it clears the current heap. It does not
  change the limit and does not fix a leak.
- Memory-limit and resource changes are outside the v1 allowlist. Record the
  recommended limit in the postmortem and hand it to a human change process.

## Verification

- The pod stays below the limit for the observation window.
- No new `OOMKilled` termination appears.
- Restart count is stable and the alert clears.

## Escalate when

- The working set returns to the limit within the window.
- The service is still unavailable after one approved restart. Do not loop
  restarts; escalate with the leak evidence.
