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

function handle_admin_route(string $route): bool {
    global $method;
    switch ($route) {
        case 'client-error':
            // Anonymous visitors may report (strict per-IP limit); logged-in users must send the CSRF token.
            method('POST');
            if ((int)($_SERVER['CONTENT_LENGTH'] ?? 0) > 131072) fail(413, 'too_large', 'Report too large.');
            $s = current_session();
            if ($s) { require_user(true); rate_limit('cerr-user|' . $s['id'], 120, 3600); }
            else rate_limit('cerr-ip|' . client_ip(), 30, 3600);
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
                $role = $d['role'] === 'admin' ? 'admin' : 'user';
                if ($role === 'user' && $self) fail(400, 'self', 'You cannot remove your own admin role.');
                if ($role === 'user' && $u['role'] === 'admin' && active_admin_count((int)$u['id']) === 0) fail(400, 'last_admin', 'At least one active admin is required.');
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
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'status' => $r['status'], 'description' => $r['description'], 'category' => (string)$r['category'], 'submitter' => $r['submitter'], 'submitted_at' => (int)$r['submitted_at'],
            ], $rows)]);

        case 'catalog-update':
            method('POST');
            require_admin(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            if (!one('SELECT 1 AS x FROM catalog WHERE book_id = ?', [$bookId])) fail(404, 'not_found', 'Book is not in the catalog.');
            // Each field is updated only when its key is present; everything is validated before anything is written.
            $desc = array_key_exists('description', $d) ? str_param($d, 'description', 2000, false) : null;
            $category = opt_str_param($d, 'category', 80);
            $cols = book_meta_from($d);
            if (array_key_exists('title', $d)) $cols['title'] = str_param($d, 'title', 500);
            if (array_key_exists('author', $d)) $cols['author'] = str_param($d, 'author', 300, false);
            if ($desc !== null) q('UPDATE catalog SET description = ? WHERE book_id = ?', [$desc, $bookId]);
            if ($category !== null) q('UPDATE catalog SET category = ? WHERE book_id = ?', [$category, $bookId]);
            update_book_cols($bookId, $cols);
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

        case 'book-delete':
            method('POST');
            require_admin(true);
            delete_book_everywhere(int_param(body_json()['book_id'] ?? ''));
            json_out(200, ['ok' => true]);

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
