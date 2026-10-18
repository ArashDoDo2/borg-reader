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
        'has_cover' => (int)$b['has_cover'] === 1,
    ];
}
function user_has_book(int $uid, int $bookId): bool {
    return (bool)one('SELECT 1 AS x FROM user_books WHERE user_id = ? AND book_id = ?', [$uid, $bookId]);
}
function can_read_book(array $s, int $bookId): bool {
    if ($s['role'] === 'admin' || user_has_book((int)$s['id'], $bookId)) return true;
    return (bool)one("SELECT 1 AS x FROM catalog WHERE book_id = ? AND status = 'approved'", [$bookId]);
}
/** Delete a book's files and row when no library and no catalog entry references it. */
function gc_book(int $bookId): void {
    $refs = (int)one('SELECT COUNT(*) AS n FROM user_books WHERE book_id = ?', [$bookId])['n']
          + (int)one('SELECT COUNT(*) AS n FROM catalog WHERE book_id = ?', [$bookId])['n'];
    if ($refs > 0) return;
    $b = one('SELECT * FROM books WHERE id = ?', [$bookId]);
    if (!$b) return;
    @unlink(book_path($b['sha256']));
    @unlink(cover_path($bookId));
    q('DELETE FROM books WHERE id = ?', [$bookId]);
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

/** Accepts a data: URL with a small raster image and stores it as the book cover. */
function store_cover(int $bookId, ?string $dataUrl): bool {
    if (!$dataUrl || !preg_match('#^data:image/(png|jpe?g|webp|gif);base64,#', $dataUrl)) return false;
    $bin = base64_decode(substr($dataUrl, strpos($dataUrl, ',') + 1), true);
    if ($bin === false || strlen($bin) > 1500000 || @getimagesizefromstring($bin) === false) return false;
    file_put_contents(cover_path($bookId), $bin);
    return true;
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
            $rows = all("SELECT b.*, p.cfi, p.percentage, p.updated_at, c.status AS catalog_status
                         FROM user_books ub JOIN books b ON b.id = ub.book_id
                         LEFT JOIN progress p ON p.user_id = ub.user_id AND p.book_id = b.id
                         LEFT JOIN catalog c ON c.book_id = b.id
                         WHERE ub.user_id = ? ORDER BY ub.added_at DESC", [$s['id']]);
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'progress' => $r['cfi'] !== null ? ['cfi' => $r['cfi'], 'percentage' => (int)$r['percentage'], 'updated_at' => (int)$r['updated_at']] : null,
                'catalog_status' => $r['catalog_status'],
            ], $rows)]);

        case 'upload-init':
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $sha = strtolower(str_param($d, 'sha256', 64));
            if (!preg_match('/^[a-f0-9]{64}$/', $sha)) fail(400, 'bad_sha256', 'Invalid hash.');
            $size = (int)($d['size'] ?? 0);
            if ($size <= 0 || $size > config()['max_book_bytes']) fail(413, 'too_large', 'Book is too large.');
            $existing = one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
            if ($existing) {
                insert_ignore('user_books', ['user_id' => $s['id'], 'book_id' => $existing['id'], 'added_at' => time()]);
                json_out(200, ['exists' => true, 'book' => book_out($existing)]);
            }
            cleanup_stale_uploads();
            $id = bin2hex(random_bytes(16));
            $meta = [
                'title' => str_param($d, 'title', 500), 'author' => str_param($d, 'author', 300, false),
                'language' => str_param($d, 'language', 20, false),
            ];
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
            $u = one('SELECT * FROM uploads WHERE id = ? AND user_id = ?', [$id, $s['id']]);
            if (!$u) fail(404, 'no_upload', 'Upload not found.');
            $part = upload_dir() . "/$id.part";
            $finish = function () use ($id, $part) {
                @unlink($part); @unlink(upload_dir() . "/$id.cover"); q('DELETE FROM uploads WHERE id = ?', [$id]);
            };
            if ((int)$u['received'] !== (int)$u['size'] || filesize($part) !== (int)$u['size']) { $finish(); fail(400, 'incomplete', 'Upload is incomplete.'); }
            if (!hash_equals($u['sha256'], hash_file('sha256', $part))) { $finish(); fail(400, 'hash_mismatch', 'File hash does not match.'); }
            try { validate_epub($part); } catch (ApiError $e) { $finish(); throw $e; }
            $existing = one('SELECT * FROM books WHERE sha256 = ?', [$u['sha256']]);
            if (!$existing) {
                $meta = json_decode($u['meta'], true) ?: [];
                if (!rename($part, book_path($u['sha256']))) { $finish(); fail(500, 'storage', 'Could not store the file.'); }
                q('INSERT INTO books (sha256, size, title, author, language, has_cover, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)',
                  [$u['sha256'], $u['size'], $meta['title'] ?? 'بدون عنوان', $meta['author'] ?? '', $meta['language'] ?? '', $s['id'], time()]);
                $existing = one('SELECT * FROM books WHERE sha256 = ?', [$u['sha256']]);
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
            $path = $route === 'book-file' ? book_path($b['sha256']) : cover_path($bookId);
            if (!is_file($path)) fail(404, 'not_found', 'File not found.');
            $type = 'application/epub+zip';
            if ($route === 'book-cover') {
                $info = @getimagesize($path);
                $type = $info['mime'] ?? 'application/octet-stream';
            }
            header('Content-Type: ' . $type);
            header('Content-Length: ' . filesize($path));
            header('Cache-Control: private, max-age=86400');
            header('X-Content-Type-Options: nosniff');
            if ($route === 'book-file') header('Content-Disposition: attachment; filename="book-' . $bookId . '.epub"');
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

        // ---------- Public catalog ----------
        case 'catalog':
            method('GET');
            $s = require_user();
            $rows = all("SELECT b.*, c.description, c.submitted_at, u.display_name AS submitter,
                           (SELECT 1 FROM user_books ub WHERE ub.user_id = ? AND ub.book_id = b.id) AS in_library
                         FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                         WHERE c.status = 'approved' ORDER BY c.reviewed_at DESC, c.submitted_at DESC", [$s['id']]);
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'description' => $r['description'], 'submitter' => $r['submitter'], 'in_library' => (bool)$r['in_library'],
            ], $rows)]);

        case 'catalog-submit':
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            if (!user_has_book((int)$s['id'], $bookId)) fail(404, 'not_found', 'Book not in your library.');
            $cur = one('SELECT status FROM catalog WHERE book_id = ?', [$bookId]);
            if ($cur && $cur['status'] !== 'rejected') json_out(200, ['status' => $cur['status']]);
            $auto = $s['role'] === 'admin' || !config()['catalog_requires_approval'];
            $status = $auto ? 'approved' : 'pending';
            q('DELETE FROM catalog WHERE book_id = ?', [$bookId]);
            q('INSERT INTO catalog (book_id, status, description, submitted_by, submitted_at, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
              [$bookId, $status, str_param($d, 'description', 2000, false), $s['id'], time(), $auto ? $s['id'] : null, $auto ? time() : null]);
            json_out(201, ['status' => $status]);

        case 'catalog-pending':
            method('GET');
            require_admin();
            $rows = all("SELECT b.*, c.description, c.submitted_at, u.display_name AS submitter
                         FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                         WHERE c.status = 'pending' ORDER BY c.submitted_at", []);
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'description' => $r['description'], 'submitter' => $r['submitter'], 'submitted_at' => (int)$r['submitted_at'],
            ], $rows)]);

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
            $bookId = int_param(body_json()['book_id'] ?? '');
            if (!one("SELECT 1 AS x FROM catalog WHERE book_id = ? AND status = 'approved'", [$bookId])) fail(404, 'not_found', 'Book is not in the catalog.');
            insert_ignore('user_books', ['user_id' => $s['id'], 'book_id' => $bookId, 'added_at' => time()]);
            $b = one('SELECT * FROM books WHERE id = ?', [$bookId]);
            json_out(200, ['book' => book_out($b)]);
    }
    return false;
}
