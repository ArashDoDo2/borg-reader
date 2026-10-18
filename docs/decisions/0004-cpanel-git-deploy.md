# 0004 — Deploy with cPanel Git Version Control

Note: the staging branch and manual deploy clicks were replaced by ADR 0007 (GitHub Actions, `main` only).

Status: accepted (2026-10-18)

## Context
Manual ZIP upload caused an outage: files extracted with mode 600 returned HTTP 403. ZIP files were also left
publicly downloadable.

## Decision
cPanel clones the private GitHub repository (deploy key, read-only) outside `public_html`. `.cpanel.yml` calls
`scripts/cpanel-deploy.sh`, which copies only `src/` to `/borg` (branch `main`) or `/borg-test` (branch
`staging`) and forces 644/755. `scripts/build-zip.sh` remains as a fallback.

## Consequences
No ZIPs, tests or docs ever reach the public folder. A release is a pull + deploy in cPanel followed by
`scripts/smoke-test.sh`. Rollback is a deploy of the previous tag with a higher `CACHE_NAME`.
