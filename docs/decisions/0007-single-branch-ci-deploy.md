# 0007 — One production branch, deployed by GitHub Actions

Status: accepted (2026-10-18). Supersedes the staging parts of ADR 0004 and ADR 0005.

## Context
The original flow (ADR 0004) used a `staging` branch deployed to `/borg-test/` and manual "Deploy HEAD Commit"
clicks in cPanel. The owner prefers speed and dropped staging. The repo is public, so GitHub Actions is free.

## Decision
- Only `main` is developed and deployed. Every push to `main` runs `.github/workflows/deploy.yml`:
  API tests + e2e tests (must pass) → cPanel UAPI `VersionControl/update` + `VersionControlDeployment/create`
  (secrets `CPANEL_TOKEN`, `CPANEL_USER`, `CPANEL_HOST`, set by the owner) → `scripts/smoke-test.sh` against
  https://parsun.com/borg/. A WebKit probe job runs in parallel and only reports.
- Deployments never overlap (`concurrency: cpanel-deploy`).
- Problems are fixed forward with a new patch version.

## Consequences
No pre-production check on a real device; the automated suites and the smoke test are the gate, and the owner
checks on devices after release. `/borg-test/` and the `-test` storage suffix (ADR 0005) remain in code but are
unused. Re-introducing staging later only needs the branch and the deploy script case that already exist.
