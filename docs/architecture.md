# Architecture — Borg (بُرگ)

Status: describes the code in `src/` as of v7.1.0. Product goals, roadmap and ideas live in the
private product spec (not in this repo); this file is the technical source of truth.

## 1. Overview
A static PWA. The browser does everything: parse EPUB, render, store books and progress.
Since v7 an optional PHP API (`src/api/`) adds accounts, cloud library, progress sync and a public catalog; without an account the app stays fully local.

```
index.html ──> assets/js/app.js ──> epub.js (+ JSZip) ──> book rendered in a sandboxed iframe
                     │                                        └─ reader-fonts.css injected (Vazirmatn)
                     ├─> IndexedDB  (books, progress)
                     ├─> localStorage (reader prefs, theme)
                     └─> service-worker.js (offline app shell)
```

## 2. Components
| File | Role |
|---|---|
| `src/index.html` | Library + reader UI; loads vendor scripts then `app.js`. Holds `<meta name="app-version">`. |
| `src/assets/js/app.js` | All app logic (single IIFE): import, metadata, library, reader, progress, bookmark, settings, SW registration. |
| `src/assets/vendor/jszip.min.js` | JSZip 3.10.1. Must load **before** epub.js (epub.js needs it to open an EPUB from an ArrayBuffer). |
| `src/assets/vendor/epub.min.js` | epub.js 0.3.93. Paginated rendition, `spread: auto`. Scripts inside EPUBs are not enabled. |
| `src/assets/fonts/Vazirmatn-Variable.woff2` | UI and book font, weights 100–900. |
| `src/assets/css/reader-fonts.css` | `@font-face` injected into every book iframe via `rendition.hooks.content` (absolute URL). |
| `src/service-worker.js` | Offline shell, cache versioned by `CACHE_NAME`. |
| `src/offline.html` | Last-resort page when nothing is cached. |

## 3. Data model (browser)
IndexedDB database `ketabkhane-db`, `DB_VERSION = 1` (name kept for data continuity, ADR 0003;
staging uses `ketabkhane-db-test`, ADR 0005):

| Store | Key | Fields |
|---|---|---|
| `books` | `id` (UUID) | `title`, `author`, `cover` (data URL), `file` (Blob), `size`, `addedAt`, `locations` (cached epub.js locations, JSON), `contentHash` (SHA-256, v6.2; filled lazily for older books) |
| `progress` | `bookId` | `cfi`, `percentage`, `updatedAt`, `bookmark` (single CFI) |

localStorage: `ketabkhane-reader-preferences` → `{font, lineHeight, fontSize, theme, align, librarySort}`; `ketab-theme` → `light|dark`.

Target schema v2 and the mandatory migration rules are in spec §8. Rules that apply now:
- Never delete/recreate the database; bump `DB_VERSION` and migrate step by step in `onupgradeneeded`.
- A migration must be tested on real data from the previous version and must leave old data intact on failure.

## 4. Key flows
- **Import:** validate extension/size → `extractMetadata` (opens with epub.js; listens for `openFailed`, 20 s timeout; waits for background loads before `destroy`) → store record. Errors are `UserError` with a Persian message; nothing is stored on failure.
- **Open:** restore saved CFI directly (`display(cfi)`), build TOC, then `prepareLocations` in background (load cached locations or generate with 1600 chars/location and save to the book record) so percentages are real.
- **Navigation state:** `relocated` sets progress, chapter label, `prev` disabled only at `atStart`, `next` disabled at `atEnd`; progress is saved per relocation.
- **Navigation input (v6.1):** keyboard on the main document plus `rendition.on("keydown"|"touchstart"|"touchend"|"click")`
  for input inside the book iframe (it does not bubble to the page). Directions are physical and follow the book:
  for RTL books (`page-progression-direction` or a Persian/Arabic-script language) ArrowLeft, a left-edge tap and a
  finger swipe to the right go **forward**; LTR books are mirrored. PageDown/Space forward, PageUp back, Escape closes
  drawer → immersive → reader. Edge zones are 25% of the viewer width each; taps on links or with selected text are
  ignored; a swipe suppresses the click that follows it.
- **Immersive mode:** a middle tap toggles `.immersive` on `#reader` (toolbar, page nav and bottom bar hidden) and
  calls `rendition.resize()` because the viewer changes size without a window resize.
- **Mobile TOC:** below 700 px the TOC panel becomes a right-side drawer (`.toc-open`), with a backdrop, focus moved
  into it on open and back to the toggle on close.
- **Settings panel (v6.2):** one `Aa` button opens a panel (bottom sheet on phones) with font, size, line height,
  alignment and page colour (light / sepia / dark). Escape priority: settings → TOC drawer → immersive → close reader.
  A tap inside the book while the panel is open only closes the panel.
- **Library (v6.2):** "continue reading" shelf (books with a saved CFI and < 100%, newest first), progress bar and
  badge on every card, sort by newest added / last read / title. Duplicate imports are refused by size + SHA-256.
  After the first import the app requests persistent storage; usage is shown under the grid.
- **Updates (v6.2):** when a new Service Worker takes control of a page that already had one, a banner offers a reload;
  `reg.update()` runs whenever the app becomes visible.
- **Race guard:** `openToken` invalidates async work when the reader is closed or another book opens.

## 5. Service Worker
- Install: pre-cache `APP_SHELL` (every file in `src/` the app needs, including vendor, font and `offline.html`).
- Navigation requests: network first, cached copy (then `index.html`, then `offline.html`) when offline.
- Other same-origin GETs: cache first, network fallback, cached on success.
- Activate: delete every cache whose name is not `CACHE_NAME`.
- Scope is the folder the SW is served from, so `/borg/` and `/borg-test/` have independent caches.
- Books are **not** in the SW cache; they live in IndexedDB.

## 6. Deployment
Host: shared cPanel, HTTPS. Repository cloned by cPanel Git Version Control outside `public_html`.
`.cpanel.yml` runs `scripts/cpanel-deploy.sh`, which copies `src/` to:
- branch `main` → `~/public_html/borg/` (production)
- branch `staging` → `~/public_html/borg-test/` (staging)

and forces permissions to 644/755 (v5 shipped files with 600 → HTTP 403, ADR 0004).
`scripts/smoke-test.sh` verifies a deployment over HTTP. The legacy `/reader/` path was removed on
2026-10-18 (ADR 0002).

## 7. Testing
`tests/e2e_test.py` serves `src/` under `/borg/`, blocks all non-local requests and runs 75 checks
(import, render, TOC, font in iframe, paging, progress, bookmark, persistence, offline, corrupt file,
missing JSZip, mobile layout, keyboard, edge taps, immersive mode, mobile TOC drawer, swipe via CDP touch events). Checks assert visibility (non-zero viewer height), not just DOM presence.

## 8. Known gaps
- Single bookmark per book (multiple bookmarks need IndexedDB schema v2).
- Not yet tested on real Android/iPad devices or large real-world Persian EPUBs.

## 9. Backend (v7)
- **Stack:** PHP 8.1+ (production runs 8.4), PDO, MySQL/MariaDB in production, SQLite in tests. No framework, no Composer.
- **Entry:** `src/api/index.php?r=<route>`; `src/api/lib/` is denied by `.htaccess` and guarded by `BORG_API`.
- **Config:** `/home/<user>/borg-config/config.php` (outside the web root; `BORG_CONFIG` overrides in tests):
  `db_dsn`, `db_user`, `db_pass`, `setup_key`, optional `storage_dir` (default `~/borg-storage`), `max_book_bytes`,
  `catalog_requires_approval` (default true), `session_days`.
- **Schema:** created/migrated automatically on first request (`schema_migrations`); DDL is portable via `{AI}`/`{TXT}`.
  Tables: users, sessions, invites, login_attempts, books (unique `sha256`), user_books, catalog, progress, uploads.
- **Auth:** invite-only. First admin via `setup` (needs `setup_key`, only while no users exist). Session token in an
  HttpOnly/Secure/SameSite=Lax cookie scoped to the app path; DB stores only its SHA-256. Mutating routes need the
  per-session CSRF token in `X-CSRF-Token`; JSON routes reject non-JSON bodies. Login/register/setup are rate limited.
- **Books:** chunked upload (`upload-init` → `upload-chunk` × n → `upload-complete`), chunk ≤ min(2 MB, 80% of PHP
  limits). Server verifies size, SHA-256 and EPUB structure (no extraction), stores `~/borg-storage/books/xx/<sha>.epub`.
  Same file = one copy for everyone (dedupe by hash). Downloads/covers go through PHP with a permission check
  (own library, approved catalog entry, or admin). Unreferenced books are garbage-collected.
- **Progress:** last write wins by client timestamp (ms); older writes are ignored.
- **Catalog:** user submissions are `pending` until an admin approves; admin submissions are approved directly.
- **Metadata:** `book-meta` lets the uploader or an admin correct title/author/cover.

## 10. Client sync (v7)
- `me` on start (skipped offline); when logged in, `sync()` runs on login, start, import, visibility and reconnect.
- Order: drop local books removed from the account → hash local books → upload missing ones → download missing
  ones (with cover) → reconcile progress both ways by timestamp → push corrected metadata / adopt server metadata.
- Progress is pushed 4 s after the last page turn. Deleting a synced book removes it from the account (confirmation).
- Without an account or offline, everything works locally exactly as before.
- Share target (`share-target`, POST multipart) is handled by the Service Worker: files are parked in the
  `borg-share` cache and imported by the page on `?shared=1`. Desktop "open with" uses `launchQueue`.
