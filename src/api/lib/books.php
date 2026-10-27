<?php
// Books: chunked upload, download, personal library, reading progress, public catalog with moderation.
declare(strict_types=1);
if (!defined('BORG_API')) { http_response_code(404); exit; }

function ini_bytes(string $v): int {
    $v = trim($v); if ($v === '') return 0;
    $n = (int)$v; $u = strtolower(substr($v, -1));
    return match ($u) { 'g' => $n * 1073741824, 'm' => $n * 1048576, 'k' => $n * 1024, default => (int)$v };
}
/** Largest safe chunk: below both upload_max_filesize and post_max_size, capped at 2 MB. */
function chunk_bytes(): int {
    $limits = array_filter([ini_bytes((string)ini_get('post_max_size')), ini_bytes((string)ini_get('upload_max_filesize'))]);
    $min = $limits ? min($limits) : 2097152;
    return max(65536, min(2097152, (int)($min * 0.8)));
}

function book_out(array $b): array {
    return [
        'id' => (int)$b['id'], 'sha256' => $b['sha256'], 'size' => (int)$b['size'],
        'title' => $b['title'], 'author' => $b['author'], 'language' => $b['language'],
        'publisher' => (string)($b['publisher'] ?? ''), 'published' => (string)($b['published'] ?? ''),
        'has_cover' => (int)$b['has_cover'] === 1,
        'format' => ($b['format'] ?? 'epub') === 'pdf' ? 'pdf' : 'epub',
        'series_id' => (int)($b['series_id'] ?? 0), // v8.14 (migration 10): 0 = not a magazine issue
    ];
}
/** Optional EPUB metadata fields shared by upload, book-meta and catalog-update: key => max length. */
function book_meta_fields(): array { return ['language' => 20, 'publisher' => 300, 'published' => 40]; }
/** Present optional metadata fields from a request body (absent keys are left out). */
function book_meta_from(array $d): array {
    $out = [];
    foreach (book_meta_fields() as $k => $max) { $v = opt_str_param($d, $k, $max); if ($v !== null) $out[$k] = $v; }
    return $out;
}
/** UPDATE books with only the given columns (keys come from book_meta_fields / fixed names, never from input). */
function update_book_cols(int $bookId, array $cols): void {
    if (!$cols) return;
    $set = implode(', ', array_map(fn($k) => "$k = ?", array_keys($cols)));
    q("UPDATE books SET $set WHERE id = ?", array_merge(array_values($cols), [$bookId]));
    if (array_intersect_key($cols, ['title' => 1, 'author' => 1, 'publisher' => 1])) refresh_search_key($bookId);
}

// ---------- Search (migration 8): one spelling for books.search_key and the query ----------
const SEARCH_KEY_MAX = 1000;        // books.search_key VARCHAR(1000)
const SEARCH_BACKFILL_BATCH = 500;  // rows with an empty search_key filled per list request
/**
 * Same folding as the client's normalizeForSearch + library search: ي/ى → ی, ك → ک; ZWNJ/ZWJ/bidi marks (LRM, RLM,
 * embeddings, isolates), BOM, tatweel and harakat removed; Persian/Arabic-Indic digits → 0-9; lower case; whitespace
 * runs → one space (kept, so a query can be split into words).
 */
function search_norm(string $s): string {
    static $map = null;
    if ($map === null) {
        $map = ['ي' => 'ی', 'ى' => 'ی', 'ك' => 'ک'];
        foreach (['۰۱۲۳۴۵۶۷۸۹', '٠١٢٣٤٥٦٧٨٩'] as $digits) {
            foreach (mb_str_split($digits) as $i => $d) $map[$d] = (string)$i;
        }
    }
    $s = strtr($s, $map);
    $s = preg_replace('/[\x{200B}-\x{200F}\x{202A}-\x{202E}\x{2066}-\x{2069}\x{FEFF}\x{0640}\x{064B}-\x{065F}\x{0670}]/u', '', $s) ?? '';
    $s = mb_strtolower($s, 'UTF-8');
    return trim(preg_replace('/[\s\x{00A0}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}]+/u', ' ', $s) ?? '');
}
/** books.search_key for a title/author/publisher; never '' (an empty key means "not computed yet"). */
function book_search_key(string $title, string $author, string $publisher): string {
    $k = rtrim(mb_substr(search_norm("$title $author $publisher"), 0, SEARCH_KEY_MAX));
    return $k === '' ? ' ' : $k;
}
function refresh_search_key(int $bookId): void {
    $b = one('SELECT title, author, publisher FROM books WHERE id = ?', [$bookId]);
    if ($b) q('UPDATE books SET search_key = ? WHERE id = ?', [book_search_key((string)$b['title'], (string)$b['author'], (string)$b['publisher']), $bookId]);
}
/** Fill up to one batch of rows whose search_key is still empty (rows from before migration 8). Returns rows filled. */
function backfill_search_keys(int $batch = SEARCH_BACKFILL_BATCH): int {
    $rows = all("SELECT id, title, author, publisher FROM books WHERE search_key = '' LIMIT " . max(1, $batch));
    if (!$rows) return 0;
    db()->beginTransaction();
    try {
        $st = db()->prepare('UPDATE books SET search_key = ? WHERE id = ?');
        foreach ($rows as $b) $st->execute([book_search_key((string)$b['title'], (string)$b['author'], (string)$b['publisher']), (int)$b['id']]);
        db()->commit();
    } catch (Throwable $e) {
        if (db()->inTransaction()) db()->rollBack();
        throw $e;
    }
    return count($rows);
}
/** LIKE conditions (AND) matching every word of $q against $col; '!' escapes % _ ! (portable MySQL/SQLite ESCAPE). */
function search_where(string $q, string $col = 'b.search_key'): array {
    $words = array_slice(array_values(array_unique(array_filter(explode(' ', search_norm(mb_substr($q, 0, 200))), fn($w) => $w !== ''))), 0, 10);
    $where = []; $args = [];
    foreach ($words as $w) {
        $where[] = "$col LIKE ? ESCAPE '!'";
        $args[] = '%' . strtr($w, ['!' => '!!', '%' => '!%', '_' => '!_']) . '%';
    }
    return [$where, $args];
}
/** "?, ?, ?" for $n placeholders. */
function placeholders(int $n): string { return implode(',', array_fill(0, $n, '?')); }
/** Positive int request parameter clamped to [1, $max] ($default when absent or not a number). */
function page_param(array $p, string $k, int $default, int $max = PHP_INT_MAX): int {
    $v = $p[$k] ?? null;
    if (!is_scalar($v) || !is_numeric($v)) return $default;
    return max(1, min($max, (int)$v));
}
/** A scalar request parameter as a trimmed string ('' when absent or not scalar). */
function scalar_param(array $p, string $k): string { return isset($p[$k]) && is_scalar($p[$k]) ? trim((string)$p[$k]) : ''; }
function user_has_book(int $uid, int $bookId): bool {
    return (bool)one('SELECT 1 AS x FROM user_books WHERE user_id = ? AND book_id = ?', [$uid, $bookId]);
}
function can_read_book(array $s, int $bookId): bool {
    if ($s['role'] === 'admin' || user_has_book((int)$s['id'], $bookId)) return true;
    if ($s['role'] === 'student') return false;
    return (bool)one("SELECT 1 AS x FROM catalog WHERE book_id = ? AND status = 'approved'", [$bookId]);
}
/** Delete a book's files and row when no library and no catalog entry references it. */
function gc_book(int $bookId): void {
    $refs = (int)one('SELECT COUNT(*) AS n FROM user_books WHERE book_id = ?', [$bookId])['n']
          + (int)one('SELECT COUNT(*) AS n FROM catalog WHERE book_id = ?', [$bookId])['n'];
    if ($refs > 0) return;
    $b = one('SELECT * FROM books WHERE id = ?', [$bookId]);
    if (!$b) return;
    @unlink(book_path($b['sha256'], 'epub'));
    @unlink(book_path($b['sha256'], 'pdf'));
    @unlink(cover_path($bookId));
    q('DELETE FROM books WHERE id = ?', [$bookId]);
}

/**
 * Move one user's library entry for $old to $new (book-replace). Returns false when the user does not have $old.
 * user_books: status/shelves/meta_updated_at come from the newer of the two rows (old wins a tie), added_at from old.
 * progress: newer updated_at wins. annotations: re-pointed to $new (uid is unique per user, so no clashes) and their
 * updated_at is bumped past "now" so clients pulling `annotations?since=` see the new book_id.
 */
function move_user_book(int $uid, int $old, int $new): bool {
    $o = one('SELECT * FROM user_books WHERE user_id = ? AND book_id = ?', [$uid, $old]);
    if (!$o) return false;
    $n = one('SELECT * FROM user_books WHERE user_id = ? AND book_id = ?', [$uid, $new]);
    if (!$n) {
        q('INSERT INTO user_books (user_id, book_id, added_at, status, shelves, meta_updated_at) VALUES (?, ?, ?, ?, ?, ?)',
          [$uid, $new, (int)$o['added_at'], $o['status'], $o['shelves'], (int)$o['meta_updated_at']]);
    } else {
        $src = (int)$o['meta_updated_at'] >= (int)$n['meta_updated_at'] ? $o : $n;
        q('UPDATE user_books SET added_at = ?, status = ?, shelves = ?, meta_updated_at = ? WHERE user_id = ? AND book_id = ?',
          [(int)$o['added_at'], $src['status'], $src['shelves'], (int)$src['meta_updated_at'], $uid, $new]);
    }
    $po = one('SELECT * FROM progress WHERE user_id = ? AND book_id = ?', [$uid, $old]);
    if ($po) {
        $pn = one('SELECT updated_at FROM progress WHERE user_id = ? AND book_id = ?', [$uid, $new]);
        if (!$pn) {
            q('UPDATE progress SET book_id = ? WHERE user_id = ? AND book_id = ?', [$new, $uid, $old]);
        } else {
            if ((int)$po['updated_at'] > (int)$pn['updated_at']) {
                q('UPDATE progress SET cfi = ?, percentage = ?, updated_at = ? WHERE user_id = ? AND book_id = ?',
                  [$po['cfi'], (int)$po['percentage'], (int)$po['updated_at'], $uid, $new]);
            }
            q('DELETE FROM progress WHERE user_id = ? AND book_id = ?', [$uid, $old]);
        }
    }
    $nowMs = (int)(microtime(true) * 1000);
    q('UPDATE annotations SET book_id = ?, updated_at = CASE WHEN updated_at < ? THEN ? ELSE updated_at + 1 END WHERE user_id = ? AND book_id = ?',
      [$new, $nowMs, $nowMs, $uid, $old]);
    q('DELETE FROM user_books WHERE user_id = ? AND book_id = ?', [$uid, $old]);
    return true;
}
/** Fill empty metadata (and a missing cover) of the replacement book from the book it replaces. */
function copy_missing_meta(array $old, array $new): void {
    $cols = [];
    foreach (['title', 'author', 'language', 'publisher', 'published'] as $k) {
        $nv = trim((string)($new[$k] ?? '')); $ov = (string)($old[$k] ?? '');
        if (($nv === '' || ($k === 'title' && $nv === 'بدون عنوان')) && trim($ov) !== '') $cols[$k] = $ov;
    }
    if ((int)$new['has_cover'] !== 1 && (int)$old['has_cover'] === 1 && is_file(cover_path((int)$old['id']))
        && @copy(cover_path((int)$old['id']), cover_path((int)$new['id']))) $cols['has_cover'] = 1;
    update_book_cols((int)$new['id'], $cols);
}

/** Structural EPUB check that never extracts anything (zip-bomb safe). */
function validate_epub(string $path): void {
    if (class_exists('ZipArchive')) {
        $z = new ZipArchive();
        if ($z->open($path) !== true) fail(422, 'not_epub', 'File is not a valid EPUB.');
        $n = $z->numFiles; $total = 0;
        if ($n < 2 || $n > 20000) { $z->close(); fail(422, 'not_epub', 'File is not a valid EPUB.'); }
        for ($i = 0; $i < $n; $i++) { $st = $z->statIndex($i); $total += $st ? (int)$st['size'] : 0; }
        $mime = $z->getFromName('mimetype');
        $container = $z->locateName('META-INF/container.xml');
        $z->close();
        if ($total > 1024 * 1024 * 1024) fail(422, 'not_epub', 'EPUB is too large when uncompressed.');
        if (trim((string)$mime) !== 'application/epub+zip' || $container === false) fail(422, 'not_epub', 'File is not a valid EPUB.');
        return;
    }
    $head = (string)file_get_contents($path, false, null, 0, 64);
    if (!str_starts_with($head, "PK\x03\x04") || !str_contains($head, 'mimetypeapplication/epub+zip')) fail(422, 'not_epub', 'File is not a valid EPUB.');
}

/** PDF check without parsing: "%PDF-" within the first 1024 bytes and "%%EOF" within the last 2 KB. */
function is_pdf_file(string $path): bool {
    $size = (int)@filesize($path);
    if ($size < 16) return false;
    $head = (string)@file_get_contents($path, false, null, 0, 1024);
    $tail = (string)@file_get_contents($path, false, null, max(0, $size - 2048));
    return str_contains($head, '%PDF-') && str_contains($tail, '%%EOF');
}
/**
 * The format of an uploaded book, detected from its bytes ('pdf' or 'epub'); the client's hint only picks the error
 * code. A ZIP is checked as an EPUB. Otherwise a PDF header without "%%EOF" near the end, or a "pdf" hint for a file
 * that is neither, fails as not_pdf; anything else must pass validate_epub (not_epub).
 * $status: HTTP status of a failure (422 in the upload flow, 400 in api-book).
 */
function detect_book_format(string $path, string $hint = '', int $status = 422): string {
    $head = (string)@file_get_contents($path, false, null, 0, 1024);
    $zip = str_starts_with($head, "PK\x03\x04"); // a ZIP is never taken for a PDF (an EPUB may embed one)
    if (!$zip && is_pdf_file($path)) return 'pdf';
    if (!$zip && str_contains($head, '%PDF-')) fail($status, 'not_pdf', 'File is not a valid PDF (incomplete or damaged).');
    try {
        validate_epub($path);
    } catch (ApiError $e) {
        if ($hint === 'pdf') fail($status, 'not_pdf', 'File is not a valid PDF.');
        fail($status, 'not_epub', $e->getMessage());
    }
    return 'epub';
}

/** Accepts a data: URL with a small raster image and stores it as the book cover. */
function store_cover(int $bookId, ?string $dataUrl): bool {
    if (!$dataUrl || !preg_match('#^data:image/(png|jpe?g|webp|gif);base64,#', $dataUrl)) return false;
    $bin = base64_decode(substr($dataUrl, strpos($dataUrl, ',') + 1), true);
    return $bin !== false && store_cover_bytes($bookId, $bin);
}
/** Stores raw image bytes (at most 1.5 MB, must be a readable image) as the book cover. */
function store_cover_bytes(int $bookId, string $bin): bool {
    if ($bin === '' || strlen($bin) > 1500000 || @getimagesizefromstring($bin) === false) return false;
    file_put_contents(cover_path($bookId), $bin);
    return true;
}
/** Insert a books row for a stored file and return it (shared by upload-complete and api-book). */
/** $source: 'app' (upload flow) or 'api' (api-book). */
function insert_book(string $sha, int $size, array $meta, int $uploadedBy, string $source = 'app'): array {
    $title = (string)($meta['title'] ?? 'بدون عنوان'); $author = (string)($meta['author'] ?? ''); $publisher = (string)($meta['publisher'] ?? '');
    q('INSERT INTO books (sha256, size, title, author, language, publisher, published, has_cover, uploaded_by, created_at, format, source, search_key) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)',
      [$sha, $size, $title, $author, $meta['language'] ?? '',
       $publisher, $meta['published'] ?? '', $uploadedBy, time(), ($meta['format'] ?? 'epub') === 'pdf' ? 'pdf' : 'epub',
       $source === 'api' ? 'api' : 'app', book_search_key($title, $author, $publisher)]);
    return one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
}

// ---------- Catalog categories (migration 9): two levels, several per book ----------
const CATEGORY_MAX_PER_BOOK = 10;

/**
 * Every category as id => [id, parent_id, name, sort, pos], in display order: top level by sort, name, id, each followed
 * by its children (sort, name, id). pos = index in that order. Cached per request; $fresh reloads after a change.
 */
function categories_tree(bool $fresh = false): array {
    static $cache = null;
    if ($cache !== null && !$fresh) return $cache;
    $tops = []; $kids = [];
    foreach (all('SELECT id, parent_id, name, sort FROM categories ORDER BY sort, name, id') as $r) {
        $c = ['id' => (int)$r['id'], 'parent_id' => (int)$r['parent_id'], 'name' => (string)$r['name'], 'sort' => (int)$r['sort']];
        if ($c['parent_id'] === 0) $tops[] = $c; else $kids[$c['parent_id']][] = $c;
    }
    $out = [];
    foreach ($tops as $t) {
        $out[$t['id']] = $t;
        foreach ($kids[$t['id']] ?? [] as $k) $out[$k['id']] = $k;
        unset($kids[$t['id']]);
    }
    foreach ($kids as $list) foreach ($list as $k) $out[$k['id']] = $k; // orphans (never expected) last
    $i = 0;
    foreach ($out as &$c) $c['pos'] = $i++;
    unset($c);
    return $cache = $out;
}
/** {id, name, parent_id, parent_name} of a categories_tree() entry ('' parent_name for a top-level one). */
function category_ref(array $c): array {
    $tree = categories_tree();
    return ['id' => $c['id'], 'name' => $c['name'], 'parent_id' => $c['parent_id'],
            'parent_name' => $c['parent_id'] ? (string)($tree[$c['parent_id']]['name'] ?? '') : ''];
}
/** book id => [category_ref…] in display order, for the given catalog book ids (books without links are absent). */
function book_categories(array $bookIds): array {
    $tree = categories_tree(); $map = [];
    $bookIds = array_values(array_unique(array_map('intval', $bookIds)));
    foreach (array_chunk($bookIds, 500) as $chunk) {
        foreach (all('SELECT catalog_id, category_id FROM catalog_categories WHERE catalog_id IN (' . placeholders(count($chunk)) . ')', $chunk) as $r) {
            $c = $tree[(int)$r['category_id']] ?? null;
            if ($c) $map[(int)$r['catalog_id']][] = $c;
        }
    }
    foreach ($map as &$list) {
        usort($list, fn($a, $b) => $a['pos'] <=> $b['pos']);
        $list = array_map('category_ref', $list);
    }
    unset($list);
    return $map;
}
/** Adds `categories` to book output arrays (keyed by their 'id'). */
function with_categories(array $books): array {
    $map = book_categories(array_column($books, 'id'));
    foreach ($books as &$b) $b['categories'] = $map[$b['id']] ?? [];
    unset($b);
    return $books;
}
/** catalog.category (legacy) := top-level name of each book's first category, '' when it has none. */
function sync_legacy_category(array $bookIds): void {
    $bookIds = array_values(array_unique(array_map('intval', $bookIds)));
    if (!$bookIds) return;
    $map = book_categories($bookIds);
    foreach (array_chunk($bookIds, 500) as $chunk) {
        foreach (all('SELECT book_id, category FROM catalog WHERE book_id IN (' . placeholders(count($chunk)) . ')', $chunk) as $r) {
            $first = $map[(int)$r['book_id']][0] ?? null;
            $want = $first ? ($first['parent_name'] !== '' ? $first['parent_name'] : $first['name']) : '';
            if ((string)$r['category'] !== $want) q('UPDATE catalog SET category = ? WHERE book_id = ?', [$want, (int)$r['book_id']]);
        }
    }
}
/** Replace a catalog book's categories (ids already validated) and refresh its legacy column. */
function set_book_categories(int $bookId, array $catIds): void {
    q('DELETE FROM catalog_categories WHERE catalog_id = ?', [$bookId]);
    foreach (array_values(array_unique($catIds)) as $cid) insert_ignore('catalog_categories', ['catalog_id' => $bookId, 'category_id' => (int)$cid]);
    sync_legacy_category([$bookId]);
}
/** Catalog book ids linked to any of the categories. */
function books_in_categories(array $catIds): array {
    $out = [];
    foreach (array_chunk(array_values(array_unique(array_map('intval', $catIds))), 500) as $chunk) {
        foreach (all('SELECT DISTINCT catalog_id FROM catalog_categories WHERE category_id IN (' . placeholders(count($chunk)) . ')', $chunk) as $r) $out[] = (int)$r['catalog_id'];
    }
    return array_values(array_unique($out));
}
/** Id of the category $name under $parentId (0 = top level), or 0. */
function category_find(string $name, int $parentId = 0): int {
    $r = one('SELECT id FROM categories WHERE parent_id = ? AND name = ?', [$parentId, trim($name)]);
    return $r ? (int)$r['id'] : 0;
}
/** Id of the category $name under $parentId, created (last among its siblings) when missing. */
function category_find_or_create(string $name, int $parentId = 0): int {
    $name = trim($name);
    if ($name === '') fail(400, 'missing_name', "Field 'name' is required.");
    if (mb_strlen($name) > 80) fail(400, 'too_long_category', "Field 'category' is too long.");
    $id = category_find($name, $parentId);
    if ($id) return $id;
    $sort = (int)one('SELECT COALESCE(MAX(sort), -1) + 1 AS n FROM categories WHERE parent_id = ?', [$parentId])['n'];
    insert_ignore('categories', ['parent_id' => $parentId, 'name' => $name, 'sort' => $sort, 'created_at' => time()]);
    categories_tree(true);
    return category_find($name, $parentId);
}
/** Optional `categories` list of existing category ids (max 10, duplicates once); null when the key is absent. */
function category_ids_param(array $d, string $k = 'categories'): ?array {
    if (!array_key_exists($k, $d)) return null;
    $raw = $d[$k];
    if (!is_array($raw) || !array_is_list($raw)) fail(400, 'bad_categories', 'categories must be a list of category ids.');
    $ids = [];
    foreach ($raw as $v) {
        if (!(is_int($v) || (is_string($v) && ctype_digit($v))) || (int)$v <= 0) fail(400, 'bad_categories', 'categories must be a list of category ids.');
        $ids[(int)$v] = true;
    }
    $ids = array_keys($ids);
    if (count($ids) > CATEGORY_MAX_PER_BOOK) fail(400, 'too_many_categories', 'At most ' . CATEGORY_MAX_PER_BOOK . ' categories per book.');
    $tree = categories_tree();
    foreach ($ids as $id) if (!isset($tree[$id])) fail(400, 'unknown_category', "Category $id does not exist.");
    return $ids;
}
/**
 * api-book `category`: paths separated by ";" or newlines, each "Parent/Child" or "Parent" ("›" and ">" also separate
 * levels). Returns [[parent, child|null], …] (deduplicated); 400 too_deep / too_long_category / too_many_categories.
 */
function parse_category_paths(string $raw): array {
    $out = [];
    foreach (preg_split('/[;\r\n]+/u', $raw) ?: [] as $path) {
        $segs = array_values(array_filter(array_map(fn($s) => trim(preg_replace('/\s+/u', ' ', $s) ?? $s), preg_split('#[/›>]#u', $path) ?: []), fn($s) => $s !== ''));
        if (!$segs) continue;
        if (count($segs) > 2) fail(400, 'too_deep', 'Categories have two levels: "Parent/Child".');
        foreach ($segs as $s) if (mb_strlen($s) > 80) fail(400, 'too_long_category', "Field 'category' is too long.");
        $out[implode("\n", $segs)] = [$segs[0], $segs[1] ?? null];
    }
    if (count($out) > CATEGORY_MAX_PER_BOOK) fail(400, 'too_many_categories', 'At most ' . CATEGORY_MAX_PER_BOOK . ' categories per book.');
    return array_values($out);
}
/** Category ids for parsed paths, creating missing ones (parent first). */
function category_ids_for_paths(array $paths): array {
    $ids = [];
    foreach ($paths as [$parent, $child]) {
        $pid = category_find_or_create($parent, 0);
        $ids[] = $child === null ? $pid : category_find_or_create($child, $pid);
    }
    return array_values(array_unique($ids));
}
/**
 * Category filters over a query with `b` = books and `c` = catalog: cat=<id> (a top-level id includes its children),
 * cat=none (no category), and the legacy category=<name> (any linked category with that name, or catalog.category).
 */
function category_where(array $p, array &$where, array &$args, bool $legacyName = true, bool $strictLen = true): void {
    $cat = scalar_param($p, 'cat');
    if ($cat === 'none') $where[] = 'NOT EXISTS (SELECT 1 FROM catalog_categories cc WHERE cc.catalog_id = b.id)';
    elseif ($cat !== '') {
        if (!ctype_digit($cat) || strlen($cat) > 18) fail(400, 'bad_cat', 'cat must be a category id or "none".');
        $where[] = 'EXISTS (SELECT 1 FROM catalog_categories cc JOIN categories k ON k.id = cc.category_id WHERE cc.catalog_id = b.id AND (k.id = ? OR k.parent_id = ?))';
        array_push($args, (int)$cat, (int)$cat);
    }
    if (!$legacyName) return;
    $name = scalar_param($p, 'category');
    if ($name === '') return;
    if (mb_strlen($name) > 80) {
        if ($strictLen) fail(400, 'too_long_category', "Field 'category' is too long.");
        $name = mb_substr($name, 0, 80);
    }
    $where[] = '(c.category = ? OR EXISTS (SELECT 1 FROM catalog_categories cc JOIN categories k ON k.id = cc.category_id WHERE cc.catalog_id = b.id AND k.name = ?))';
    array_push($args, $name, $name);
}
/** category id => distinct catalog books (top level: itself or any child), for catalog rows matching $cond. */
function category_counts(string $cond): array {
    $w = $cond !== '' ? "WHERE $cond" : '';
    $n = [];
    foreach (all("SELECT cc.category_id AS id, COUNT(DISTINCT cc.catalog_id) AS n FROM catalog_categories cc
                  JOIN catalog c ON c.book_id = cc.catalog_id $w GROUP BY cc.category_id") as $r) $n[(int)$r['id']] = (int)$r['n'];
    $top = 'CASE WHEN k.parent_id = 0 THEN k.id ELSE k.parent_id END';
    foreach (all("SELECT $top AS id, COUNT(DISTINCT cc.catalog_id) AS n FROM catalog_categories cc
                  JOIN categories k ON k.id = cc.category_id JOIN catalog c ON c.book_id = cc.catalog_id $w GROUP BY $top") as $r) {
        $tree = categories_tree();
        if (($tree[(int)$r['id']]['parent_id'] ?? -1) === 0) $n[(int)$r['id']] = (int)$r['n'];
    }
    return $n;
}
/** GET categories: flat list in display order with approved-book counts (admins also count_all, any status). */
function categories_list(bool $admin): array {
    $count = category_counts("c.status = 'approved'");
    $all = $admin ? category_counts('') : [];
    $out = [];
    foreach (categories_tree() as $c) {
        $o = ['id' => $c['id'], 'name' => $c['name'], 'parent_id' => $c['parent_id'], 'sort' => $c['sort'], 'count' => $count[$c['id']] ?? 0];
        if ($admin) $o['count_all'] = $all[$c['id']] ?? 0;
        $out[] = $o;
    }
    return $out;
}

function upload_dir(): string { return storage('uploads'); }
function cleanup_stale_uploads(): void {
    foreach (all('SELECT id FROM uploads WHERE created_at < ?', [time() - 86400]) as $u) {
        @unlink(upload_dir() . "/{$u['id']}.part"); @unlink(upload_dir() . "/{$u['id']}.cover");
        q('DELETE FROM uploads WHERE id = ?', [$u['id']]);
    }
}

function handle_books_route(string $route): bool {
    global $method;
    switch ($route) {
        case 'library':
            method('GET');
            $s = require_user();
            deliver_issues(); // v8.14: new magazine issues reach subscribers on their next sync at the latest
            $rows = all("SELECT b.*, p.cfi, p.percentage, p.updated_at, c.status AS catalog_status,
                                ub.status AS read_status, ub.shelves AS shelf_ids, ub.meta_updated_at
                         FROM user_books ub JOIN books b ON b.id = ub.book_id
                         LEFT JOIN progress p ON p.user_id = ub.user_id AND p.book_id = b.id
                         LEFT JOIN catalog c ON c.book_id = b.id
                         WHERE ub.user_id = ? ORDER BY ub.added_at DESC", [$s['id']]);
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'progress' => $r['cfi'] !== null ? ['cfi' => $r['cfi'], 'percentage' => (int)$r['percentage'], 'updated_at' => (int)$r['updated_at']] : null,
                'catalog_status' => $r['catalog_status'],
                'status' => (string)$r['read_status'], 'shelves' => json_decode((string)$r['shelf_ids'], true) ?: [],
                'meta_updated_at' => (int)$r['meta_updated_at'],
            ], $rows)]);

        case 'library-meta':
            // Reading status and shelf membership per book; last write wins by client timestamp.
            method('POST');
            $s = require_user(true);
            $items = body_json()['items'] ?? [];
            if (!is_array($items) || count($items) > 1000) fail(400, 'bad_items', 'Invalid items.');
            $saved = 0;
            foreach ($items as $it) {
                if (!is_array($it)) continue;
                $bookId = (int)($it['book_id'] ?? 0);
                $status = (string)($it['status'] ?? '');
                if (!in_array($status, ['', 'want', 'reading', 'finished'], true)) continue;
                $shelves = array_values(array_filter(array_map('strval', is_array($it['shelves'] ?? null) ? $it['shelves'] : []), fn($x) => preg_match('/^[A-Za-z0-9-]{8,40}$/', $x)));
                $at = (int)($it['updated_at'] ?? 0);
                if ($at <= 0 || $at > (time() + 300) * 1000) $at = (int)(microtime(true) * 1000);
                $saved += q('UPDATE user_books SET status = ?, shelves = ?, meta_updated_at = ? WHERE user_id = ? AND book_id = ? AND meta_updated_at < ?',
                            [$status, json_encode(array_slice($shelves, 0, 50)), $at, $s['id'], $bookId, $at])->rowCount();
            }
            json_out(200, ['saved' => $saved]);

        case 'shelves':
            if ($method === 'GET') {
                $s = require_user();
                json_out(200, ['shelves' => array_map(fn($r) => [
                    'uid' => $r['uid'], 'name' => $r['name'], 'position' => (int)$r['position'],
                    'updated_at' => (int)$r['updated_at'], 'deleted' => (int)$r['deleted'] === 1,
                ], all('SELECT * FROM shelves WHERE user_id = ? ORDER BY position, updated_at', [$s['id']]))]);
            }
            method('POST');
            $s = require_user(true);
            $items = body_json()['items'] ?? [];
            if (!is_array($items) || count($items) > 200) fail(400, 'bad_items', 'Invalid items.');
            $saved = 0;
            foreach ($items as $it) {
                if (!is_array($it)) continue;
                $uid = (string)($it['uid'] ?? '');
                $name = trim((string)($it['name'] ?? ''));
                if (!preg_match('/^[A-Za-z0-9-]{8,40}$/', $uid) || $name === '' || mb_strlen($name) > 80) continue;
                $at = (int)($it['updated_at'] ?? 0);
                if ($at <= 0 || $at > (time() + 300) * 1000) $at = (int)(microtime(true) * 1000);
                $cur = one('SELECT updated_at FROM shelves WHERE user_id = ? AND uid = ?', [$s['id'], $uid]);
                $row = [$name, (int)($it['position'] ?? 0), $at, !empty($it['deleted']) ? 1 : 0];
                if (!$cur) { insert_ignore('shelves', ['uid' => $uid, 'user_id' => $s['id'], 'name' => $row[0], 'position' => $row[1], 'updated_at' => $at, 'deleted' => $row[3]]); $saved++; }
                elseif ($at > (int)$cur['updated_at']) { q('UPDATE shelves SET name = ?, position = ?, updated_at = ?, deleted = ? WHERE user_id = ? AND uid = ?', array_merge($row, [$s['id'], $uid])); $saved++; }
            }
            json_out(200, ['saved' => $saved]);

        case 'stats':
            // Reading seconds per day; each device reports its own running total, totals are summed across devices.
            if ($method === 'GET') {
                $s = require_user();
                $since = date('Y-m-d', time() - 400 * 86400);
                json_out(200, ['days' => array_map(fn($r) => ['day' => $r['day'], 'seconds' => (int)$r['seconds']],
                    all('SELECT day, SUM(seconds) AS seconds FROM reading_days WHERE user_id = ? AND day >= ? GROUP BY day ORDER BY day', [$s['id'], $since]))]);
            }
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $device = (string)($d['device'] ?? '');
            if (!preg_match('/^[A-Za-z0-9-]{8,40}$/', $device)) fail(400, 'bad_device', 'Invalid device.');
            $days = is_array($d['days'] ?? null) ? array_slice($d['days'], 0, 400) : [];
            foreach ($days as $it) {
                $day = (string)($it['day'] ?? ''); $sec = (int)($it['seconds'] ?? 0);
                if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $day) || $sec < 0 || $sec > 86400) continue;
                $cur = one('SELECT seconds FROM reading_days WHERE user_id = ? AND device = ? AND day = ?', [$s['id'], $device, $day]);
                if (!$cur) insert_ignore('reading_days', ['user_id' => $s['id'], 'device' => $device, 'day' => $day, 'seconds' => $sec]);
                elseif ($sec > (int)$cur['seconds']) q('UPDATE reading_days SET seconds = ? WHERE user_id = ? AND device = ? AND day = ?', [$sec, $s['id'], $device, $day]);
            }
            json_out(200, ['ok' => true]);

        case 'upload-init':
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $sha = strtolower(str_param($d, 'sha256', 64));
            if (!preg_match('/^[a-f0-9]{64}$/', $sha)) fail(400, 'bad_sha256', 'Invalid hash.');
            $size = (int)($d['size'] ?? 0);
            if ($size <= 0 || $size > config()['max_book_bytes']) fail(413, 'too_large', 'Book is too large.');
            // Optional format hint ("epub" | "pdf"); the real format is detected from the bytes in upload-complete.
            $hint = strtolower((string)(opt_str_param($d, 'format', 8) ?? ''));
            if (!in_array($hint, ['', 'epub', 'pdf'], true)) fail(400, 'bad_format', 'format must be "epub" or "pdf".');
            $existing = one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
            if ($existing) {
                insert_ignore('user_books', ['user_id' => $s['id'], 'book_id' => $existing['id'], 'added_at' => time()]);
                json_out(200, ['exists' => true, 'book' => book_out($existing)]);
            }
            cleanup_stale_uploads();
            $id = bin2hex(random_bytes(16));
            $meta = [
                'title' => str_param($d, 'title', 500), 'author' => str_param($d, 'author', 300, false),
                'language' => '', 'publisher' => '', 'published' => '', 'format_hint' => $hint,
            ];
            $meta = array_merge($meta, book_meta_from($d));
            q('INSERT INTO uploads (id, user_id, sha256, size, meta, received, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)',
              [$id, $s['id'], $sha, $size, json_encode($meta, JSON_UNESCAPED_UNICODE), time()]);
            touch(upload_dir() . "/$id.part");
            if (!empty($d['cover']) && is_string($d['cover']) && strlen($d['cover']) < 2100000) file_put_contents(upload_dir() . "/$id.cover", $d['cover']);
            json_out(201, ['exists' => false, 'upload_id' => $id, 'chunk_bytes' => chunk_bytes()]);

        case 'upload-chunk':
            method('POST');
            $s = require_user(true);
            $id = is_string($_GET['upload_id'] ?? null) ? $_GET['upload_id'] : '';
            $offset = (int)($_GET['offset'] ?? -1);
            $u = preg_match('/^[a-f0-9]{32}$/', $id) ? one('SELECT * FROM uploads WHERE id = ? AND user_id = ?', [$id, $s['id']]) : null;
            if (!$u) fail(404, 'no_upload', 'Upload not found.');
            if ($offset !== (int)$u['received']) fail(409, 'bad_offset', 'Unexpected offset.');
            $data = file_get_contents('php://input') ?: '';
            $len = strlen($data);
            if ($len === 0 || $len > chunk_bytes() + 1024 || $offset + $len > (int)$u['size']) fail(400, 'bad_chunk', 'Invalid chunk.');
            file_put_contents(upload_dir() . "/$id.part", $data, FILE_APPEND | LOCK_EX);
            q('UPDATE uploads SET received = received + ? WHERE id = ?', [$len, $id]);
            json_out(200, ['received' => $offset + $len]);

        case 'upload-complete':
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $id = str_param($d, 'upload_id', 32);
            $late = book_meta_from($d); // optional metadata may also arrive here; it overrides upload-init values
            $u = one('SELECT * FROM uploads WHERE id = ? AND user_id = ?', [$id, $s['id']]);
            if (!$u) fail(404, 'no_upload', 'Upload not found.');
            $part = upload_dir() . "/$id.part";
            $finish = function () use ($id, $part) {
                @unlink($part); @unlink(upload_dir() . "/$id.cover"); q('DELETE FROM uploads WHERE id = ?', [$id]);
            };
            if ((int)$u['received'] !== (int)$u['size'] || filesize($part) !== (int)$u['size']) { $finish(); fail(400, 'incomplete', 'Upload is incomplete.'); }
            if (!hash_equals($u['sha256'], hash_file('sha256', $part))) { $finish(); fail(400, 'hash_mismatch', 'File hash does not match.'); }
            $meta = array_merge(json_decode($u['meta'], true) ?: [], $late);
            try { $format = detect_book_format($part, (string)($meta['format_hint'] ?? '')); } catch (ApiError $e) { $finish(); throw $e; }
            $existing = one('SELECT * FROM books WHERE sha256 = ?', [$u['sha256']]);
            if (!$existing) {
                $meta['format'] = $format;
                if (!rename($part, book_path($u['sha256'], $format))) { $finish(); fail(500, 'storage', 'Could not store the file.'); }
                $existing = insert_book($u['sha256'], (int)$u['size'], $meta, (int)$s['id']);
                $coverFile = upload_dir() . "/$id.cover";
                if (is_file($coverFile) && store_cover((int)$existing['id'], (string)file_get_contents($coverFile))) {
                    q('UPDATE books SET has_cover = 1 WHERE id = ?', [$existing['id']]);
                    $existing['has_cover'] = 1;
                }
            }
            insert_ignore('user_books', ['user_id' => $s['id'], 'book_id' => $existing['id'], 'added_at' => time()]);
            $finish();
            json_out(201, ['book' => book_out($existing)]);

        case 'book-file':
        case 'book-cover':
            method('GET');
            $s = require_user();
            $bookId = int_param($_GET['id'] ?? '');
            $b = one('SELECT * FROM books WHERE id = ?', [$bookId]);
            if (!$b || !can_read_book($s, $bookId)) fail(404, 'not_found', 'Book not found.');
            $path = $route === 'book-file' ? book_file_of($b) : cover_path($bookId);
            if (!is_file($path)) fail(404, 'not_found', 'File not found.');
            $isPdf = ($b['format'] ?? 'epub') === 'pdf';
            $type = $isPdf ? 'application/pdf' : 'application/epub+zip';
            if ($route === 'book-cover') {
                $info = @getimagesize($path);
                $type = $info['mime'] ?? 'application/octet-stream';
            }
            header('Content-Type: ' . $type);
            header('Content-Length: ' . filesize($path));
            header('Cache-Control: private, max-age=86400');
            header('X-Content-Type-Options: nosniff');
            if ($route === 'book-file') header('Content-Disposition: attachment; filename="book-' . $bookId . ($isPdf ? '.pdf' : '.epub') . '"');
            readfile($path);
            exit;

        case 'library-remove':
            method('POST');
            $s = require_user(true);
            $bookId = int_param(body_json()['book_id'] ?? '');
            q('DELETE FROM user_books WHERE user_id = ? AND book_id = ?', [$s['id'], $bookId]);
            q('DELETE FROM progress WHERE user_id = ? AND book_id = ?', [$s['id'], $bookId]);
            gc_book($bookId);
            json_out(200, ['ok' => true]);

        case 'book-replace':
            // A cleaned copy (new file, uploaded with the normal upload flow) replaces a book in the caller's library.
            // Admins replacing a catalog book also move the catalog entry and every other user's copy.
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $oldId = int_param($d['old_book_id'] ?? '');
            $newId = int_param($d['new_book_id'] ?? '');
            if ($oldId === $newId) fail(400, 'same_book', 'Old and new book are the same.');
            $me = (int)$s['id'];
            $isAdmin = $s['role'] === 'admin';
            $old = one('SELECT * FROM books WHERE id = ?', [$oldId]);
            if (!$old || !($isAdmin || user_has_book($me, $oldId))) fail(404, 'not_found', 'Book not in your library.');
            $new = one('SELECT * FROM books WHERE id = ?', [$newId]);
            if (!$new || !user_has_book($me, $newId)) fail(404, 'not_found', 'New book not in your library.');
            $migrated = 0; $catalogMoved = false;
            db()->beginTransaction();
            try {
                move_user_book($me, $oldId, $newId);
                if ($isAdmin) {
                    if (one('SELECT 1 AS x FROM catalog WHERE book_id = ?', [$oldId])) {
                        if (one('SELECT 1 AS x FROM catalog WHERE book_id = ?', [$newId])) q('DELETE FROM catalog WHERE book_id = ?', [$oldId]);
                        else q('UPDATE catalog SET book_id = ? WHERE book_id = ?', [$newId, $oldId]);
                        $catalogMoved = true;
                    }
                    foreach (all('SELECT user_id FROM user_books WHERE book_id = ? AND user_id <> ?', [$oldId, $me]) as $r) {
                        if (move_user_book((int)$r['user_id'], $oldId, $newId)) $migrated++;
                    }
                }
                copy_missing_meta($old, $new);
                // v8.14: a cleaned magazine issue stays an issue of its magazine, and keeps its "delivered" mark so the
                // new copy is not sent to the subscribers a second time.
                if ($catalogMoved && (int)($old['series_id'] ?? 0) > 0 && (int)($new['series_id'] ?? 0) === 0) {
                    q('UPDATE books SET series_id = ? WHERE id = ?', [(int)$old['series_id'], $newId]);
                    if (one('SELECT 1 AS x FROM series_deliveries WHERE book_id = ?', [$oldId])) mark_issue_handled($newId);
                }
                db()->commit();
            } catch (Throwable $e) {
                if (db()->inTransaction()) db()->rollBack();
                throw $e;
            }
            gc_book($oldId);
            json_out(200, ['book' => book_out(one('SELECT * FROM books WHERE id = ?', [$newId])), 'migrated_users' => $migrated, 'catalog_moved' => $catalogMoved]);

        case 'progress':
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            if (!user_has_book((int)$s['id'], $bookId)) fail(404, 'not_found', 'Book not in your library.');
            $cfi = str_param($d, 'cfi', 1000);
            $pct = max(0, min(100, (int)($d['percentage'] ?? 0)));
            $at = (int)($d['updated_at'] ?? 0);
            if ($at <= 0 || $at > (time() + 300) * 1000) $at = time() * 1000; // client clock in ms; reject far-future values
            $cur = one('SELECT * FROM progress WHERE user_id = ? AND book_id = ?', [$s['id'], $bookId]);
            if (!$cur) {
                insert_ignore('progress', ['user_id' => $s['id'], 'book_id' => $bookId, 'cfi' => $cfi, 'percentage' => $pct, 'updated_at' => $at]);
            } elseif ($at > (int)$cur['updated_at']) {
                q('UPDATE progress SET cfi = ?, percentage = ?, updated_at = ? WHERE user_id = ? AND book_id = ? AND updated_at < ?',
                  [$cfi, $pct, $at, $s['id'], $bookId, $at]);
            }
            $now = one('SELECT cfi, percentage, updated_at FROM progress WHERE user_id = ? AND book_id = ?', [$s['id'], $bookId]);
            json_out(200, ['progress' => ['cfi' => $now['cfi'], 'percentage' => (int)$now['percentage'], 'updated_at' => (int)$now['updated_at']]]);

        case 'book-meta':
            // Correct title/author/language/publisher/published/cover. Allowed for the uploader (if the book is in their
            // library) and admins. Each field is updated only when its key is present; title, when present, must be non-empty.
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            $b = one('SELECT * FROM books WHERE id = ?', [$bookId]);
            $owner = $b && user_has_book((int)$s['id'], $bookId) && (int)$b['uploaded_by'] === (int)$s['id'];
            if (!$b || !($owner || $s['role'] === 'admin')) fail(404, 'not_found', 'Book not found.');
            $cols = book_meta_from($d);
            if (array_key_exists('title', $d)) $cols['title'] = str_param($d, 'title', 500);
            if (array_key_exists('author', $d)) $cols['author'] = (string)opt_str_param($d, 'author', 300);
            update_book_cols($bookId, $cols);
            if (!empty($d['cover']) && is_string($d['cover']) && store_cover($bookId, $d['cover'])) q('UPDATE books SET has_cover = 1 WHERE id = ?', [$bookId]);
            json_out(200, ['book' => book_out(one('SELECT * FROM books WHERE id = ?', [$bookId]))]);

        // ---------- Bookmarks, highlights, notes (last write wins per item) ----------
        case 'annotations':
            if ($method === 'GET') {
                $s = require_user();
                $since = (int)($_GET['since'] ?? 0);
                $rows = all('SELECT * FROM annotations WHERE user_id = ? AND updated_at > ? ORDER BY updated_at', [$s['id'], $since]);
                json_out(200, ['annotations' => array_map('annotation_out', $rows), 'server_time' => (int)(microtime(true) * 1000)]);
            }
            method('POST');
            $s = require_user(true);
            $items = body_json()['items'] ?? [];
            if (!is_array($items) || count($items) > 500) fail(400, 'bad_items', 'Invalid items.');
            $saved = 0;
            $owned = array_flip(array_map('intval', array_column(all('SELECT book_id FROM user_books WHERE user_id = ?', [$s['id']]), 'book_id')));
            foreach ($items as $it) {
                if (!is_array($it)) continue;
                $uid = (string)($it['uid'] ?? '');
                $bookId = (int)($it['book_id'] ?? 0);
                $type = (string)($it['type'] ?? '');
                if (!preg_match('/^[A-Za-z0-9-]{8,40}$/', $uid) || !isset($owned[$bookId]) || !in_array($type, ['bookmark', 'highlight'], true)) continue;
                $at = (int)($it['updated_at'] ?? 0);
                if ($at <= 0 || $at > (time() + 300) * 1000) $at = (int)(microtime(true) * 1000);
                $row = [
                    'cfi' => mb_substr((string)($it['cfi'] ?? ''), 0, 1000), 'quote' => mb_substr((string)($it['quote'] ?? ''), 0, 2000),
                    'note' => mb_substr((string)($it['note'] ?? ''), 0, 10000), 'color' => mb_substr((string)($it['color'] ?? ''), 0, 20),
                    'deleted' => !empty($it['deleted']) ? 1 : 0,
                ];
                if ($row['cfi'] === '') continue;
                $cur = one('SELECT updated_at FROM annotations WHERE user_id = ? AND uid = ?', [$s['id'], $uid]);
                if (!$cur) {
                    insert_ignore('annotations', ['uid' => $uid, 'user_id' => $s['id'], 'book_id' => $bookId, 'type' => $type] + $row
                        + ['created_at' => (int)($it['created_at'] ?? $at), 'updated_at' => $at]);
                    $saved++;
                } elseif ($at > (int)$cur['updated_at']) {
                    q('UPDATE annotations SET cfi = ?, quote = ?, note = ?, color = ?, deleted = ?, updated_at = ? WHERE user_id = ? AND uid = ? AND updated_at < ?',
                      [$row['cfi'], $row['quote'], $row['note'], $row['color'], $row['deleted'], $at, $s['id'], $uid, $at]);
                    $saved++;
                }
            }
            json_out(200, ['saved' => $saved]);

        // ---------- Public catalog ----------
        case 'catalog':
            method('GET');
            $s = require_user();
            if ($s['role'] === 'student') {
                json_out(200, ['books' => [], 'total' => 0, 'page' => 1, 'pages' => 1]);
            }
            backfill_search_keys();
            $catOut = fn($r) => book_out($r) + [
                'description' => $r['description'], 'category' => (string)$r['category'], 'submitter' => $r['submitter'], 'in_library' => (bool)$r['in_library'],
            ];
            if (!array_key_exists('page', $_GET)) { // unpaged: the full list, exactly as before migration 8
                $rows = all("SELECT b.*, c.description, c.category, c.submitted_at, u.display_name AS submitter,
                               (SELECT 1 FROM user_books ub WHERE ub.user_id = ? AND ub.book_id = b.id) AS in_library
                             FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                             WHERE c.status = 'approved' ORDER BY c.reviewed_at DESC, c.submitted_at DESC", [$s['id']]);
                json_out(200, ['books' => with_categories(array_map($catOut, $rows))]);
            }
            json_out(200, catalog_page($_GET, (int)$s['id'], $catOut));

        case 'categories':
            method('GET');
            $s = require_user();
            if ($s['role'] === 'student') {
                json_out(200, ['categories' => []]);
            }
            json_out(200, ['categories' => categories_list($s['role'] === 'admin')]);

        case 'catalog-submit':
            method('POST');
            $s = require_user(true);
            if ($s['role'] === 'student') fail(403, 'forbidden', 'ثبت در کتابخانهٔ عمومی برای نقش دانشجو مجاز نیست.');
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            if (!user_has_book((int)$s['id'], $bookId)) fail(404, 'not_found', 'Book not in your library.');
            $cur = one('SELECT status FROM catalog WHERE book_id = ?', [$bookId]);
            if ($cur && $cur['status'] !== 'rejected') json_out(200, ['status' => $cur['status']]);
            $auto = $s['role'] === 'admin' || !catalog_requires_approval();
            $status = $auto ? 'approved' : 'pending';
            $desc = str_param($d, 'description', 2000, false);
            // v8.14: an admin can submit a book as a magazine issue; set before the catalog row so its approval delivers it.
            $seriesId = $s['role'] === 'admin' ? series_id_param($d) : null;
            // categories: existing ids. Legacy `category` (name): admins find or create a top-level category; for other
            // users it only matches an existing top-level name (else it is ignored).
            $catIds = category_ids_param($d);
            $category = (string)opt_str_param($d, 'category', 80);
            if ($catIds === null) {
                if ($category === '') $catIds = [];
                elseif ($s['role'] === 'admin') $catIds = [category_find_or_create($category, 0)];
                else $catIds = ($cid = category_find($category, 0)) ? [$cid] : [];
            }
            q('DELETE FROM catalog WHERE book_id = ?', [$bookId]);
            if ($seriesId !== null) set_book_series($bookId, $seriesId);
            q('INSERT INTO catalog (book_id, status, description, category, submitted_by, submitted_at, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
              [$bookId, $status, $desc, '', $s['id'], time(), $auto ? $s['id'] : null, $auto ? time() : null]);
            set_book_categories($bookId, $catIds);
            json_out(201, ['status' => $status]);

        case 'catalog-pending':
            method('GET');
            require_admin();
            $rows = all("SELECT b.*, c.description, c.category, c.submitted_at, u.display_name AS submitter
                         FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                         WHERE c.status = 'pending' ORDER BY c.submitted_at", []);
            json_out(200, ['books' => with_categories(array_map(fn($r) => book_out($r) + [
                'description' => $r['description'], 'category' => (string)$r['category'], 'submitter' => $r['submitter'], 'submitted_at' => (int)$r['submitted_at'],
            ], $rows))]);

        case 'catalog-review':
            method('POST');
            $s = require_admin(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            $status = !empty($d['approve']) ? 'approved' : 'rejected';
            $n = q("UPDATE catalog SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE book_id = ? AND status = 'pending'",
                   [$status, $s['id'], time(), $bookId])->rowCount();
            if ($n !== 1) fail(404, 'not_found', 'No pending submission for this book.');
            json_out(200, ['status' => $status]);

        case 'catalog-remove':
            method('POST');
            require_admin(true);
            $bookId = int_param(body_json()['book_id'] ?? '');
            q('DELETE FROM catalog WHERE book_id = ?', [$bookId]);
            gc_book($bookId);
            json_out(200, ['ok' => true]);

        case 'catalog-add':
            method('POST');
            $s = require_user(true);
            if ($s['role'] === 'student') fail(403, 'forbidden', 'دسترسی به کتابخانهٔ عمومی برای نقش دانشجو مجاز نیست.');
            $bookId = int_param(body_json()['book_id'] ?? '');
            if (!one("SELECT 1 AS x FROM catalog WHERE book_id = ? AND status = 'approved'", [$bookId])) fail(404, 'not_found', 'Book is not in the catalog.');
            insert_ignore('user_books', ['user_id' => $s['id'], 'book_id' => $bookId, 'added_at' => time()]);
            $b = one('SELECT * FROM books WHERE id = ?', [$bookId]);
            json_out(200, ['book' => book_out($b)]);
    }
    return false;
}

/**
 * GET catalog with `page`: approved books filtered by q (all words, normalised), cat (category id, a top-level id
 * includes its children; "none" = no category), category (legacy name), author (exact), format (epub|pdf); sort
 * new|title|author; per ≤60 (default 24). categories = legacy-name facets of approved books matching q only.
 */
function catalog_page(array $p, int $uid, callable $out): array {
    $per = page_param($p, 'per', 24, 60);
    $page = page_param($p, 'page', 1, 100000);
    [$where, $args] = search_where(scalar_param($p, 'q'));
    array_unshift($where, "c.status = 'approved'");
    [$qWhere, $qArgs] = [$where, $args];
    category_where($p, $where, $args, true, false); // cat=<id>|none, legacy category=<name>
    $author = scalar_param($p, 'author');
    if ($author !== '') { $where[] = 'b.author = ?'; $args[] = mb_substr($author, 0, 300); }
    // A single book (deep link #catalog/book/<id>) without loading the whole catalog.
    $idParam = scalar_param($p, 'id');
    if ($idParam !== '') {
        if (!ctype_digit($idParam)) fail(400, 'bad_id', 'id must be a book id.');
        $where[] = 'b.id = ?'; $args[] = (int)$idParam;
    }
    $format = scalar_param($p, 'format');
    if ($format !== '') {
        if (!in_array($format, ['epub', 'pdf'], true)) fail(400, 'bad_format', 'format must be "epub" or "pdf".');
        $where[] = 'b.format = ?'; $args[] = $format;
    }
    $sort = scalar_param($p, 'sort') ?: 'new';
    $order = match ($sort) {
        'new' => 'c.reviewed_at DESC, c.submitted_at DESC, b.id DESC',
        'title' => 'b.title ASC, b.id ASC',
        'author' => 'b.author ASC, b.title ASC, b.id ASC',
        default => fail(400, 'bad_sort', 'sort must be new, title or author.'),
    };
    $w = 'WHERE ' . implode(' AND ', $where);
    $total = (int)one("SELECT COUNT(*) AS n FROM catalog c JOIN books b ON b.id = c.book_id $w", $args)['n'];
    $off = ($page - 1) * $per;
    $rows = all("SELECT b.*, c.description, c.category, c.submitted_at, u.display_name AS submitter,
                   (SELECT 1 FROM user_books ub WHERE ub.user_id = ? AND ub.book_id = b.id) AS in_library
                 FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                 $w ORDER BY $order LIMIT $per OFFSET $off", array_merge([$uid], $args));
    $cats = all("SELECT c.category AS name, COUNT(*) AS n FROM catalog c JOIN books b ON b.id = c.book_id
                 WHERE " . implode(' AND ', $qWhere) . " AND c.category <> '' GROUP BY c.category ORDER BY n DESC, c.category ASC LIMIT 500", $qArgs);
    return [
        'books' => with_categories(array_map($out, $rows)), 'total' => $total, 'page' => $page, 'per' => $per,
        'categories' => array_map(fn($r) => ['name' => (string)$r['name'], 'count' => (int)$r['n']], $cats),
    ];
}

function annotation_out(array $r): array {
    return [
        'uid' => $r['uid'], 'book_id' => (int)$r['book_id'], 'type' => $r['type'], 'cfi' => $r['cfi'], 'quote' => $r['quote'],
        'note' => (string)$r['note'], 'color' => $r['color'], 'created_at' => (int)$r['created_at'],
        'updated_at' => (int)$r['updated_at'], 'deleted' => (int)$r['deleted'] === 1,
    ];
}
