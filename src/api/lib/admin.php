<?php
// Admin: stats, users, invites, catalog curation, all books, settings.
declare(strict_types=1);
if (!defined('BORG_API')) { http_response_code(404); exit; }

function active_admin_count(int $exceptId = 0): int {
    return (int)one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0 AND id <> ?", [$exceptId])['n'];
}
function target_user(array $d): array {
    $u = one('SELECT * FROM users WHERE id = ?', [int_param($d['user_id'] ?? '')]);
    if (!$u) fail(404, 'not_found', 'User not found.');
    return $u;
}
function delete_book_everywhere(int $bookId): void {
    $b = one('SELECT * FROM books WHERE id = ?', [$bookId]);
    if (!$b) fail(404, 'not_found', 'Book not found.');
    q('DELETE FROM user_books WHERE book_id = ?', [$bookId]);
    q('DELETE FROM catalog WHERE book_id = ?', [$bookId]);
    q('DELETE FROM progress WHERE book_id = ?', [$bookId]);
    q('DELETE FROM annotations WHERE book_id = ?', [$bookId]);
    gc_book($bookId);
}

// ---------- Admin book list and bulk actions (migration 8) ----------
const ADMIN_BULK_MAX = 5000;      // books affected by one admin-bulk call
const ADMIN_BULK_ACTIONS = ['approve', 'reject', 'set_category', 'clear_category', 'add_category', 'remove_category', 'remove_catalog', 'add_catalog', 'delete'];

/**
 * WHERE parts for admin-books / admin-bulk filters over `books b LEFT JOIN catalog c`. Returns [where, args, qWhere,
 * qArgs] where q* is the search alone (used for facets). Unknown enum values → 400.
 */
function admin_books_filter(array $p): array {
    [$where, $args] = search_where(scalar_param($p, 'q'));
    [$qWhere, $qArgs] = [$where, $args];
    $status = scalar_param($p, 'status') ?: 'any';
    if (!in_array($status, ['any', 'approved', 'pending', 'rejected', 'none'], true)) fail(400, 'bad_status', 'status must be any, approved, pending, rejected or none.');
    if ($status === 'none') $where[] = 'c.book_id IS NULL';
    elseif ($status !== 'any') { $where[] = 'c.status = ?'; $args[] = $status; }
    // cat=<id>|none (migration 9) and the legacy category=<name> (__none__ = no legacy category).
    $legacyNone = scalar_param($p, 'category') === '__none__';
    if ($legacyNone) $where[] = "(c.category IS NULL OR c.category = '')";
    category_where($p, $where, $args, !$legacyNone);
    $format = scalar_param($p, 'format');
    if ($format !== '') {
        if (!in_array($format, ['epub', 'pdf'], true)) fail(400, 'bad_format', 'format must be "epub" or "pdf".');
        $where[] = 'b.format = ?'; $args[] = $format;
    }
    // uploader: "api" = api-book uploads; a user id = that user's uploads through the app (api uploads excluded,
    // matching the uploaders facet).
    $uploader = scalar_param($p, 'uploader');
    if ($uploader === 'api') $where[] = "b.source = 'api'";
    elseif ($uploader !== '') {
        if (!ctype_digit($uploader)) fail(400, 'bad_uploader', 'uploader must be a user id or "api".');
        $where[] = "b.uploaded_by = ? AND b.source <> 'api'"; $args[] = (int)$uploader;
    }
    if (!empty($p['no_cover']) && scalar_param($p, 'no_cover') !== '0') $where[] = 'b.has_cover = 0';
    return [$where, $args, $qWhere, $qArgs];
}
function where_sql(array $where): string { return $where ? 'WHERE ' . implode(' AND ', $where) : ''; }

function admin_books_list(array $p): array {
    [$where, $args, $qWhere, $qArgs] = admin_books_filter($p);
    $per = page_param($p, 'per', 50, 100);
    $page = page_param($p, 'page', 1, 100000);
    $sort = scalar_param($p, 'sort') ?: 'date';
    $dir = scalar_param($p, 'dir') ?: (in_array($sort, ['title', 'status'], true) ? 'asc' : 'desc');
    if (!in_array($dir, ['asc', 'desc'], true)) fail(400, 'bad_dir', 'dir must be asc or desc.');
    $d = strtoupper($dir);
    $order = match ($sort) {
        'date' => "b.created_at $d, b.id $d",
        'title' => "b.title $d, b.id $d",
        'size' => "b.size $d, b.id $d",
        // pending, approved, rejected, not in catalog; newest first within a status
        'status' => "CASE c.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 WHEN 'rejected' THEN 2 ELSE 3 END $d, b.created_at DESC, b.id DESC",
        default => fail(400, 'bad_sort', 'sort must be date, title, size or status.'),
    };
    $from = 'FROM books b LEFT JOIN catalog c ON c.book_id = b.id';
    $w = where_sql($where);
    $total = (int)one("SELECT COUNT(*) AS n $from $w", $args)['n'];
    $off = ($page - 1) * $per;
    $rows = all("SELECT b.*, c.status AS catalog_status, c.category, c.description, su.display_name AS submitter, u.display_name AS uploader_name
                 $from LEFT JOIN users u ON u.id = b.uploaded_by LEFT JOIN users su ON su.id = c.submitted_by
                 $w ORDER BY $order LIMIT $per OFFSET $off", $args);
    $readers = [];
    if ($rows) {
        $ids = array_map(fn($r) => (int)$r['id'], $rows);
        foreach (all('SELECT book_id, COUNT(*) AS n FROM user_books WHERE book_id IN (' . placeholders(count($ids)) . ') GROUP BY book_id', $ids) as $r) {
            $readers[(int)$r['book_id']] = (int)$r['n'];
        }
    }
    // Facets: for the search (q) only, other filters ignored.
    $qw = where_sql($qWhere);
    $status = ['approved' => 0, 'pending' => 0, 'rejected' => 0, 'none' => 0];
    foreach (all("SELECT c.status AS s, COUNT(*) AS n $from $qw GROUP BY c.status", $qArgs) as $r) {
        $k = $r['s'] === null ? 'none' : (string)$r['s'];
        if (isset($status[$k])) $status[$k] += (int)$r['n'];
    }
    $cats = all("SELECT c.category AS name, COUNT(*) AS n $from " . where_sql(array_merge($qWhere, ["c.category <> ''"]))
                . ' GROUP BY c.category ORDER BY n DESC, c.category ASC LIMIT 100', $qArgs);
    $up = [];
    foreach (all("SELECT CASE WHEN b.source = 'api' THEN 1 ELSE 0 END AS api, b.uploaded_by AS uid, u.display_name AS name, COUNT(*) AS n
                  FROM books b LEFT JOIN users u ON u.id = b.uploaded_by $qw GROUP BY CASE WHEN b.source = 'api' THEN 1 ELSE 0 END, b.uploaded_by, u.display_name", $qArgs) as $r) {
        $key = (int)$r['api'] === 1 ? 'api' : (string)(int)$r['uid'];
        if (!isset($up[$key])) $up[$key] = ['id' => $key === 'api' ? 'api' : (int)$r['uid'], 'name' => $key === 'api' ? 'API' : (string)($r['name'] ?? ''), 'count' => 0];
        $up[$key]['count'] += (int)$r['n'];
    }
    $up = array_values($up);
    usort($up, fn($a, $b) => $b['count'] <=> $a['count'] ?: strcmp((string)$a['id'], (string)$b['id']));
    $formats = ['epub' => 0, 'pdf' => 0];
    foreach (all("SELECT b.format AS f, COUNT(*) AS n FROM books b $qw GROUP BY b.format", $qArgs) as $r) {
        $formats[$r['f'] === 'pdf' ? 'pdf' : 'epub'] += (int)$r['n'];
    }
    return [
        'books' => with_categories(array_map(fn($r) => book_out($r) + [
            'catalog_status' => $r['catalog_status'], 'category' => (string)($r['category'] ?? ''), 'description' => (string)($r['description'] ?? ''),
            'submitter' => $r['submitter'], 'uploader_id' => (int)$r['uploaded_by'], 'uploader_name' => $r['uploader_name'],
            'created_at' => (int)$r['created_at'], 'source' => (string)$r['source'], 'in_libraries' => $readers[(int)$r['id']] ?? 0,
        ], $rows)),
        'total' => $total, 'page' => $page, 'per' => $per,
        'facets' => ['status' => $status, 'categories' => array_map(fn($r) => ['name' => (string)$r['name'], 'count' => (int)$r['n']], $cats),
                     'uploaders' => $up, 'formats' => $formats],
    ];
}

/** Run $fn(chunk, "?,?,…") over $ids in chunks of 500 (portable bound-parameter limits). */
function in_chunks(array $ids, callable $fn): void {
    foreach (array_chunk($ids, 500) as $chunk) $fn($chunk, placeholders(count($chunk)));
}
/** Inside a transaction: delete books rows among $ids that nothing references any more; returns their [id, sha256]. */
function gc_books_rows(array $ids): array {
    $gone = [];
    in_chunks($ids, function ($chunk, $ph) use (&$gone) {
        $rows = all("SELECT b.id, b.sha256 FROM books b WHERE b.id IN ($ph)
                     AND NOT EXISTS (SELECT 1 FROM user_books ub WHERE ub.book_id = b.id)
                     AND NOT EXISTS (SELECT 1 FROM catalog c WHERE c.book_id = b.id)", $chunk);
        if (!$rows) return;
        $del = array_map(fn($r) => (int)$r['id'], $rows);
        q('DELETE FROM books WHERE id IN (' . placeholders(count($del)) . ')', $del);
        foreach ($rows as $r) $gone[] = [(int)$r['id'], (string)$r['sha256']];
    });
    return $gone;
}

/** POST admin-bulk: one action on up to ADMIN_BULK_MAX books (ids or an admin-books filter), in one transaction. */
function admin_bulk(array $d, int $adminId): array {
    $action = scalar_param($d, 'action');
    if (!in_array($action, ADMIN_BULK_ACTIONS, true)) fail(400, 'bad_action', 'Unknown action.');
    $category = ''; $catId = 0;
    if ($action === 'set_category') $category = str_param($d, 'category', 80);
    if ($action === 'add_category' || $action === 'remove_category') {
        if (!array_key_exists('category_id', $d)) fail(400, 'missing_category_id', "Field 'category_id' is required.");
        $v = $d['category_id'];
        if (!(is_int($v) || (is_string($v) && ctype_digit($v))) || (int)$v <= 0) fail(400, 'bad_category_id', 'category_id must be a category id.');
        $catId = (int)$v;
        if (!isset(categories_tree()[$catId])) fail(400, 'unknown_category', "Category $catId does not exist.");
    }
    if (array_key_exists('ids', $d)) {
        $raw = $d['ids'];
        if (!is_array($raw) || !array_is_list($raw)) fail(400, 'bad_ids', 'ids must be a list of book ids.');
        if (count($raw) > ADMIN_BULK_MAX) fail(400, 'too_many', 'At most ' . ADMIN_BULK_MAX . ' books per call.');
        $ids = [];
        foreach ($raw as $v) {
            if (!(is_int($v) || (is_string($v) && ctype_digit($v))) || (int)$v <= 0) fail(400, 'bad_ids', 'ids must be a list of book ids.');
            $ids[(int)$v] = true;
        }
        $ids = array_keys($ids);
    } elseif (isset($d['filter']) && is_array($d['filter'])) {
        [$where, $args] = admin_books_filter($d['filter']);
        $w = where_sql($where);
        $n = (int)one("SELECT COUNT(*) AS n FROM books b LEFT JOIN catalog c ON c.book_id = b.id $w", $args)['n'];
        if ($n > ADMIN_BULK_MAX) fail(400, 'too_many', "The filter matches $n books; at most " . ADMIN_BULK_MAX . ' per call.');
        $ids = array_map('intval', array_column(all("SELECT b.id FROM books b LEFT JOIN catalog c ON c.book_id = b.id $w", $args), 'id'));
    } else {
        fail(400, 'missing_target', 'Give ids or filter.');
    }
    $requested = count($ids);
    $affected = 0; $files = []; $touched = [];
    $now = time();
    db()->beginTransaction();
    try {
        $run = function (string $sql, array $before, array $after = []) use ($ids, &$affected) {
            in_chunks($ids, function ($chunk, $ph) use ($sql, $before, $after, &$affected) {
                $affected += q(str_replace('{IN}', $ph, $sql), array_merge($before, $chunk, $after))->rowCount();
            });
        };
        switch ($action) {
            case 'approve':
            case 'reject':
                $st = $action === 'approve' ? 'approved' : 'rejected';
                $run('UPDATE catalog SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE book_id IN ({IN}) AND status <> ?', [$st, $adminId, $now], [$st]);
                break;
            case 'set_category': // legacy name -> find or create a top-level category, replace the set with it
            case 'clear_category':
                $cid = $action === 'set_category' ? category_find_or_create($category, 0) : 0;
                in_chunks($ids, function ($chunk, $ph) use ($cid, &$affected, &$touched) {
                    $rows = all("SELECT book_id, category FROM catalog WHERE book_id IN ($ph)", $chunk);
                    if (!$rows) return;
                    $books = array_map(fn($r) => (int)$r['book_id'], $rows);
                    $links = [];
                    foreach (all('SELECT catalog_id, category_id FROM catalog_categories WHERE catalog_id IN (' . placeholders(count($books)) . ')', $books) as $l) {
                        $links[(int)$l['catalog_id']][] = (int)$l['category_id'];
                    }
                    foreach ($rows as $r) {
                        $b = (int)$r['book_id'];
                        $cur = $links[$b] ?? [];
                        if ($cid ? $cur === [$cid] : ($cur === [] && (string)$r['category'] === '')) continue;
                        q('DELETE FROM catalog_categories WHERE catalog_id = ?', [$b]);
                        if ($cid) q('INSERT INTO catalog_categories (catalog_id, category_id) VALUES (?, ?)', [$b, $cid]);
                        $affected++; $touched[] = $b;
                    }
                });
                break;
            case 'add_category': // books in the catalog that lack it and have fewer than the maximum
                in_chunks($ids, function ($chunk, $ph) use ($catId, &$affected, &$touched) {
                    $rows = all("SELECT c.book_id FROM catalog c WHERE c.book_id IN ($ph)
                                 AND NOT EXISTS (SELECT 1 FROM catalog_categories x WHERE x.catalog_id = c.book_id AND x.category_id = ?)
                                 AND (SELECT COUNT(*) FROM catalog_categories y WHERE y.catalog_id = c.book_id) < " . CATEGORY_MAX_PER_BOOK, array_merge($chunk, [$catId]));
                    foreach ($rows as $r) {
                        q('INSERT INTO catalog_categories (catalog_id, category_id) VALUES (?, ?)', [(int)$r['book_id'], $catId]);
                        $affected++; $touched[] = (int)$r['book_id'];
                    }
                });
                break;
            case 'remove_category':
                in_chunks($ids, function ($chunk, $ph) use ($catId, &$affected, &$touched) {
                    $rows = all("SELECT catalog_id FROM catalog_categories WHERE category_id = ? AND catalog_id IN ($ph)", array_merge([$catId], $chunk));
                    if (!$rows) return;
                    $books = array_map(fn($r) => (int)$r['catalog_id'], $rows);
                    $affected += q('DELETE FROM catalog_categories WHERE category_id = ? AND catalog_id IN (' . placeholders(count($books)) . ')', array_merge([$catId], $books))->rowCount();
                    array_push($touched, ...$books);
                });
                break;
            case 'add_catalog':
                in_chunks($ids, function ($chunk, $ph) use ($adminId, $now, &$affected) {
                    $new = all("SELECT b.id FROM books b WHERE b.id IN ($ph) AND NOT EXISTS (SELECT 1 FROM catalog c WHERE c.book_id = b.id)", $chunk);
                    foreach ($new as $r) {
                        q('INSERT INTO catalog (book_id, status, description, category, submitted_by, submitted_at, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          [(int)$r['id'], 'approved', '', '', $adminId, $now, $adminId, $now]);
                        $affected++;
                    }
                });
                break;
            case 'remove_catalog':
                $run('DELETE FROM catalog WHERE book_id IN ({IN})', []);
                $files = gc_books_rows($ids);
                break;
            case 'delete': // book-delete semantics for each book
                in_chunks($ids, function ($chunk, $ph) use (&$affected) {
                    $affected += (int)one("SELECT COUNT(*) AS n FROM books WHERE id IN ($ph)", $chunk)['n'];
                    foreach (['user_books', 'catalog', 'progress', 'annotations'] as $t) q("DELETE FROM $t WHERE book_id IN ($ph)", $chunk);
                });
                $files = gc_books_rows($ids);
                break;
        }
        sync_legacy_category($touched);
        db()->commit();
    } catch (Throwable $e) {
        if (db()->inTransaction()) db()->rollBack();
        throw $e;
    }
    foreach ($files as [$id, $sha]) { // files only after the rows are gone for good
        @unlink(book_path($sha, 'epub')); @unlink(book_path($sha, 'pdf')); @unlink(cover_path($id));
    }
    return ['affected' => $affected, 'skipped' => $requested - $affected];
}

// ---------- Catalog categories (migration 9) ----------
/** An optional id field: 0 when absent, null, '' or 0; else a positive id (400 bad_<k>). */
function opt_id_param(array $d, string $k): int {
    $v = $d[$k] ?? null;
    if ($v === null || $v === '' || $v === 0 || $v === '0') return 0;
    if (!(is_int($v) || (is_string($v) && ctype_digit($v))) || (int)$v <= 0) fail(400, 'bad_' . $k, "Field '$k' must be an id.");
    return (int)$v;
}
function category_has_children(int $id): bool { return (bool)one('SELECT 1 AS x FROM categories WHERE parent_id = ? LIMIT 1', [$id]); }

/** POST category-save {id?, name, parent_id}: create, rename or move. Returns the saved category. */
function category_save(array $d): array {
    $name = str_param($d, 'name', 80);
    $id = opt_id_param($d, 'id');
    $tree = categories_tree();
    $cur = null;
    if ($id) {
        $cur = $tree[$id] ?? null;
        if (!$cur) fail(404, 'not_found', 'Category not found.');
    }
    // parent_id absent on an update keeps the current parent.
    $parent = ($cur && !array_key_exists('parent_id', $d)) ? $cur['parent_id'] : opt_id_param($d, 'parent_id');
    if ($parent !== 0) {
        $p = $tree[$parent] ?? null;
        if (!$p || $parent === $id) fail(400, 'bad_parent', 'parent_id must be 0 or an existing top-level category.');
        if ($p['parent_id'] !== 0) fail(400, 'too_deep', 'Categories have two levels; a subcategory cannot have children.');
        if ($id && category_has_children($id)) fail(409, 'has_children', 'A category with subcategories cannot become a subcategory.');
    }
    if (one('SELECT id FROM categories WHERE parent_id = ? AND name = ? AND id <> ?', [$parent, $name, $id])) fail(409, 'duplicate', 'A category with this name already exists here.');
    $sort = $cur && $cur['parent_id'] === $parent ? $cur['sort']
          : (int)one('SELECT COALESCE(MAX(sort), -1) + 1 AS n FROM categories WHERE parent_id = ?', [$parent])['n'];
    try {
        if ($cur) {
            q('UPDATE categories SET name = ?, parent_id = ?, sort = ? WHERE id = ?', [$name, $parent, $sort, $id]);
        } else {
            q('INSERT INTO categories (parent_id, name, sort, created_at) VALUES (?, ?, ?, ?)', [$parent, $name, $sort, time()]);
            $id = category_find($name, $parent);
        }
    } catch (PDOException $e) { // unique (parent_id, name) raced
        if (category_find($name, $parent) && category_find($name, $parent) !== $id) fail(409, 'duplicate', 'A category with this name already exists here.');
        throw $e;
    }
    $tree = categories_tree(true);
    if ($cur) { // the top-level name of its books (and of its children's books) may have changed
        $scope = [$id];
        foreach ($tree as $c) if ($c['parent_id'] === $id) $scope[] = $c['id'];
        sync_legacy_category(books_in_categories($scope));
    }
    $c = $tree[$id];
    return ['id' => $c['id'], 'name' => $c['name'], 'parent_id' => $c['parent_id'], 'sort' => $c['sort'],
            'parent_name' => category_ref($c)['parent_name']];
}

/** POST category-delete {id, move_to?}: relink its books to move_to (or unlink them), then delete it. */
function category_delete(array $d): array {
    $id = int_param($d['id'] ?? '');
    $tree = categories_tree();
    if (!isset($tree[$id])) fail(404, 'not_found', 'Category not found.');
    if (category_has_children($id)) fail(409, 'has_children', 'Delete or move its subcategories first.');
    $to = opt_id_param($d, 'move_to');
    if ($to && ($to === $id || !isset($tree[$to]))) fail(400, 'unknown_category', 'move_to must be another existing category.');
    $books = books_in_categories([$id]);
    db()->beginTransaction();
    try {
        if ($to) foreach ($books as $b) insert_ignore('catalog_categories', ['catalog_id' => $b, 'category_id' => $to]);
        q('DELETE FROM catalog_categories WHERE category_id = ?', [$id]);
        q('DELETE FROM categories WHERE id = ?', [$id]);
        categories_tree(true);
        sync_legacy_category($books);
        db()->commit();
    } catch (Throwable $e) {
        if (db()->inTransaction()) db()->rollBack();
        categories_tree(true);
        throw $e;
    }
    return ['moved' => $to ? count($books) : 0, 'unlinked' => $to ? 0 : count($books)];
}

/** POST category-order {parent_id, ids}: sort = position among those siblings. */
function category_order(array $d): void {
    $parent = opt_id_param($d, 'parent_id');
    $raw = $d['ids'] ?? null;
    if (!is_array($raw) || !array_is_list($raw) || !$raw) fail(400, 'bad_ids', 'ids must be a non-empty list of category ids.');
    $tree = categories_tree();
    $ids = [];
    foreach ($raw as $v) {
        if (!(is_int($v) || (is_string($v) && ctype_digit($v)))) fail(400, 'bad_ids', 'ids must be a list of category ids.');
        $v = (int)$v;
        if (isset($ids[$v]) || !isset($tree[$v]) || $tree[$v]['parent_id'] !== $parent) fail(400, 'bad_ids', 'ids must be distinct categories under parent_id.');
        $ids[$v] = true;
    }
    db()->beginTransaction();
    try {
        $i = 0;
        foreach (array_keys($ids) as $cid) q('UPDATE categories SET sort = ? WHERE id = ?', [$i++, $cid]);
        $tree = categories_tree(true);
        // The first category (and so catalog.category) of the books under these siblings may have changed.
        $scope = array_keys($ids);
        if ($parent === 0) foreach ($tree as $c) if ($c['parent_id'] !== 0) $scope[] = $c['id'];
        sync_legacy_category(books_in_categories($scope));
        db()->commit();
    } catch (Throwable $e) {
        if (db()->inTransaction()) db()->rollBack();
        categories_tree(true);
        throw $e;
    }
}

// ---------- v8.4 client error reports ----------
/** Remove control chars, URL query strings/fragments, e-mails, token-like strings and secrets; trim to $max. */
function scrub_report(string $s, int $max): string {
    $s = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $s) ?? '';
    $s = preg_replace('~((?:https?|blob|file)://[^\s?#"\'<>()]*)[?#][^\s"\'<>()]*?(?=:\d+:\d+|:\d+\b|[\s"\'<>()]|$)~iu', '$1', $s) ?? '';
    $s = preg_replace('/[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}/iu', '[email]', $s) ?? '';
    $s = preg_replace('/\b(token|csrf|password|passwd|secret|sid|setup_key|invite)(["\']?\s*[=:]\s*)["\']?[^\s"\'&,;]+/iu', '$1$2[redacted]', $s) ?? '';
    $s = preg_replace('/\b[A-Fa-f0-9]{32,}\b/', '[token]', $s) ?? '';
    $s = preg_replace('/\b[A-Za-z0-9_\-]{40,}\b/', '[token]', $s) ?? '';
    return mb_substr(trim($s), 0, $max);
}
function report_str(array $it, string $k): string { return isset($it[$k]) && is_scalar($it[$k]) ? (string)$it[$k] : ''; }
/** Store one report: dedupe by fingerprint within the same (UTC) day. Returns false when the item is skipped. */
function store_client_error(array $it, int $userId): bool {
    $message = scrub_report(report_str($it, 'message'), 500);
    if ($message === '') return false;
    $kind = strtolower(trim(report_str($it, 'kind')));
    if (!preg_match('/^[a-z][a-z0-9_-]{0,19}$/', $kind)) $kind = 'other';
    $version = substr(preg_replace('/[^0-9A-Za-z.+\-]/', '', report_str($it, 'app_version')) ?? '', 0, 20);
    $source = scrub_report(report_str($it, 'source'), 300);
    $stack = scrub_report(report_str($it, 'stack'), 4000);
    $path = parse_url(report_str($it, 'path') ?: report_str($it, 'url_path'), PHP_URL_PATH);
    $path = scrub_report(is_string($path) ? $path : '', 300);
    $ua = report_str($it, 'user_agent') ?: (string)($_SERVER['HTTP_USER_AGENT'] ?? '');
    $ua = mb_substr(trim(preg_replace('/[\x00-\x1F\x7F]/u', '', $ua) ?? ''), 0, 300);
    $n = isset($it['count']) && is_numeric($it['count']) ? max(1, min(100, (int)$it['count'])) : 1;
    $fp = hash('sha256', $kind . "\n" . $message . "\n" . $source . "\n" . $version);
    $now = time(); $day = $now - ($now % 86400);
    $row = one('SELECT id FROM client_errors WHERE fingerprint = ? AND created_at >= ? ORDER BY id DESC LIMIT 1', [$fp, $day]);
    if ($row) {
        q('UPDATE client_errors SET count = count + ?, last_seen = ? WHERE id = ?', [$n, $now, $row['id']]);
    } else {
        q('INSERT INTO client_errors (created_at, user_id, app_version, kind, message, source, stack, url_path, user_agent, count, fingerprint, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [$now, $userId, $version, $kind, $message, $source, $stack, $path, $ua, $n, $fp, $now]);
    }
    return true;
}
function cap_client_errors(): void {
    $cap = max(1, (int)config()['client_errors_max']);
    if ((int)one('SELECT COUNT(*) AS n FROM client_errors')['n'] <= $cap) return;
    $edge = one("SELECT id, last_seen FROM client_errors ORDER BY last_seen DESC, id DESC LIMIT 1 OFFSET $cap");
    if ($edge) q('DELETE FROM client_errors WHERE last_seen < ? OR (last_seen = ? AND id <= ?)', [$edge['last_seen'], $edge['last_seen'], $edge['id']]);
}

// ---------- Admin: import book directly from URL (EPUB / PDF) ----------
function validate_safe_download_url(string $url): string {
    $parts = parse_url($url);
    if (!$parts || empty($parts['host'])) fail(400, 'invalid_url', 'نشانی واردشده نامعتبر است.');
    $scheme = strtolower($parts['scheme'] ?? '');
    if ($scheme !== 'http' && $scheme !== 'https') fail(400, 'invalid_scheme', 'فقط پیوندهای http و https پشتیبانی می‌شوند.');
    if (isset($parts['port']) && !in_array((int)$parts['port'], [80, 443, 8080, 8443], true)) {
        fail(400, 'forbidden_port', 'پورت مشخص‌شده مجاز نیست.');
    }
    $host = strtolower($parts['host']);
    if ($host === 'localhost' || str_ends_with($host, '.localhost') || str_ends_with($host, '.local') || str_ends_with($host, '.internal')) {
        fail(400, 'forbidden_host', 'دانلود از شبکه داخلی مجاز نیست.');
    }
    $ips = [];
    if (filter_var($host, FILTER_VALIDATE_IP)) {
        $ips[] = $host;
    } else {
        $records = @dns_get_record($host, DNS_A);
        if ($records) {
            foreach ($records as $r) {
                if (!empty($r['ip'])) $ips[] = $r['ip'];
            }
        }
        if (!$ips) {
            $ip = @gethostbyname($host);
            if ($ip && $ip !== $host) $ips[] = $ip;
        }
    }
    if (!$ips) fail(400, 'resolve_failed', 'آدرس سرور یافت نشد یا دامنه در دسترس نیست.');
    foreach ($ips as $ip) {
        if (!filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE)) {
            fail(400, 'forbidden_host', 'دانلود از آدرس‌های شبکه داخلی یا نامعتبر مجاز نیست.');
        }
        if ($ip === '127.0.0.1' || $ip === '0.0.0.0' || str_starts_with($ip, '127.') || str_starts_with($ip, '169.254.')) {
            fail(400, 'forbidden_host', 'دانلود از این نشانی مجاز نیست.');
        }
    }
    return $url;
}

function download_book_from_url(string $url, string $destPath, int $maxBytes, int $timeout = 45): array {
    $curUrl = validate_safe_download_url($url);
    $redirects = 0;
    $maxRedirects = 5;
    $finalUrl = $curUrl;

    if (extension_loaded('curl')) {
        while ($redirects <= $maxRedirects) {
            $fp = @fopen($destPath, 'wb');
            if (!$fp) fail(500, 'storage', 'امکان ذخیره فایل موقت در سرور وجود ندارد.');

            $ch = curl_init($curUrl);
            curl_setopt_array($ch, [
                CURLOPT_FILE => $fp,
                CURLOPT_FOLLOWLOCATION => false,
                CURLOPT_TIMEOUT => $timeout,
                CURLOPT_CONNECTTIMEOUT => 15,
                CURLOPT_USERAGENT => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 BorgReader/8.13',
                CURLOPT_HTTPHEADER => ['Accept: application/epub+zip, application/octet-stream, */*'],
                CURLOPT_SSL_VERIFYPEER => true,
                CURLOPT_SSL_VERIFYHOST => 2,
                CURLOPT_NOPROGRESS => false,
                CURLOPT_PROGRESSFUNCTION => function ($resource, $downloadSize, $downloaded) use ($maxBytes) {
                    return ($downloaded > $maxBytes) ? 1 : 0;
                },
            ]);

            $ok = curl_exec($ch);
            $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
            $curlErr = curl_errno($ch);
            $curlMsg = curl_error($ch);
            fclose($fp);

            if ($curlErr === CURLE_ABORTED_BY_CALLBACK) {
                curl_close($ch);
                @unlink($destPath);
                fail(413, 'too_large', 'حجم فایل بیشتر از سقف مجاز سرور است (بیش از ' . round($maxBytes / 1048576) . ' مگابایت).');
            }

            if ($curlErr !== 0) {
                curl_close($ch);
                @unlink($destPath);
                if ($curlErr === CURLE_OPERATION_TIMEDOUT) {
                    fail(504, 'download_timeout', 'زمان دریافت فایل به پایان رسید (سرور مبدا پاسخ نداد).');
                }
                fail(502, 'download_failed', 'خطا در برقراری ارتباط با سرور فایل: ' . $curlMsg);
            }

            if ($httpCode >= 301 && $httpCode <= 308) {
                $redirectUrl = (string)curl_getinfo($ch, CURLINFO_REDIRECT_URL);
                curl_close($ch);
                @unlink($destPath);
                if ($redirectUrl === '') fail(502, 'bad_redirect', 'تغییر مسیر نامعتبر از سرور مبدا.');
                if (!preg_match('#^https?://#i', $redirectUrl)) {
                    $p = parse_url($curUrl);
                    $base = $p['scheme'] . '://' . $p['host'] . (isset($p['port']) ? ':' . $p['port'] : '');
                    if (str_starts_with($redirectUrl, '/')) {
                        $redirectUrl = $base . $redirectUrl;
                    } else {
                        $dir = dirname($p['path'] ?? '/');
                        $redirectUrl = $base . ($dir === '/' ? '' : $dir) . '/' . $redirectUrl;
                    }
                }
                $curUrl = validate_safe_download_url($redirectUrl);
                $finalUrl = $curUrl;
                $redirects++;
                continue;
            }

            curl_close($ch);

            if ($httpCode < 200 || $httpCode >= 300) {
                @unlink($destPath);
                fail(502, 'http_error', 'سرور مبدا کد وضعیت خطای ' . $httpCode . ' برگرداند.');
            }

            $size = (int)filesize($destPath);
            if ($size <= 0) {
                @unlink($destPath);
                fail(400, 'empty_file', 'فایل دریافت‌شده خالی است.');
            }
            if ($size > $maxBytes) {
                @unlink($destPath);
                fail(413, 'too_large', 'حجم فایل بیشتر از سقف مجاز سرور است.');
            }

            return ['size' => $size, 'final_url' => $finalUrl];
        }
        @unlink($destPath);
        fail(502, 'too_many_redirects', 'تعداد هدایت‌های تغییر مسیر بیش از ۵ بار است.');
    } else {
        // Stream fallback
        while ($redirects <= $maxRedirects) {
            $ctx = stream_context_create([
                'http' => [
                    'method' => 'GET',
                    'timeout' => $timeout,
                    'follow_location' => 0,
                    'user_agent' => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 BorgReader/8.13',
                    'header' => "Accept: application/epub+zip, application/octet-stream, */*\r\n",
                    'ignore_errors' => true,
                ],
                'ssl' => [
                    'verify_peer' => true,
                    'verify_peer_name' => true,
                ],
            ]);

            $in = @fopen($curUrl, 'rb', false, $ctx);
            if (!$in) fail(502, 'download_failed', 'اتصال به سرور فایل برقرار نشد.');

            $headers = $http_response_header ?? [];
            $statusLine = $headers[0] ?? '';
            preg_match('#HTTP/\S+\s+(\d+)#', $statusLine, $sm);
            $httpCode = isset($sm[1]) ? (int)$sm[1] : 200;

            if ($httpCode >= 301 && $httpCode <= 308) {
                $location = '';
                foreach ($headers as $h) {
                    if (stripos($h, 'Location:') === 0) {
                        $location = trim(substr($h, 9));
                        break;
                    }
                }
                fclose($in);
                if ($location === '') fail(502, 'bad_redirect', 'تغییر مسیر نامعتبر از سرور مبدا.');
                if (!preg_match('#^https?://#i', $location)) {
                    $p = parse_url($curUrl);
                    $base = $p['scheme'] . '://' . $p['host'] . (isset($p['port']) ? ':' . $p['port'] : '');
                    if (str_starts_with($location, '/')) {
                        $location = $base . $location;
                    } else {
                        $dir = dirname($p['path'] ?? '/');
                        $location = $base . ($dir === '/' ? '' : $dir) . '/' . $location;
                    }
                }
                $curUrl = validate_safe_download_url($location);
                $finalUrl = $curUrl;
                $redirects++;
                continue;
            }

            if ($httpCode < 200 || $httpCode >= 300) {
                fclose($in);
                fail(502, 'http_error', 'سرور مبدا کد وضعیت خطای ' . $httpCode . ' برگرداند.');
            }

            $out = @fopen($destPath, 'wb');
            if (!$out) { fclose($in); fail(500, 'storage', 'امکان ذخیره فایل موقت در سرور وجود ندارد.'); }

            $bytes = 0;
            while (!feof($in)) {
                $chunk = fread($in, 65536);
                if ($chunk === false) break;
                $bytes += strlen($chunk);
                if ($bytes > $maxBytes) {
                    fclose($in); fclose($out); @unlink($destPath);
                    fail(413, 'too_large', 'حجم فایل بیشتر از سقف مجاز سرور است.');
                }
                fwrite($out, $chunk);
            }
            fclose($in);
            fclose($out);

            $size = (int)filesize($destPath);
            if ($size <= 0) { @unlink($destPath); fail(400, 'empty_file', 'فایل دریافت‌شده خالی است.'); }
            return ['size' => $size, 'final_url' => $finalUrl];
        }
        @unlink($destPath);
        fail(502, 'too_many_redirects', 'تعداد هدایت‌های تغییر مسیر بیش از ۵ بار است.');
    }
}

function handle_admin_route(string $route): bool {
    global $method;
    switch ($route) {
        case 'client-error':
            // Anonymous visitors may report (strict per-IP limit); logged-in users must send the CSRF token.
            method('POST');
            if ((int)($_SERVER['CONTENT_LENGTH'] ?? 0) > 131072) fail(413, 'too_large', 'Report too large.');
            $s = current_session();
            if ($s) { require_user(true); rate_limit('cerr-user|' . $s['id'], 120, 3600); }
            else rate_limit('cerr-ip|' . client_ip(), max(1, (int)config()['client_errors_ip_per_hour']), 3600);
            $items = body_json()['items'] ?? null;
            if (!is_array($items) || !$items || !array_is_list($items)) fail(400, 'bad_items', 'items must be a non-empty list.');
            if (count($items) > 20) fail(400, 'too_many_items', 'At most 20 items per request.');
            $ok = 0;
            foreach ($items as $it) if (is_array($it) && store_client_error($it, $s ? (int)$s['id'] : 0)) $ok++;
            if ($ok) cap_client_errors();
            json_out(200, ['accepted' => $ok, 'skipped' => count($items) - $ok]);

        case 'errors':
            method('GET');
            require_admin();
            $where = []; $args = [];
            foreach (['version' => 'app_version', 'kind' => 'kind'] as $p => $col) {
                if (isset($_GET[$p]) && is_string($_GET[$p]) && $_GET[$p] !== '') { $where[] = "$col = ?"; $args[] = mb_substr($_GET[$p], 0, 20); }
            }
            $w = $where ? 'WHERE ' . implode(' AND ', $where) : '';
            $per = max(1, min(100, (int)($_GET['per'] ?? 50)));
            $page = max(1, (int)($_GET['page'] ?? 1));
            $off = ($page - 1) * $per;
            $groups = all("SELECT fingerprint, MAX(id) AS id, SUM(count) AS total, MAX(last_seen) AS last_seen, MIN(created_at) AS first_seen,
                             COUNT(DISTINCT CASE WHEN user_id > 0 THEN user_id END) AS users, MIN(user_id) AS min_user
                           FROM client_errors $w GROUP BY fingerprint ORDER BY last_seen DESC, id DESC LIMIT $per OFFSET $off", $args);
            $detail = [];
            if ($groups) {
                $ids = array_map(fn($g) => (int)$g['id'], $groups);
                foreach (all('SELECT * FROM client_errors WHERE id IN (' . implode(',', $ids) . ')') as $r) $detail[(int)$r['id']] = $r;
            }
            $col = fn(string $c) => array_values(array_filter(array_column(all("SELECT DISTINCT $c FROM client_errors ORDER BY $c DESC"), $c), fn($v) => $v !== ''));
            json_out(200, [
                'errors' => array_map(function ($g) use ($detail) {
                    $r = $detail[(int)$g['id']];
                    return ['id' => (int)$r['id'], 'fingerprint' => $g['fingerprint'], 'kind' => $r['kind'], 'app_version' => $r['app_version'],
                        'message' => $r['message'], 'source' => $r['source'], 'stack' => (string)$r['stack'], 'url_path' => $r['url_path'],
                        'user_agent' => $r['user_agent'], 'count' => (int)$g['total'], 'first_seen' => (int)$g['first_seen'],
                        'last_seen' => (int)$g['last_seen'], 'users' => (int)$g['users'], 'anonymous' => (int)$g['min_user'] === 0];
                }, $groups),
                'total' => (int)one("SELECT COUNT(DISTINCT fingerprint) AS n FROM client_errors $w", $args)['n'],
                'page' => $page, 'per' => $per, 'versions' => $col('app_version'), 'kinds' => $col('kind'),
            ]);

        case 'errors-clear':
            method('POST');
            require_admin(true);
            $d = body_json();
            if (!empty($d['all'])) $n = q('DELETE FROM client_errors')->rowCount();
            elseif (isset($d['fingerprint']) && is_string($d['fingerprint']) && preg_match('/^[a-f0-9]{64}$/', $d['fingerprint'])) $n = q('DELETE FROM client_errors WHERE fingerprint = ?', [$d['fingerprint']])->rowCount();
            elseif (isset($d['id'])) $n = q('DELETE FROM client_errors WHERE id = ?', [int_param($d['id'])])->rowCount();
            else fail(400, 'missing_target', 'Give all, id or fingerprint.');
            json_out(200, ['deleted' => $n]);

        case 'admin-stats':
            method('GET');
            require_admin();
            $n = fn(string $sql) => (int)one($sql)['n'];
            json_out(200, [
                'users' => $n('SELECT COUNT(*) AS n FROM users'),
                'active_users' => $n('SELECT COUNT(*) AS n FROM users WHERE disabled = 0'),
                'books' => $n('SELECT COUNT(*) AS n FROM books'),
                'storage_bytes' => (int)(one('SELECT COALESCE(SUM(size), 0) AS n FROM books')['n']),
                'catalog_approved' => $n("SELECT COUNT(*) AS n FROM catalog WHERE status = 'approved'"),
                'catalog_pending' => $n("SELECT COUNT(*) AS n FROM catalog WHERE status = 'pending'"),
                'open_invites' => $n('SELECT COUNT(*) AS n FROM invites WHERE used_by IS NULL AND expires_at > ' . time()),
                'client_errors' => $n('SELECT COUNT(DISTINCT fingerprint) AS n FROM client_errors'),
            ]);

        case 'users':
            method('GET');
            require_admin();
            json_out(200, ['users' => array_map(fn($r) => public_user($r) + [
                'created_at' => (int)$r['created_at'], 'books' => (int)$r['books'], 'disabled' => (int)$r['disabled'] === 1,
                'last_seen' => $r['last_seen'] ? (int)$r['last_seen'] : null,
            ], all('SELECT u.*, (SELECT COUNT(*) FROM user_books ub WHERE ub.user_id = u.id) AS books,
                           (SELECT MAX(created_at) FROM sessions s WHERE s.user_id = u.id) AS last_seen
                    FROM users u ORDER BY u.created_at'))]);

        case 'user-update':
            method('POST');
            $s = require_admin(true);
            $d = body_json();
            $u = target_user($d);
            $self = (int)$u['id'] === (int)$s['id'];
            if (array_key_exists('disabled', $d)) {
                $dis = !empty($d['disabled']) ? 1 : 0;
                if ($dis && $self) fail(400, 'self', 'You cannot disable yourself.');
                if ($dis && $u['role'] === 'admin' && active_admin_count((int)$u['id']) === 0) fail(400, 'last_admin', 'At least one active admin is required.');
                q('UPDATE users SET disabled = ? WHERE id = ?', [$dis, $u['id']]);
                if ($dis) q('DELETE FROM sessions WHERE user_id = ?', [$u['id']]);
            }
            if (array_key_exists('role', $d)) {
                $role = in_array($d['role'], ['admin', 'student', 'user'], true) ? $d['role'] : 'user';
                if ($role !== 'admin' && $self) fail(400, 'self', 'You cannot remove your own admin role.');
                if ($role !== 'admin' && $u['role'] === 'admin' && active_admin_count((int)$u['id']) === 0) fail(400, 'last_admin', 'At least one active admin is required.');
                q('UPDATE users SET role = ? WHERE id = ?', [$role, $u['id']]);
            }
            if (array_key_exists('name', $d)) q('UPDATE users SET display_name = ? WHERE id = ?', [str_param($d, 'name', 120), $u['id']]);
            json_out(200, ['user' => public_user(one('SELECT * FROM users WHERE id = ?', [$u['id']]))]);

        case 'user-reset-password':
            method('POST');
            require_admin(true);
            $u = target_user(body_json());
            $alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789';
            $pass = '';
            for ($i = 0; $i < 12; $i++) $pass .= $alphabet[random_int(0, strlen($alphabet) - 1)];
            q('UPDATE users SET password_hash = ? WHERE id = ?', [password_hash($pass, PASSWORD_DEFAULT), $u['id']]);
            q('DELETE FROM sessions WHERE user_id = ?', [$u['id']]);
            json_out(200, ['password' => $pass]);

        case 'user-delete':
            method('POST');
            $s = require_admin(true);
            $u = target_user(body_json());
            if ((int)$u['id'] === (int)$s['id']) fail(400, 'self', 'You cannot delete yourself.');
            if ($u['role'] === 'admin' && active_admin_count((int)$u['id']) === 0) fail(400, 'last_admin', 'At least one active admin is required.');
            $books = array_map('intval', array_column(all('SELECT book_id FROM user_books WHERE user_id = ?', [$u['id']]), 'book_id'));
            foreach (['annotations', 'progress', 'user_books', 'sessions'] as $t) q("DELETE FROM $t WHERE user_id = ?", [$u['id']]);
            q('DELETE FROM users WHERE id = ?', [$u['id']]);
            foreach ($books as $bid) gc_book($bid);
            json_out(200, ['ok' => true]);

        case 'admin-assign-book':
            method('POST');
            $s = require_admin(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            $userId = int_param($d['user_id'] ?? '');
            $action = ($d['action'] ?? 'assign') === 'unassign' ? 'unassign' : 'assign';
            if (!one('SELECT 1 AS x FROM books WHERE id = ?', [$bookId])) fail(404, 'not_found', 'Book not found.');
            if (!one('SELECT 1 AS x FROM users WHERE id = ?', [$userId])) fail(404, 'not_found', 'User not found.');
            if ($action === 'assign') {
                insert_ignore('user_books', ['user_id' => $userId, 'book_id' => $bookId, 'added_at' => time()]);
            } else {
                q('DELETE FROM user_books WHERE user_id = ? AND book_id = ?', [$userId, $bookId]);
            }
            json_out(200, ['ok' => true]);

        case 'admin-book-users':
            method('GET');
            require_admin();
            $bookId = int_param($_GET['id'] ?? '');
            $assigned = array_map('intval', array_column(all('SELECT user_id FROM user_books WHERE book_id = ?', [$bookId]), 'user_id'));
            json_out(200, ['assigned_user_ids' => $assigned]);

        case 'invite-revoke':
            method('POST');
            require_admin(true);
            $id = str_param(body_json(), 'id', 64);
            $n = q('DELETE FROM invites WHERE code_hash = ? AND used_by IS NULL', [$id])->rowCount();
            if ($n !== 1) fail(404, 'not_found', 'Invite not found or already used.');
            json_out(200, ['ok' => true]);

        case 'catalog-all':
            method('GET');
            require_admin();
            $rows = all('SELECT b.*, c.status, c.description, c.category, c.submitted_at, u.display_name AS submitter
                         FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                         ORDER BY c.submitted_at DESC');
            json_out(200, ['books' => with_categories(array_map(fn($r) => book_out($r) + [
                'status' => $r['status'], 'description' => $r['description'], 'category' => (string)$r['category'], 'submitter' => $r['submitter'], 'submitted_at' => (int)$r['submitted_at'],
            ], $rows))]);

        case 'catalog-update':
            method('POST');
            require_admin(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            if (!one('SELECT 1 AS x FROM catalog WHERE book_id = ?', [$bookId])) fail(404, 'not_found', 'Book is not in the catalog.');
            // Each field is updated only when its key is present; everything is validated before anything is written.
            $desc = array_key_exists('description', $d) ? str_param($d, 'description', 2000, false) : null;
            $category = opt_str_param($d, 'category', 80);
            $catIds = category_ids_param($d); // wins over the legacy `category` name when both are sent
            $seriesId = series_id_param($d); // v8.14; attaching an approved book makes it a back issue (not delivered)
            $cols = book_meta_from($d);
            if (array_key_exists('title', $d)) $cols['title'] = str_param($d, 'title', 500);
            if (array_key_exists('author', $d)) $cols['author'] = str_param($d, 'author', 300, false);
            if ($desc !== null) q('UPDATE catalog SET description = ? WHERE book_id = ?', [$desc, $bookId]);
            if ($catIds !== null) set_book_categories($bookId, $catIds);
            elseif ($category !== null) set_book_categories($bookId, $category === '' ? [] : [category_find_or_create($category, 0)]);
            if ($seriesId !== null) set_book_series($bookId, $seriesId);
            update_book_cols($bookId, $cols);
            json_out(200, ['ok' => true]);

        // ---------- Catalog categories (migration 9) ----------
        case 'category-save':
            method('POST');
            require_admin(true);
            json_out(200, ['category' => category_save(body_json())]);

        case 'category-delete':
            method('POST');
            require_admin(true);
            json_out(200, category_delete(body_json()));

        case 'category-order':
            method('POST');
            require_admin(true);
            category_order(body_json());
            json_out(200, ['ok' => true]);

        case 'books-all':
            method('GET');
            require_admin();
            $rows = all('SELECT b.*, u.display_name AS uploader, c.status AS catalog_status,
                           (SELECT COUNT(*) FROM user_books ub WHERE ub.book_id = b.id) AS readers
                         FROM books b LEFT JOIN users u ON u.id = b.uploaded_by LEFT JOIN catalog c ON c.book_id = b.id
                         ORDER BY b.created_at DESC');
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'uploader' => $r['uploader'], 'catalog_status' => $r['catalog_status'], 'readers' => (int)$r['readers'], 'created_at' => (int)$r['created_at'],
            ], $rows)]);

        case 'admin-books':
            method('GET');
            require_admin();
            backfill_search_keys();
            json_out(200, admin_books_list($_GET));

        case 'admin-bulk':
            method('POST');
            $s = require_admin(true);
            json_out(200, admin_bulk(body_json(), (int)$s['id']));

        case 'book-delete':
            method('POST');
            require_admin(true);
            delete_book_everywhere(int_param(body_json()['book_id'] ?? ''));
            json_out(200, ['ok' => true]);

        case 'admin-import-url':
            method('POST');
            $s = require_admin(true);
            $d = body_json();
            $url = trim(str_param($d, 'url', 2048));
            if ($url === '') fail(400, 'missing_url', 'نشانی اینترنتی کتاب الزامی است.');

            $userTitle = opt_str_param($d, 'title', 500);
            $userAuthor = opt_str_param($d, 'author', 300);
            $desc = opt_str_param($d, 'description', 2000);
            $catIds = category_ids_param($d);
            $legacyCat = opt_str_param($d, 'category', 80);
            if ($catIds === null && $legacyCat !== null && $legacyCat !== '') {
                $catIds = [category_find_or_create($legacyCat, 0)];
            }
            $status = scalar_param($d, 'status') ?: 'approved';
            if (!in_array($status, ['approved', 'pending', 'none'], true)) {
                fail(400, 'bad_status', 'وضعیت نامعتبر است.');
            }
            $seriesId = series_id_param($d); // v8.14 magazine issue
            $addToLibrary = !isset($d['add_to_library']) || !empty($d['add_to_library']);
            $autoClean = !isset($d['clean']) || !empty($d['clean']);

            $maxBytes = (int)config()['max_book_bytes'];
            $work = upload_dir() . '/url-' . bin2hex(random_bytes(12)) . '.book';

            try {
                $dl = download_book_from_url($url, $work, $maxBytes, 45);
                $format = detect_book_format($work, '', 400);

                if ($format === 'pdf') {
                    $opf = pdf_info_meta($work) + ['language' => '', 'publisher' => '', 'published' => '', 'cover' => null];
                    $clean = ['removed' => 0, 'status' => 'not-applicable', 'skipped' => []];
                } else {
                    $opf = epub_opf_meta($work);
                    $clean = $autoClean ? clean_epub_file($work, stored_clean_rules()) : ['removed' => 0, 'status' => 'skipped', 'skipped' => []];
                    validate_epub($work);
                }

                clearstatcache(true, $work);
                $sha = hash_file('sha256', $work);
                $size = (int)filesize($work);
                $book = one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
                $duplicate = (bool)$book;

                if (!$book) {
                    $urlPath = parse_url($dl['final_url'] ?? $url, PHP_URL_PATH) ?? '';
                    $rawFilename = urldecode(basename($urlPath));
                    $fileTitle = clean_meta_text(preg_replace('/\.(epub|pdf)$/i', '', $rawFilename) ?? '', 500);

                    $title = $userTitle !== null && $userTitle !== ''
                        ? $userTitle
                        : ($opf['title'] !== '' ? $opf['title'] : ($fileTitle !== '' ? $fileTitle : 'بدون عنوان'));
                    $author = $userAuthor !== null && $userAuthor !== ''
                        ? $userAuthor
                        : ($opf['author'] !== '' ? $opf['author'] : '');

                    $meta = [
                        'title' => $title,
                        'author' => $author,
                        'language' => $opf['language'] ?? '',
                        'publisher' => $opf['publisher'] ?? '',
                        'published' => $opf['published'] ?? '',
                        'format' => $format,
                    ];

                    if (!rename($work, book_path($sha, $format))) fail(500, 'storage', 'امکان ذخیره فایل کتاب در سرور وجود ندارد.');
                    try {
                        $book = insert_book($sha, $size, $meta, (int)$s['id'], 'admin');
                    } catch (PDOException $e) {
                        $book = one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
                        if (!$book) throw $e;
                        $duplicate = true;
                    }
                }

                $bookId = (int)$book['id'];

                if ((int)$book['has_cover'] !== 1 && !empty($opf['cover']) && store_cover_bytes($bookId, $opf['cover'])) {
                    q('UPDATE books SET has_cover = 1 WHERE id = ?', [$bookId]);
                    $book['has_cover'] = 1;
                }

                if ($addToLibrary) {
                    insert_ignore('user_books', ['user_id' => $s['id'], 'book_id' => $bookId, 'added_at' => time()]);
                }
                if ($seriesId !== null) set_book_series($bookId, $seriesId); // before the catalog row: its approval delivers

                if ($status !== 'none') {
                    $cur = one('SELECT status FROM catalog WHERE book_id = ?', [$bookId]);
                    if (!$cur || $cur['status'] === 'rejected') {
                        q('DELETE FROM catalog WHERE book_id = ?', [$bookId]);
                        q('INSERT INTO catalog (book_id, status, description, category, submitted_by, submitted_at, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          [$bookId, $status, (string)$desc, '', (int)$s['id'], time(), $status === 'approved' ? (int)$s['id'] : null, $status === 'approved' ? time() : null]);
                        if ($catIds) set_book_categories($bookId, $catIds);
                    } else {
                        if ($status === 'approved' && $cur['status'] === 'pending') {
                            q('UPDATE catalog SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE book_id = ?', ['approved', (int)$s['id'], time(), $bookId]);
                        }
                        if ($catIds) set_book_categories($bookId, $catIds);
                    }
                }

                $freshBook = one('SELECT b.*, c.status AS catalog_status FROM books b LEFT JOIN catalog c ON c.book_id = b.id WHERE b.id = ?', [$bookId]);
                $resp = [
                    'ok' => true,
                    'book' => book_out($freshBook) + ['catalog_status' => $freshBook['catalog_status'] ?? 'none'],
                    'duplicate' => $duplicate,
                    'cleaned' => $clean['removed'] ?? 0,
                    'cleanup' => $clean['status'] ?? 'unknown',
                ];
            } finally {
                @unlink($work);
                @unlink("$work.clean");
            }
            json_out(201, $resp);

        case 'settings':
            if ($method === 'POST') {
                require_admin(true);
                $d = body_json();
                if (array_key_exists('catalog_requires_approval', $d)) set_setting('catalog_requires_approval', (bool)$d['catalog_requires_approval']);
            } else {
                method('GET');
                require_admin();
            }
            json_out(200, ['catalog_requires_approval' => catalog_requires_approval()]);
    }
    return false;
}
