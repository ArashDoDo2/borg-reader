# Project status — Borg (بُرگ)

Last updated: 2026-10-25, version **8.13.26** on `main` (CI tests green; the cPanel deploy step failed, so the live site at
https://parsun.com/borg/ is still **8.13.24** until the owner redeploys — see open item 1).
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
| 8.13.10 | Chapter summary persistence & notes integration: cache chapter summaries locally, auto-restore on reopening AI panel, add `📌 ذخیره در یادداشت‌های کتاب` and `📋 کپی متن`, and completely eliminate empty green context badge |
| 8.13.11 | Support Google Interactions API & migrate to `gemini-3.8-flash`: purge deprecated `gemini-2.5-flash`, add `callGeminiInteractions` endpoint support, and prioritize Gemini 3.8 / 3.5 models |
| 8.13.12 | Add «🔍 استخراج ناشر و تاریخ» button: inspect EPUB OPF & colophon text, with AI strict non-guessing prompt to only fill publisher/date when 100% verified |
| 8.13.13 | Student role & admin book assignment: new `student` user group for placement students with zero public catalog access, ability to upload to personal library, invite role selector, admin user role toggling, and admin book assignment tool (`👥 تخصیص به کاربر / دانشجو`) |
| 8.13.14 | Book replacement tool & homepage auth visibility: add `🔄 جایگزینی با فایل جدید` in admin detail to upload new editions and auto-migrate all readers; fix hero AI badge contrast on dark green; add animated pulsing golden `ورود` CTA button and hero inline login trigger for unauthenticated visitors |
| 8.13.15 | Mobile/iOS Chrome touch & reader toolbar fixes: widen finger tap movement tolerance (22px) and lower lockout to 350ms for reliable middle tap & page turns; fallback iframe discovery for WebKit; stop click propagation on reader toolbar buttons (`settingsToggle`, `searchToggle`, `aiToggle`, `tocToggle`) and use `.closest()` in outside click handlers to prevent immediate close; elevate reader-toolbar `z-index: 35` and set `touch-action: manipulation` |
| 8.13.16 | Mobile image viewer tap-to-close: tapping anywhere on the enlarged image or background backdrop immediately closes the viewer, and expand close button hit target (48px) |
| 8.13.17 | Mobile reader viewport lock: lock vertical overscroll (`overscroll-behavior: none`, `overflow-y: hidden`) and unify theme background on both `html` and `body` inside the EPUB chapter iframe and container in paginated reading, preventing rubber-band peek-through and white flash on mobile |
| 8.13.18 | Fix horizontal column scrolling: eliminate unwanted horizontal scrollbar and column splitting in paginated mode by enforcing `overflow: hidden` on iframe, `html`, and `body` (preventing browser auto-conversion of `overflow-x` to `auto` when `overflow-y: hidden`) |
| 8.13.19 | Fix iOS Chrome touch interactions: remove touchmove preventDefault that cancelled clicks/taps on iOS Chrome, set `touch-action: manipulation` to prevent browser history navigation hijacking, handle `touchcancel`, and broaden tap gesture tolerance |
| 8.13.20 | Fix blank pages & mobile middle tap: restore epub.js multi-column pagination by removing `overflow: hidden` on iframe/html/body, suppress scrollbars cleanly via `scrollbar-width: none` and webkit pseudo-elements, prevent synthetic double-tap toggle on iOS Chrome, and add viewer container click fallback |
| 8.13.21 | Comprehensive mobile optimizations: multi-touch gesture guard preventing pinch release from triggering page turns/taps, orientationchange re-pagination, iOS auto-zoom prevention (16px minimum font size for TTS and AI inputs), dynamic viewport height (`dvh`), overscroll containment on drawers/sheets, interactive-widget viewport configuration, and standalone iOS PWA meta tags |
| 8.13.22 | Fix iOS Chrome black circle rendering glitch & reader interaction lock: enforce `fill="none"` SVG presentation attributes on goal-ring and reader icons, remove premature touch release return in `onTouchEnd` restoring edge taps & immersive full-screen toggle, and enforce `min-width: 0` truncation on `.reader-heading` so long book titles never push reader toolbar buttons off-screen |
| 8.13.23 | Fix middle tap immersive toggle on iOS Chrome: add 400ms debounce on `setImmersive` to prevent synthetic click reversals, broaden tap tolerance to 48px/750ms without swipe dead zone, add direct touch listeners on chapter `doc`, register container tap fallback on `#viewer`, and enforce `display: none !important` on `.reader.immersive .reader-toolbar` |
| 8.13.24 | Isolate viewer tap fallback from PDF stage: guard `#viewer` touch and click handlers against `.pdf-stage` and active PDF mode so PDF edge taps and double-tap zoom resets never fire duplicate events, adjust tap ratio to avoid adding `frameLeft` when events originate in top-level window, and lower `setImmersive` debounce to 250ms |
| 8.13.25 | Root-cause fix for the iOS Chrome middle tap: the click that Chrome synthesises after a touch is paired with its touch by **state** (`touchConsumedGesture`), not by the wall clock. It arrives late there because it queues behind the chapter re-layout (`rendition.resize()` → epub.js `clear()` + `display()`) that `setImmersive(true)` starts, so every time window (350→650 ms, debounce 400→250 ms) let it through and it toggled immersive straight back off. Flag set on a consumed tap/swipe, cleared by the next `touchstart` or a non-touch `pointerdown`; `suppressClickUntil` kept as is (PDF stage untouched). Regression test: touch tap + 900 ms late click in `tests/e2e_test.py` §7 | — needs the owner's real-device check on iPhone Chrome |
| 8.13.26 | On-device tap trace (`?taptrace` in the URL or borg-debug `{"taps":true}`): a fixed overlay lists every reader input event (touchstart/end/cancel, click, pointerdown, source main/frame), what `handleTap`/`setImmersive` did with it and, 1 s after each toggle, the `immersive` class, the toolbar's computed display and the viewer height — so iOS Chrome can be diagnosed without a console. Footer shows the release time (`meta app-released`, device time zone) next to the version | — owner tests 8.13.25 on iPhone Chrome and, if the bars still stay, screenshots the overlay |

## Open items (not started unless the owner says so)
0. **Deploy blocked (owner):** since run 40168747739 (the 8.13.24 docs commit, 2026-10-25 06:20 UTC) the deploy job fails
   at «Pull branch on the server» (cPanel UAPI `VersionControl/update`), also on rerun; tests pass. Two pushes a minute
   apart (06:19 / 06:20) probably left the server clone with a stale `.git/index.lock` or an unfinished pull. Check cPanel →
   Git Version Control → Manage → Pull or Deploy for the real error, clear it, then rerun the failed job of run 40172498038
   (`gh run rerun 40172498038 -R ArashDoDo2/borg-reader --failed`) or push the next commit.
1. **Verify on device (owner):** the owner reported the bars still stay on iPhone Chrome on 2026-10-25 18:49 AEDT, but no deploy
   had succeeded since 8.13.24 at that time, so 8.13.25 was most likely not what was tested (the footer now shows version +
   release time to settle this). Edge on iPhone works. Next: with 8.13.26 live, open `https://parsun.com/borg/?taptrace` in
   Chrome, open a book, tap the middle once, wait 2 s, screenshot the overlay. Its lines decide the fix: a `click … handled` →
   `setImmersive(false)` line means the JS pairing missed a path; `1s later: class=true toolbar display=none` with bars visible
   on screen means a WKWebView repaint problem; `touchcancel` lines mean Chrome's gesture recogniser eats the tap.
2. **Waiting for the owner:** screenshot of the WebKit `NotFoundError` details from «خطاهای کاربران» (seen in 8.6.5);
   real-device check on iPhone/iPad of the 8.6–8.10 mobile changes (taps fixed at the root in 8.6.1).
3. **Known, not fixed:** with margin «زیاد», swipes may not turn pages on phones.
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
