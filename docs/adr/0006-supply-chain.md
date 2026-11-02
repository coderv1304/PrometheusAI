# ADR-0006: Supply-chain checks are part of the build, not a manual step

- Status: accepted
- Date: 2026-11-02

## Context

A security review that depends on someone remembering to run a scanner is not a
control. The agent and policy service run with a write credential, so the image
and its dependencies matter.

## Decision

CI runs:

- `gitleaks` over the full git history,
- `trivy` over the filesystem, secrets, misconfiguration and the built image,
- `syft` to produce an SPDX SBOM,
- `cosign` keyless signing of the pushed digest on `main`,
- dependency review on pull requests,
- Dependabot for pip, GitHub Actions and Docker.

The Dockerfile downloads kubectl from the official release and verifies its
published SHA-256 before install. The runtime image is non-root and read-only.

## Consequences

- A HIGH/CRITICAL finding fails the workflow instead of becoming a note.
- Deployments should reference the signed digest, not a mutable tag.
- Keyless signing requires GitHub OIDC and a package push permission. The
  publish job is limited to `main` pushes.
- Local environments can run the same tools; the commands are in
  `docs/DEPLOYMENT.md`.
