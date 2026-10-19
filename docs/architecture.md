# Architecture — Borg (بُرگ)

Status: describes the code in `src/` as of v8.8.1 (2026-10-20). Project status and open items: `docs/context/status.md`;
reader pitfalls: `docs/context/reader-engine-notes.md`. Product goals, roadmap and ideas live in the
private product spec (not in this repo); this file is the technical source of truth.

## 1. Overview
A static PWA. The browser does everything: parse EPUB, render, store books and progress.
Since v7 an optional PHP API (`src/api/`) adds accounts, cloud library, progress sync and a public catalog; without an account the app stays fully local.

```
index.html ──> assets/js/app.js ──> epub.js (+ JSZip) ──> book rendered in a sandboxed iframe
                     │                                        └─ reader-fonts.css injected (Vazirmatn)
                     ├─> pdf.js (v8.8, import() on first PDF use) ──> PDF pages on canvases + text layer
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
| `src/assets/vendor/pdfjs/` | v8.8: pdf.js 4.10.38 **legacy** build, unmodified (`pdf.min.mjs`, `pdf.worker.min.mjs`, `cmaps/`, `standard_fonts/`, Apache-2.0 `LICENSE`). Loaded with `import()` only when a PDF is imported or opened; not in `APP_SHELL` (ADR 0009). |
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

v8.8 PDFs are records in the same store with optional `format: "pdf"` (absent = EPUB) and `pages` (page count); title
and author come from the PDF info dictionary (`Title`/`Author`, else file name / «نویسنده نامشخص»), `cover` is page 1
as a JPEG data URL. Their progress `cfi` is `pdf:<page>` (first visible page) with `percentage` = last visible page /
pages × 100; page bookmarks are `annotations` with `cfi: "pdf:<page>"`. No `DB_VERSION` change.
Books also carry `serverId`, `catalogStatus`, reading `status` and `shelves` when synced. When a browser refuses to
store Blobs, `file` is null and `fileBuffer` (ArrayBuffer) + `fileName` are stored instead (`fileOf()`).
v8.3 text cleanup adds `sourceHash` (SHA-256 of the file as first imported, so re-importing the original is still a
duplicate), `cleanedAt`, and for synced books `cleanPending` (new file not yet swapped in on the server) +
`cleanAnns` (uids of re-anchored annotations to re-push after the swap).

localStorage: `ketabkhane-reader-preferences` → `{font, lineHeight, fontSize, theme (light/sepia/dark/black/auto),
align, librarySort, libraryFilter, dailyGoal, margin, para, flow, spread, digits, taps, wake, ttsRate, ttsVoice, pdfFit
("page" | "width"; unset = width on phones, page on desktop), pdfInvert (dark/black themes invert PDF pages, default on)}`;
`ketab-theme`; `borg-clean-rules` (v8.3 cleanup rules: `[{id, type: "phrase" | "line", text} | {id, type: "telegram"}]`;
mirrored to the server for an admin since v8.7, §4a);
`borg-reading-days`/`-other`/`-pushed` (stats); `borg-device-id`; `borg-debug` (diagnostics only,
read by the WebKit probe; empty for real users).

Migration rules:
- Never delete/recreate the database; bump `DB_VERSION` and migrate step by step in `onupgradeneeded`.
- A migration must be tested on real data from the previous version and must leave old data intact on failure.

## 4. Key flows
- **Import:** validate extension/size → `extractMetadata` (opens with epub.js; listens for `openFailed`, 20 s timeout; waits for background loads before `destroy`) → store record. Errors are `UserError` with a Persian message; nothing is stored on failure.
  v8.8: the file inputs, the share target and "open with" (`launchQueue`, manifest `file_handlers`) also accept PDFs.
  `sniffPdf()` decides by the bytes (`%PDF-` in the first 1 KB), not the name; a PDF goes through `extractPdfMetadata`
  (pdf.js, 30 s timeout; password-protected / broken files → `UserError`), duplicates are refused by SHA-256 like EPUBs.
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
- **Library search (v8.5):** `#searchInput` filters the grid live (180 ms debounce) by title, author, publisher and
  shelf names, combined with the filter chips and sort. Query and fields go through `libFold()`: `normalizeForSearch`
  (ی/ي/ى, ک/ك, RLM half-space) then spaces/ZWNJ/bidi marks/tatweel/harakat dropped, Persian/Arabic digits → 0-9,
  Latin lower-cased; the match is marked in title/author (`libMarkHtml`, built from `esc()` pieces). Shows «N کتاب
  پیدا شد» or an empty state with a clear button; × and Escape clear; "/" focuses it (library view, reader closed, no
  dialog, not typing). The «ادامهٔ مطالعه» shelf is hidden while searching. The query lives in sessionStorage
  `borg-library-search` only.
- **Updates (v6.2):** when a new Service Worker takes control of a page that already had one, a banner offers a reload;
  `reg.update()` runs whenever the app becomes visible.
- **Catalog book page (v8.1):** `#viewBook` (tab "catalog" stays selected), deep link `#catalog/book/<id>`, catalog
  `#catalog`; `popstate` routes between them. Large cover, title, author link (→ catalog filtered by that author, with
  a removable chip), description clamped to 5 lines with «بیشتر بخوانید» shown only when it is really cut off,
  metadata table (category, language, publisher, date, size, submitter — only rows with a value), breadcrumb
  (catalog / category / title), «کتاب‌های مشابه» (same author first, then same category, max 6).
  The flat category chips of 8.1 were replaced by the v8.10 category navigator (§4f); a book without `categories` still
  links its free-text `category`, shown as a removable chip like the author filter.
- **Online preview (v8.1):** «مطالعهٔ آنلاین» fetches `book-file` into memory and opens the reader with an ephemeral
  record `{id: "preview-<catalogId>", preview: true}`. Nothing is written: no `books` record or cached locations,
  no progress (`saveProgress` and `saveAnnotation` ignore `preview-` ids as a last guard), no bookmarks/highlights/notes
  (toast instead), no status change, no reading-time stats, so nothing is synced. A banner offers «افزودن به
  کتابخانه»; adding reuses the in-memory file, then the reader switches to the stored copy and saves the position from
  then on. Closing the preview returns to the book page.
- **Add from the catalog (v8.1):** the main button adds in one click; the ▾ menu lists existing shelves (and can create
  one) and puts the book on the checked shelves after `catalog-add` + download. `catalog-submit` asks for an optional
  category; admins edit catalog books in the book details panel of the admin «کتاب‌ها» tab (§4e).
- **Race guard:** `openToken` invalidates async work when the reader is closed or another book opens.

## 4a. Text cleanup («پاک‌سازی متن», v8.3)
Removes unwanted phrases (typically Telegram channel links) from the **EPUB file itself**, for all books; only the
matched text is removed and no original is kept (owner's decisions, `docs/context/status.md`).
- **UI:** library toolbar button «🧹 پاک‌سازی متن» → `#cleanDialog` (library level, works without an account; the
  reader «Aa» panel stays display-only). Add an exact phrase (≥ 4 characters, else rejected) or the preset
  «لینک‌های تلگرام»: `t.me/…`, `telegram.me/…`, `telegram.dog/…` URLs (with or without scheme) and `@handle`
  (5–32 `[A-Za-z0-9_]`, not part of an e-mail address). Rules live in localStorage `borg-clean-rules`.
- **Search («جستجو در همهٔ کتاب‌ها»):** for each local book `cleanEpub(buf, rules, false)` opens the file with JSZip,
  reads the OPF manifest (container.xml → rootfile), parses each XHTML/HTML item with `DOMParser` (XHTML as XML; an
  XHTML file that is not well-formed is parsed as HTML and re-serialised as XML) and removes matches inside text
  nodes (not `script`/`style`; a phrase split across elements is not matched). Results are grouped per book: count,
  up to 5 snippets with `<mark>`, a checkbox per book (checked). A rule with > 300 matches in one book or > 2% of its
  text is flagged «احتمالاً بیش از حد کلی است» and excluded unless its checkbox is ticked. Progress bar; the loop
  yields between books.
- **Apply (`applyCleanToBook`):** changed documents are written back (XMLSerializer for XHTML, original XML
  declaration kept), a new zip is built (`mimetype` first and STORED, everything else DEFLATE), stored in the same
  book record through `put()` (ArrayBuffer fallback still applies) with the new `size`/`contentHash`, cached
  `locations` dropped; id, title, author, cover, shelves, status and progress (CFI kept as is) stay. A link whose text
  became empty is removed with it. Annotations with a `quote` are re-anchored (`reanchorAnnotations`): both files are
  opened with epub.js, the quote is searched in the same spine item of the new file (whitespace-insensitive; RLM/ZWNJ,
  digits and ي/ك folded; nearest occurrence to the old position) and `section.cfiFromRange()` gives the new CFI;
  if the quote is gone the old CFI is kept. Updated annotations are saved with a new `updatedAt` (dirty → synced).
  If the book is open in the reader it is reopened at the current position.
- **Import:** `importFile` runs the saved rules on the new book before it is uploaded (`autoCleanImported`); rules
  that look too broad for that book are skipped silently; the toast then says «… N مورد حذف شد».
- **Server swap:** see §10 (`book-replace`).
- **Server copy for the admin (v8.7):** when the logged-in user is an admin, every change to the rules (add, delete,
  Telegram preset — all go through `saveCleanRules`) POSTs the full list to `clean-rules` as `{rules: [{id, type,
  text?}]}`, debounced 1.2 s (`scheduleCleanPush` → `pushCleanRules`); the server applies these rules to books sent
  with an API key. On admin login / app start (`setAccount` → `pullCleanRules`) the client GETs `clean-rules`: if the
  local list is empty and the server has rules they are adopted (saved with `fromServer`, no push back); if both exist
  and differ, **local wins** and is pushed. Offline the push is kept (`cleanSync.dirty`) and retried on `online` or
  when the dialog opens; real failures go to `reportError("api", …, "clean-rules:push|get")`. Limitation: a list
  emptied on one device while offline is refilled from the server at the next login (empty local = adopt). The dialog
  shows admins «قانون‌ها برای کتاب‌های ارسالی با API هم روی سرور اعمال می‌شوند» with a state chip (`#cleanSyncState`:
  pending / saving / saved / offline / failed / not supported when the route answers 404). Non-admins: local only, as before.

- **PDFs (v8.8):** never cleaned. `runCleanSearch` lists only EPUBs, `autoCleanImported`/`applyCleanToBook` return 0
  for `format: "pdf"`, `repairMetadata` skips them, so JSZip/epub.js never see a PDF.

## 4c. API keys (admin, v8.7, client side)
Admin panel block «کلیدهای API» (`#apiBlock`, tab «کلیدهای API» since v8.9, §4e). Keys let other programs send books straight to the
public catalog through the machine route `api-book` (Bearer token, multipart; documented in `docs/api-books.md`;
the UI never calls it). `loadApiTokens()` (run by `loadAdmin`) GETs `api-tokens` and lists name, created date, last
use (Persian date/time, «هرگز» when 0), use count, review flag («منتظر تأیید» / «انتشار مستقیم»), status; revoked
keys are greyed (`.api-item.revoked`) and have no button. «ساخت کلید» POSTs `api-token-create {name,
require_review}` (name required) and shows the returned plaintext `token` **once** in a read-only field
(`#apiTokenValue`) with «کپی» (Clipboard API, falling back to select + `execCommand`) and the warning «این کلید فقط
همین یک بار نمایش داده می‌شود»; the field is cleared when the admin view is opened again or the admin logs out.
«لغو» asks for confirmation and POSTs `api-token-revoke {id}`. A 404 from `api-tokens` shows «این بخش هنوز روی سرور
فعال نیست.». A curl example built from the site's API URL is shown under the list.

## 4e. Admin panel (v8.9, client side)
Built for thousands of server books (≈3,000/year). `#viewAdmin` has a sticky sub-navigation `#adminTabs`
(`[data-admin-tab]` → `section[data-admin-panel]`, ids `admTab-<tab>` / `admPanel-<tab>`): «نمای کلی» `overview`,
«صف تأیید» `queue` (badge `#admBadgeQueue` = pending), «کتاب‌ها» `books`, «کاربران» `users`, «دعوت‌ها» `invites`,
«کلیدهای API» `api`, «خطاها» `errors` (badge `#admBadgeErrors` = error groups), «تنظیمات» `settings`. The older blocks
moved into the tabs with their ids and behaviour (`#userList`, `#inviteNote`/`#createInvite`, `#apiBlock`,
`#errorsBlock`, `#settingApproval`, `#pendingList`); the long unpaginated «کتابخانهٔ عمومی» / «همهٔ کتاب‌های روی سرور»
lists and the `#catalogEditDialog` editor were removed (replaced by the books tab and its details panel).
- **Routing:** deep link `#admin/<tab>`; the books tab adds its state as a query (`#admin/books?q=…&status=pending&page=2`,
  only non-default values). `showView("admin")` → `openAdmin()` reads the hash (`admReadHash`), else keeps the last tab
  and writes it with `replaceState`; a tab click pushes a history entry (back returns to the previous tab), filter and
  page changes replace it. `routeFromHash()` opens `#admin…` for admins; logout clears it.
- **Lazy loading:** `loadAdmin()` = `admin-stats` (overview cards + both badges) + the active tab's loader only
  (`loadQueue`, `loadAdminBooks`, `loadUsers`, `loadInvites`, `loadApiTokens`, `loadErrors`, `loadSettings`). It is also the
  refresh after every `adminAction`.
- **Overview:** the 7 stat cards (`#adminStats .stat`) and shortcuts (`[data-adm-go]`, optional `data-adm-filter`):
  queue, books without category (`category=__none__`), without cover, sent with an API key (`uploader=api`), rejected,
  errors, invites.
- **Books tab:** `GET admin-books` with `q, status (any|approved|pending|rejected|none), category (exact, __none__ = empty),
  format, uploader (id or api), no_cover=1, sort (date|title|size|status), dir, page, per=50` → `{books, total, page, per,
  facets}`. Search is debounced 300 ms; every filter/sort change goes back to page 1; a stale response is dropped
  (`adm.seq`). Category and uploader selects and the category suggestions (`#admCatList`) come from `facets`. Desktop:
  compact table `#admBooks` (checkbox, lazy 32×44 cover, title + author `dir=auto`, format chip, category, status chip,
  uploader or «API» chip for `source: "api"`, Persian date, size, `in_libraries`), pager «قبلی/بعدی» and «صفحهٔ X از Y ·
  N کتاب». ≤700 px: the same rows become grid list rows (checkbox · cover · title + one line of chips) and the filters
  move into a bottom sheet (`#admFilters.open`, backdrop, Escape, «نمایش نتیجه‌ها») opened by «فیلترها» with the
  active-filter count. Servers without `admin-books` (404) get the same result built in the browser from `books-all` +
  `catalog-all` (`admLegacyBooks`; no `source`/uploader ids there).
- **Selection and bulk actions:** row checkboxes (shift-click selects a range), «انتخاب همهٔ این صفحه» (`#admSelPage`),
  then «انتخاب همهٔ N نتیجه» (`#admSelectAll`, `adm.all`). The fixed bar `#admBulkBar` shows the count and `POST
  admin-bulk {action, ids}` — or `{action, filter: {q, status, category, format, uploader, no_cover}}` for all results —
  with actions approve / reject / set_category (`#admCatDialog`, a small form with the category datalist) /
  clear_category / add_catalog / remove_catalog / delete. Delete, remove, reject and clear-category (and any action on
  all results) ask with the exact count. Afterwards: toast «انجام شد: N کتاب · M مورد نادیده گرفته شد» from
  `{affected, skipped}`, selection cleared, stats and the page reloaded. Changing page/filters clears the selection.
- **Details panel:** clicking a row (or Enter) opens `#admDetail`, a modal `<dialog>` docked to the left edge at full
  height on desktop and a bottom sheet on phones: bigger cover, chips, uploader/submitter/date/size/libraries, status
  actions (pending → `catalog-review`; remove → `catalog-remove`; add / approve a rejected book → `admin-bulk` with one
  id), the edit form (title, author, category with datalist, description, publisher, published, language) saved through
  `catalog-update` for catalog books or `book-meta` (no category/description) for books outside the catalog,
  «مطالعهٔ آنلاین» (`adminPreview`: the same ephemeral `preview-<id>` reader as the catalog page), download, delete
  (`book-delete`).
- **Review queue:** `catalog-pending`; review mode shows one book (cover, metadata table, description, submitter, «کتاب
  i از n») with تأیید / رد / تعیین دسته / قبلی / بعدی plus preview, details and download. Keys (only on this tab, not
  while typing or with a dialog open): A approve, R reject (`catalog-review`), C category (`#admCatDialog` →
  `catalog-update {book_id, category}`), J or ← next, K or → previous (← is "forward" in RTL, as in the reader). A
  reviewed book leaves the local list and the next one is shown without a reload. «فهرست» switches to the compact
  `#pendingList` (approve/reject/details per row).
- **CSS:** one block `/* v8.9 admin */` at the end of `app.css` (palette variables, `body.dark` variants).

## 4f. Categories (v8.10, client side)
A fixed two-level list (category → subcategory) kept by the admin; a book can have several. `GET categories` →
`{categories: [{id, name, parent_id, sort, count, count_all (admins)}]}`, flat, each top-level entry followed by its children
(a top-level count includes its children). `loadCategories()` caches it for 5 min (dedupes concurrent calls, `force` after an
admin change); a 404 counts as an empty list. **An empty list switches every category UI off** and the free-text
`category` flows of 8.1/8.9 stay (prompt-based suggestion, details text field, «تعیین/حذف دسته», `category=` filter).
Books carry `categories: [{id, name, parent_id, parent_name}]` (`bookCats()`; `catPathOf()` → «ادبیات › شعر»).
- **Public catalog:** `catalogFilter.cat` → `catalog&cat=ID` (a top-level id includes its children); old whole-list
  servers filter locally (`bookInCat`). ≥ 900 px: `#catNav` column on the right (RTL) with «همهٔ کتاب‌ها» and the top
  categories (count, chevron when they have children); hover (fine pointer) or click opens `#catNavPanel` to its left:
  «همهٔ کتاب‌های X ›» + subcategories in two columns; Escape / leaving / clicking outside closes it. Below 900 px:
  «دسته‌ها» (`#catSheetOpen`, shows the chosen name) opens `#catSheet` (bottom sheet on phones) with a drill-down: top
  level → subcategories, back arrow, «همه…» at each level; it reopens at the chosen category's level. The choice is
  shown as `#catCrumbs` «کتابخانهٔ عمومی › ادبیات › شعر» with «✕ همهٔ دسته‌ها». Hash `#catalog?cat=ID`: a choice pushes a
  history entry; `routeFromHash` reads it (back, refresh, deep link). Navigator and button are hidden when the list is empty.
- **Book page:** row «دسته‌ها» with one link per category («ادبیات › شعر» → `#catalog?cat=ID`); the breadcrumb uses the
  first category. «کتاب‌های مشابه» (`relatedSteps`): same subcategory (≤ 2), same author, same top category, old
  free-text category — one `per=6` page each until 6 books.
- **Admin «دسته‌ها» tab** (`#admin/cats`, after «کتاب‌ها»): tree `#catTree` with counts (`count_all`); add a top category
  (`#catAddTop`), «+ زیردسته», rename inline (Enter saves, Escape cancels), «انتقال به…» for a subcategory (other top
  categories) — all `category-save {id?, name, parent_id}`; ↑/↓ → `category-order {parent_id, ids}` (the siblings in their
  new order); «حذف» → `#catDelDialog`: unlink (`{id}`) or move the books (`{id, move_to}`), toast from `{moved, unlinked}`.
  "No parent" is sent as the value the server uses for its top-level entries (null when unknown). Persian errors
  (`CAT_ERRORS`): duplicate, too_deep, has_children (explained in the dialog), missing/too long name, not found.
- **Picker** (`makeCatPicker`): chips with ×, search (`libFold`), options grouped by parent (the parent stays as the group
  head while searching), max 10 (the rest disabled with a note), Enter in the search picks the first match. Used in the
  details panel (replaces the text field; saved as `catalog-update {…, categories}` without `category`), in
  `#catPickDialog` (review queue «C» → `catalog-update {book_id, categories}`; single mode for bulk) and in the user's
  suggestion dialog `#submitDialog` (`catalog-submit {book_id, description, categories}`).
- **Books tab:** `#admFCategory` becomes the tree (parents first, children indented; `cat=ID` / `cat=none`, kept in the hash;
  «کتاب‌های بدون دسته» → `cat=none`). Bulk «افزودن دسته…» / «برداشتن دسته…» pick one category → `admin-bulk {action:
  add_category | remove_category, category_id, ids | filter}` (remove asks with the name and count).
- **CSS:** one block `/* v8.10 categories */` at the end of `app.css`.

## 4b. Reader engine (v8)
Details and history of each workaround: `docs/context/reader-engine-notes.md`.
- **Open sequence:** read file → `ePub(bytes)` → `detectBookDirection()` sets `bookDir` (ADR 0008) →
  `patchTextWidth("rects")` (Safari fix) → `renderTo("viewer", {flow, spread, defaultDirection: bookDir})` →
  serialize hook adds the load veil (after `book.opened`) → content hooks (`onBookContent`: half-space fix,
  Persian digits, footnote/image click handlers; fonts stylesheet then veil removal) → theme (`applyReaderStyles`).
- **Navigation:** `goLeft/goRight` map to forward/back through `bookIsRtl()`. Mouse wheel/trackpad (8.6.3, `onReaderWheel`, listeners on the book document and `#viewer`): summed deltas ≥ 40 px turn one page, then locked until 250 ms of quiet; vertical = forward/back, horizontal = physical; off in scrolled flow or with pref `wheel: false`. Taps are recognised on `touchend`
  (iOS) and on `click` (mouse) through one `handleTap()`; edge zones 25%, middle tap toggles immersive.
- **Reading features:** TTS (`speechSynthesis`, sentence queue with `.tts-hl` highlight, follows pages),
  footnote popups, word lookup / copy / quote card from the selection bar, highlights in 4 colours, notes,
  multiple bookmarks, in-book search with Persian normalisation, image viewer, wake lock, layout options
  (margin, paragraph spacing, paged/scrolled, single/two columns), 9 font choices, Persian digits option.
- **TOC and header (8.2):** nested entries up to 3 levels (`.toc-sub`), labels `dir="auto"`. The header label shows the
  matching TOC title (deepest entry for the file; `#fragment` entries matched against the end of the visible page),
  falling back to «بخش N»; the matching entry gets `.active`/`aria-current`. TOC hrefs are resolved against the nav/NCX
  document's own path to spine hrefs (`resolveTocHref`, v8.4) before they are rendered, clicked or matched.
- **Seek bar (8.2):** `#bookSeek` range 0–1000 over `book.locations`, direction = `bookDir`, disabled until locations
  are ready; dragging previews chapter + percentage, release navigates via `jumpTo()`. Shows «٪X از کتاب · حدود N
  دقیقه تا پایان کتاب».
- **Jumps (8.2):** every non-page-turn navigation goes through `jumpTo(target)` (TOC, search, notes list, footnote
  "go to", seek bar; internal links record their origin via a click listener). It keeps a stack (max 10) and shows
  `#backChip` «↩ برگشت به صفحهٔ قبل», placed above the bottom bar by `placeBackChip()`; hidden after 5 page turns.
- **First-run guide (8.2):** overlay `role="dialog"` once per device (`borg-guide-seen`), wording from `bookIsRtl()`
  and the taps/flow prefs; reopen from settings. Tests pre-set the key in `new_ctx` (`show_guide=True` to test it).

## 4d. PDF reader (v8.8, ADR 0009)
Text-based PDF magazines open in their original layout inside the same `#reader` shell; `#reader.is-pdf` switches the
UI. `openReader()` hands a `format: "pdf"` record (or one whose bytes are a PDF) to `openPdfReader()`; `rendition` and
`bookInstance` stay `null` and the state lives in `pdf` (document, pages, zoom, canvas/text caches). `closePdf()` cancels
renders, frees canvases and destroys the pdf.js worker.
- **Loading:** `loadPdfJs()` = `import("assets/vendor/pdfjs/pdf.min.mjs")`, worker `pdf.worker.min.mjs` (module
  worker, `worker-src 'self'`). Options: `isEvalSupported: false`, `enableXfa: false`, vendored `cMapUrl` and
  `standardFontDataUrl`; PDF scripting is never enabled. Offline before the first use → toast, nothing breaks.
- **Layout (`pdfRender`):** `.pdf-stage` (scrolls when the page is larger) > `.pdf-spread` > `.pdf-page` (canvas +
  pdf.js `TextLayer`, `--scale-factor`). Scale = fit («اندازهٔ صفحه»: whole page, desktop default; «عرض صفحه»: viewer
  width, phone default) × zoom (0.5–5). Two pages side by side when «ستون‌ها» is «خودکار» and the viewer is ≥ 800 px
  wide and landscape (page 1 alone, then 2–3, 4–5…; RTL books put the lower page on the right). Canvases are drawn at
  `devicePixelRatio` (≤ 3, ≤ 10 M pixels each) with cancellable render tasks; the neighbouring spreads are pre-rendered
  when not zoomed in; every other canvas is released (`width = 0`) and its page `cleanup()`ed. Re-laid out on viewer
  resize (`ResizeObserver`), immersive mode, fit/spread/zoom changes.
- **Direction:** `detectPdfDirection()` counts Arabic-script vs Latin letters in the text of the first ≤ 6 pages (like
  `detectBookDirection`, cached in `dirCache`); «جهت این کتاب» overrides it. Image-only PDFs fall back to RTL.
- **Navigation:** the existing inputs — edge taps (`handleTap`, page coordinates instead of iframe ones), swipes,
  arrows/PageUp/PageDown/Space, mouse wheel (`onReaderWheel`; a page larger than the viewer scrolls first and turns at
  its edge with a new gesture), prev/next buttons — call `goForward/goBack` → `pdfStep()`. TOC from `getOutline()`
  (destinations resolved to page numbers, nested to 3 levels); TOC, search results, bookmarks, the seek bar (page based)
  and the back chip go through `jumpTo("pdf:<page>")` / `safeDisplay`. Header = the outline entry for the page or
  «صفحهٔ N از M»; bottom bar shows the page and the time left in the book.
- **Zoom:** «−/+» in «Aa», ctrl+wheel / trackpad pinch (live CSS preview, re-rendered after 180 ms), two-finger pinch on
  touch (`touch-action: pan-x pan-y`, so the browser does not zoom the app); the point under the fingers/cursor stays in
  place; a zoomed page pans (swipes pan instead of turning); a double tap/click in the middle resets the zoom.
- **Search:** text of each page from `getTextContent()` (cached per page), folded by `pdfFold` (NFKC for Arabic
  presentation forms, ی/ک/ه variants, digits, half-spaces/harakat dropped, lower case); for Arabic-script queries the
  reversed query is searched too, because some PDFs store glyphs in visual order. Max 100 results; a result opens its
  page and marks the match in the text layer (`.pdf-hit`).
- **Themes:** light; sepia tints the page (`mix-blend-mode: multiply`); dark/black invert the page canvas
  (`invert(1) hue-rotate(180deg)`, can be switched off: pref `pdfInvert`), the chrome follows the theme.
- **Settings («Aa»):** font, size, line height, alignment, paragraph spacing, margin, flow and Persian digits
  (`[data-epub-only]`) are hidden for PDFs; zoom, fit and invert (`[data-pdf-only]`) are shown. Read aloud is hidden.
- **Not in v1:** highlights/notes on PDF text, TTS, text cleanup, digits, links/forms inside the PDF. Page bookmarks work.

## 5. Service Worker
- Install: pre-cache `APP_SHELL` (every file in `src/` the app needs, including vendor, font and `offline.html`).
- Navigation requests: network first, cached copy (then `index.html`, then `offline.html`) when offline.
- Other same-origin GETs: cache first, network fallback, cached on success.
- Activate: delete every cache whose name is not `CACHE_NAME`.
- Scope is the folder the SW is served from, so `/borg/` and `/borg-test/` have independent caches.
- Books are **not** in the SW cache; they live in IndexedDB.
- v8.8: pdf.js (`assets/vendor/pdfjs/…`) is deliberately **not** in `APP_SHELL`; the first PDF import/open fetches it and
  the cache-first asset handler keeps it (module, worker, the CMaps/fonts a PDF needed) until the next `CACHE_NAME`.

## 6. Deployment
Host: shared cPanel (`parsun.com`, user `parsunco`), HTTPS. Single branch `main` (ADR 0007).
1. `git push origin main` triggers `.github/workflows/deploy.yml`.
2. Job `test`: `tests/api_test.py` + `tests/e2e_test.py` (Chromium). Failures are printed as `::error`
   annotations (readable with `gh api repos/ArashDoDo2/borg-reader/check-runs/<job-id>/annotations`).
   Job `webkit` (in parallel): `tests/webkit_e2e.py` in WebKit, retried once as a flake guard (first attempt reports
   `::warning`, the retry `::error title=webkit`); on failure `tests/webkit_probe.py` adds diagnostics.
3. Job `deploy` (needs `test` and `webkit`): first checks that the tested commit is still the head of `main`; if a newer
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
  section 14: direction detection (no-direction Persian book, English book), nested TOC, load veil; section 17: v8.3
  text cleanup (rules, preview, apply, re-anchored highlight, auto-clean on import; server swap is in `api_test.py`).
  Checks assert visibility (geometry), not DOM presence. Use `poll()` (CSP forbids `wait_for_function`).
- e2e section 28 (v8.8 PDF): fixtures printed by Chromium (`page.pdf(outline=True, tagged=True)`: 3 English pages,
  3 Persian RTL pages in Vazirmatn, 120 pages): pdf.js not precached and not loaded before use (runtime-cached after),
  import (card, JPEG cover, badge, `format`/`pages`, duplicate), canvas pixels inside the viewer, text layer + selection,
  outline TOC/header, next/prev, keys, edge taps, wheel, seek + back chip, progress `pdf:N` restored after reload,
  bookmark, search + highlight, settings rows, «+» zoom and pan, double click reset, ctrl+wheel, fit width/page,
  dark invert, two-page spread, cleanup skips PDFs, 120-page responsiveness (one canvas in the DOM), phone 390 px:
  fit width, RTL taps and swipes, Persian search, pinch zoom, pan instead of turn, double tap reset; no errors or
  external requests.
- `tests/webkit_e2e.py` (~19 checks, gates the deploy): WebKit with iPad (820×1180) and iPhone (390×844) touch contexts
  and a desktop one: import (IndexedDB), text laid out inside the viewer (frame width), touch edge taps turn exactly one
  page, middle tap immersive, TOC jump heading in view + header title, veil lifted, seek bar, back chip, English LTR book
  with nested TOC, no page errors; v8.8: a hand-written text PDF on iPhone (canvas pixels, text layer, edge taps).
  `--browser chromium` runs the same checks locally (WebKit cannot run in the agent's container).
- `tests/webkit_probe.py`: WebKit diagnostics per rendering variant (frame width, what is under the viewer); CI runs it
  only when `webkit_e2e.py` fails.
- e2e section 18 (v8.4): TOC hrefs that go up a folder (`nav/nav.xhtml` → `../text/c1.xhtml`, NCX in `toc/`).
- e2e section 30 (v8.9 admin): route mocks of `admin-books` (230 books with facets), `admin-bulk`, `catalog-pending`,
  `catalog-review`, `catalog-update`, `book-meta` and the mocked covers (real admin login): 8 tabs + `#admin/overview`, only
  the active tab loads, 50 rows / «صفحهٔ ۱ از ۵ · ۲۳۰ کتاب» / request params, next page, debounced search (one request),
  every filter and sort in the request URL, refresh keeps tab + filters, reset, shift-click range, set_category dialog
  (ids + category), select page → approve (50 ids), select all results → delete confirms with the exact count and sends
  the filter, skipped count in the toast, details side panel saves via catalog-update / book-meta, review queue A/R/C/J/K/←/→,
  list mode, back between tabs, `#admin/settings` deep link, sticky tabs; 390 px: list rows, filters sheet + active count,
  bulk bar and details sheet fit, no horizontal scroll; no JS errors or external requests. Older admin checks (sections
  10, 12, 15, 19, 27) click the matching sub-tab first; section 12/15 use the books tab (real `admin-books`, or the
  `books-all` + `catalog-all` fallback on an older server).
- e2e section 32 (v8.10 categories, mocks of `categories`, `catalog`, `category-*`, `admin-books`, `admin-bulk`,
  `catalog-update/-pending/-submit`; the invited user with role "admin" patched in): navigator geometry and panel (two
  columns, left of the list), `cat=` requests, hash + back + refresh + deep link, clearing, book page links and similar
  books by subcategory, phone sheet drill-down, admin tree CRUD/order/delete payloads and Persian errors, tree filter in the
  hash, picker (search, grouping, ×, max 10) payloads, bulk add/remove payloads, queue «C», the user's suggestion dialog,
  390 px without horizontal scroll; no JS errors or external requests. `BORG_SHOTS=<dir>` saves screenshots.
  `new_ctx` (and section 31's contexts) answer a 404 from `categories` with an empty list (`categories_fallback`), so the
  older sections do not depend on the server version.
- e2e section 27 (v8.7): API keys and admin cleanup-rule sync with Playwright route mocks of `api-tokens`,
  `api-token-create`, `api-token-revoke` and `clean-rules` (real admin login): adopt server rules when local is empty,
  debounced POST of the full list on add/delete, key shown once + copy, list/revoked state, revoke call, 390 px fit,
  nothing sent when logged out, local wins when both differ.

## 8. Known gaps
- No WebKit/Safari gate in CI yet; iOS behaviour is verified by the owner on devices after release.
- Direction detection follows the majority script; a per-book override exists since 8.6.5 (not synced).
- Very large books: locations generation and direction detection run on open; not profiled on 50 MB+ files.

## 9. Backend (v7)
- **Stack:** PHP 8.1+ (production runs 8.4), PDO, MySQL/MariaDB in production, SQLite in tests. No framework, no Composer.
- **Entry:** `src/api/index.php?r=<route>`; `src/api/lib/` is denied by `.htaccess` and guarded by `BORG_API`.
- **Routes:** `health setup register login logout me password invites` (index.php); `library library-meta
  library-remove upload-init upload-chunk upload-complete book-file book-cover book-meta book-replace progress annotations
  shelves stats catalog catalog-submit catalog-pending catalog-review catalog-remove catalog-add` (books.php);
  `admin-stats users user-update user-reset-password user-delete invite-revoke catalog-all catalog-update
  books-all admin-books admin-bulk book-delete settings client-error errors errors-clear` (admin.php); `api-tokens api-token-create
  api-token-revoke clean-rules api-book` (apibook.php, v8.7; cleanup engine in clean.php).
- **Config:** `/home/<user>/borg-config/config.php` (outside the web root; `BORG_CONFIG` overrides in tests):
  `db_dsn`, `db_user`, `db_pass`, `setup_key`, optional `storage_dir` (default `~/borg-storage`), `max_book_bytes`,
  `catalog_requires_approval` (default true), `session_days`.
- **Schema:** created/migrated automatically on first request (`schema_migrations`); DDL is portable via `{AI}`/`{TXT}`.
  Migrations: 1 users, sessions, invites, login_attempts, books (unique `sha256`), user_books, catalog, progress,
  uploads; 2 annotations, settings; 3 user_books status/shelves/meta_updated_at, shelves, reading_days.
  Migration 4 adds `books.publisher` (≤300), `books.published` (≤40, free-text date/year from EPUB metadata) and
  `catalog.category` (≤80), all `NOT NULL DEFAULT ''`. `health` reports `schema_version`.
  Migration 5 (v8.4) adds `client_errors` (id, created_at, user_id `0` = anonymous, app_version ≤20, kind ≤20, message ≤500,
  source ≤300, stack ≤4000, url_path ≤300, user_agent ≤300, count, fingerprint = SHA-256 of kind+message+source+app_version,
  last_seen), indexed on (fingerprint, created_at) and last_seen.
  Migration 6 (v8.7) adds `api_tokens` (id, name ≤80, token_hash = SHA-256 of the key (unique), created_by = admin id,
  created_at, last_used_at, uses, revoked, require_review; counters/flags `NOT NULL DEFAULT 0`) and `settings_text`
  (name, value TEXT) for settings too long for `settings.value` VARCHAR(500) — the cleanup rules live there as
  `clean_rules`.
  Migration 7 (PDF support) adds `books.format` VARCHAR(8) `NOT NULL DEFAULT 'epub'` (`epub`|`pdf`); existing rows are EPUBs.
  Migration 8 (large catalogs, ~3k books/year, target 10k rows) adds `books.source` VARCHAR(8) `NOT NULL DEFAULT 'app'`
  (`app` = upload flow, `api` = `api-book`), `books.search_key` VARCHAR(1000) `NOT NULL DEFAULT ''` and the indexes
  `books_created` (books.created_at), `books_format` (books.format), `catalog_status`, `catalog_category`,
  `user_books_book` (user_books.book_id). Existing rows get `source = 'app'` and an empty `search_key`.
- **Auth:** invite-only. First admin via `setup` (needs `setup_key`, only while no users exist). Session token in an
  HttpOnly/Secure/SameSite=Lax cookie scoped to the app path; DB stores only its SHA-256. Mutating routes need the
  per-session CSRF token in `X-CSRF-Token`; JSON routes reject non-JSON bodies. Login/register/setup are rate limited.
- **Books:** chunked upload (`upload-init` → `upload-chunk` × n → `upload-complete`), chunk ≤ min(2 MB, 80% of PHP
  limits). Server verifies size, SHA-256 and EPUB structure (no extraction), stores `~/borg-storage/books/xx/<sha>.epub`.
  **PDFs (text-based magazines opened directly):** accepted by the same flow. `upload-init` takes an optional
  `format` hint (`epub`|`pdf`, else 400 `bad_format`); `upload-complete` detects the type from the bytes and ignores
  the hint: a file starting with the ZIP signature is checked as an EPUB (`validate_epub`); otherwise PDF = `%PDF-`
  within the first 1024 bytes and `%%EOF` within the last 2 KB. A `%PDF-` file without `%%EOF`, or a file that is
  neither while the hint says `pdf`, → 422 `not_pdf`; anything else that is not an EPUB → 422 `not_epub` (as before).
  PDFs are stored as `books/xx/<sha>.pdf` (`book_path($sha, $format)`; EPUB naming unchanged; `book_file_of()` looks
  up by `books.format` with the other extension as fallback; `gc_book` removes both). `book-file` sends
  `application/pdf` and `book-<id>.pdf` for PDFs (EPUB unchanged). `book-cover`/`book-meta` covers work the same (the
  client renders and uploads a PDF's first page). Same `max_book_bytes` limit and SHA-256 dedupe.
  Same file = one copy for everyone (dedupe by hash). Downloads/covers go through PHP with a permission check
  (own library, approved catalog entry, or admin). Unreferenced books are garbage-collected.
- **Progress:** last write wins by client timestamp (ms); older writes are ignored.
- **Catalog:** user submissions are `pending` until an admin approves; admin submissions are approved directly.
  `catalog-submit` takes optional `category` (≤80, trimmed). `catalog`, `catalog-pending` and `catalog-all` return
  `category` per book. Admin `catalog-update` updates only the keys present among `description`, `title`, `author`,
  `category`, `publisher`, `published`, `language`; all are validated before anything is written.
- **Book fields:** every book object (`book_out`) has `id, sha256, size, title, author, language, publisher, published,
  has_cover, format` (`format`: `"epub"` or `"pdf"`). `upload-init` accepts optional `language` (≤20), `publisher` (≤300), `published` (≤40); the same keys
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
- **Client error reports (v8.4):** `POST client-error {items: [...]}` (1–20 items, body ≤128 KB). Anonymous visitors
  need no CSRF but are limited to 30 requests/hour per IP; a logged-in session must send `X-CSRF-Token` (403 otherwise)
  and is limited to 120 requests/hour per user (429 `rate_limited`). Item keys: `kind` (`[a-z][a-z0-9_-]{0,19}`, else
  `other`), `message` (required; empty → item skipped), `source`, `stack`, `path`, `app_version`, `user_agent` (falls back
  to the request header), `count` (1–100). Server-side every text is stripped of control characters, URL query
  strings/fragments, e-mails, `token=`/`csrf=`/`password=`/… values and long hex/token-like runs, then truncated to the
  column limit; `path` keeps only the URL path. Same fingerprint on the same UTC day → `count += n`, `last_seen = now`
  instead of a new row. Table capped at `client_errors_max` rows (config, default 5000; oldest `last_seen` deleted).
  Response `{accepted, skipped}`. Admin: `GET errors` (`version`, `kind`, `page`, `per` ≤100) returns groups by
  fingerprint, newest `last_seen` first: `{errors: [{id, fingerprint, kind, app_version, message, source, stack,
  url_path, user_agent, count (sum), first_seen, last_seen, users, anonymous}], total, page, per, versions, kinds}`;
  `POST errors-clear` with `{all: true}`, `{fingerprint}` or `{id}` → `{deleted}`. `admin-stats` has `client_errors`
  (number of groups).
- **API keys and machine uploads (v8.7, guide: `docs/api-books.md`):** the owner's own program uploads EPUBs straight
  into the **public catalog** (never into a library). Admin routes (session; POSTs need CSRF): `GET api-tokens` →
  `{tokens: [{id, name, created_at, last_used_at, uses, revoked, require_review}]}` (never the key or its hash);
  `POST api-token-create {name (1–80), require_review}` → 201 `{id, token, name, require_review, created_at}` — the key
  (`borg_` + 40 base64url chars from `random_bytes(30)`) is shown only here, the DB keeps its SHA-256;
  `POST api-token-revoke {id}` → `{ok}` (404 unknown). `GET clean-rules` → `{rules}`; `POST clean-rules {rules: [{id,
  type: phrase|line|telegram, text?}]}` → `{rules}` stored normalised (≤50 rules, id `[A-Za-z0-9_-]{1,40}` unique,
  phrase/line text trimmed 4–200 characters, telegram without text, no duplicate rule; else 400).
  `POST api-book` (no session, no CSRF): key in `Authorization: Bearer <key>` or `X-Borg-Token`; missing/unknown/revoked
  key, or a key whose creator is no longer an active admin → 401 (`missing_token`/`bad_token`; 30 failures per IP per
  15 min → 429). 60 requests per key per hour (every request counts) → 429. `multipart/form-data`: `file` (required),
  optional `title` ≤500, `author` ≤300, `language` ≤20, `publisher` ≤300, `published` ≤40, `category` ≤80,
  `description` ≤2000. Errors: 415 not multipart; 413 `post_too_large` / `file_too_large_for_php` (PHP
  `post_max_size` / `upload_max_filesize`, value named in the message) / `too_large` (`max_book_bytes`); 400
  `missing_file`, `partial_upload`, `empty_file`, `not_epub` (same `validate_epub` as the upload flow), `not_pdf`
  (same detection as the upload flow), `too_long_*`.
  Steps: validate → read OPF metadata (dc:title/creator/language/publisher/date, date `YYYY-MM-DDT…` cut to the date)
  and the cover (EPUB 3 `properties~="cover-image"`, else EPUB 2 `<meta name="cover">`; png/jpeg/webp/gif ≤1.5 MB, else
  skipped) → apply `clean_rules` → validate again → SHA-256 of the **cleaned** file → dedupe → store with
  `uploaded_by` = key creator and `source = 'api'`. Request fields win over the OPF; title falls back to the file name, then «بدون عنوان».
  Catalog: a new (or previously `rejected`) entry is `approved` with `reviewed_by` = key creator, or `pending` when the
  key has `require_review`; an existing approved/pending entry keeps its status and gets `category`/`description` when
  sent. `last_used_at` is set on every authenticated request, `uses` counts accepted books. Response 201
  `{book, catalog_status, duplicate, cleaned (matches removed), cleanup: applied|no-rules|unavailable, skipped_rules
  (ids judged too broad for this book)}`. Without ZipArchive/DOM the book is stored unmodified (`unavailable`), metadata
  then comes only from the request. `health` reports `zip` and `dom`. `.htaccess` copies the `Authorization` header to
  `HTTP_AUTHORIZATION` for CGI/FastCGI.
  **PDF via api-book:** detected from the bytes like the upload flow and stored unchanged (no cleanup: `cleanup:
  "not-applicable"`, `cleaned: 0`, `skipped_rules: []`; no server-side cover). Title/author: request fields, else the
  trailer's `/Info` dictionary (`pdf_info_meta`, best effort, never fails: last `/Info N G R` in the last 64 KB, object
  found through a classic `xref` table (following `/Prev`) or by scanning ≤32 MB; literal strings with escapes, hex
  strings, UTF-16BE with BOM, UTF-8, else PDFDocEncoding read as Latin-1; one level of indirect string; skipped for
  encrypted files and objects inside compressed object streams), else the file name without `.pdf`.
  `language`/`publisher`/`published` only from the request.
- **Server-side cleanup (`lib/clean.php`):** a port of the browser's `cleanEpub`/`cleanDocument`/`cleanLines`/
  `epubContentFiles` (§4a) and must stay in step with it. Content documents come from the OPF manifest
  (`application/xhtml+xml`, `text/html`; href URL-decoded and resolved; fallback every `*.x?html?` entry). XHTML is
  parsed with libxml as XML (HTML named entities are turned into numeric ones when the DOCTYPE names an XHTML/MathML
  public id, as browsers resolve them); any libxml error → HTML parsing, like the browser's parsererror fallback. Root =
  `<body>` (XHTML namespace) or the document element. Line rules first (smallest `LINE_BLOCKS` element whose squashed
  text contains the squashed rule text; blocks longer than text + 400 are kept), then phrase rules (literal) and the
  Telegram rule (`TG_SOURCE`, same lookbehinds in PCRE) on text nodes outside script/style; a link left with only
  whitespace and no img/svg/image/video/audio/object is removed. Lengths are UTF-16 code units like JS. Serialisation
  mirrors XMLSerializer (original XML declaration kept, no newline between top-level nodes, `<p></p>`, `<br />`; a
  non-UTF-8 encoding in the declaration becomes utf-8). Like auto-clean on import: a dry run first, rules with no match
  or too broad for the book (> 300 matches, or > 2% of the text; line rules > 10%) are skipped, the rest applied. The
  zip is rebuilt only when something changed: `mimetype` first and STORED, the rest DEFLATE, entry times kept
  (deterministic → the same original gives the same SHA-256), unchanged entries streamed through temp files. Content
  documents over 16 MB are left untouched. Checked byte-for-byte against the browser code in Chromium on test books.
- **Search key (migration 8):** `search_norm()` (lib/books.php) folds like the client's `normalizeForSearch` + library
  search: ي/ى → ی, ك → ک; ZWNJ/ZWJ, LRM/RLM and other bidi marks, BOM, tatweel and harakat (U+064B–065F, U+0670, so
  also the hamza of «هٔ») removed; Persian and Arabic-Indic digits → 0-9; lower case; whitespace runs → one space.
  `books.search_key` = `search_norm(title + " " + author + " " + publisher)`, cut to 1000 characters, never empty (a
  key that folds to nothing is stored as one space; `''` means "not computed yet"). It is written by `insert_book`
  (upload-complete, api-book) and by `update_book_cols` whenever title/author/publisher change (book-meta,
  catalog-update, book-replace's metadata copy). `admin-books` and `catalog` (both modes) first fill up to 500 rows
  whose key is still `''` (rows from before migration 8), in one transaction. A query `q` is folded the same way
  (first 200 characters, at most 10 distinct words) and every word must occur: `search_key LIKE '%word%' ESCAPE '!'`
  per word, AND-ed (`%`, `_`, `!` in the word are escaped, so they match literally).
- **Admin book list (`GET admin-books`, admin):** params `q`; `status` `any` (default) | `approved` | `pending` |
  `rejected` | `none` (not in the catalog); `category` exact, `__none__` = no category (empty or not in the catalog),
  ≤80; `format` `epub`|`pdf`; `uploader` = a user id (that user's uploads through the app; `api-book` books are
  excluded) or `api` (`source = 'api'`); `no_cover=1` (`has_cover = 0`); `sort` `date` (created_at, default) | `title`
  | `size` | `status` (pending, approved, rejected, not in catalog; newest first inside a status); `dir` `asc`|`desc`
  (default `desc` for date/size, `asc` for title/status; ties broken by id in the same direction); `page` (1-based,
  default 1, ≤100000), `per` (1–100, default 50; out-of-range values are clamped). Unknown `status`/`format`/`uploader`/`sort`/
  `dir` → 400 `bad_<param>`, category > 80 → 400 `too_long_category`. Response `{books: [book_out + catalog_status
  (null when not in catalog), category ('' when none), description, submitter (name or null), uploader_id,
  uploader_name, created_at, source, in_libraries (user_books count)], total, page, per, facets: {status: {approved,
  pending, rejected, none}, categories: [{name, count}] (non-empty categories, count desc, top 100), uploaders: [{id
  (user id, or "api"), name ("API" for api), count}] (count desc), formats: {epub, pdf}}}`. Facets are computed for
  `q` only (the other filters are ignored). `books-all` and `catalog-all` are unchanged.
- **Bulk actions (`POST admin-bulk`, admin, CSRF):** `{action, ids: [book ids] | filter: {admin-books params}, category}`.
  `ids` (a list of positive ints or digit strings; duplicates count once) wins when both are sent; neither → 400
  `missing_target`; bad ids → 400 `bad_ids`; unknown action → 400 `bad_action`. At most 5000 books per call
  (`ADMIN_BULK_MAX`): more ids, or a filter matching more → 400 `too_many`. Everything runs in one transaction; files
  of garbage-collected books are removed after the commit. Actions: `approve` / `reject` set the status of existing
  catalog rows that have another status (`reviewed_by`/`reviewed_at` = admin/now); `set_category` (`category` required,
  trimmed, ≤80) / `clear_category` change existing catalog rows whose category differs; `add_catalog` creates approved
  rows (description/category empty, `submitted_by` = `reviewed_by` = admin) for books not in the catalog;
  `remove_catalog` deletes catalog rows, then books in no library are garbage-collected (like `catalog-remove`);
  `delete` = `book-delete` for each (libraries, catalog, progress, annotations, then the book and its files). Response
  `{affected, skipped}`: `affected` = rows actually changed/created/deleted (for `delete`: books deleted), `skipped`
  = the other requested books (unknown id, not in the catalog, already in that state). Nothing is logged.
- **Catalog paging (`GET catalog` with `page`):** approved books only; params `q` (as above), `category` (exact),
  `author` (exact `books.author`), `format` `epub`|`pdf`, `sort` `new` (reviewed_at, submitted_at, id — all desc;
  default) | `title` | `author` (author, title), `page` (default 1, ≤100000), `per` (1–60, default 24). Response `{books (same
  fields as the unpaged list: book_out + description, category, submitter, in_library), total, page, per, categories:
  [{name, count}]}` — `categories` are the non-empty categories of approved books matching `q` (other filters
  ignored), count desc. Bad `sort`/`format` → 400. Without `page` the response is exactly the old full list `{books}`.
- **Validation:** optional string fields that are present but not a string/number → 400 `bad_<field>`; over the
  length limit → 400 `too_long_<field>`.

## 10. Client sync (v7)
- `me` on start (skipped offline); when logged in, `sync()` runs on login, start, import, visibility and reconnect.
- Order: drop local books removed from the account → hash local books → upload missing ones → download missing
  ones (with cover) → reconcile progress both ways by timestamp → push corrected metadata / adopt server metadata.
  v8.1: `upload-init`/`upload-complete` and `book-meta` carry `publisher`/`published`/`language` when known; on sync
  the client fills fields the server lacks (once, `metaExtPush`) and adopts non-empty server values. Books imported
  before v8.1 are re-read once (`metaExt`) to pick these fields up.
- v8.3 cleaned books (`replaceOnServer`): the new file goes through `upload-init/chunk/complete`, then `book-replace`
  `{old_book_id, new_book_id}` moves progress, annotations, shelves (and for an admin the catalog entry and other
  users' copies) to it; the record gets the new `serverId`, then the re-anchored annotations (`cleanAnns`) are saved
  again with fresh timestamps and pushed (the server re-stamps moved annotations, so an earlier push would lose).
  Offline or on failure the book keeps `cleanPending`: `sync()` retries first, skips the book in the normal
  upload/adopt pass, never re-downloads the old server copy, and `syncAnnotations` does not push that book's
  annotations until the swap is done.
- v8.8 PDFs: `upload-init` carries `format: "pdf"` as a hint (the server detects the format from the bytes); a server
  book with `format: "pdf"` (or, when the field is missing, PDF bytes) is stored as a PDF record (`.pdf`,
  `application/pdf`); «مطالعهٔ آنلاین» opens catalog PDFs in the PDF reader the same way (preview rules unchanged).
  Progress `pdf:<page>` and page bookmarks sync like EPUB CFIs.
- Progress is pushed 4 s after the last page turn. Deleting a synced book removes it from the account (confirmation).
- Without an account or offline, everything works locally exactly as before.
- v8.4 error reports: `reportError(kind, err, context)` (top of `app.js`) queues window `error` / `unhandledrejection`
  events (same-origin only) and selected caught failures (`epub` openFailed/displayError/import metadata, `sync`
  upload/download/replace/meta/annotations, `storage`, `import`, `reader`, `cleanup`, `api`, `sw`). Not reported:
  offline, `AbortError`, `UserError`, API 401/403/404/409/413/429. Identical kind+message+source are merged (`count`);
  max 50 queued, kept in memory and localStorage `borg-error-queue` (+ `STORAGE_SUFFIX`). Sent in batches of 20 to
  `client-error` 30 s after the first new report, on `visibilitychange` → hidden / `pagehide` (fetch `keepalive`, ≤60 KB;
  not `sendBeacon`, which cannot carry the CSRF header) and on `online`; skipped offline; a 429 pauses sending for
  15 min. Items carry app version (meta), kind, message, context as `source` (or file:line:col), trimmed stack,
  `location.pathname`, user agent — never book text, file names, titles or query strings. Admin panel «خطاهای کاربران»
  lists the groups (filter by version, «نمایش بیشتر» paging, expandable details, clear one / clear all) and the stats
  row shows «خطای کاربران».
- Share target (`share-target`, POST multipart) is handled by the Service Worker: files are parked in the
  `borg-share` cache and imported by the page on `?shared=1`. Desktop "open with" uses `launchQueue`.
