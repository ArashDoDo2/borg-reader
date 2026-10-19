<?php
// v8.7 machine uploads: admin-managed API keys and POST api-book, which puts an EPUB straight into the PUBLIC CATALOG
// (never into anyone's library) after applying the admin's cleanup rules («پاک‌سازی متن», lib/clean.php).
// Text-based PDFs are accepted too (stored unchanged, metadata from the request or the PDF Info dictionary).
// Guide for the owner's program: docs/api-books.md.
declare(strict_types=1);
if (!defined('BORG_API')) { http_response_code(404); exit; }

const API_BOOK_PER_HOUR = 60;        // api-book requests per key per hour
const API_AUTH_FAILS_PER_IP = 30;    // bad / missing keys per IP per 15 minutes before 429

/** "borg_" + 40 URL-safe characters (30 random bytes, base64url). */
function api_token_new(): string { return 'borg_' . rtrim(strtr(base64_encode(random_bytes(30)), '+/', '-_'), '='); }

/** The key from "Authorization: Bearer <key>" (also via REDIRECT_ on CGI/FastCGI hosts) or "X-Borg-Token: <key>". */
function api_token_from_request(): string {
    $h = (string)($_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '');
    if ($h === '' && function_exists('getallheaders')) {
        foreach ((array)getallheaders() as $k => $v) if (strcasecmp((string)$k, 'Authorization') === 0) $h = (string)$v;
    }
    if (preg_match('/^\s*Bearer\s+(\S+)\s*$/i', $h, $m)) return $m[1];
    return trim((string)($_SERVER['HTTP_X_BORG_TOKEN'] ?? ''));
}

/** The api_tokens row for a valid, unrevoked key whose creator is still an active admin; else 401 (429 after many). */
function api_token_auth(): array {
    $t = api_token_from_request();
    $row = null;
    if (preg_match('/^borg_[A-Za-z0-9_-]{40}$/', $t)) {
        $row = one('SELECT t.*, u.role AS owner_role, u.disabled AS owner_disabled FROM api_tokens t JOIN users u ON u.id = t.created_by WHERE t.token_hash = ?',
                   [hash('sha256', $t)]);
    }
    if (!$row || (int)$row['revoked'] === 1 || $row['owner_role'] !== 'admin' || (int)$row['owner_disabled'] === 1) {
        rate_limit('api-auth-fail|' . client_ip(), API_AUTH_FAILS_PER_IP, 900);
        if ($t === '') fail(401, 'missing_token', 'API key required: send "Authorization: Bearer <key>".');
        fail(401, 'bad_token', 'API key is invalid or revoked.');
    }
    return $row;
}

function api_token_out(array $r): array {
    return [
        'id' => (int)$r['id'], 'name' => $r['name'], 'created_at' => (int)$r['created_at'], 'last_used_at' => (int)$r['last_used_at'],
        'uses' => (int)$r['uses'], 'revoked' => (int)$r['revoked'] === 1, 'require_review' => (int)$r['require_review'] === 1,
    ];
}

/** The uploaded "file" field as a validated temp path, with clear errors for PHP's own upload limits. */
function api_uploaded_file(): array {
    $len = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
    $postMax = ini_bytes((string)ini_get('post_max_size'));
    if (!$_FILES && !$_POST && $postMax > 0 && $len > $postMax) {
        fail(413, 'post_too_large', 'The request is larger than this server accepts (PHP post_max_size = ' . ini_get('post_max_size') . ').');
    }
    if (!str_starts_with(strtolower((string)($_SERVER['CONTENT_TYPE'] ?? '')), 'multipart/form-data')) {
        fail(415, 'bad_content_type', 'Send multipart/form-data with the EPUB or PDF in the "file" field.');
    }
    $f = $_FILES['file'] ?? null;
    if (!is_array($f) || !isset($f['error']) || is_array($f['error'])) fail(400, 'missing_file', 'The "file" field (the EPUB or PDF) is required.');
    switch ((int)$f['error']) {
        case UPLOAD_ERR_OK: break;
        case UPLOAD_ERR_INI_SIZE:
            fail(413, 'file_too_large_for_php', 'The file is larger than this server accepts (PHP upload_max_filesize = ' . ini_get('upload_max_filesize') . ').');
        case UPLOAD_ERR_FORM_SIZE: fail(413, 'too_large', 'Book is too large.');
        case UPLOAD_ERR_PARTIAL: fail(400, 'partial_upload', 'The file arrived incomplete; send it again.');
        case UPLOAD_ERR_NO_FILE: fail(400, 'missing_file', 'The "file" field (the EPUB or PDF) is required.');
        default: fail(500, 'upload_failed', 'The server could not save the uploaded file (temp directory).');
    }
    if (!is_uploaded_file((string)$f['tmp_name'])) fail(400, 'missing_file', 'The "file" field (the EPUB or PDF) is required.');
    $size = (int)filesize((string)$f['tmp_name']);
    if ($size <= 0) fail(400, 'empty_file', 'The file is empty.');
    $max = (int)config()['max_book_bytes'];
    if ($size > $max) fail(413, 'too_large', 'Book is too large (limit ' . round($max / 1048576) . ' MB).');
    return ['tmp' => (string)$f['tmp_name'], 'name' => is_string($f['name'] ?? null) ? $f['name'] : '', 'size' => $size];
}

function handle_api_route(string $route): bool {
    global $method;
    switch ($route) {
        // ---------- Admin: API keys ----------
        case 'api-tokens':
            method('GET');
            require_admin();
            json_out(200, ['tokens' => array_map('api_token_out', all('SELECT * FROM api_tokens ORDER BY created_at DESC, id DESC'))]);

        case 'api-token-create':
            method('POST');
            $s = require_admin(true);
            $d = body_json();
            $name = str_param($d, 'name', 80);
            $review = !empty($d['require_review']) ? 1 : 0;
            $token = api_token_new();
            $now = time();
            q('INSERT INTO api_tokens (name, token_hash, created_by, created_at, last_used_at, uses, revoked, require_review) VALUES (?, ?, ?, ?, 0, 0, 0, ?)',
              [$name, hash('sha256', $token), $s['id'], $now, $review]);
            $row = one('SELECT * FROM api_tokens WHERE token_hash = ?', [hash('sha256', $token)]);
            // The plaintext key is returned only here; the server keeps its SHA-256.
            json_out(201, ['id' => (int)$row['id'], 'token' => $token, 'name' => $name, 'require_review' => $review === 1, 'created_at' => $now]);

        case 'api-token-revoke':
            method('POST');
            require_admin(true);
            $id = int_param(body_json()['id'] ?? '');
            if (!one('SELECT id FROM api_tokens WHERE id = ?', [$id])) fail(404, 'not_found', 'API key not found.');
            q('UPDATE api_tokens SET revoked = 1 WHERE id = ?', [$id]);
            json_out(200, ['ok' => true]);

        // ---------- Admin: cleanup rules applied to api-book uploads ----------
        case 'clean-rules':
            if ($method === 'POST') {
                require_admin(true);
                $rules = validate_clean_rules(body_json()['rules'] ?? null);
                set_text_setting('clean_rules', $rules);
                json_out(200, ['rules' => $rules]);
            }
            method('GET');
            require_admin();
            json_out(200, ['rules' => stored_clean_rules()]);

        // ---------- Machine upload into the public catalog ----------
        case 'api-book':
            method('POST');
            $tok = api_token_auth();
            rate_limit('api-book|' . $tok['id'], API_BOOK_PER_HOUR, 3600);
            q('UPDATE api_tokens SET last_used_at = ? WHERE id = ?', [time(), $tok['id']]);
            $owner = (int)$tok['created_by'];
            $up = api_uploaded_file();
            // Validate every text field before any work.
            $req = [];
            foreach (['title' => 500, 'author' => 300] + book_meta_fields() as $k => $max) {
                $v = opt_str_param($_POST, $k, $max);
                if ($v !== null && $v !== '') $req[$k] = preg_replace('/\s+/u', ' ', $v) ?? $v;
            }
            $category = opt_str_param($_POST, 'category', 80);
            $desc = opt_str_param($_POST, 'description', 2000);
            $work = upload_dir() . '/api-' . bin2hex(random_bytes(12)) . '.book';
            if (!move_uploaded_file($up['tmp'], $work)) fail(500, 'storage', 'Could not store the file.');
            try {
                $format = detect_book_format($work, '', 400);
                if ($format === 'pdf') {
                    // No server-side cleanup and no cover for PDFs (the client renders and uploads the cover later).
                    $opf = pdf_info_meta($work) + ['language' => '', 'publisher' => '', 'published' => '', 'cover' => null];
                    $clean = ['removed' => 0, 'status' => 'not-applicable', 'skipped' => []];
                } else {
                    $opf = epub_opf_meta($work);
                    $clean = clean_epub_file($work, stored_clean_rules());
                    validate_epub($work);
                }
                clearstatcache(true, $work);
                $sha = hash_file('sha256', $work);
                $size = (int)filesize($work);
                $book = one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
                $duplicate = (bool)$book;
                if (!$book) {
                    $fileTitle = clean_meta_text(preg_replace('/\.(epub|pdf)$/i', '', basename($up['name'])) ?? '', 500);
                    $meta = [
                        'title' => $req['title'] ?? ($opf['title'] !== '' ? $opf['title'] : ($fileTitle !== '' ? $fileTitle : 'بدون عنوان')),
                        'author' => $req['author'] ?? $opf['author'],
                    ];
                    foreach (array_keys(book_meta_fields()) as $k) $meta[$k] = $req[$k] ?? $opf[$k];
                    $meta['format'] = $format;
                    if (!rename($work, book_path($sha, $format))) fail(500, 'storage', 'Could not store the file.');
                    try {
                        $book = insert_book($sha, $size, $meta, $owner, 'api');
                    } catch (PDOException $e) { // the same file stored by a parallel request
                        $book = one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
                        if (!$book) throw $e;
                        $duplicate = true;
                    }
                }
                $bookId = (int)$book['id'];
                if ((int)$book['has_cover'] !== 1 && $opf['cover'] !== null && store_cover_bytes($bookId, $opf['cover'])) {
                    q('UPDATE books SET has_cover = 1 WHERE id = ?', [$bookId]);
                }
                // Catalog entry: new (or previously rejected) -> approved, or pending when the key requires review.
                // An entry that is already approved/pending keeps its status; category/description are updated when sent.
                $cur = one('SELECT status FROM catalog WHERE book_id = ?', [$bookId]);
                if (!$cur || $cur['status'] === 'rejected') {
                    $status = (int)$tok['require_review'] === 1 ? 'pending' : 'approved';
                    $auto = $status === 'approved';
                    q('DELETE FROM catalog WHERE book_id = ?', [$bookId]);
                    q('INSERT INTO catalog (book_id, status, description, category, submitted_by, submitted_at, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                      [$bookId, $status, (string)$desc, (string)$category, $owner, time(), $auto ? $owner : null, $auto ? time() : null]);
                } else {
                    $status = $cur['status'];
                    if ($desc !== null && $desc !== '') q('UPDATE catalog SET description = ? WHERE book_id = ?', [$desc, $bookId]);
                    if ($category !== null && $category !== '') q('UPDATE catalog SET category = ? WHERE book_id = ?', [$category, $bookId]);
                }
                q('UPDATE api_tokens SET uses = uses + 1, last_used_at = ? WHERE id = ?', [time(), $tok['id']]);
                $resp = [
                    'book' => book_out(one('SELECT * FROM books WHERE id = ?', [$bookId])), 'catalog_status' => $status,
                    'duplicate' => $duplicate, 'cleaned' => $clean['removed'], 'cleanup' => $clean['status'], 'skipped_rules' => $clean['skipped'],
                ];
            } finally { // (json_out exits, which would skip a finally block, so it is called after it)
                @unlink($work); @unlink("$work.clean");
            }
            json_out(201, $resp);
    }
    return false;
}

/** The admin's cleanup rules (settings_text clean_rules), re-validated; [] when unset or unreadable. */
function stored_clean_rules(): array {
    $v = text_setting('clean_rules', []);
    try { return validate_clean_rules(is_array($v) ? $v : []); } catch (ApiError $e) { return []; }
}

// ---------- PDF metadata: Title / Author from the trailer's Info dictionary (best effort, never fails) ----------
const PDF_SCAN_MAX = 33554432; // read at most 32 MB when an object has to be found without a classic xref table

/** ['title' => …, 'author' => …], empty strings when unknown, encrypted, or stored in a compressed object stream. */
function pdf_info_meta(string $path): array {
    $out = ['title' => '', 'author' => ''];
    try {
        $size = (int)filesize($path);
        $fh = fopen($path, 'rb');
        if (!$fh || $size <= 0) return $out;
        try {
            fseek($fh, max(0, $size - 65536));
            $tail = (string)fread($fh, 65536);
            // The last /Info wins (incremental updates append a newer trailer); xref-stream dictionaries are plain text too.
            if (!preg_match_all('/\/Info\s+(\d+)\s+(\d+)\s+R/', $tail, $m, PREG_SET_ORDER)) return $out;
            if (preg_match('/\/Encrypt\s+(?:\d+\s+\d+\s+R|<<)/', $tail)) return $out; // strings would be encrypted
            $ref = end($m);
            $dict = pdf_object_text($fh, $size, $tail, (int)$ref[1], (int)$ref[2]);
            if ($dict === null) return $out;
            foreach (['title' => ['Title', 500], 'author' => ['Author', 300]] as $k => [$key, $max]) {
                $v = pdf_dict_string($dict, $key);
                if ($v === null && preg_match('/\/' . $key . '\s+(\d+)\s+(\d+)\s+R/', $dict, $r)) { // indirect string
                    $obj = pdf_object_text($fh, $size, $tail, (int)$r[1], (int)$r[2]);
                    if ($obj !== null) $v = pdf_dict_string('/X ' . $obj, 'X');
                }
                if ($v !== null) $out[$k] = clean_meta_text(preg_replace('/[\x{0}-\x{1F}\x{7F}\x{FEFF}]/u', ' ', $v) ?? '', $max);
            }
        } finally {
            fclose($fh);
        }
    } catch (Throwable $e) {
        return ['title' => '', 'author' => ''];
    }
    return $out;
}

/** Text of "N G obj … endobj" (at most 64 KB), located through a classic xref table, else by scanning the file. */
function pdf_object_text($fh, int $size, string $tail, int $num, int $gen): ?string {
    $off = null;
    if (preg_match_all('/startxref\s+(\d+)/', $tail, $sx)) {
        $off = pdf_xref_offset($fh, (int)end($sx[1]), $num, $gen);
    }
    $re = '/(?<![0-9])' . $num . '\s+' . $gen . '\s+obj\b/';
    $read = function (int $at) use ($fh): string { fseek($fh, $at); return (string)fread($fh, 65536); };
    if ($off !== null) {
        $buf = $read($off);
        if (!preg_match($re, substr($buf, 0, 64))) $off = null; // stale or wrong table: fall back to scanning
    }
    if ($off === null) {
        $limit = min($size, PDF_SCAN_MAX); $chunk = 1048576; $found = null;
        for ($pos = 0; $pos < $limit && $found === null; $pos += $chunk - 64) {
            $part = $read($pos) . (string)fread($fh, $chunk - 65536);
            if (preg_match_all($re, $part, $mm, PREG_OFFSET_CAPTURE)) $found = $pos + (int)end($mm[0])[1];
        }
        if ($found === null) return null; // e.g. inside a compressed object stream
        $buf = $read($found);
    }
    $start = strpos($buf, 'obj');
    $end = strpos($buf, 'endobj', (int)$start);
    if ($start === false || $end === false) return null;
    return substr($buf, $start + 3, $end - $start - 3);
}

/** Byte offset of object $num from a classic "xref" table at $at (following /Prev); null for xref streams. */
function pdf_xref_offset($fh, int $at, int $num, int $gen, int $depth = 0): ?int {
    if ($depth > 8 || $at < 0) return null;
    fseek($fh, $at);
    $buf = (string)fread($fh, 262144);
    if (!preg_match('/^\s*xref\s+/', $buf, $h)) return null;
    $p = strlen($h[0]);
    while (preg_match('/\G(\d+)\s+(\d+)\s*[\r\n]+/', $buf, $sm, 0, $p)) {
        $first = (int)$sm[1]; $count = (int)$sm[2]; $p += strlen($sm[0]);
        if ($num >= $first && $num < $first + $count) {
            $e = substr($buf, $p + ($num - $first) * 20, 20);
            if (preg_match('/^(\d{10}) (\d{5}) n/', $e, $em) && (int)$em[2] === $gen) return (int)$em[1];
            return null;
        }
        $p += $count * 20;
        if ($p > strlen($buf)) return null;
    }
    if (preg_match('/trailer\s*<<.*?\/Prev\s+(\d+)/s', substr($buf, $p, 4096), $pm)) {
        return pdf_xref_offset($fh, (int)$pm[1], $num, $gen, $depth + 1);
    }
    return null;
}

/** A /Key string value of a dictionary text as UTF-8: literal (…) with escapes or hex <…>; null when absent. */
function pdf_dict_string(string $dict, string $key): ?string {
    if (!preg_match('/\/' . $key . '\s*([(<])/', $dict, $m, PREG_OFFSET_CAPTURE)) return null;
    $i = (int)$m[1][1];
    if ($m[1][0] === '<') {
        if (($dict[$i + 1] ?? '') === '<') return null; // a dictionary, not a string
        $j = strpos($dict, '>', $i);
        if ($j === false) return null;
        $hex = preg_replace('/[^0-9A-Fa-f]/', '', substr($dict, $i + 1, $j - $i - 1)) ?? '';
        if (strlen($hex) % 2) $hex .= '0';
        return pdf_text_decode((string)hex2bin($hex));
    }
    $bytes = ''; $depth = 0; $n = strlen($dict);
    for ($j = $i + 1; $j < $n; $j++) {
        $c = $dict[$j];
        if ($c === '\\') {
            $d = $dict[++$j] ?? '';
            $map = ['n' => "\n", 'r' => "\r", 't' => "\t", 'b' => "\x08", 'f' => "\x0C", '(' => '(', ')' => ')', '\\' => '\\'];
            if (isset($map[$d])) $bytes .= $map[$d];
            elseif ($d >= '0' && $d <= '7') {
                $oct = $d;
                while (strlen($oct) < 3 && ($dict[$j + 1] ?? '') >= '0' && ($dict[$j + 1] ?? '') <= '7') $oct .= $dict[++$j];
                $bytes .= chr(octdec($oct) & 0xFF);
            } elseif ($d === "\r") { if (($dict[$j + 1] ?? '') === "\n") $j++; } // line continuation
            elseif ($d !== "\n") $bytes .= $d;
            continue;
        }
        if ($c === '(') $depth++;
        elseif ($c === ')') { if ($depth === 0) return pdf_text_decode($bytes); $depth--; }
        $bytes .= $c;
    }
    return null;
}

/** PDF text string bytes -> UTF-8: UTF-16BE (BOM FE FF), UTF-8 (BOM, or valid UTF-8), else PDFDocEncoding ~ Latin-1. */
function pdf_text_decode(string $b): string {
    if (str_starts_with($b, "\xFE\xFF")) return (string)mb_convert_encoding(substr($b, 2), 'UTF-8', 'UTF-16BE');
    if (str_starts_with($b, "\xEF\xBB\xBF")) $b = substr($b, 3);
    if (mb_check_encoding($b, 'UTF-8')) return $b;
    return (string)mb_convert_encoding($b, 'UTF-8', 'ISO-8859-1');
}
