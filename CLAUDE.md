# CLAUDE.md — Borg (بُرگ)

Persian-first EPUB reader: an offline-capable PWA (static files, no build step) plus an optional PHP 8 API
for invite-only accounts, cloud sync and a moderated public catalog. Live at https://parsun.com/borg/.
Current version: see `<meta name="app-version">` in `src/index.html` (8.4.0 when this file was last updated).

Owner and final decision-maker: Arash Naderpour. Work only inside the agreed scope; no refactors, extra files
or deletions beyond what was asked. When something is ambiguous, ask instead of guessing.

## Read first (in this order)
1. `docs/context/status.md` — what is done, what is open, decisions the owner already made. **Start here.**
2. `docs/architecture.md` — components, data model, reader engine, API, sync, deployment, tests.
3. `docs/context/reader-engine-notes.md` — epub.js / Safari / Persian-EPUB pitfalls and the fixes in place.
   Read before touching anything in the reader.
4. `docs/decisions/` — ADRs. Do not contradict one without proposing a new ADR.
5. The product spec (Persian) is private. If a copy is in `docs/private/` (git-ignored), read it; never commit it.

## Layout
- `src/` — exactly what is deployed to `public_html/borg/`.
  - `index.html`, `assets/js/app.js` (all client logic, one IIFE), `assets/css/`, `assets/fonts/`, `assets/vendor/`
    (epub.js 0.3.93, JSZip 3.10.1), `service-worker.js`, `offline.html`, `manifest.webmanifest`.
  - `api/` — PHP API: `index.php` (router), `lib/core.php` (config, DB, migrations, sessions, CSRF, rate limits),
    `lib/books.php` (upload, library, progress, annotations, shelves, stats, catalog), `lib/admin.php`.
- `tests/api_test.py` — API tests (PHP built-in server + SQLite). `tests/e2e_test.py` — Playwright/Chromium
  end-to-end tests. `tests/webkit_e2e.py` — WebKit (Safari engine) checks that gate the deploy in CI (iPad/iPhone/desktop;
  `--browser chromium` to check its logic locally). `tests/webkit_probe.py` — WebKit diagnostics, run in CI only when those fail. `tests/fixtures/` — sample EPUBs.
- `scripts/cpanel-deploy.sh` (run by `.cpanel.yml`), `scripts/smoke-test.sh`, `scripts/build-zip.sh` (fallback).
- `.github/workflows/deploy.yml` — CI: tests → deploy through cPanel UAPI → smoke test.

## Commands
- `python3 tests/api_test.py` and `python3 tests/e2e_test.py` (needs `pip install playwright`, Chromium, `php`).
  Both must be 100% green before a release. The e2e suite takes several minutes; run long jobs in the background.
- Quick syntax checks: `node --check src/assets/js/app.js`, `php -l src/api/lib/*.php`.
- Smoke test a deployment: `scripts/smoke-test.sh https://parsun.com/borg/ <version>`.

## Release flow (single branch, see ADR 0007)
1. Commit on `main`. Bump the version in **three** places: `app-version` meta and the footer «نسخهٔ …» in
   `src/index.html`, and `CACHE_NAME` in `src/service-worker.js`. Add new assets to `APP_SHELL`.
2. `git push origin main`. GitHub Actions runs API + e2e tests (Chromium) and the WebKit e2e checks (both gate), then deploys via
   cPanel UAPI and runs the smoke test. A failing test blocks the deploy.
3. Check the run: `gh run list -R ArashDoDo2/borg-reader --limit 1`; failure details are emitted as annotations:
   `gh api repos/ArashDoDo2/borg-reader/check-runs/<job-id>/annotations` (log downloads are blocked here).
- Commits are authored as `Arash Naderpour <arash.naderpour@gmail.com>`. Pushing tags is blocked from the agent
  environment. There is no staging site any more.

## Rules
- All dependencies are local. No CDN or external request; the e2e suite fails on any non-local request.
- Strict CSP meta in `index.html`: no inline handlers, no `eval`. In tests use the `poll()` helper, not
  Playwright's `wait_for_function` (it needs eval).
- Never rename storage: IndexedDB `ketabkhane-db`, localStorage `ketabkhane-reader-preferences`, `ketab-theme`
  (ADR 0003). Schema changes only via `DB_VERSION` + stepwise `onupgradeneeded` migrations.
- Server DB changes only as a new numbered, portable migration in `lib/core.php` (`{AI}`/`{TXT}` placeholders,
  `NOT NULL DEFAULT` for added columns). Keep API changes backward compatible.
- Secrets live only in `/home/parsunco/borg-config/config.php` on the server and in GitHub Actions secrets.
  Never ask for, paste or commit tokens or passwords; the owner sets them.
- Deployed files must be 644 / dirs 755 (the deploy script enforces it).
- Tests assert visibility (geometry), not just DOM presence. Every bug fix gets a regression test that fails
  without the fix.
- Reader direction is per book (declared → detected from text → language tag → RTL). Never hard-code RTL in the
  reader again; use `bookDir` / `bookIsRtl()`.
