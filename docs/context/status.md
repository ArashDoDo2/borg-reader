# Project status — Borg (بُرگ)

Last updated: 2026-10-19, version **8.6.7** (live at https://parsun.com/borg/).
Read this first when picking the project up. Technical detail is in `docs/architecture.md`; reader pitfalls in
`docs/context/reader-engine-notes.md`.

## Where things stand
- Production runs a single branch (`main`), deployed automatically by GitHub Actions after the tests pass.
  The repo is public: `ArashDoDo2/borg-reader`. There is no staging site (owner's decision, ADR 0007).
- Users: the owner (admin) and a few invited people. Accounts are invite-only.
- Server: shared cPanel host `parsun.com`, user `parsunco`, PHP 8.4, MySQL. Config in
  `/home/parsunco/borg-config/config.php`; book files in `~/borg-storage`; repo clone in
  `/home/parsunco/repositories/borg-reader`.
- Tests: API 159 checks, e2e ~215 checks (Chromium), WebKit probe (Safari engine, informational) — all green.

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
| 8.2.0 | Chapter title in the header (from the TOC, active TOC entry), whole-book seek bar with preview and time left in book, «برگشت به صفحهٔ قبل» chip after any jump (`jumpTo()`), first-run page-turn guide |
| 8.3.0 | Text cleanup «پاک‌سازی متن» (library toolbar): phrase rules + Telegram preset, preview, edits the EPUB file, re-anchors highlights, auto-clean on import; API `book-replace` moves library/progress/annotations (admin: catalog + other users) |
| 8.4.0 | WebKit (Safari) e2e tests gate the deploy (iPad, iPhone, desktop); TOC hrefs relative to the nav file resolved (epub.js "No Section Found"); client error reporting to the admin («خطاهای کاربران», API migration 5) | — not deployed: the new WebKit gate failed (veil never lifted in WebKit; taps failed)
| 8.4.1 | Veil fix for Safari: self-expiring CSS animation (1.5 s) + removal from the live document; production 8.1.2–8.3.0 likely showed blank chapters on Safari |
| 8.5.0 | Library search (title/author/publisher/shelf, Persian normalisation); faster open (direction cache, locations pause 8 ms: seek bar 8.9 s → 1.7 s on a 2 MB book) with an opening indicator; sync status chip; WebKit diagnostics (`diag()` with borg-debug stats) | — not deployed (WebKit tap checks failed)
| 8.5.1 | Safari: veil no longer waits for the <link> load event that WebKit never fires (polls `link.sheet`, ≤600 ms); WebKit tap checks report without gating (synthetic touches never reached the iframe in Playwright WebKit; real-device check pending) |
| 8.6.0 | Mobile polish. Library: hero only when the library is empty, compact toolbar, one-row scrolling chips, stats card below the grid on phones, cleaner cards, SVG theme icon, offline-only connectivity pill, bottom-sheet dialogs. Reader: icon back button, title + chapter line in the toolbar, page-nav row hidden on phones, compact bottom bar with icon bookmark, centred block images, settings/search sheets with title/close/green focus, safe areas, reader themes colour the chrome | — not deployed (WebKit test read the hidden chapter label)
| 8.6.1 | **iPhone/iPad taps fixed at the root:** the book iframe was sandboxed without allow-scripts and WebKit runs no event listeners there (taps, footnote/image clicks, keys in the book). `allowScriptedContent: true`; book scripts still cannot run (inherited CSP script-src 'self' + `stripBookScripts()` removes scripts, on* handlers, javascript: URLs, iframes/objects). WebKit tap checks gate again |
| 8.6.2 | Every link inside a chapter is handled by the app (`followBookLink`): internal → resolved against the chapter path and opened via `jumpTo()` (back chip), external → new tab, unknown → toast; the iframe never navigates (owner saw a link go to parsun.com/feed_4/… outside the reader; not reproducible with the August issue, fixed at the handler level) |
| 8.6.3 | Mouse wheel / trackpad turns pages in paginated mode (summed deltas, one page per gesture, ctrl+wheel zoom untouched); setting «ورق زدن با چرخ موس» (pref `wheel`) |
| 8.6.4 | Text cleanup rule type «کل خطِ شامل این متن» (`type: "line"`): removes the smallest block containing the fixed part even when the rest varies (URLs) or spans links; blocks longer than the part + 400 chars are kept and reported; share limit 10% for line rules |
| 8.6.5 | «جهت این کتاب» in the «Aa» panel: per-book override auto/RTL/LTR (book record `dirOverride`, kept by every book write via STICKY_FIELDS, local to the device); changing it reopens at the same position |
| 8.6.6 | Phone toolbar icons 21 → 26 px (owner: too small on iPhone), «Aa» 19 px, bookmark 26 px |
| 8.6.7 | Every navigation (jumpTo, back chip, TTS) goes through `safeDisplay()`: a target epub.js cannot resolve shows a toast and is reported as kind `nav` with its origin and target (admin error reports showed unhandled "No Section Found" up to 8.6.3; WebKit NotFoundError in 8.6.5 still under investigation) |

## Open items (not started unless the owner says so)
0. Text cleanup shipped in 8.3.0. Limits: a phrase split across formatting tags is not matched; the
   "too broad" override is per rule, not per book; server swap path is covered by API tests, not e2e.
- Fixed (v8.4.0): TOC hrefs that go up a folder (`../text/c1.xhtml` from `nav/`) failed with "No Section Found".
2. **Safari tests in CI** (v8.4.0): `tests/webkit_e2e.py` runs in the CI job `webkit` and gates the deploy
   (open book, text laid out, touch edge taps, TOC, veil, seek bar, back chip). The probe now runs only when it fails.
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
