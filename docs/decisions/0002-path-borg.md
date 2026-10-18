# 0002 — App moves from /reader/ to /borg/

Status: accepted (2026-10-18). Handling of the old path: pending owner approval.

## Context
The project was renamed to Borg (بُرگ). The owner chose the new path `/borg/`.

## Decision
Production is served from `https://parsun.com/borg/`, staging from `/borg-test/`.

## Consequences
- IndexedDB and localStorage are per origin, not per path, so books saved under `/reader/` stay available
  under `/borg/` as long as the storage names do not change (ADR 0003).
- Users who installed the PWA from `/reader/` keep an icon that points to `/reader/` and an old Service Worker
  that serves the cached v5. Proposed: replace `/reader/` with a tiny page that redirects to `/borg/` plus a
  Service Worker that unregisters itself and clears its caches. Not implemented until approved.
