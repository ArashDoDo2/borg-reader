<?php
// Borg API front controller. All routes: api/?r=<route>. JSON in, JSON out (except file downloads).
declare(strict_types=1);
define('BORG_API', true);
require __DIR__ . '/lib/core.php';
require __DIR__ . '/lib/books.php';

$route = is_string($_GET['r'] ?? null) ? $_GET['r'] : '';
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

function method(string ...$allowed): void {
    global $method;
    if (!in_array($method, $allowed, true)) fail(405, 'method_not_allowed', 'Method not allowed.');
}

try {
    switch ($route) {
        // ---------- Status ----------
        case 'health':
            method('GET');
            $out = [
                'ok' => true, 'php' => PHP_VERSION,
                'extensions' => ['pdo_mysql' => extension_loaded('pdo_mysql'), 'zip' => class_exists('ZipArchive'), 'fileinfo' => extension_loaded('fileinfo')],
                'limits' => ['upload_max_filesize' => ini_get('upload_max_filesize'), 'post_max_size' => ini_get('post_max_size'), 'chunk_bytes' => chunk_bytes()],
                'configured' => false, 'database' => false, 'needs_setup' => false,
            ];
            try {
                config(); $out['configured'] = true;
                $out['needs_setup'] = (int)one('SELECT COUNT(*) AS n FROM users')['n'] === 0;
                $out['database'] = true;
            } catch (Throwable $e) {
                $out['ok'] = false;
                // Only the numeric driver error code, never the message (it can contain user names or paths).
                if ($e instanceof PDOException) {
                    $info = $e->errorInfo;
                    $out['database_error'] = isset($info[1]) ? (int)$info[1] : (int)$e->getCode();
                } elseif ($e instanceof ApiError) {
                    $out['database_error'] = $e->codeName;
                }
            }
            json_out(200, $out);

        // ---------- Auth ----------
        case 'setup':
            method('POST');
            $d = body_json();
            rate_limit('setup|' . client_ip(), 10);
            $key = (string)(config()['setup_key'] ?? '');
            if (strlen($key) < 16) fail(403, 'setup_disabled', 'Setup key is not configured.');
            if ((int)one('SELECT COUNT(*) AS n FROM users')['n'] > 0) fail(409, 'already_setup', 'Setup already completed.');
            if (!hash_equals($key, (string)($d['setup_key'] ?? ''))) fail(403, 'bad_setup_key', 'Invalid setup key.');
            $email = validate_email(str_param($d, 'email', 190));
            $pass = validate_password((string)($d['password'] ?? ''));
            $name = str_param($d, 'name', 120);
            q('INSERT INTO users (email, password_hash, display_name, role, created_at) VALUES (?, ?, ?, ?, ?)',
              [$email, password_hash($pass, PASSWORD_DEFAULT), $name, 'admin', time()]);
            $u = one('SELECT * FROM users WHERE email = ?', [$email]);
            $s = start_session((int)$u['id']);
            json_out(201, ['user' => public_user($u), 'csrf' => $s['csrf']]);

        case 'register':
            method('POST');
            $d = body_json();
            rate_limit('register|' . client_ip(), 10);
            $code = str_param($d, 'invite', 64);
            $inv = one('SELECT * FROM invites WHERE code_hash = ?', [hash('sha256', strtoupper($code))]);
            if (!$inv || $inv['used_by'] !== null || $inv['expires_at'] < time()) fail(403, 'bad_invite', 'Invite code is invalid or used.');
            $email = validate_email(str_param($d, 'email', 190));
            $pass = validate_password((string)($d['password'] ?? ''));
            $name = str_param($d, 'name', 120);
            if (one('SELECT id FROM users WHERE email = ?', [$email])) fail(409, 'email_taken', 'This email is already registered.');
            db()->beginTransaction();
            q('INSERT INTO users (email, password_hash, display_name, role, created_at) VALUES (?, ?, ?, ?, ?)',
              [$email, password_hash($pass, PASSWORD_DEFAULT), $name, 'user', time()]);
            $u = one('SELECT * FROM users WHERE email = ?', [$email]);
            $upd = q('UPDATE invites SET used_by = ?, used_at = ? WHERE code_hash = ? AND used_by IS NULL', [$u['id'], time(), $inv['code_hash']]);
            if ($upd->rowCount() !== 1) { db()->rollBack(); fail(403, 'bad_invite', 'Invite code is invalid or used.'); }
            db()->commit();
            $s = start_session((int)$u['id']);
            json_out(201, ['user' => public_user($u), 'csrf' => $s['csrf']]);

        case 'login':
            method('POST');
            $d = body_json();
            $email = mb_strtolower(str_param($d, 'email', 190));
            rate_limit('login-ip|' . client_ip(), 30);
            rate_limit('login-email|' . $email, 10);
            $u = one('SELECT * FROM users WHERE email = ?', [$email]);
            $pass = (string)($d['password'] ?? '');
            if (!$u || (int)$u['disabled'] === 1 || !password_verify($pass, $u['password_hash'])) fail(401, 'bad_credentials', 'Email or password is wrong.');
            if (password_needs_rehash($u['password_hash'], PASSWORD_DEFAULT)) {
                q('UPDATE users SET password_hash = ? WHERE id = ?', [password_hash($pass, PASSWORD_DEFAULT), $u['id']]);
            }
            $s = start_session((int)$u['id']);
            json_out(200, ['user' => public_user($u), 'csrf' => $s['csrf']]);

        case 'logout':
            method('POST');
            $s = require_user(true);
            q('DELETE FROM sessions WHERE id = ?', [$s['sid']]);
            set_session_cookie('', time() - 3600);
            json_out(200, ['ok' => true]);

        case 'me':
            method('GET');
            $s = current_session();
            if (!$s) json_out(200, ['user' => null]);
            json_out(200, ['user' => public_user($s), 'csrf' => $s['csrf']]);

        case 'password':
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $u = one('SELECT * FROM users WHERE id = ?', [$s['id']]);
            if (!password_verify((string)($d['current'] ?? ''), $u['password_hash'])) fail(403, 'bad_credentials', 'Current password is wrong.');
            q('UPDATE users SET password_hash = ? WHERE id = ?', [password_hash(validate_password((string)($d['new'] ?? '')), PASSWORD_DEFAULT), $s['id']]);
            q('DELETE FROM sessions WHERE user_id = ? AND id <> ?', [$s['id'], $s['sid']]);
            json_out(200, ['ok' => true]);

        // ---------- Admin ----------
        case 'invites':
            if ($method === 'POST') {
                $s = require_admin(true);
                $d = body_json();
                $days = max(1, min(90, (int)($d['days'] ?? 14)));
                $code = strtoupper(bin2hex(random_bytes(5))); // 10 hex chars
                q('INSERT INTO invites (code_hash, created_by, created_at, expires_at, note) VALUES (?, ?, ?, ?, ?)',
                  [hash('sha256', $code), $s['id'], time(), time() + $days * 86400, str_param($d, 'note', 120, false)]);
                json_out(201, ['code' => $code, 'expires_at' => time() + $days * 86400]);
            }
            method('GET');
            require_admin();
            json_out(200, ['invites' => array_map(fn($r) => [
                'note' => $r['note'], 'created_at' => (int)$r['created_at'], 'expires_at' => (int)$r['expires_at'],
                'used_by' => $r['used_name'], 'used_at' => $r['used_at'] ? (int)$r['used_at'] : null,
            ], all('SELECT i.*, u.display_name AS used_name FROM invites i LEFT JOIN users u ON u.id = i.used_by ORDER BY i.created_at DESC LIMIT 100'))]);

        case 'users':
            method('GET');
            require_admin();
            json_out(200, ['users' => array_map(fn($r) => public_user($r) + ['created_at' => (int)$r['created_at'], 'books' => (int)$r['books']],
                all('SELECT u.*, (SELECT COUNT(*) FROM user_books ub WHERE ub.user_id = u.id) AS books FROM users u ORDER BY u.created_at'))]);

        // ---------- Books, library, progress, catalog ----------
        default:
            if (!handle_books_route($route)) fail(404, 'not_found', 'Unknown route.');
    }
} catch (ApiError $e) {
    json_out($e->status, ['error' => $e->codeName, 'message' => $e->getMessage()]);
} catch (Throwable $e) {
    error_log('Borg API error: ' . $e->getMessage() . ' @ ' . $e->getFile() . ':' . $e->getLine());
    json_out(500, ['error' => 'server_error', 'message' => 'Internal server error.']);
}
