# Runbook: Bad image release

Applies to: `demo/api`, `demo/web`, `demo/worker`.

This runbook is evidence, not authority. The agent cannot change an image; the
v1 allowlist only contains restart, undo and scale.

## Symptoms

- Pods fail with `ImagePullBackOff`, `ErrImagePull` or `InvalidImageName`.
- The failure starts immediately after a rollout.
- The previous ReplicaSet is still available.
- Readiness is zero for the new revision.

## Diagnosis

1. Read the event message: a missing tag, a private registry credential or a
   typo in the image reference are the usual causes.
2. Compare the current image with the last known-good revision in
   `rollout history`.
3. Confirm the previous revision is healthy and has ready replicas.
4. Check that the image reference is expected; do not trust a tag found only
   in an alert annotation.

## Safe remediation

- `rollout_undo` restores the previous ReplicaSet. It is the narrow action for
  a bad release.
- `rollout_restart` does not help and may make the outage longer.
- Image and registry fixes are human actions outside the allowlist.

## Verification

- The deployment rolls back and the previous revision becomes ready.
- The scrape target returns and the alert clears.
- No new image-pull events appear.

## Escalate when

- The previous revision is also unhealthy.
- The rollback does not complete within the rollout timeout. Do not retry the
  undo automatically.
