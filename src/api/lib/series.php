<?php
// Magazines (ADR 0011, migration 10): series of issues, subscriptions, delivery of new issues to subscribers.
declare(strict_types=1);
if (!defined('BORG_API')) { http_response_code(404); exit; }

const SERIES_DELIVER_BATCH = 200; // issues handled per deliver_issues() call

/**
 * Deliver every approved issue not handled yet: one user_books row per subscriber, then a series_deliveries mark.
 * Delivery is driven by state, not by the approval request, so every approval path (review, bulk, api-book, URL
 * import, admin submit, any added later) delivers the same way. It runs at the start of library / series routes, so a
 * subscriber gets a new issue on their next sync at the latest. The mark is taken first inside a transaction, so two
 * parallel requests cannot deliver one issue twice.
 */
function deliver_issues(): void {
    $rows = all("SELECT b.id, b.series_id FROM books b JOIN catalog c ON c.book_id = b.id
                 WHERE b.series_id > 0 AND c.status = 'approved'
                   AND NOT EXISTS (SELECT 1 FROM series_deliveries d WHERE d.book_id = b.id)
                 ORDER BY c.reviewed_at, b.id LIMIT " . SERIES_DELIVER_BATCH);
    $verb = is_sqlite() ? 'INSERT OR IGNORE' : 'INSERT IGNORE';
    foreach ($rows as $r) {
        $bookId = (int)$r['id'];
        $now = time();
        db()->beginTransaction();
        try {
            $marked = q("$verb INTO series_deliveries (book_id, delivered_at) VALUES (?, ?)", [$bookId, $now])->rowCount();
            if ($marked === 1) {
                q("$verb INTO user_books (user_id, book_id, added_at)
                   SELECT s.user_id, ?, ? FROM series_subscriptions s WHERE s.series_id = ?", [$bookId, $now, (int)$r['series_id']]);
            }
            db()->commit();
        } catch (Throwable $e) {
            if (db()->inTransaction()) db()->rollBack();
            throw $e;
        }
    }
}
/** Mark an issue as handled without delivering it (attached to a series after approval = a back issue). */
function mark_issue_handled(int $bookId): void {
    insert_ignore('series_deliveries', ['book_id' => $bookId, 'delivered_at' => time()]);
}
/**
 * Put a book in a series (0 = none). Attaching an already approved book counts as a back issue: it is marked handled
 * and reaches no one. Attaching before approval means the approval delivers it.
 */
function set_book_series(int $bookId, int $seriesId): void {
    $b = one('SELECT series_id FROM books WHERE id = ?', [$bookId]);
    if (!$b || (int)$b['series_id'] === $seriesId) return;
    if ($seriesId > 0 && !one('SELECT 1 AS x FROM series WHERE id = ?', [$seriesId])) fail(404, 'series_not_found', 'Series not found.');
    q('UPDATE books SET series_id = ? WHERE id = ?', [$seriesId, $bookId]);
    if ($seriesId > 0 && one("SELECT 1 AS x FROM catalog WHERE book_id = ? AND status = 'approved'", [$bookId])) mark_issue_handled($bookId);
}
/** Optional series_id field: null when absent; 0 clears; otherwise an id that must exist. */
function series_id_param(array $d): ?int {
    if (!array_key_exists('series_id', $d) || $d['series_id'] === null || $d['series_id'] === '') return null;
    $v = $d['series_id'];
    if (!(is_int($v) || (is_string($v) && ctype_digit($v)))) fail(400, 'bad_series_id', 'series_id must be a series id (0 = none).');
    $id = (int)$v;
    if ($id > 0 && !one('SELECT 1 AS x FROM series WHERE id = ?', [$id])) fail(404, 'series_not_found', 'Series not found.');
    return $id;
}
function series_find_or_create(string $name, int $by): int {
    $name = preg_replace('/\s+/u', ' ', trim($name)) ?? trim($name);
    if ($name === '') fail(400, 'missing_series', 'Series name is required.');
    if (mb_strlen($name) > 190) fail(400, 'too_long_series', 'Series name is too long.');
    $r = one('SELECT id FROM series WHERE name = ?', [$name]);
    if ($r) return (int)$r['id'];
    try {
        q('INSERT INTO series (name, description, created_by, created_at) VALUES (?, ?, ?, ?)', [$name, '', $by, time()]);
    } catch (PDOException $e) { // created by a parallel request
        if (!($r = one('SELECT id FROM series WHERE name = ?', [$name]))) throw $e;
        return (int)$r['id'];
    }
    return (int)one('SELECT id FROM series WHERE name = ?', [$name])['id'];
}
/** The newest approved issue of a series (approval time, then upload time), or null. */
function latest_issue(int $seriesId): ?array {
    return one("SELECT b.* FROM books b JOIN catalog c ON c.book_id = b.id WHERE b.series_id = ? AND c.status = 'approved'
                ORDER BY c.reviewed_at DESC, b.created_at DESC, b.id DESC LIMIT 1", [$seriesId]) ?: null;
}
/** Subscribe (adding the latest approved issue to the library) or unsubscribe (issues already received stay). */
function set_subscription(int $userId, int $seriesId, bool $on, int $by): void {
    if (!$on) { q('DELETE FROM series_subscriptions WHERE user_id = ? AND series_id = ?', [$userId, $seriesId]); return; }
    insert_ignore('series_subscriptions', ['user_id' => $userId, 'series_id' => $seriesId, 'subscribed_at' => time(), 'added_by' => $by]);
    $latest = latest_issue($seriesId);
    if ($latest) insert_ignore('user_books', ['user_id' => $userId, 'book_id' => (int)$latest['id'], 'added_at' => time()]);
}
function series_out(array $r, int $uid): array {
    $latest = latest_issue((int)$r['id']);
    return [
        'id' => (int)$r['id'], 'name' => $r['name'], 'description' => (string)$r['description'],
        'issues' => (int)one("SELECT COUNT(*) AS n FROM books b JOIN catalog c ON c.book_id = b.id WHERE b.series_id = ? AND c.status = 'approved'", [(int)$r['id']])['n'],
        'latest' => $latest ? book_out($latest) : null,
        'subscribed' => (bool)one('SELECT 1 AS x FROM series_subscriptions WHERE user_id = ? AND series_id = ?', [$uid, (int)$r['id']]),
    ];
}
function series_or_404(int $id): array {
    $r = one('SELECT * FROM series WHERE id = ?', [$id]);
    if (!$r) fail(404, 'series_not_found', 'Series not found.');
    return $r;
}

function handle_series_route(string $route): bool {
    global $method;
    switch ($route) {
        // ---------- Catalog side (catalog roles; a student sees none, like the catalog) ----------
        case 'series':
            method('GET');
            $s = require_user();
            if ($s['role'] === 'student') json_out(200, ['series' => []]);
            deliver_issues();
            json_out(200, ['series' => array_map(fn($r) => series_out($r, (int)$s['id']), all('SELECT * FROM series ORDER BY name'))]);

        case 'series-issues':
            method('GET');
            $s = require_user();
            if ($s['role'] === 'student') fail(403, 'forbidden', 'دسترسی به کتابخانهٔ عمومی برای نقش دانشجو مجاز نیست.');
            $sr = series_or_404(int_param($_GET['id'] ?? ''));
            deliver_issues();
            $rows = all("SELECT b.*, c.reviewed_at, (SELECT 1 FROM user_books ub WHERE ub.user_id = ? AND ub.book_id = b.id) AS in_library
                         FROM books b JOIN catalog c ON c.book_id = b.id WHERE b.series_id = ? AND c.status = 'approved'
                         ORDER BY c.reviewed_at DESC, b.created_at DESC, b.id DESC LIMIT 500", [$s['id'], (int)$sr['id']]);
            json_out(200, ['series' => series_out($sr, (int)$s['id']), 'issues' => array_map(fn($r) => book_out($r) + [
                'in_library' => (bool)$r['in_library'], 'approved_at' => (int)$r['reviewed_at'],
            ], $rows)]);

        case 'series-subscribe':
            method('POST');
            $s = require_user(true);
            if ($s['role'] === 'student') fail(403, 'forbidden', 'اشتراک مجله برای نقش دانشجو فقط از طریق مدیر ممکن است.');
            $d = body_json();
            $sr = series_or_404(int_param($d['series_id'] ?? ''));
            deliver_issues();
            set_subscription((int)$s['id'], (int)$sr['id'], !empty($d['subscribe']), (int)$s['id']);
            json_out(200, ['series' => series_out($sr, (int)$s['id'])]);

        // ---------- Admin ----------
        case 'admin-series':
            if ($method === 'POST') {
                $s = require_admin(true);
                $d = body_json();
                $action = scalar_param($d, 'action') ?: 'save';
                if ($action === 'delete') {
                    $id = (int)series_or_404(int_param($d['id'] ?? ''))['id'];
                    q('UPDATE books SET series_id = 0 WHERE series_id = ?', [$id]); // the issues stay as ordinary books
                    q('DELETE FROM series_subscriptions WHERE series_id = ?', [$id]);
                    q('DELETE FROM series WHERE id = ?', [$id]);
                    json_out(200, ['ok' => true]);
                }
                if ($action !== 'save') fail(400, 'bad_action', 'action must be save or delete.');
                $name = preg_replace('/\s+/u', ' ', str_param($d, 'name', 190)) ?? '';
                $desc = str_param($d, 'description', 2000, false);
                $id = isset($d['id']) && $d['id'] !== '' && $d['id'] !== null ? int_param($d['id']) : 0;
                $clash = one('SELECT id FROM series WHERE name = ? AND id <> ?', [$name, $id]);
                if ($clash) fail(409, 'series_exists', 'A series with this name already exists.');
                if ($id > 0) {
                    series_or_404($id);
                    q('UPDATE series SET name = ?, description = ? WHERE id = ?', [$name, $desc, $id]);
                } else {
                    q('INSERT INTO series (name, description, created_by, created_at) VALUES (?, ?, ?, ?)', [$name, $desc, $s['id'], time()]);
                    $id = (int)one('SELECT id FROM series WHERE name = ?', [$name])['id'];
                }
                json_out(200, ['series' => series_out(series_or_404($id), (int)$s['id'])]);
            }
            method('GET');
            $s = require_admin();
            deliver_issues();
            json_out(200, ['series' => array_map(fn($r) => series_out($r, (int)$s['id']) + [
                'subscribers' => (int)one('SELECT COUNT(*) AS n FROM series_subscriptions WHERE series_id = ?', [(int)$r['id']])['n'],
                'issues_all' => (int)one('SELECT COUNT(*) AS n FROM books WHERE series_id = ?', [(int)$r['id']])['n'],
            ], all('SELECT * FROM series ORDER BY name'))]);

        case 'admin-series-subscribers':
            method('GET');
            require_admin();
            $sr = series_or_404(int_param($_GET['id'] ?? ''));
            json_out(200, ['user_ids' => array_map('intval', array_column(all('SELECT user_id FROM series_subscriptions WHERE series_id = ? ORDER BY user_id', [(int)$sr['id']]), 'user_id'))]);

        case 'admin-series-subscribe':
            // Any role, a student included (a student cannot subscribe on their own).
            method('POST');
            $s = require_admin(true);
            $d = body_json();
            $sr = series_or_404(int_param($d['series_id'] ?? ''));
            $raw = $d['user_ids'] ?? null;
            if (!is_array($raw) || !$raw || count($raw) > 500) fail(400, 'bad_user_ids', 'user_ids must be a list of 1–500 user ids.');
            $ids = [];
            foreach ($raw as $v) {
                if (!(is_int($v) || (is_string($v) && ctype_digit($v))) || (int)$v <= 0) fail(400, 'bad_user_ids', 'user_ids must be a list of user ids.');
                $ids[(int)$v] = true;
            }
            $ids = array_keys($ids);
            $found = array_map('intval', array_column(all('SELECT id FROM users WHERE id IN (' . placeholders(count($ids)) . ')', $ids), 'id'));
            if (count($found) !== count($ids)) fail(404, 'not_found', 'User not found.');
            deliver_issues();
            foreach ($ids as $uid) set_subscription($uid, (int)$sr['id'], !empty($d['subscribe']), (int)$s['id']);
            json_out(200, ['ok' => true, 'subscribers' => (int)one('SELECT COUNT(*) AS n FROM series_subscriptions WHERE series_id = ?', [(int)$sr['id']])['n']]);
    }
    return false;
}
