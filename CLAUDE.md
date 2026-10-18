# CLAUDE.md — Borg (بُرگ)

Persian (RTL) EPUB reader, shipped as an offline-capable PWA. Static files only; no build step, no backend yet.
Owner and final decision-maker: Volgash. Do not change files outside the agreed scope.

## Read first
1. `docs/architecture.md` — components, data model, Service Worker, deployment.
2. `docs/decisions/` — ADRs. Do not contradict one without proposing a new ADR.
3. `docs/context/product-spec-v1.1-fa.docx` — product spec (Persian), priorities and roadmap.

## Layout
- `src/` — exactly what is deployed to `public_html/borg/`. Nothing else goes to the server.
- `tests/e2e_test.py` — Playwright end-to-end tests; `tests/fixtures/` sample and corrupt EPUBs.
- `scripts/cpanel-deploy.sh` — run by `.cpanel.yml`; `main` → `/borg`, `staging` → `/borg-test`.
- `scripts/smoke-test.sh` — checks a deployed copy over HTTP.
- `scripts/build-zip.sh` — fallback ZIP with 644/755 permissions.

## Commands
- Tests: `python3 tests/e2e_test.py` (needs `pip install playwright` and Chromium). Must be 100% green before any release.
- Smoke test after deploy: `scripts/smoke-test.sh https://parsun.com/borg-test/ <version>`.

## Rules
- All dependencies are local (`src/assets/vendor`, `src/assets/fonts`). Never add a CDN or external request; tests fail on any non-local request.
- Every release: bump `app-version` meta in `src/index.html` AND `CACHE_NAME` in `src/service-worker.js`; add any new asset to `APP_SHELL`.
- IndexedDB: never delete or rename the database to fix a bug. Schema changes only through `DB_VERSION` + stepwise `onupgradeneeded` migrations, tested on data from the previous version.
- Keep storage key names (`ketabkhane-db`, `ketabkhane-reader-preferences`, `ketab-theme`) — renaming loses users' books (ADR 0003).
- Release flow: branch `staging` → deploy to `/borg-test` → smoke test + manual check → merge to `main` → deploy → smoke test → tag `vX.Y.Z`.
- Tests must check that things are visible, not only present in the DOM.
