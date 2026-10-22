# Project status — Borg (بُرگ)

Last updated: 2026-10-22, version **8.13.9** (live at https://parsun.com/borg/).
Read this first when picking the project up. Technical detail is in `docs/architecture.md`; reader pitfalls in
`docs/context/reader-engine-notes.md`.

## Where things stand
- Production runs a single branch (`main`), deployed automatically by GitHub Actions after the tests pass.
  The repo is public: `ArashDoDo2/borg-reader`. There is no staging site (owner's decision, ADR 0007).
- Users: the owner (admin) and a few invited people. Accounts are invite-only.
- Server: shared cPanel host `parsun.com`, user `parsunco`, PHP 8.4, MySQL. Config in
  `/home/parsunco/borg-config/config.php`; book files in `~/borg-storage`; repo clone in
  `/home/parsunco/repositories/borg-reader`.
- Tests: API 401 checks, e2e 537 checks (Chromium), WebKit e2e (Safari engine, gates the deploy) — all green.

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
| 8.7.0 | API upload for the owner's own program: admin API keys («کلیدهای API», migration 6 `api_tokens`), `POST api-book` (Bearer key, multipart, public catalog only, approved unless the key requires review), server-side text cleanup (`lib/clean.php`, byte-identical to the browser on the test corpus) with the admin's rules synced to the server (`clean-rules`, `settings_text`); guide in docs/api-books.md |
| 8.8.0 | Text-based PDF magazines open directly in their original layout (pdf.js 4.10.38 legacy, vendored, lazy-loaded): import/sync/catalog/API accept PDFs (migration 7 `books.format`), page view with text layer, zoom/pinch, outline TOC, search, page progress `pdf:N`, taps/swipes/wheel/keys/seek; highlights/notes/TTS/cleanup not for PDFs yet (ADR 0009) |
| 8.8.1 | Select or drag & drop several EPUB/PDF files at once: imported one after another with a progress toast and one summary (added / duplicate / failed / cleaned); one sync at the end |
| 8.9.0 | Scale to thousands of books. Admin: sub-tabs with `#admin/<tab>` links (overview, review queue, books, users, invites, API keys, errors, settings), server-paged books table (`admin-books`: search, status/category/format/uploader/no-cover filters, sort, 50/page, filters kept in the URL), bulk actions (`admin-bulk`, ids or the whole filter, confirmation with count), details panel, one-at-a-time review queue (A/R/C, J/K, arrows). Public catalog paged from the server (`catalog?page=&per=`, search/filters/sort server-side, infinite scroll + «بیشتر», book deep link fetches one book by `id`, back keeps loaded pages and position). Migration 8: `books.source`, `books.search_key`, indexes. Old servers fall back to the full lists |
| 8.10.0 | Two-level categories (category → subcategory), several per book (max 10), admin-managed list (migration 9: `categories`, `catalog_categories`; existing category strings became top-level categories; legacy `catalog.category` mirrors the first top-level name). Public catalog: category navigator (desktop panel, phone drill-down sheet), `cat=ID` filter in the hash, breadcrumb, category links on the book page. Admin: «دسته‌ها» tab (add/rename/move/reorder/delete with move-to), category picker in details/queue (C)/user submission, tree filter and bulk add/remove in the books tab. `api-book` accepts `Parent/Child; Other` and creates missing categories. No per-category icons yet |
| 8.10.1 | Landing polish & public catalog teaser: hero feature badges, dropzone empty state, unauthenticated public catalog callout card, refined book card shadows and micro-interactions |
| 8.10.2 | Fix iPhone/Safari SVG icon sizing & solid black fill glitch: base `.ico` vector rules, explicit SVG width/height and presentation stroke attributes across all landing & action icons |
| 8.10.3 | 1,000+ books catalog UX: curated horizontal showcase shelf («تازه‌ها و برگزیده‌ها») for browsing without filter, and instant-filter quick category pill bar |
| 8.10.4 | Mobile/iPhone reading experience optimization: streamlined compact reader bottom bar (-24px vertical height), direct fullscreen button and auto-immersive option («پنهان کردن خودکار نوارها هنگام ورق زدن») in Aa settings panel |
| 8.10.5 | Sync page-nav direction and button labels/arrows with book reading direction: in English (LTR) books, «← قبلی» is on the left and «بعدی →» on the right, matching edge margin taps |
| 8.11.0 | Visual comfort & eye care: Warm Amber («کهربایی») blue-light-free night theme, Mint Paper («نعنایی») daytime low-glare theme, font weight picker (معمولی/متوسط/توپر) for crisper mobile typography, and in-reader screen brightness dimmer |
| 8.12.0 | AI Reading Companion (BYOK): landing page showcase & onboarding guide, 100% private client-side integration for Google Gemini & OpenAI, selection bar AI explanation & translation, chapter summary, key insights, and interactive book Q&A |
| 8.13.0 | Admin URL book importer («دریافت با لینک»): direct server-side download and processing for EPUB/PDF links, SSRF-safe manual redirect & IP validation, automatic OPF metadata extraction, text cleanup, category & public catalog curation |
| 8.13.1 | Fix Gemini API 404 error: update model endpoint to stable production models (`gemini-1.5-flash`, `gemini-2.0-flash`, `gemini-1.5-pro`) with automatic fallback and dual header/query authentication |
| 8.13.2 | Clarify AI guide copy: remove rigid prefix references (`AIzaSy...`) in instructions and input placeholder |
| 8.13.3 | Dynamic Gemini model discovery: auto-query `ListModels` on active Google AI Studio project, sort/select best active model (`flash`/`gemini`), cache working model, and report exact Google API diagnostic messages on error |
| 8.13.4 | Filter out TTS/Audio models: exclude `gemini-2.5-flash-preview-tts` and audio models from text generation, prioritize `gemini-2.5-flash` / `gemini-3.8-flash`, and continue to next model on modality mismatch 400 |
| 8.13.5 | 10x faster AI response: disable thinking budget (`thinkingBudget: 0`) and enable SSE token streaming (`streamGenerateContent`) for real-time word-by-word summaries |
| 8.13.6 | Prioritize `gemini-2.5-flash-lite` (default thinking: off) for sub-second responses, extract text across delta/parts while discarding internal thought tokens, and bypass stale cached preview models |
| 8.13.7 | Prioritize `gemini-2.0-flash` / `gemini-2.5-flash-lite` over heavy reasoning models, fix context badge hiding and clear label, render markdown headings (`###`) and dividers cleanly, and sharpen summary prompts |
| 8.13.8 | Hide purple AI teaser card when AI key is configured (`hasAiKey()`), add compact `✨ هوش مصنوعی` button in library controls and account dialog to manage/clear key, and auto-sync AI UI state on startup |
| 8.13.9 | AI Book Description Generation: add `✨ نگارش با هوش مصنوعی` button to admin book edit form, catalog suggestion dialog, import from URL dialog, and library book options dialog; extract EPUB description on import |

## Open items (not started unless the owner says so)
1. **Waiting for the owner:** screenshot of the WebKit `NotFoundError` details from «خطاهای کاربران» (seen in 8.6.5);
   real-device check on iPhone/iPad of the 8.6–8.10 mobile changes (taps fixed at the root in 8.6.1).
2. **Known, not fixed:** with margin «زیاد», swipes may not turn pages on phones.
3. **Server setting (owner):** production health (2026-10-20): PHP 8.4.26, zip/dom on, schema 9, `upload_max_filesize`
   128M but `post_max_size` 32M. Browser uploads are chunked (2 MB) and unaffected; `api-book` sends the whole file in
   one request, so books over ~32 MB are rejected until `post_max_size` is raised to 128M (cPanel MultiPHP INI Editor).
4. Text cleanup limits: a phrase split across formatting tags is not matched; the "too broad" override is per rule,
   not per book. PDFs: no highlights/notes/TTS/cleanup yet (ADR 0009). Categories: no per-category icons (ADR 0010).
5. Later candidates: upload resume, performance on very large books, Android app wrapper (needs owner's package name,
   signing key, Play account).
6. Ideas deferred on purpose: ratings and comments in the catalog (too few users), "send to Kindle" (needs SMTP).

## Decisions the owner already made (don't re-ask)
- One production branch, no staging. Speed matters: ship, keep tests green, fix forward with a new revision.
- GitHub Actions is fine because it is free for a public repo.
- Login by email + password; invite-only; public catalog with admin approval (toggle in admin settings).
- Backups are handled by the host's cPanel backup, not by the app. (Recommended to the owner: confirm that
  `~/borg-storage` and the database are included, kept off-server, and that a restore has been tried once.)
- Product spec stays private (`docs/private/`, git-ignored).
- Copyright: the public catalog is the site owner's legal responsibility; moderation stays on.
- Text cleanup edits the EPUB file itself, for all books, no original kept, auto-applied on import; an admin cleanup
  replaces the catalog file and other users' copies.
- API upload (8.7): public catalog only, the owner's own program, a few books a day, server-side cleanup.
- PDFs open directly in their layout, no conversion (ADR 0009). Reading direction is per book, auto + manual (ADR 0008).
- Scale target: ~2,000–3,000 books a year (8.9 server paging everywhere).
- Categories: two levels, several per book, admin-managed list, API creates missing ones (ADR 0010).
