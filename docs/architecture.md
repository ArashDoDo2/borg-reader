# Architecture — Borg (بُرگ)

Status: describes the code in `src/` as of v6.1.0. Product goals, roadmap and ideas live in the
private product spec (not in this repo); this file is the technical source of truth.

## 1. Overview
A static PWA. The browser does everything: parse EPUB, render, store books and progress.
No server code yet; the planned PHP API (spec §9–10) is out of scope until phase 5.

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
| `books` | `id` (UUID) | `title`, `author`, `cover` (data URL), `file` (Blob), `size`, `addedAt`, `locations` (cached epub.js locations, JSON) |
| `progress` | `bookId` | `cfi`, `percentage`, `updatedAt`, `bookmark` (single CFI) |

localStorage: `ketabkhane-reader-preferences` → `{font, lineHeight, fontSize}`; `ketab-theme` → `light|dark`.

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
`tests/e2e_test.py` serves `src/` under `/borg/`, blocks all non-local requests and runs 53 checks
(import, render, TOC, font in iframe, paging, progress, bookmark, persistence, offline, corrupt file,
missing JSZip, mobile layout, keyboard, edge taps, immersive mode, mobile TOC drawer, swipe via CDP touch events). Checks assert visibility (non-zero viewer height), not just DOM presence.

## 8. Known gaps
- Reader toolbar is crowded on phones (settings panel planned).
- Single bookmark per book; no sepia theme; no text alignment setting.
- Not yet tested on real Android/iPad devices or large real-world Persian EPUBs.
