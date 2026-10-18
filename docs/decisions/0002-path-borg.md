# 0002 — App moves from /reader/ to /borg/

Status: accepted and completed (2026-10-18).

## Context
The project was renamed to Borg (بُرگ). The owner chose the new path `/borg/`.

## Decision
Production is served from `https://parsun.com/borg/`, staging from `/borg-test/`.

## Consequences
- IndexedDB and localStorage are per origin, not per path, so books saved under `/reader/` stay available
  under `/borg/` as long as the storage names do not change (ADR 0003).
- Verified after deploy: books imported under `/reader/` appear under `/borg/`.
- The owner deleted `/reader/` entirely on 2026-10-18 (no redirect); it now returns 404. Anyone who installed
  the PWA from `/reader/` must remove that icon and install again from `/borg/`. A leftover v5 Service Worker
  scoped to `/reader/` is harmless and does not affect `/borg/`.
