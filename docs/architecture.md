# Architecture — Borg (بُرگ)

Status: describes the code in `src/` as of v8.2.0 (2026-10-19). Project status and open items: `docs/context/status.md`;
reader pitfalls: `docs/context/reader-engine-notes.md`. Product goals, roadmap and ideas live in the
private product spec (not in this repo); this file is the technical source of truth.

## 1. Overview
A static PWA. The browser does everything: parse EPUB, render, store books and progress.
Since v7 an optional PHP API (`src/api/`) adds accounts, cloud library, progress sync and a public catalog; without an account the app stays fully local.

```
index.html ──> assets/js/app.js ──> epub.js (+ JSZip) ──> book rendered in a sandboxed iframe
                     │                                        └─ reader-fonts.css injected (Vazirmatn)
                     ├─> IndexedDB  (books, progress, annotations, shelves)
                     ├─> localStorage (reader prefs, theme, reading stats, device id)
                     ├─> src/api/ (PHP, optional: accounts, sync, catalog)
                     └─> service-worker.js (offline app shell)
```

## 2. Components
| File | Role |
|---|---|
| `src/index.html` | Library + reader UI; loads vendor scripts then `app.js`. Holds `<meta name="app-version">`. |
| `src/assets/js/app.js` | All app logic (single IIFE): import, metadata, library, shelves, reader, navigation, annotations, TTS, search, settings, account/sync, catalog, admin, SW registration. |
| `src/assets/vendor/jszip.min.js` | JSZip 3.10.1. Must load **before** epub.js (epub.js needs it to open an EPUB from an ArrayBuffer). |
| `src/assets/vendor/epub.min.js` | epub.js 0.3.93. Paginated rendition, `spread: auto`. Scripts inside EPUBs are not enabled. |
| `src/assets/fonts/` | Vazirmatn (UI + default book font), Shabnam, Parastoo, Noto Naskh Arabic, Amiri (OFL; license files alongside). |
| `src/assets/css/reader-fonts.css` | `@font-face` for the book fonts + `.tts-hl`; injected into every book iframe via `rendition.hooks.content` (absolute URL). |
| `src/api/` | PHP API (§9). |
| `src/service-worker.js` | Offline shell, cache versioned by `CACHE_NAME`. |
| `src/offline.html` | Last-resort page when nothing is cached. |

## 3. Data model (browser)
IndexedDB database `ketabkhane-db`, `DB_VERSION = 3` (v2/7.2 annotations, v3/7.3 shelves; name kept for data
continuity, ADR 0003; the unused staging path would use `ketabkhane-db-test`, ADR 0005):

| Store | Key | Fields |
|---|---|---|
| `books` | `id` (UUID) | `title`, `author`, `cover` (data URL), `file` (Blob), `size`, `addedAt`, `locations` (cached epub.js locations, JSON), `contentHash` (SHA-256, v6.2; filled lazily for older books); v8.1 optional `publisher`, `published` (`YYYY-MM-DD` or free text), `language` read from the EPUB `<dc:publisher>`/`<dc:date>`/`<dc:language>`, plus `metaExt` (already read) and `metaExtPush` (still to send to the server) |
| `progress` | `bookId` | `cfi`, `percentage`, `updatedAt` (the v1 single `bookmark` was migrated into `annotations`) |
| `annotations` | `uid` (index `bookId`) | `type` (bookmark/highlight/note), `cfi`, `quote`, `note`, `color`, timestamps, sync flags |
| `shelves` | `uid` | `name`, `position`, `updatedAt`, `deleted` |

Books also carry `serverId`, `catalogStatus`, reading `status` and `shelves` when synced. When a browser refuses to
store Blobs, `file` is null and `fileBuffer` (ArrayBuffer) + `fileName` are stored instead (`fileOf()`).

localStorage: `ketabkhane-reader-preferences` → `{font, lineHeight, fontSize, theme (light/sepia/dark/black/auto),
align, librarySort, libraryFilter, dailyGoal, margin, para, flow, spread, digits, taps, wake, ttsRate, ttsVoice}`;
`ketab-theme`; `borg-reading-days`/`-other`/`-pushed` (stats); `borg-device-id`; `borg-debug` (diagnostics only,
read by the WebKit probe; empty for real users).

Migration rules:
- Never delete/recreate the database; bump `DB_VERSION` and migrate step by step in `onupgradeneeded`.
- A migration must be tested on real data from the previous version and must leave old data intact on failure.

## 4. Key flows
- **Import:** validate extension/size → `extractMetadata` (opens with epub.js; listens for `openFailed`, 20 s timeout; waits for background loads before `destroy`) → store record. Errors are `UserError` with a Persian message; nothing is stored on failure.
- **Open:** restore saved CFI directly (`display(cfi)`), build TOC, then `prepareLocations` in background (load cached locations or generate with 1600 chars/location and save to the book record) so percentages are real.
- **Navigation state:** `relocated` sets progress, chapter label, `prev` disabled only at `atStart`, `next` disabled at `atEnd`; progress is saved per relocation.
- **Navigation input (v6.1):** keyboard on the main document plus `rendition.on("keydown"|"touchstart"|"touchend"|"click")`
  for input inside the book iframe (it does not bubble to the page). Directions are physical and follow the book:
  for RTL books (`bookDir`, see §4b / ADR 0008) ArrowLeft, a left-edge tap and a
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
- **Catalog book page (v8.1):** `#viewBook` (tab "catalog" stays selected), deep link `#catalog/book/<id>`, catalog
  `#catalog`; `popstate` routes between them. Large cover, title, author link (→ catalog filtered by that author, with
  a removable chip), description clamped to 5 lines with «بیشتر بخوانید» shown only when it is really cut off,
  metadata table (category, language, publisher, date, size, submitter — only rows with a value), breadcrumb
  (catalog / category / title), «کتاب‌های مشابه» (same author first, then same category, max 6).
  The catalog view has category chips (built from `category` of the loaded books) combined with search and author filter.
- **Online preview (v8.1):** «مطالعهٔ آنلاین» fetches `book-file` into memory and opens the reader with an ephemeral
  record `{id: "preview-<catalogId>", preview: true}`. Nothing is written: no `books` record or cached locations,
  no progress (`saveProgress` and `saveAnnotation` ignore `preview-` ids as a last guard), no bookmarks/highlights/notes
  (toast instead), no status change, no reading-time stats, so nothing is synced. A banner offers «افزودن به
  کتابخانه»; adding reuses the in-memory file, then the reader switches to the stored copy and saves the position from
  then on. Closing the preview returns to the book page.
- **Add from the catalog (v8.1):** the main button adds in one click; the ▾ menu lists existing shelves (and can create
  one) and puts the book on the checked shelves after `catalog-add` + download. `catalog-submit` asks for an optional
  category; the admin catalog editor is a dialog (title, author, description, category, publisher, date, language).
- **Race guard:** `openToken` invalidates async work when the reader is closed or another book opens.

## 4b. Reader engine (v8)
Details and history of each workaround: `docs/context/reader-engine-notes.md`.
- **Open sequence:** read file → `ePub(bytes)` → `detectBookDirection()` sets `bookDir` (ADR 0008) →
  `patchTextWidth("rects")` (Safari fix) → `renderTo("viewer", {flow, spread, defaultDirection: bookDir})` →
  serialize hook adds the load veil (after `book.opened`) → content hooks (`onBookContent`: half-space fix,
  Persian digits, footnote/image click handlers; fonts stylesheet then veil removal) → theme (`applyReaderStyles`).
- **Navigation:** `goLeft/goRight` map to forward/back through `bookIsRtl()`. Taps are recognised on `touchend`
  (iOS) and on `click` (mouse) through one `handleTap()`; edge zones 25%, middle tap toggles immersive.
- **Reading features:** TTS (`speechSynthesis`, sentence queue with `.tts-hl` highlight, follows pages),
  footnote popups, word lookup / copy / quote card from the selection bar, highlights in 4 colours, notes,
  multiple bookmarks, in-book search with Persian normalisation, image viewer, wake lock, layout options
  (margin, paragraph spacing, paged/scrolled, single/two columns), 9 font choices, Persian digits option.
- **TOC and header (8.2):** nested entries up to 3 levels (`.toc-sub`), labels `dir="auto"`. The header label shows the
  matching TOC title (deepest entry for the file; `#fragment` entries matched against the end of the visible page),
  falling back to «بخش N»; the matching entry gets `.active`/`aria-current`.
- **Seek bar (8.2):** `#bookSeek` range 0–1000 over `book.locations`, direction = `bookDir`, disabled until locations
  are ready; dragging previews chapter + percentage, release navigates via `jumpTo()`. Shows «٪X از کتاب · حدود N
  دقیقه تا پایان کتاب».
- **Jumps (8.2):** every non-page-turn navigation goes through `jumpTo(target)` (TOC, search, notes list, footnote
  "go to", seek bar; internal links record their origin via a click listener). It keeps a stack (max 10) and shows
  `#backChip` «↩ برگشت به صفحهٔ قبل», placed above the bottom bar by `placeBackChip()`; hidden after 5 page turns.
- **First-run guide (8.2):** overlay `role="dialog"` once per device (`borg-guide-seen`), wording from `bookIsRtl()`
  and the taps/flow prefs; reopen from settings. Tests pre-set the key in `new_ctx` (`show_guide=True` to test it).

## 5. Service Worker
- Install: pre-cache `APP_SHELL` (every file in `src/` the app needs, including vendor, font and `offline.html`).
- Navigation requests: network first, cached copy (then `index.html`, then `offline.html`) when offline.
- Other same-origin GETs: cache first, network fallback, cached on success.
- Activate: delete every cache whose name is not `CACHE_NAME`.
- Scope is the folder the SW is served from, so `/borg/` and `/borg-test/` have independent caches.
- Books are **not** in the SW cache; they live in IndexedDB.

## 6. Deployment
Host: shared cPanel (`parsun.com`, user `parsunco`), HTTPS. Single branch `main` (ADR 0007).
1. `git push origin main` triggers `.github/workflows/deploy.yml`.
2. Job `test`: `tests/api_test.py` + `tests/e2e_test.py` (Chromium). Failures are printed as `::error`
   annotations (readable with `gh api repos/ArashDoDo2/borg-reader/check-runs/<job-id>/annotations`).
   Job `webkit-probe` (`tests/webkit_probe.py`, `continue-on-error`) reports Safari-engine measurements as `::warning`.
3. Job `deploy` (needs `test`): first checks that the tested commit is still the head of `main`; if a newer
   commit was pushed meanwhile it skips (the newer run, queued behind it, tests and deploys that commit) — the server
   always pulls the branch head, so this is what guarantees only tested code is deployed. Then cPanel UAPI `VersionControl/update` then `VersionControlDeployment/create` on the
   clone `/home/parsunco/repositories/borg-reader`. `.cpanel.yml` runs `scripts/cpanel-deploy.sh`, which rsyncs
   `src/` into `~/public_html/borg/` and forces 644/755 (v5 lesson: 600 → HTTP 403, ADR 0004).
4. `scripts/smoke-test.sh https://parsun.com/borg/ <version>` retries for ~1 minute until the new
   `app-version` is served.
Secrets (`CPANEL_TOKEN`, `CPANEL_USER`, `CPANEL_HOST`) are GitHub Actions secrets set by the owner. The server
config with DB credentials and `setup_key` is `/home/parsunco/borg-config/config.php` (never in the repo).
Versioning: bump `app-version` meta, footer text in `index.html` and `CACHE_NAME` in `service-worker.js`.
Rollback: revert the commit on `main` and push (data in IndexedDB and MySQL is untouched; migrations are additive).

## 7. Testing
- `tests/api_test.py` (105 checks): PHP built-in server + SQLite via `BORG_CONFIG`; auth, CSRF, rate limits,
  upload/dedupe, permissions, progress, annotations, shelves, stats, catalog moderation, admin, migrations.
- `tests/e2e_test.py` (~215 checks): serves `src/` under `/borg/` with `php -S` and a SQLite config, blocks every
  non-local request. Covers import, render, TOC, fonts in the iframe, paging, progress, annotations, settings,
  persistence, offline, corrupt files, mobile layout, keyboard, edge taps, swipe (CDP touch), immersive mode,
  accounts/sync between two browsers, catalog/admin, v8 reading features, v8.1 catalog page and preview, and
  section 14: direction detection (no-direction Persian book, English book), nested TOC, load veil.
  Checks assert visibility (geometry), not DOM presence. Use `poll()` (CSP forbids `wait_for_function`).
- `tests/webkit_probe.py`: opens a sample in WebKit (iPad viewport) under rendering variants and reports frame
  width and what is under the viewer; diagnostics, not a gate (see open item in `docs/context/status.md`).

## 8. Known gaps
- No WebKit/Safari gate in CI yet; iOS behaviour is verified by the owner on devices after release.
- Direction detection follows the majority script; no manual per-book override.
- Very large books: locations generation and direction detection run on open; not profiled on 50 MB+ files.

## 9. Backend (v7)
- **Stack:** PHP 8.1+ (production runs 8.4), PDO, MySQL/MariaDB in production, SQLite in tests. No framework, no Composer.
- **Entry:** `src/api/index.php?r=<route>`; `src/api/lib/` is denied by `.htaccess` and guarded by `BORG_API`.
- **Routes:** `health setup register login logout me password invites` (index.php); `library library-meta
  library-remove upload-init upload-chunk upload-complete book-file book-cover book-meta book-replace progress annotations
  shelves stats catalog catalog-submit catalog-pending catalog-review catalog-remove catalog-add` (books.php);
  `admin-stats users user-update user-reset-password user-delete invite-revoke catalog-all catalog-update
  books-all book-delete settings` (admin.php).
- **Config:** `/home/<user>/borg-config/config.php` (outside the web root; `BORG_CONFIG` overrides in tests):
  `db_dsn`, `db_user`, `db_pass`, `setup_key`, optional `storage_dir` (default `~/borg-storage`), `max_book_bytes`,
  `catalog_requires_approval` (default true), `session_days`.
- **Schema:** created/migrated automatically on first request (`schema_migrations`); DDL is portable via `{AI}`/`{TXT}`.
  Migrations: 1 users, sessions, invites, login_attempts, books (unique `sha256`), user_books, catalog, progress,
  uploads; 2 annotations, settings; 3 user_books status/shelves/meta_updated_at, shelves, reading_days.
  Migration 4 adds `books.publisher` (≤300), `books.published` (≤40, free-text date/year from EPUB metadata) and
  `catalog.category` (≤80), all `NOT NULL DEFAULT ''`. `health` reports `schema_version`.
- **Auth:** invite-only. First admin via `setup` (needs `setup_key`, only while no users exist). Session token in an
  HttpOnly/Secure/SameSite=Lax cookie scoped to the app path; DB stores only its SHA-256. Mutating routes need the
  per-session CSRF token in `X-CSRF-Token`; JSON routes reject non-JSON bodies. Login/register/setup are rate limited.
- **Books:** chunked upload (`upload-init` → `upload-chunk` × n → `upload-complete`), chunk ≤ min(2 MB, 80% of PHP
  limits). Server verifies size, SHA-256 and EPUB structure (no extraction), stores `~/borg-storage/books/xx/<sha>.epub`.
  Same file = one copy for everyone (dedupe by hash). Downloads/covers go through PHP with a permission check
  (own library, approved catalog entry, or admin). Unreferenced books are garbage-collected.
- **Progress:** last write wins by client timestamp (ms); older writes are ignored.
- **Catalog:** user submissions are `pending` until an admin approves; admin submissions are approved directly.
  `catalog-submit` takes optional `category` (≤80, trimmed). `catalog`, `catalog-pending` and `catalog-all` return
  `category` per book. Admin `catalog-update` updates only the keys present among `description`, `title`, `author`,
  `category`, `publisher`, `published`, `language`; all are validated before anything is written.
- **Book fields:** every book object (`book_out`) has `id, sha256, size, title, author, language, publisher, published,
  has_cover`. `upload-init` accepts optional `language` (≤20), `publisher` (≤300), `published` (≤40); the same keys
  sent to `upload-complete` override them (used only when the file is new, not on dedupe).
- **Metadata:** `book-meta` lets the uploader or an admin correct title/author/language/publisher/published/cover;
  each field changes only when its key is present (`title`, if present, must be non-empty).
- **Book replace (text cleanup):** the client uploads the cleaned EPUB with the normal upload flow (new SHA-256 → new
  book id, or an existing one on dedupe), then `POST book-replace {old_book_id, new_book_id}` (CSRF). 400 `same_book`
  if equal; 404 unless `old_book_id` is in the caller's library (admins: exists at all) and `new_book_id` exists and is
  in the caller's library. In one transaction, for the caller: the `user_books` row moves to the new id (`added_at` from
  old; `status`/`shelves`/`meta_updated_at` from the row with the newer `meta_updated_at` if both exist), progress moves
  (newer `updated_at` wins if both exist), annotations are re-pointed (uid is unique per user) with `updated_at` bumped to
  at least server "now" so `annotations?since=` returns them, and the old library row is removed. If the caller is an
  admin and the old book has a catalog row, the row is re-pointed to the new book with all its fields (if the new book
  already has one, that one is kept and the old one deleted) and every other user who has the old book is migrated the
  same way. A non-admin replacing a catalog book moves only their own library; the catalog keeps the old file. Empty
  title («بدون عنوان» counts as empty)/author/language/publisher/published and a missing cover of the new book are copied
  from the old one. Afterwards the old book is garbage-collected if unreferenced. Response:
  `{book, migrated_users, catalog_moved}`. Clients should push re-anchored annotations after this call (fresh timestamps)
  so they win over the bumped server copies; `library` no longer lists the old id, so normal sync drops it and
  downloads the new one.
- **Validation:** optional string fields that are present but not a string/number → 400 `bad_<field>`; over the
  length limit → 400 `too_long_<field>`.

## 10. Client sync (v7)
- `me` on start (skipped offline); when logged in, `sync()` runs on login, start, import, visibility and reconnect.
- Order: drop local books removed from the account → hash local books → upload missing ones → download missing
  ones (with cover) → reconcile progress both ways by timestamp → push corrected metadata / adopt server metadata.
  v8.1: `upload-init`/`upload-complete` and `book-meta` carry `publisher`/`published`/`language` when known; on sync
  the client fills fields the server lacks (once, `metaExtPush`) and adopts non-empty server values. Books imported
  before v8.1 are re-read once (`metaExt`) to pick these fields up.
- Progress is pushed 4 s after the last page turn. Deleting a synced book removes it from the account (confirmation).
- Without an account or offline, everything works locally exactly as before.
- Share target (`share-target`, POST multipart) is handled by the Service Worker: files are parked in the
  `borg-share` cache and imported by the page on `?shared=1`. Desktop "open with" uses `launchQueue`.
