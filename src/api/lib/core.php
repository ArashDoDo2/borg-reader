<?php
// Borg API core: config, database, migrations, JSON I/O, sessions, CSRF, auth helpers.
// Not web-accessible (lib/.htaccess denies it); included only by api/index.php.
declare(strict_types=1);
if (!defined('BORG_API')) { http_response_code(404); exit; }

final class ApiError extends Exception {
    public int $status; public string $codeName;
    public function __construct(int $status, string $codeName, string $message) {
        parent::__construct($message); $this->status = $status; $this->codeName = $codeName;
    }
}
function fail(int $status, string $code, string $message): void { throw new ApiError($status, $code, $message); }

// ---------- Config ----------
// Production: /home/<user>/public_html/borg/api -> /home/<user>/borg-config/config.php
// Tests:      BORG_CONFIG=/path/to/config.php
function config(): array {
    static $cfg = null;
    if ($cfg !== null) return $cfg;
    $path = getenv('BORG_CONFIG') ?: '';
    if ($path === '') {
        $home = dirname(__DIR__, 4); // lib -> api -> borg -> public_html -> home
        $path = $home . '/borg-config/config.php';
    }
    if (!is_file($path)) fail(503, 'not_configured', 'Server is not configured yet.');
    $c = require $path;
    if (!is_array($c) || empty($c['db_dsn'])) fail(503, 'not_configured', 'Server configuration is invalid.');
    $c += [
        'db_user' => null, 'db_pass' => null, 'setup_key' => '',
        'storage_dir' => dirname($path, 2) . '/borg-storage',
        'max_book_bytes' => 100 * 1024 * 1024,
        'catalog_requires_approval' => true,
        'session_days' => 60,
        'client_errors_ip_per_hour' => 30, // anonymous client-error requests per IP per hour
        'client_errors_max' => 5000, // rows kept in client_errors (oldest deleted beyond this)
    ];
    return $cfg = $c;
}

// ---------- Database ----------
function db(): PDO {
    static $pdo = null;
    if ($pdo) return $pdo;
    $c = config();
    $pdo = new PDO($c['db_dsn'], $c['db_user'], $c['db_pass'], [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES => false,
    ]);
    if (is_sqlite()) { $pdo->exec('PRAGMA foreign_keys = ON'); $pdo->exec('PRAGMA busy_timeout = 5000'); }
    migrate($pdo);
    return $pdo;
}
function is_sqlite(): bool { return str_starts_with(config()['db_dsn'], 'sqlite:'); }
function q(string $sql, array $params = []): PDOStatement { $st = db()->prepare($sql); $st->execute($params); return $st; }
function one(string $sql, array $params = []): ?array { $r = q($sql, $params)->fetch(); return $r === false ? null : $r; }
function all(string $sql, array $params = []): array { return q($sql, $params)->fetchAll(); }
/** INSERT that silently skips duplicates of a unique key. */
function insert_ignore(string $table, array $row): void {
    $cols = implode(',', array_keys($row));
    $ph = implode(',', array_fill(0, count($row), '?'));
    $verb = is_sqlite() ? 'INSERT OR IGNORE' : 'INSERT IGNORE';
    q("$verb INTO $table ($cols) VALUES ($ph)", array_values($row));
}

// ---------- Migrations (portable DDL: {AI} = auto-increment primary key, {TXT} = indexed short text) ----------
function migrations(): array {
    return [
        1 => [
            "CREATE TABLE users (id {AI}, email {TXT} NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL,
               display_name VARCHAR(120) NOT NULL, role VARCHAR(10) NOT NULL DEFAULT 'user',
               disabled INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL)",
            "CREATE TABLE sessions (id CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, csrf CHAR(64) NOT NULL,
               created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL, user_agent VARCHAR(255),
               FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)",
            "CREATE TABLE invites (code_hash CHAR(64) PRIMARY KEY, created_by INTEGER NOT NULL, created_at BIGINT NOT NULL,
               expires_at BIGINT NOT NULL, used_by INTEGER NULL, used_at BIGINT NULL, note VARCHAR(120))",
            "CREATE TABLE login_attempts (id {AI}, key_hash CHAR(64) NOT NULL, at BIGINT NOT NULL)",
            "CREATE INDEX login_attempts_key ON login_attempts (key_hash, at)",
            "CREATE TABLE books (id {AI}, sha256 CHAR(64) NOT NULL UNIQUE, size BIGINT NOT NULL,
               title VARCHAR(500) NOT NULL, author VARCHAR(300) NOT NULL DEFAULT '', language VARCHAR(20) NOT NULL DEFAULT '',
               has_cover INTEGER NOT NULL DEFAULT 0, uploaded_by INTEGER NOT NULL, created_at BIGINT NOT NULL)",
            "CREATE TABLE user_books (user_id INTEGER NOT NULL, book_id INTEGER NOT NULL, added_at BIGINT NOT NULL,
               PRIMARY KEY (user_id, book_id),
               FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE)",
            "CREATE TABLE catalog (book_id INTEGER PRIMARY KEY, status VARCHAR(10) NOT NULL, description VARCHAR(2000) NOT NULL DEFAULT '',
               submitted_by INTEGER NOT NULL, submitted_at BIGINT NOT NULL, reviewed_by INTEGER NULL, reviewed_at BIGINT NULL,
               FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE)",
            "CREATE TABLE progress (user_id INTEGER NOT NULL, book_id INTEGER NOT NULL, cfi VARCHAR(1000) NOT NULL,
               percentage INTEGER NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL, PRIMARY KEY (user_id, book_id),
               FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE)",
            "CREATE TABLE uploads (id CHAR(32) PRIMARY KEY, user_id INTEGER NOT NULL, sha256 CHAR(64) NOT NULL, size BIGINT NOT NULL,
               meta TEXT NOT NULL, received BIGINT NOT NULL DEFAULT 0, created_at BIGINT NOT NULL)",
        ],
        2 => [
            "CREATE TABLE annotations (uid VARCHAR(40) NOT NULL, user_id INTEGER NOT NULL, book_id INTEGER NOT NULL,
               type VARCHAR(10) NOT NULL, cfi VARCHAR(1000) NOT NULL, quote VARCHAR(2000) NOT NULL DEFAULT '', note TEXT,
               color VARCHAR(20) NOT NULL DEFAULT '', created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
               deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, uid),
               FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE)",
            "CREATE INDEX annotations_user_updated ON annotations (user_id, updated_at)",
            "CREATE TABLE settings (name VARCHAR(64) PRIMARY KEY, value VARCHAR(500) NOT NULL)",
        ],
        3 => [
            "ALTER TABLE user_books ADD COLUMN status VARCHAR(12) NOT NULL DEFAULT ''",
            "ALTER TABLE user_books ADD COLUMN shelves VARCHAR(2000) NOT NULL DEFAULT '[]'",
            "ALTER TABLE user_books ADD COLUMN meta_updated_at BIGINT NOT NULL DEFAULT 0",
            "CREATE TABLE shelves (uid VARCHAR(40) NOT NULL, user_id INTEGER NOT NULL, name VARCHAR(80) NOT NULL,
               position INTEGER NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (user_id, uid), FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)",
            "CREATE TABLE reading_days (user_id INTEGER NOT NULL, device VARCHAR(40) NOT NULL, day CHAR(10) NOT NULL,
               seconds INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, device, day),
               FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE)",
        ],
        4 => [
            "ALTER TABLE books ADD COLUMN publisher VARCHAR(300) NOT NULL DEFAULT ''",
            "ALTER TABLE books ADD COLUMN published VARCHAR(40) NOT NULL DEFAULT ''",
            "ALTER TABLE catalog ADD COLUMN category VARCHAR(80) NOT NULL DEFAULT ''",
        ],
        5 => [
            "CREATE TABLE client_errors (id {AI}, created_at BIGINT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,
               app_version VARCHAR(20) NOT NULL DEFAULT '', kind VARCHAR(20) NOT NULL DEFAULT 'error',
               message VARCHAR(500) NOT NULL DEFAULT '', source VARCHAR(300) NOT NULL DEFAULT '', stack TEXT,
               url_path VARCHAR(300) NOT NULL DEFAULT '', user_agent VARCHAR(300) NOT NULL DEFAULT '',
               count INTEGER NOT NULL DEFAULT 1, fingerprint CHAR(64) NOT NULL, last_seen BIGINT NOT NULL DEFAULT 0)",
            "CREATE INDEX client_errors_fp ON client_errors (fingerprint, created_at)",
            "CREATE INDEX client_errors_seen ON client_errors (last_seen)",
        ],
        6 => [
            // v8.7 API keys for the owner's upload program (api-book). Only the SHA-256 of a key is stored.
            "CREATE TABLE api_tokens (id {AI}, name VARCHAR(80) NOT NULL, token_hash CHAR(64) NOT NULL UNIQUE,
               created_by INTEGER NOT NULL, created_at BIGINT NOT NULL, last_used_at BIGINT NOT NULL DEFAULT 0,
               uses INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0, require_review INTEGER NOT NULL DEFAULT 0)",
            // Settings whose JSON value does not fit settings.value (VARCHAR(500)), e.g. clean_rules (up to 50 rules).
            "CREATE TABLE settings_text (name VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL)",
        ],
    ];
}
function schema_version(): int {
    return (int)(one('SELECT MAX(version) AS v FROM schema_migrations')['v'] ?? 0);
}
function migrate(PDO $pdo): void {
    $sqlite = str_starts_with(config()['db_dsn'], 'sqlite:');
    $pdo->exec('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at BIGINT NOT NULL)');
    $done = array_map('intval', $pdo->query('SELECT version FROM schema_migrations')->fetchAll(PDO::FETCH_COLUMN));
    $pending = array_diff_key(migrations(), array_flip($done));
    if (!$pending) return;
    ksort($pending);
    $ai = $sqlite ? 'INTEGER PRIMARY KEY AUTOINCREMENT' : 'INTEGER PRIMARY KEY AUTO_INCREMENT';
    $txt = $sqlite ? 'TEXT' : 'VARCHAR(190)';
    $suffix = $sqlite ? '' : ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci';
    foreach ($pending as $version => $stmts) {
        foreach ($stmts as $sql) {
            $sql = str_replace(['{AI}', '{TXT}'], [$ai, $txt], $sql);
            if (str_starts_with(ltrim($sql), 'CREATE TABLE')) $sql .= $suffix;
            $pdo->exec($sql);
        }
        $st = $pdo->prepare('INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)');
        $st->execute([$version, time()]);
    }
}

// ---------- HTTP helpers ----------
function json_out(int $status, array $body): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    echo json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}
function body_json(): array {
    $raw = file_get_contents('php://input') ?: '';
    if ($raw === '') return [];
    if (!str_contains($_SERVER['CONTENT_TYPE'] ?? '', 'application/json')) fail(415, 'bad_content_type', 'JSON body required.');
    $d = json_decode($raw, true);
    if (!is_array($d)) fail(400, 'bad_json', 'Invalid JSON.');
    return $d;
}
function str_param(array $d, string $k, int $max = 500, bool $required = true): string {
    $v = isset($d[$k]) && is_scalar($d[$k]) ? trim((string)$d[$k]) : '';
    if ($required && $v === '') fail(400, 'missing_' . $k, "Field '$k' is required.");
    if (mb_strlen($v) > $max) fail(400, 'too_long_' . $k, "Field '$k' is too long.");
    return $v;
}
/** Optional field: null when the key is absent; 400 when present with a non-scalar value; else trimmed and length-checked. */
function opt_str_param(array $d, string $k, int $max): ?string {
    if (!array_key_exists($k, $d)) return null;
    if ($d[$k] !== null && !is_scalar($d[$k])) fail(400, 'bad_' . $k, "Field '$k' must be a string.");
    return str_param($d, $k, $max, false);
}
function int_param($v): int { if (!is_numeric($v)) fail(400, 'bad_id', 'Invalid id.'); return (int)$v; }
function client_ip(): string { return $_SERVER['REMOTE_ADDR'] ?? '0.0.0.0'; }

// ---------- Sessions and auth ----------
function cookie_name(): string { return 'borg_sid'; }
function cookie_path(): string {
    // /borg/api/index.php -> /borg/
    $p = rtrim(dirname(dirname($_SERVER['SCRIPT_NAME'] ?? '/borg/api/index.php')), '/');
    return ($p === '' ? '' : $p) . '/';
}
function is_https(): bool {
    return (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') || (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https');
}
function set_session_cookie(string $token, int $expires): void {
    setcookie(cookie_name(), $token, [
        'expires' => $expires, 'path' => cookie_path(), 'secure' => is_https(), 'httponly' => true, 'samesite' => 'Lax',
    ]);
}
function start_session(int $userId): array {
    $token = bin2hex(random_bytes(32));
    $csrf = bin2hex(random_bytes(32));
    $now = time(); $exp = $now + config()['session_days'] * 86400;
    q('INSERT INTO sessions (id, user_id, csrf, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?, ?, ?)',
      [hash('sha256', $token), $userId, $csrf, $now, $exp, mb_substr($_SERVER['HTTP_USER_AGENT'] ?? '', 0, 250)]);
    set_session_cookie($token, $exp);
    return ['csrf' => $csrf];
}
/** Current session joined with user, or null. */
function current_session(): ?array {
    static $cached = false;
    if ($cached !== false) return $cached;
    $token = $_COOKIE[cookie_name()] ?? '';
    if (!is_string($token) || !preg_match('/^[a-f0-9]{64}$/', $token)) return $cached = null;
    $row = one('SELECT s.id AS sid, s.csrf, s.expires_at, u.id, u.email, u.display_name, u.role, u.disabled
                FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?', [hash('sha256', $token)]);
    if (!$row || $row['expires_at'] < time() || (int)$row['disabled'] === 1) return $cached = null;
    return $cached = $row;
}
function require_user(bool $mutating = false): array {
    $s = current_session();
    if (!$s) fail(401, 'unauthorized', 'Login required.');
    if ($mutating) {
        $h = $_SERVER['HTTP_X_CSRF_TOKEN'] ?? '';
        if (!is_string($h) || !hash_equals($s['csrf'], $h)) fail(403, 'csrf', 'Invalid CSRF token.');
    }
    return $s;
}
function require_admin(bool $mutating = false): array {
    $s = require_user($mutating);
    if ($s['role'] !== 'admin') fail(403, 'forbidden', 'Admins only.');
    return $s;
}
function public_user(array $u): array {
    return ['id' => (int)$u['id'], 'email' => $u['email'], 'name' => $u['display_name'], 'role' => $u['role']];
}
// Rate limit: at most $max attempts per key in $window seconds.
function rate_limit(string $key, int $max = 10, int $window = 900): void {
    $h = hash('sha256', $key); $since = time() - $window;
    q('DELETE FROM login_attempts WHERE at < ?', [time() - 86400]);
    $n = (int)one('SELECT COUNT(*) AS n FROM login_attempts WHERE key_hash = ? AND at >= ?', [$h, $since])['n'];
    if ($n >= $max) fail(429, 'rate_limited', 'Too many attempts. Try again later.');
    q('INSERT INTO login_attempts (key_hash, at) VALUES (?, ?)', [$h, time()]);
}
function validate_email(string $e): string {
    $e = mb_strtolower(trim($e));
    if (!filter_var($e, FILTER_VALIDATE_EMAIL) || strlen($e) > 190) fail(400, 'bad_email', 'Invalid email.');
    return $e;
}
function validate_password(string $p): string {
    if (mb_strlen($p) < 8) fail(400, 'weak_password', 'Password must be at least 8 characters.');
    if (strlen($p) > 200) fail(400, 'bad_password', 'Password too long.');
    return $p;
}

// ---------- Settings (DB overrides config defaults) ----------
function setting(string $name, $default) {
    $r = one('SELECT value FROM settings WHERE name = ?', [$name]);
    return $r === null ? $default : json_decode($r['value'], true);
}
function set_setting(string $name, $value): void {
    q('DELETE FROM settings WHERE name = ?', [$name]);
    q('INSERT INTO settings (name, value) VALUES (?, ?)', [$name, json_encode($value)]);
}
/** Long settings (JSON in settings_text). */
function text_setting(string $name, $default) {
    $r = one('SELECT value FROM settings_text WHERE name = ?', [$name]);
    return $r === null ? $default : json_decode($r['value'], true);
}
function set_text_setting(string $name, $value): void {
    q('DELETE FROM settings_text WHERE name = ?', [$name]);
    q('INSERT INTO settings_text (name, value) VALUES (?, ?)', [$name, json_encode($value, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)]);
}
function catalog_requires_approval(): bool { return (bool)setting('catalog_requires_approval', (bool)config()['catalog_requires_approval']); }

// ---------- Storage ----------
function storage(string $sub = ''): string {
    $base = rtrim(config()['storage_dir'], '/');
    $dir = $sub === '' ? $base : "$base/$sub";
    if (!is_dir($dir) && !mkdir($dir, 0700, true) && !is_dir($dir)) fail(500, 'storage', 'Storage is not writable.');
    return $dir;
}
function book_path(string $sha): string { return storage('books/' . substr($sha, 0, 2)) . "/$sha.epub"; }
function cover_path(int $id): string { return storage('covers') . "/$id.img"; }
