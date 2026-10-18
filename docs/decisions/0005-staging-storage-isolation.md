# 0005 — Staging uses its own IndexedDB database

Status: superseded by ADR 0007 (staging dropped 2026-10-18); the `-test` suffix code remains but is unused

## Context
`/borg/` and `/borg-test/` share the origin `parsun.com`, therefore the same IndexedDB. A staging build that
bumps `DB_VERSION` would upgrade the tester's production database, and the older production code would then
fail to open it (VersionError).

## Decision
When the page path contains `/borg-test/`, `app.js` uses `ketabkhane-db-test` and storage keys with a
`-test` suffix.

## Consequences
Staging never touches production data. Testing a migration on staging requires importing books there first
(or a future export/import feature).
