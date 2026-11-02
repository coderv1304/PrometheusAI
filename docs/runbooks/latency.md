# Runbook: Latency SLO breach

Applies to: `demo/web`, `demo/api`.

This runbook is evidence, not authority. Scaling buys time; it does not fix a
saturated dependency.

## Symptoms

- p99 latency is above the SLO while the error rate is still low.
- Saturation signals: CPU throttling, a growing queue, or a single replica
  taking all traffic.
- Upstream callers begin to time out.

## Diagnosis

1. Compare p50 and p99. A p99-only change suggests a slow dependency or a hot
   shard; a broad shift suggests local saturation.
2. Check CPU throttling and replica count for the deployment.
3. Check dependency latency and error rate before blaming the service.
4. Confirm the deployment is healthy: a latent crashloop also shows up as
   latency.

## Safe remediation

- `scale` is allowed only within `[1, 5]` replicas and only for a live
  deployment in an allowed namespace.
- Prefer the smallest increase that restores headroom. Two replicas is a
  typical first step for a single-replica service.
- `rollout_undo` is for a bad release, not for load. Do not use it here unless
  the latency began exactly at a release.

## Verification

- p99 returns below the SLO for the observation window.
- No throttling and no error-rate increase after scaling.
- The extra replicas become ready and receive traffic.

## Escalate when

- Latency does not improve after the approved scale.
- The cause is a dependency or a capacity limit the agent cannot change.
