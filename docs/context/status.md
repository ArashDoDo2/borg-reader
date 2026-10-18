# Project status — Borg (بُرگ)

Last updated: 2026-10-19, version **8.1.6** (live at https://parsun.com/borg/).
Read this first when picking the project up. Technical detail is in `docs/architecture.md`; reader pitfalls in
`docs/context/reader-engine-notes.md`.

## Where things stand
- Production runs a single branch (`main`), deployed automatically by GitHub Actions after the tests pass.
  The repo is public: `ArashDoDo2/borg-reader`. There is no staging site (owner's decision, ADR 0007).
- Users: the owner (admin) and a few invited people. Accounts are invite-only.
- Server: shared cPanel host `parsun.com`, user `parsunco`, PHP 8.4, MySQL. Config in
  `/home/parsunco/borg-config/config.php`; book files in `~/borg-storage`; repo clone in
  `/home/parsunco/repositories/borg-reader`.
- Tests: API 105 checks, e2e ~215 checks (Chromium), WebKit probe (Safari engine, informational) — all green.

## Version history (what each release added)
| Version | Highlights |
|---|---|
| 6.0 | Renamed to Borg, `/borg/` path, app-version meta |
| 6.1 | Mobile TOC drawer, keyboard/swipe navigation, edge tap zones, immersive mode |
| 6.2 | Settings panel (sepia, alignment), continue-reading shelf, sorting, duplicate detection, update banner |
| 7.0 | PHP API: invite-only accounts, cloud library (chunked upload, SHA-256 dedupe), progress sync, moderated public catalog |
| 7.1 | In-book search, metadata repair, Android share target, desktop open-with |
| 7.2 | Full admin panel; bookmarks/highlights/notes with sync; catalog search and details |
| 7.3 | Shelves and reading status, reading stats and daily goal, time left in chapter, quote cards, CSP |
| 8.0.0 | Read aloud (TTS), footnote popups, word lookup/copy, layout options (margins, paragraph spacing, scroll/paged, spread), more fonts, Persian digits, wake lock, image viewer, black/auto themes |
| 8.0.1 | Safari: store books as ArrayBuffer when IndexedDB refuses Blobs |
| 8.0.2 | RLM used as half-space shown as real ZWNJ; search normalises ی/ي/ى, ک/ك and half-spaces |
| 8.0.3 | Replaced look-alike fonts with Parastoo, Noto Naskh Arabic, Amiri |
| 8.0.4 | Safari/iPad blank page fixed (epub.js RTL column width measurement) |
| 8.0.5 | iPhone/iPad edge taps fixed (taps detected from touch events) |
| 8.1.0 | Catalog book page (deep link, metadata table, breadcrumb, related books), online preview, add-with-shelves, author/category filters, publisher/date/language metadata (API migration 4) |
| 8.1.1 | RTL by default so TOC jumps land on the chapter start in books without a declared direction |
| 8.1.2 | Chapters hidden until font and theme are applied (no text "jump" on load) |
| 8.1.3 | Per-book reading direction (English books LTR) detected from text; nested TOC entries |
| 8.1.4 | Direction-aware TOC/notes/title text (English labels left-aligned) |
| 8.1.5 | Drawn SVG toolbar icons, larger touch targets on mobile (its tests failed; it reached production through the CI race fixed in 8.1.6) |
| 8.1.6 | Desktop TOC toggle hidden again; CI deploys only when the tested commit is still the branch head |

## Open items (not started unless the owner says so)
1. **Chapter name in the reader header** instead of «بخش N» (label currently counts spine files). Proposed, awaiting approval.
2. **Safari tests in CI**: turn `tests/webkit_probe.py` into real WebKit checks (open book, text painted, edge taps,
   TOC). Proposed, awaiting approval. The probe already runs in CI but only reports.
3. **Real-device confirmation** still pending from the owner: iPad shows text (8.0.4), iPhone edge taps (8.0.5).
4. Later candidates (owner chose reading features first): error reporting to the admin, upload progress/resume,
   performance on very large books, Android app wrapper (needs owner's package name, signing key, Play account).
5. Ideas deferred on purpose: ratings and comments in the catalog (too few users), "send to Kindle" (needs SMTP).

## Decisions the owner already made (don't re-ask)
- One production branch, no staging. Speed matters: ship, keep tests green, fix forward with a new revision.
- GitHub Actions is fine because it is free for a public repo.
- Login by email + password; invite-only; public catalog with admin approval (toggle in admin settings).
- Backups are handled by the host's cPanel backup, not by the app. (Recommended to the owner: confirm that
  `~/borg-storage` and the database are included, kept off-server, and that a restore has been tried once.)
- Product spec stays private (`docs/private/`, git-ignored).
- Copyright: the public catalog is the site owner's legal responsibility; moderation stays on.
