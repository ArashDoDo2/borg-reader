<?php
// Server-side text cleanup («پاک‌سازی متن») for api-book: a port of the browser's cleanEpub / cleanDocument /
// cleanLines / epubContentFiles (src/assets/js/app.js, v8.3–8.6.4). Keep the two in step: same rule types, same
// regexes, same block list, same limits, same "too broad" test. Lengths are counted in UTF-16 code units like JS.
// Also: OPF reading (metadata, cover) for api-book. Needs ZipArchive + DOM; clean_available() says whether they exist.
declare(strict_types=1);
if (!defined('BORG_API')) { http_response_code(404); exit; }

const CLEAN_MAX_HITS = 300;         // JS CLEAN_MAX_HITS
const CLEAN_MAX_SHARE = 0.02;       // JS CLEAN_MAX_SHARE (phrase / telegram)
const CLEAN_LINE_SHARE = 0.1;       // JS isTooBroad: line rules
const CLEAN_LINE_EXTRA = 400;       // JS CLEAN_LINE_EXTRA
const CLEAN_DOC_MAX_BYTES = 16777216; // server only: larger content documents are left untouched (memory)
const CLEAN_MAX_RULES = 50;
const CLEAN_XHTML_NS = 'http://www.w3.org/1999/xhtml';
// JS \s (and String.prototype.trim) as a PCRE class body.
const CLEAN_WS = '\t\n\x{0B}\f\r \x{A0}\x{1680}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}\x{FEFF}';
// JS TG_SOURCE verbatim (PCRE supports the same fixed-length lookbehinds).
const CLEAN_TG_SOURCE = '(?:(?<![A-Za-z0-9_.\-@])https?:\/\/(?:www\.)?|(?<![A-Za-z0-9_.\-\/@:])(?:www\.)?)(?:t\.me|telegram\.me|telegram\.dog)\/[A-Za-z0-9_\-+\/?=&%.#~]*[A-Za-z0-9_\-\/]|(?<![A-Za-z0-9_.@\/])@[A-Za-z0-9_]{5,32}(?![A-Za-z0-9_])';
const CLEAN_LINE_BLOCKS = ['p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'td', 'th', 'dd', 'dt', 'figcaption', 'pre', 'caption', 'aside', 'footer', 'header', 'section', 'article'];
const CLEAN_XHTML_TYPES = ['application/xhtml+xml', 'text/html'];
const CLEAN_MEDIA_TAGS = ['img', 'svg', 'image', 'video', 'audio', 'object'];
// XHTML void elements: serialized as <br /> like the browser's XMLSerializer (never <br></br> or <div/>).
const CLEAN_VOID = 'area|base|basefont|bgsound|br|col|embed|frame|hr|img|input|keygen|link|meta|param|source|track|wbr';

function clean_available(): bool { return class_exists('ZipArchive') && class_exists('DOMDocument') && class_exists('DOMXPath'); }

/** String length in UTF-16 code units (JS .length). */
function js_len(string $s): int { return mb_strlen($s, 'UTF-8') + (int)preg_match_all('/[\x{10000}-\x{10FFFF}]/u', $s); }
/** JS squash: whitespace runs -> one space, trimmed. */
function clean_squash(string $s): string { return trim(preg_replace('/[' . CLEAN_WS . ']+/u', ' ', $s) ?? $s, ' '); }
function clean_is_blank(string $s): bool { return preg_match('/[^' . CLEAN_WS . ']/u', $s) !== 1; }
function clean_js_trim(string $s): string { return preg_replace('/^[' . CLEAN_WS . ']+|[' . CLEAN_WS . ']+$/u', '', $s) ?? trim($s); }
function clean_rule_regex(array $r): string {
    return $r['type'] === 'telegram' ? '/' . CLEAN_TG_SOURCE . '/u' : '/' . preg_quote((string)$r['text'], '/') . '/u';
}
function clean_lname(DOMNode $n): string { return strtolower((string)($n->localName ?? $n->nodeName)); }

/**
 * Validate rules from the admin («پاک‌سازی متن»): a list of {id, type: phrase|line|telegram, text?}. Returns the
 * normalised list or throws 400. Same limits as the browser (text 4–200 characters after trimming).
 */
function validate_clean_rules($rules): array {
    if (!is_array($rules) || !array_is_list($rules)) fail(400, 'bad_rules', 'rules must be a list.');
    if (count($rules) > CLEAN_MAX_RULES) fail(400, 'too_many_rules', 'At most ' . CLEAN_MAX_RULES . ' rules.');
    $out = []; $ids = []; $seen = [];
    foreach ($rules as $i => $r) {
        $n = $i + 1;
        if (!is_array($r)) fail(400, 'bad_rule', "Rule $n is not an object.");
        $id = isset($r['id']) && is_string($r['id']) ? $r['id'] : '';
        if (!preg_match('/^[A-Za-z0-9_-]{1,40}$/', $id)) fail(400, 'bad_rule_id', "Rule $n: id must be 1–40 characters [A-Za-z0-9_-].");
        if (isset($ids[$id])) fail(400, 'duplicate_rule_id', "Rule $n: duplicate id.");
        $type = $r['type'] ?? null;
        if (!in_array($type, ['phrase', 'line', 'telegram'], true)) fail(400, 'bad_rule_type', "Rule $n: type must be phrase, line or telegram.");
        if ($type === 'telegram') {
            if (isset($r['text']) && (!is_string($r['text']) || $r['text'] !== '')) fail(400, 'bad_rule_text', "Rule $n: a telegram rule has no text.");
            $key = 'telegram';
            $item = ['id' => $id, 'type' => 'telegram'];
        } else {
            if (!isset($r['text']) || !is_string($r['text'])) fail(400, 'bad_rule_text', "Rule $n: text is required.");
            $text = clean_js_trim($r['text']);
            $len = mb_strlen($text, 'UTF-8');
            if ($len < 4 || $len > 200) fail(400, 'bad_rule_text', "Rule $n: text must be 4–200 characters.");
            $key = $type . "\n" . $text;
            $item = ['id' => $id, 'type' => $type, 'text' => $text];
        }
        if (isset($seen[$key])) fail(400, 'duplicate_rule', "Rule $n: the same rule is listed twice.");
        $ids[$id] = true; $seen[$key] = true; $out[] = $item;
    }
    return $out;
}

// ---------- Parsing (mirrors DOMParser) ----------
/** XML parse like DOMParser("application/xml" / "application/xhtml+xml"): null when the browser would report a parsererror. */
function clean_load_xml(string $src): ?DOMDocument {
    if ($src === '') return null;
    $prev = libxml_use_internal_errors(true);
    libxml_clear_errors();
    $d = new DOMDocument();
    $ok = $d->loadXML($src, LIBXML_NONET);
    $errs = libxml_get_errors();
    libxml_clear_errors(); libxml_use_internal_errors($prev);
    if (!$ok || !$d->documentElement) return null;
    // Well-formedness and namespace errors are fatal in browsers; libxml reports some of them as plain errors.
    foreach ($errs as $e) if ($e->level >= LIBXML_ERR_ERROR) return null;
    return $d;
}
/**
 * Browsers resolve HTML named entities (&nbsp; …) in XML documents whose DOCTYPE names an XHTML/MathML public id;
 * libxml does not (no DTD is loaded). Turn those into numeric references so both parse the same text.
 */
function clean_xhtml_entities(string $src): string {
    if (!preg_match('/<!DOCTYPE[^>]*PUBLIC\s+["\'][^"\']*(XHTML|MathML)/i', $src)) return $src;
    return preg_replace_callback('/&([A-Za-z][A-Za-z0-9]*);/', function ($m) {
        if (in_array($m[1], ['amp', 'lt', 'gt', 'quot', 'apos'], true)) return $m[0];
        $c = html_entity_decode($m[0], ENT_QUOTES | ENT_HTML5, 'UTF-8');
        if ($c === $m[0]) return $m[0];
        $out = '';
        foreach (mb_str_split($c, 1, 'UTF-8') as $ch) $out .= '&#x' . dechex(mb_ord($ch, 'UTF-8')) . ';';
        return $out;
    }, $src) ?? $src;
}
/** HTML parse (libxml HTML parser, UTF-8). */
function clean_load_html(string $src): ?DOMDocument {
    $d = new DOMDocument();
    $prev = libxml_use_internal_errors(true);
    $ok = $d->loadHTML('<?xml encoding="UTF-8"?>' . ($src === '' ? '<html><body></body></html>' : $src), LIBXML_NONET | LIBXML_NOERROR | LIBXML_NOWARNING);
    libxml_clear_errors(); libxml_use_internal_errors($prev);
    if (!$ok || !$d->documentElement) return null;
    foreach (iterator_to_array($d->childNodes) as $n) if ($n->nodeType === XML_PI_NODE) $d->removeChild($n);
    // An HTML5 parser always creates <head> and <body>; libxml's HTML parser may leave either out.
    $de = $d->documentElement;
    if (strtolower($de->nodeName) === 'html') {
        $has = fn(string $n) => array_filter(iterator_to_array($de->childNodes), fn($c) => $c instanceof DOMElement && strtolower($c->nodeName) === $n);
        if (!$has('head')) $de->insertBefore($d->createElement('head'), $de->firstChild);
        if (!$has('body')) $de->appendChild($d->createElement('body'));
    }
    $d->encoding = 'UTF-8';
    return $d;
}
/** doc.body || doc.documentElement */
function clean_root(DOMDocument $d, bool $html): ?DOMNode {
    $de = $d->documentElement;
    if (!$de) return null;
    if ($html) {
        foreach ($de->childNodes as $c) if ($c instanceof DOMElement && in_array(clean_lname($c), ['body', 'frameset'], true)) return $c;
        $b = $d->getElementsByTagName('body')->item(0);
        return $b ?: $de;
    }
    if ($de->localName === 'html' && $de->namespaceURI === CLEAN_XHTML_NS) {
        foreach ($de->childNodes as $c) {
            if ($c instanceof DOMElement && $c->namespaceURI === CLEAN_XHTML_NS && in_array($c->localName, ['body', 'frameset'], true)) return $c;
        }
    }
    return $de;
}
/** Descendant elements of $root in document order (root excluded), like root.querySelectorAll("*"). */
function clean_elements(DOMNode $root, array &$out = []): array {
    foreach ($root->childNodes as $c) {
        if ($c instanceof DOMElement) { $out[] = $c; clean_elements($c, $out); }
    }
    return $out;
}
/** Descendant Text nodes (not CDATA) in document order, like a SHOW_TEXT TreeWalker. */
function clean_text_nodes(DOMNode $root, array &$out = []): array {
    foreach ($root->childNodes as $c) {
        if ($c->nodeType === XML_TEXT_NODE) $out[] = $c;
        elseif ($c instanceof DOMElement) clean_text_nodes($c, $out);
    }
    return $out;
}
function clean_contains(DOMNode $anc, DOMNode $n): bool {
    for ($p = $n->parentNode; $p; $p = $p->parentNode) if ($p->isSameNode($anc)) return true;
    return false;
}

// ---------- The cleanup itself ----------
function clean_new_stats(array $rules): array {
    $s = [];
    foreach ($rules as $r) $s[$r['id']] = ['count' => 0, 'chars' => 0, 'tooLong' => 0];
    return $s;
}
/** JS cleanLines: remove the smallest block whose squashed text contains the squashed rule text. */
function clean_lines(DOMNode $root, array $rules, array &$stats): bool {
    $changed = false;
    foreach ($rules as $r) {
        if ($r['type'] !== 'line') continue;
        $needle = clean_squash((string)$r['text']);
        if ($needle === '') continue;
        $hits = [];
        foreach (clean_elements($root) as $el) {
            if (in_array(clean_lname($el), CLEAN_LINE_BLOCKS, true) && str_contains(clean_squash($el->textContent), $needle)) $hits[] = $el;
        }
        $deepest = array_filter($hits, function ($el) use ($hits) {
            foreach ($hits as $o) if (!$o->isSameNode($el) && clean_contains($el, $o)) return false;
            return true;
        });
        $nlen = js_len($needle);
        foreach ($deepest as $el) {
            $len = js_len(clean_squash($el->textContent));
            if ($len > $nlen + CLEAN_LINE_EXTRA) { $stats[$r['id']]['tooLong']++; continue; }
            $stats[$r['id']]['count']++; $stats[$r['id']]['chars'] += $len;
            if ($el->parentNode) { $el->parentNode->removeChild($el); $changed = true; }
        }
    }
    return $changed;
}
/** Exactly one ' />' form for empty XHTML void elements; other empty elements keep <x></x>. */
function clean_void_fix(string $xml): string {
    return preg_replace('~<(' . CLEAN_VOID . ')(\s[^<>]*)?></\1>~', '<$1$2 />', $xml) ?? $xml;
}
/**
 * JS cleanDocument: remove rule matches from one content document. Updates $stats; returns
 * ['chars' => text length (UTF-16), 'output' => new source or null when nothing changed].
 */
function clean_document(string $src, string $type, array $rules, array &$stats): array {
    $xml = $type === 'application/xhtml+xml';
    $viaHtml = false;
    $doc = $xml ? clean_load_xml(clean_xhtml_entities($src)) : clean_load_html($src);
    if ($xml && !$doc) { $doc = clean_load_html($src); $viaHtml = true; }
    if (!$doc) return ['chars' => 0, 'output' => null];
    $html = !$xml || $viaHtml;
    $root = clean_root($doc, $html);
    if (!$root) return ['chars' => 0, 'output' => null];
    $allChars = js_len(clean_squash($root->textContent));
    $changed = clean_lines($root, $rules, $stats);
    $textRules = array_values(array_filter($rules, fn($r) => $r['type'] !== 'line'));
    $touched = []; $chars = 0;
    foreach (clean_text_nodes($root) as $n) {
        $pn = $n->parentNode;
        $pname = $pn ? clean_lname($pn) : '';
        if ($pname === 'script' || $pname === 'style') continue;
        $v = $n->data; $chars += js_len($v);
        $orig = $v;
        foreach ($textRules as $r) {
            $id = $r['id'];
            $v = preg_replace_callback(clean_rule_regex($r), function ($m) use (&$stats, $id) {
                $stats[$id]['count']++; $stats[$id]['chars'] += js_len($m[0]);
                return '';
            }, $v) ?? $v;
        }
        if ($v !== $orig) {
            $n->data = $v; $changed = true;
            for ($p = $pn; $p && !$p->isSameNode($root); $p = $p->parentNode) {
                if ($p instanceof DOMElement && clean_lname($p) === 'a') {
                    $dup = false;
                    foreach ($touched as $t) if ($t->isSameNode($p)) { $dup = true; break; }
                    if (!$dup) $touched[] = $p;
                    break;
                }
            }
        }
    }
    $chars = max($chars, $allChars);
    if (!$changed) return ['chars' => $chars, 'output' => null];
    // A link whose text became empty (e.g. <a href="https://t.me/x">https://t.me/x</a>) is removed with it.
    foreach ($touched as $a) {
        if (!clean_is_blank($a->textContent)) continue;
        $media = false;
        foreach (clean_elements($a) as $el) {
            $ln = $html ? clean_lname($el) : (string)$el->localName;
            if (in_array($ln, CLEAN_MEDIA_TAGS, true)) { $media = true; break; }
        }
        if (!$media && $a->parentNode) $a->parentNode->removeChild($a);
    }
    if ($xml) {
        $decl = preg_match('/^[' . CLEAN_WS . ']*<\?xml[^?]*\?>/u', $src, $m) ? $m[0] : '';
        // The output is always UTF-8 (like the browser's); a declaration naming another encoding would be wrong.
        if ($decl !== '' && preg_match('/encoding\s*=\s*["\']([^"\']*)["\']/i', $decl, $em) && !preg_match('/^utf-?8$/i', $em[1])) {
            $decl = str_replace($em[0], 'encoding="utf-8"', $decl);
        }
        $doctype = $viaHtml && preg_match('/<!DOCTYPE[^>]*>/i', $src, $m) ? $m[0] : '';
        $doc->encoding = 'UTF-8';
        if ($viaHtml) {
            $de = $doc->documentElement;
            $hasNs = false;
            foreach ($de->attributes as $at) if ($at->nodeName === 'xmlns') $hasNs = true;
            if (!$hasNs) $de->setAttribute('xmlns', CLEAN_XHTML_NS);
            $body = (string)$doc->saveXML($de, LIBXML_NOEMPTYTAG);
        } else {
            // Top-level nodes one by one: like XMLSerializer, no declaration and no newlines between them.
            $body = '';
            foreach ($doc->childNodes as $c) $body .= (string)$doc->saveXML($c, LIBXML_NOEMPTYTAG);
        }
        $body = clean_void_fix(rtrim($body, "\n"));
        $output = ($decl !== '' ? $decl . "\n" : '') . ($doctype !== '' ? $doctype . "\n" : '') . $body;
    } else {
        $output = (preg_match('/<!DOCTYPE/i', $src) ? "<!DOCTYPE html>\n" : '') . (string)$doc->saveHTML($doc->documentElement);
    }
    return ['chars' => $chars, 'output' => $output];
}

// ---------- EPUB structure ----------
/** zipResolve: base + href (fragment dropped), "." and ".." resolved. */
function clean_zip_resolve(string $base, string $href): string {
    $out = [];
    foreach (explode('/', $base . explode('#', $href)[0]) as $p) {
        if ($p === '..') array_pop($out); elseif ($p !== '' && $p !== '.') $out[] = $p;
    }
    return implode('/', $out);
}
/** decodeURIComponent, falling back to the raw href where JS would throw. */
function clean_decode_href(string $href): string {
    if (preg_match('/%(?![0-9A-Fa-f]{2})/', $href)) return $href;
    $d = rawurldecode($href);
    return mb_check_encoding($d, 'UTF-8') ? $d : $href;
}
function clean_zip_has(ZipArchive $z, string $name): bool {
    return $name !== '' && !str_ends_with($name, '/') && $z->locateName($name) !== false;
}
/** [opfPath, base, DOMDocument] from META-INF/container.xml, or null. */
function epub_opf(ZipArchive $z): ?array {
    $c = clean_load_xml((string)$z->getFromName('META-INF/container.xml'));
    if (!$c) return null;
    $rf = (new DOMXPath($c))->query("//*[local-name()='rootfile']")->item(0);
    $opfPath = $rf instanceof DOMElement ? $rf->getAttribute('full-path') : '';
    if ($opfPath === '' || !clean_zip_has($z, $opfPath)) return null;
    $st = $z->statName($opfPath);
    if (!$st || $st['size'] > CLEAN_DOC_MAX_BYTES) return null;
    $opf = clean_load_xml((string)$z->getFromName($opfPath));
    if (!$opf) return null;
    $base = str_contains($opfPath, '/') ? substr($opfPath, 0, strrpos($opfPath, '/') + 1) : '';
    return [$opfPath, $base, $opf];
}
/** JS epubContentFiles: content documents [[path, type]] from the OPF manifest (fallback: every .xhtml/.html file). */
function clean_content_files(ZipArchive $z): array {
    $out = [];
    $o = epub_opf($z);
    if ($o) {
        [, $base, $opf] = $o;
        foreach ((new DOMXPath($opf))->query("//*[local-name()='item']") as $it) {
            $type = $it->getAttribute('media-type'); $href = $it->getAttribute('href');
            if ($href === '' || !in_array($type, CLEAN_XHTML_TYPES, true)) continue;
            $path = clean_zip_resolve($base, clean_decode_href($href));
            if (clean_zip_has($z, $path)) $out[] = [$path, $type];
        }
        if ($out) return $out;
    }
    for ($i = 0; $i < $z->numFiles; $i++) {
        $n = (string)$z->getNameIndex($i);
        if (!str_ends_with($n, '/') && preg_match('/\.x?html?$/i', $n)) $out[] = [$n, preg_match('/\.xhtml$/i', $n) ? 'application/xhtml+xml' : 'text/html'];
    }
    return $out;
}
/** One JS cleanEpub pass over all content documents. */
function clean_epub_pass(ZipArchive $z, array $files, array $rules, bool $write): array {
    $stats = clean_new_stats($rules);
    $totalChars = 0; $changed = [];
    foreach ($files as [$path, $type]) {
        $st = $z->statName($path);
        if (!$st || $st['size'] > CLEAN_DOC_MAX_BYTES) continue;
        $src = $z->getFromName($path);
        if ($src === false) continue;
        $res = clean_document($src, $type, $rules, $stats);
        $totalChars += $res['chars'];
        if ($write && $res['output'] !== null) $changed[$path] = $res['output'];
    }
    return ['stats' => $stats, 'totalChars' => $totalChars, 'total' => array_sum(array_column($stats, 'count')), 'changed' => $changed];
}
/** JS isTooBroad. */
function clean_too_broad(array $s, int $totalChars, array $rule): bool {
    return $s['count'] > CLEAN_MAX_HITS || ($totalChars > 0 && $s['chars'] > $totalChars * ($rule['type'] === 'line' ? CLEAN_LINE_SHARE : CLEAN_MAX_SHARE));
}
/**
 * Rebuild the EPUB like the browser: "mimetype" first and STORED, every other entry DEFLATE, changed documents
 * replaced, entry times kept (so the same input always gives the same bytes and the same SHA-256).
 * Unchanged entries go through temp files, so memory stays small for large books.
 */
function clean_rebuild_zip(ZipArchive $in, string $dst, array $changed): void {
    $tmpDir = $dst . '.d';
    if (!is_dir($tmpDir) && !mkdir($tmpDir, 0700)) throw new RuntimeException('cleanup temp dir');
    $tmpFiles = [];
    try {
        $out = new ZipArchive();
        if ($out->open($dst, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) throw new RuntimeException('cleanup zip open');
        $mtStat = $in->statName('mimetype');
        $mt = $in->getFromName('mimetype');
        $mt = $mt !== false && trim($mt) !== '' ? trim($mt) : 'application/epub+zip';
        $out->addFromString('mimetype', $mt);
        $out->setCompressionName('mimetype', ZipArchive::CM_STORE);
        if (method_exists($out, 'setMtimeName')) $out->setMtimeName('mimetype', $mtStat ? (int)$mtStat['mtime'] : 315532800);
        for ($i = 0; $i < $in->numFiles; $i++) {
            $st = $in->statIndex($i);
            if (!$st) continue;
            $name = (string)$st['name'];
            if ($name === 'mimetype' || str_ends_with($name, '/')) continue;
            if (array_key_exists($name, $changed)) {
                $out->addFromString($name, $changed[$name]);
            } else {
                $f = "$tmpDir/$i";
                $rs = method_exists($in, 'getStreamIndex') ? $in->getStreamIndex($i) : $in->getStream($name);
                $ws = fopen($f, 'wb');
                if (!$rs || !$ws) throw new RuntimeException('cleanup copy');
                stream_copy_to_stream($rs, $ws);
                fclose($rs); fclose($ws);
                $tmpFiles[] = $f;
                $out->addFile($f, $name);
            }
            $out->setCompressionName($name, ZipArchive::CM_DEFLATE);
            if (method_exists($out, 'setMtimeName')) $out->setMtimeName($name, (int)$st['mtime']);
        }
        if (!$out->close()) throw new RuntimeException('cleanup zip write');
    } finally {
        foreach ($tmpFiles as $f) @unlink($f);
        @rmdir($tmpDir);
    }
}
/**
 * Apply $rules to the EPUB at $path in place, like the browser's auto-clean on import (autoCleanImported):
 * a dry run first, rules that match nothing or look too broad for this book are skipped, then the rest applied.
 * Returns ['status' => 'applied'|'no-rules'|'unavailable', 'removed' => int, 'skipped' => [rule ids too broad]].
 */
function clean_epub_file(string $path, array $rules): array {
    if (!$rules) return ['status' => 'no-rules', 'removed' => 0, 'skipped' => []];
    if (!clean_available()) return ['status' => 'unavailable', 'removed' => 0, 'skipped' => []];
    $tmp = $path . '.clean';
    $z = new ZipArchive();
    try {
        if ($z->open($path, ZipArchive::RDONLY) !== true) return ['status' => 'unavailable', 'removed' => 0, 'skipped' => []];
        $files = clean_content_files($z);
        $dry = clean_epub_pass($z, $files, $rules, false);
        $ok = []; $skipped = [];
        foreach ($rules as $r) {
            $s = $dry['stats'][$r['id']];
            if (!$s['count']) continue;
            if (clean_too_broad($s, $dry['totalChars'], $r)) $skipped[] = $r['id']; else $ok[] = $r;
        }
        $removed = 0;
        if ($ok) {
            $res = clean_epub_pass($z, $files, $ok, true);
            if ($res['changed'] && $res['total']) {
                clean_rebuild_zip($z, $tmp, $res['changed']);
                $chk = new ZipArchive();
                if ($chk->open($tmp, ZipArchive::RDONLY) !== true || $chk->getNameIndex(0) !== 'mimetype') throw new RuntimeException('cleanup produced a bad zip');
                $chk->close();
                $z->close(); $z = null;
                if (!rename($tmp, $path)) throw new RuntimeException('cleanup rename');
                $removed = (int)$res['total'];
            }
        }
        return ['status' => 'applied', 'removed' => $removed, 'skipped' => $skipped];
    } catch (Throwable $e) {
        error_log('Borg cleanup failed: ' . $e->getMessage());
        return ['status' => 'unavailable', 'removed' => 0, 'skipped' => []];
    } finally {
        if ($z) { try { $z->close(); } catch (Throwable $e) {} }
        @unlink($tmp);
    }
}

// ---------- Metadata and cover from the OPF (what epub.js gives the browser on import) ----------
function clean_meta_text(string $v, int $max): string { return mb_substr(clean_squash($v), 0, $max); }
/** ['title','author','language','publisher','published','cover' => bytes|null]; empty strings when unknown. */
function epub_opf_meta(string $path): array {
    $out = ['title' => '', 'author' => '', 'language' => '', 'publisher' => '', 'published' => '', 'cover' => null];
    if (!clean_available()) return $out;
    $z = new ZipArchive();
    if ($z->open($path, ZipArchive::RDONLY) !== true) return $out;
    try {
        $o = epub_opf($z);
        if (!$o) return $out;
        [, $base, $opf] = $o;
        $xp = new DOMXPath($opf);
        $xp->registerNamespace('dc', 'http://purl.org/dc/elements/1.1/');
        $first = function (string $tag) use ($xp): string { $n = $xp->query("//dc:$tag")->item(0); return $n ? $n->textContent : ''; };
        $out['title'] = clean_meta_text($first('title'), 500);
        $out['author'] = clean_meta_text($first('creator'), 300);
        $out['language'] = clean_meta_text($first('language'), 20);
        $out['publisher'] = clean_meta_text($first('publisher'), 300);
        $pub = clean_meta_text($first('date'), 40);
        // "2019-05-01T00:00:00Z" -> "2019-05-01"; a plain year stays as it is (browser extraMeta()).
        if (preg_match('/^(\d{4}(?:-\d{2}(?:-\d{2})?)?)T/', $pub, $m)) $pub = $m[1];
        $out['published'] = $pub;
        // Cover like epub.js: EPUB 3 item with properties~="cover-image", else EPUB 2 <meta name="cover" content="id">.
        $item = $xp->query("//*[local-name()='item'][contains(concat(' ', normalize-space(@properties), ' '), ' cover-image ')]")->item(0);
        if (!$item) {
            $meta = $xp->query("//*[local-name()='meta'][@name='cover']")->item(0);
            $cid = $meta instanceof DOMElement ? $meta->getAttribute('content') : '';
            if ($cid !== '') {
                foreach ($xp->query("//*[local-name()='item']") as $it) if ($it->getAttribute('id') === $cid) { $item = $it; break; }
            }
        }
        if ($item instanceof DOMElement && $item->getAttribute('href') !== '') {
            $cp = clean_zip_resolve($base, clean_decode_href($item->getAttribute('href')));
            $st = clean_zip_has($z, $cp) ? $z->statName($cp) : false;
            if ($st && $st['size'] > 0 && $st['size'] <= 1500000) {
                $bin = $z->getFromName($cp);
                $info = is_string($bin) ? @getimagesizefromstring($bin) : false;
                if ($info && in_array($info['mime'] ?? '', ['image/png', 'image/jpeg', 'image/webp', 'image/gif'], true)) $out['cover'] = $bin;
            }
        }
        return $out;
    } catch (Throwable $e) {
        return $out;
    } finally {
        $z->close();
    }
}
