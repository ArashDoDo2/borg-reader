# 0011 — Magazine series and subscriptions; issues arrive without their file

Status: accepted (owner, 2026-10-27; shipped in v8.14.0)

## Context
Weekly magazines are published in the catalog as separate books. The owner wants users to subscribe to a magazine so
each new issue lands in their library automatically. Owner's decisions (2026-10-27):
- Issues come from both `api-book` and the admin upload, and reach subscribers **only once approved in the catalog**.
- A user subscribes from the magazine's page in the catalog; an admin can also subscribe users. A `student` (no
  catalog) can only be subscribed by an admin.
- Subscribing adds the **latest** issue; the earlier issues are listed on the magazine page and the user picks which to add.
- Unsubscribing stops new issues; issues already received stay in the library.
- Issues arrive **without their file**; the file is downloaded the first time the issue is opened.
- An issue's file is removed from the device **two weeks after it was last opened**, unless the user put that issue on
  one of their shelves.

## Decision

### Server (migration 10, portable, backward compatible)
- `series (id {AI}, name VARCHAR(200) NOT NULL UNIQUE, description VARCHAR(2000) NOT NULL DEFAULT '',
  created_by INTEGER NOT NULL, created_at BIGINT NOT NULL)`.
- `series_subscriptions (user_id, series_id, subscribed_at BIGINT NOT NULL, added_by INTEGER NOT NULL)`, primary key
  `(user_id, series_id)`, cascading on user and series delete.
- `books.series_id INTEGER NOT NULL DEFAULT 0` (0 = not an issue). An issue belongs to at most one series.
- `series_deliveries (book_id PRIMARY KEY, delivered_at)` marks issues already handled.
- **Delivery = approval, driven by state.** `deliver_issues()` (in `lib/series.php`) finds approved issues with no
  `series_deliveries` row, marks each one and inserts a `user_books` row for every subscriber, in one transaction (the
  mark is taken first, so parallel requests cannot deliver twice). It runs at the start of `library` (every sync) and
  of the series routes, so a subscriber gets a new issue on their next sync at the latest. Because it looks at state
  rather than at the approval request, every approval path delivers the same way: admin review and bulk approve,
  bulk «add to catalog», `admin-import-url`, `api-book` with a key that skips review, catalog submit by an admin, and
  any path added later. Pending or rejected issues reach no one. An issue is delivered at most once, even if it is
  removed from the catalog and approved again. (This replaces the first draft, which called a helper at each approval
  site and could miss one.)
- Attaching an already approved book to a series later does **not** deliver it (it counts as a back issue:
  `set_book_series()` marks it handled). This avoids sending old issues to everyone when the admin tidies up the
  catalog. An admin therefore picks the series **before** approval: in the «پیشنهاد برای کتابخانهٔ عمومی» dialog, in
  «دریافت با لینک», in the book editor while the book is pending, or with `series` in `api-book`.
- An admin's text cleanup of an issue (`book-replace`, which moves the catalog entry to a new book row) copies
  `series_id` and the delivered mark to the new row, so the cleaned copy stays an issue and is not sent again.
- Issue order (latest first): the catalog approval time (`reviewed_at`), then `books.created_at`.
- Routes (CSRF on every write):
  - `GET series` lists the series with their issue count, latest issue and whether the caller is subscribed. Catalog
    roles only, like the catalog itself.
  - `GET series-issues&id=` lists a series' approved issues.
  - `POST series-subscribe {series_id, subscribe: true|false}` subscribes or unsubscribes the caller. Subscribing adds
    the latest approved issue to the caller's library. Not available to `student`.
  - Admin: `admin-series` (create, rename, delete; deleting a series leaves its books in place with `series_id` = 0)
    and `admin-series-subscribe {series_id, user_ids, subscribe}`, which is allowed for any role, `student` included.
  - Back issues use the existing `catalog-add`.
- `api-book` accepts an optional `series` name. A missing series is created automatically, as `api-book` already does
  for categories (ADR 0010).
- `library` rows gain `series_id` (0 for ordinary books). Older clients ignore it and download issues as ordinary books.

### Client
- **Issue without a file:** in `sync()`, a server book with `series_id > 0` that is not on the device becomes a record
  with `file: null`, `remoteFile: true`, `contentHash` = the server's SHA-256, and the cover only. Because it has a
  hash, it is never uploaded or downloaded again. Every place that reads the file skips such a record: open, text
  cleanup, storage usage, duplicate checks. These are the 18 `fileOf()` call sites, each checked.
- **Open on demand:** opening a record without a file downloads `book-file`, shows progress on the opening indicator,
  stores the file on the record and then opens it. Offline, it shows the toast «برای اولین بازکردن این شماره اینترنت
  لازم است.» and the reader stays closed.
- **Removal after two weeks:** each record gets `openedAt`, set by `openReader`. After every successful sync (so only
  while logged in, when the file can always be fetched again), issues (`series_id > 0`) that have a file, are not on any shelf, and were last opened more than 14 days ago get their
  file removed: the record goes back to `remoteFile: true`. The card, reading position, notes and highlights stay;
  the next open downloads the file again and resumes in place. Issues not on the server (no `serverId`) or with a
  pending text cleanup are never touched. Half-read issues are not exempt (owner, 2026-10-27:
  two weeks without opening means the user has left it). The only thing skipped is the issue that is on screen in the
  reader at the moment the cleanup runs, because its file is in use; it is removed on the next run after the reader
  closes. Books that are not issues are never removed.
- **Catalog:** a «مجله‌ها» row above the showcase lists the series (hidden while searching or filtering). A magazine page shows a subscribe/unsubscribe button and the issues
  with «افزودن» for those not yet in the library (`#catalog/series/ID`). The library card of an issue without a file
  shows the line-icon `cloud` in the bottom corner of the cover and «دریافت هنگام بازکردن» in its badge.
- **Admin:** a «مجله‌ها» tab to create, rename and delete series (delete asks twice) and to subscribe or unsubscribe
  users (role shown). Series pickers: the catalog submit dialog (admins), «دریافت با لینک», and the book editor (catalog
  books only).

## Consequences
- Subscribers' devices hold at most the issues they actually read in the last two weeks, plus those on shelves.
- An issue's first open needs the network. This is the only place the reader depends on the network for a book
  already in the library.
- Push notifications for new issues are out of scope. A new issue simply appears in the library on the next sync.
- The `remoteFile` record state is general enough to be reused for ordinary books later, but this ADR applies it to
  issues only.
- Tests:
  - `api_test.py` (`run_series`): series CRUD, subscribe/unsubscribe, delivery on each approval path, nothing for
    pending/rejected or non-subscribers, delivered at most once, back issues, `student` gating, latest issue on
    subscribe, back issues via `catalog-add`, series delete.
  - `e2e_test.py` section 36 (its own server): catalog row and magazine page, subscribe, a new issue arrives without
    its file, the cloud badge is not covered, offline first open is refused, online first open downloads it, removal
    after 14 days (clock faked through `openedAt`) keeps the card and position, the issue on screen and a shelved issue
    are kept, unsubscribe, back issue from the magazine page, admin tab subscribing a student.
  - `webkit_e2e.py` (`magazine_phone`): first open of an issue without a file on iPhone.
