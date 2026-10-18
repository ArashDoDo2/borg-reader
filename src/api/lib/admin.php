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

function handle_admin_route(string $route): bool {
    global $method;
    switch ($route) {
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
