# Runbook: Broken configuration

Applies to: `demo/api`, `demo/web`, `demo/worker`.

This runbook is evidence, not authority. Configuration edits are not in the v1
allowlist.

## Symptoms

- The process exits during startup with a configuration error.
- The new revision never becomes ready, while the previous revision was fine.
- Logs name a missing key, an invalid value or an unreadable file.
- The failure coincides with a config or environment change.

## Diagnosis

1. Read the first startup error, not the last crash line.
2. Compare the current revision with the previous revision's environment and
   mounted configuration.
3. Confirm the value is actually invalid; a missing key may be a deployment
   ordering problem rather than a code bug.
4. Check whether the previous ReplicaSet is still available.

## Safe remediation

- `rollout_undo` restores the last revision that started successfully. It is
  the narrow action when the bad configuration shipped with a release.
- `rollout_restart` only repeats the same failure with the same configuration.
- Editing the ConfigMap or Secret is a human change and outside the allowlist.

## Verification

- The rolled-back revision becomes ready.
- The startup error is gone from the new pods.
- The alert clears and the service serves traffic.

## Escalate when

- The bad configuration was applied outside a Deployment revision (for example
  a directly edited ConfigMap), so undo cannot restore it.
- The rollback does not become ready.
