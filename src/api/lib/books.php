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
function insert_book(string $sha, int $size, array $meta, int $uploadedBy): array {
    q('INSERT INTO books (sha256, size, title, author, language, publisher, published, has_cover, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)',
      [$sha, $size, $meta['title'] ?? 'بدون عنوان', $meta['author'] ?? '', $meta['language'] ?? '',
       $meta['publisher'] ?? '', $meta['published'] ?? '', $uploadedBy, time()]);
    return one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
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
            $existing = one('SELECT * FROM books WHERE sha256 = ?', [$sha]);
            if ($existing) {
                insert_ignore('user_books', ['user_id' => $s['id'], 'book_id' => $existing['id'], 'added_at' => time()]);
                json_out(200, ['exists' => true, 'book' => book_out($existing)]);
            }
            cleanup_stale_uploads();
            $id = bin2hex(random_bytes(16));
            $meta = [
                'title' => str_param($d, 'title', 500), 'author' => str_param($d, 'author', 300, false),
                'language' => '', 'publisher' => '', 'published' => '',
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
            try { validate_epub($part); } catch (ApiError $e) { $finish(); throw $e; }
            $existing = one('SELECT * FROM books WHERE sha256 = ?', [$u['sha256']]);
            if (!$existing) {
                $meta = array_merge(json_decode($u['meta'], true) ?: [], $late);
                if (!rename($part, book_path($u['sha256']))) { $finish(); fail(500, 'storage', 'Could not store the file.'); }
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
                if ($isAdmin && one('SELECT 1 AS x FROM catalog WHERE book_id = ?', [$oldId])) {
                    if (one('SELECT 1 AS x FROM catalog WHERE book_id = ?', [$newId])) q('DELETE FROM catalog WHERE book_id = ?', [$oldId]);
                    else q('UPDATE catalog SET book_id = ? WHERE book_id = ?', [$newId, $oldId]);
                    $catalogMoved = true;
                    foreach (all('SELECT user_id FROM user_books WHERE book_id = ? AND user_id <> ?', [$oldId, $me]) as $r) {
                        if (move_user_book((int)$r['user_id'], $oldId, $newId)) $migrated++;
                    }
                }
                copy_missing_meta($old, $new);
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
            $rows = all("SELECT b.*, c.description, c.category, c.submitted_at, u.display_name AS submitter,
                           (SELECT 1 FROM user_books ub WHERE ub.user_id = ? AND ub.book_id = b.id) AS in_library
                         FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                         WHERE c.status = 'approved' ORDER BY c.reviewed_at DESC, c.submitted_at DESC", [$s['id']]);
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'description' => $r['description'], 'category' => (string)$r['category'], 'submitter' => $r['submitter'], 'in_library' => (bool)$r['in_library'],
            ], $rows)]);

        case 'catalog-submit':
            method('POST');
            $s = require_user(true);
            $d = body_json();
            $bookId = int_param($d['book_id'] ?? '');
            if (!user_has_book((int)$s['id'], $bookId)) fail(404, 'not_found', 'Book not in your library.');
            $cur = one('SELECT status FROM catalog WHERE book_id = ?', [$bookId]);
            if ($cur && $cur['status'] !== 'rejected') json_out(200, ['status' => $cur['status']]);
            $auto = $s['role'] === 'admin' || !catalog_requires_approval();
            $status = $auto ? 'approved' : 'pending';
            $desc = str_param($d, 'description', 2000, false);
            $category = (string)opt_str_param($d, 'category', 80);
            q('DELETE FROM catalog WHERE book_id = ?', [$bookId]);
            q('INSERT INTO catalog (book_id, status, description, category, submitted_by, submitted_at, reviewed_by, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
              [$bookId, $status, $desc, $category, $s['id'], time(), $auto ? $s['id'] : null, $auto ? time() : null]);
            json_out(201, ['status' => $status]);

        case 'catalog-pending':
            method('GET');
            require_admin();
            $rows = all("SELECT b.*, c.description, c.category, c.submitted_at, u.display_name AS submitter
                         FROM catalog c JOIN books b ON b.id = c.book_id LEFT JOIN users u ON u.id = c.submitted_by
                         WHERE c.status = 'pending' ORDER BY c.submitted_at", []);
            json_out(200, ['books' => array_map(fn($r) => book_out($r) + [
                'description' => $r['description'], 'category' => (string)$r['category'], 'submitter' => $r['submitter'], 'submitted_at' => (int)$r['submitted_at'],
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

function annotation_out(array $r): array {
    return [
        'uid' => $r['uid'], 'book_id' => (int)$r['book_id'], 'type' => $r['type'], 'cfi' => $r['cfi'], 'quote' => $r['quote'],
        'note' => (string)$r['note'], 'color' => $r['color'], 'created_at' => (int)$r['created_at'],
        'updated_at' => (int)$r['updated_at'], 'deleted' => (int)$r['deleted'] === 1,
    ];
}
