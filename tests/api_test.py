#!/usr/bin/env python3
"""API tests for Borg. Starts `php -S` with src/ mounted at /borg/ and a throwaway config.

Database: SQLite by default; set BORG_TEST_DSN / BORG_TEST_DB_USER / BORG_TEST_DB_PASS to run against MySQL (CI).
Usage: python3 tests/api_test.py
"""
import hashlib, http.cookiejar, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
SETUP_KEY = "test-setup-key-0123456789abcdef"
ERR_CAP = 25  # small client_errors cap so the table-size limit can be tested
results = []

def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)
    if not ok and os.environ.get("GITHUB_ACTIONS"):
        print(f"::error title=api test failed::{name} [{str(info)[:300]}]", flush=True)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def start_php(tmp):
    docroot = tmp / "www"; docroot.mkdir()
    (docroot / "borg").symlink_to(ROOT / "src", target_is_directory=True)
    dsn = os.environ.get("BORG_TEST_DSN") or f"sqlite:{tmp / 'borg.sqlite'}"
    cfg = tmp / "config.php"
    cfg.write_text("<?php return " + json.dumps({
        "db_dsn": dsn, "db_user": os.environ.get("BORG_TEST_DB_USER"), "db_pass": os.environ.get("BORG_TEST_DB_PASS"),
        "setup_key": SETUP_KEY, "storage_dir": str(tmp / "storage"), "client_errors_max": ERR_CAP,
    }).replace("{", "[").replace("}", "]").replace('":', '" =>') + ";\n")
    port = free_port()
    env = dict(os.environ, BORG_CONFIG=str(cfg))
    proc = subprocess.Popen(["php", "-d", "display_errors=0", "-d", "upload_max_filesize=2M", "-d", "post_max_size=8M", "-S", f"127.0.0.1:{port}", "-t", str(docroot)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try: socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError: time.sleep(0.1)
    return proc, f"http://127.0.0.1:{port}/borg/api/"

class Client:
    def __init__(self, base):
        self.base = base; self.csrf = None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(self, method, route, body=None, raw=None, query="", csrf=True, ctype=None):
        url = f"{self.base}?r={route}{query}"
        data, headers = None, {}
        if body is not None: data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
        if raw is not None: data = raw; headers["Content-Type"] = ctype or "application/octet-stream"
        if csrf and self.csrf: headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req) as r: status, payload, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e: status, payload, hdrs = e.code, e.read(), e.headers
        if "json" in (hdrs.get("Content-Type") or ""):
            payload = json.loads(payload or b"{}")
            if isinstance(payload, dict) and payload.get("csrf"): self.csrf = payload["csrf"]
        return status, payload

def upload(c, path, title, author="", cover=None, init_extra=None, complete_extra=None):
    data = Path(path).read_bytes(); sha = hashlib.sha256(data).hexdigest()
    st, r = c.call("POST", "upload-init", {"sha256": sha, "size": len(data), "title": title, "author": author, "cover": cover, **(init_extra or {})})
    if st >= 400: return st, r
    if r.get("exists"): return st, r
    uid, chunk, off = r["upload_id"], r["chunk_bytes"], 0
    while off < len(data):
        part = data[off:off + chunk]
        st, rr = c.call("POST", "upload-chunk", raw=part, query=f"&upload_id={uid}&offset={off}")
        if st != 200: return st, rr
        off += len(part)
    return c.call("POST", "upload-complete", {"upload_id": uid, **(complete_extra or {})})

def make_big_epub(tmp):
    """An EPUB larger than one chunk, to exercise multi-chunk upload."""
    import zipfile, random
    p = tmp / "big.epub"
    src = zipfile.ZipFile(FIX / "sample-fa.epub")
    with zipfile.ZipFile(p, "w") as z:
        for info in src.infolist():
            z.writestr(info, src.read(info.filename), compress_type=zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/noise.bin", random.Random(7).randbytes(3 * 1024 * 1024), compress_type=zipfile.ZIP_STORED)
    return p

def make_variant(tmp, name):
    """A small EPUB with a unique extra file, i.e. a distinct SHA-256 (stands in for a cleaned copy)."""
    import zipfile
    p = tmp / f"{name}.epub"
    src = zipfile.ZipFile(FIX / "sample-fa.epub")
    with zipfile.ZipFile(p, "w") as z:
        for info in src.infolist():
            z.writestr(info, src.read(info.filename), compress_type=zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/variant.txt", name)
    return p

def run_replace(base, tmp, admin, user):
    """book-replace: a cleaned copy replaces a book (own library; admin also moves the catalog and other users)."""
    PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    st, r = admin.call("POST", "invites", {"note": "replace"})
    third = Client(base)
    third.call("POST", "register", {"invite": r["code"], "email": "r@x.io", "password": "password5", "name": "سوم"})
    lib = lambda c: {b["id"]: b for b in c.call("GET", "library")[1]["books"]}
    ann = lambda c, since=0: {a["uid"]: a for a in c.call("GET", "annotations", query=f"&since={since}")[1]["annotations"]}
    now = int(time.time() * 1000)

    # 1. user replaces own book: progress/annotations/status/shelves follow, old one GC'd
    st, r = upload(user, make_variant(tmp, "a-old"), "کتاب الف", "نویسنده الف", cover=PNG, init_extra={"publisher": "نشر الف", "language": "fa"})
    a_old = r["book"]["id"]
    user.call("POST", "progress", {"book_id": a_old, "cfi": "epubcfi(/6/8!/4/2)", "percentage": 42, "updated_at": now})
    user.call("POST", "annotations", {"items": [{"uid": "repl-a-0001", "book_id": a_old, "type": "highlight", "cfi": "epubcfi(/6/8!/4/2,/1:0,/1:3)", "quote": "متن", "updated_at": now}]})
    user.call("POST", "library-meta", {"items": [{"book_id": a_old, "status": "reading", "shelves": ["shelf-0001"], "updated_at": now}]})
    st, r = upload(user, make_variant(tmp, "a-new"), "کتاب الف")
    a_new = r["book"]["id"]
    check("cleaned copy uploads as a new book", st == 201 and a_new != a_old, str(r)[:200])
    st, r = user.call("POST", "book-replace", {"old_book_id": a_old, "new_book_id": a_new})
    check("book-replace own book", st == 200 and r["book"]["id"] == a_new and r["migrated_users"] == 0 and r["catalog_moved"] is False, str(r)[:300])
    check("book-replace copies missing metadata and cover from the old book", r.get("book", {}).get("author") == "نویسنده الف"
          and r["book"]["publisher"] == "نشر الف" and r["book"]["language"] == "fa" and r["book"]["has_cover"], str(r)[:300])
    L = lib(user)
    check("library has the new book, not the old", a_new in L and a_old not in L)
    nb = L.get(a_new, {})
    check("status, shelves and progress follow the new book", nb.get("status") == "reading" and nb.get("shelves") == ["shelf-0001"]
          and (nb.get("progress") or {}).get("percentage") == 42 and nb["meta_updated_at"] == now, str(nb)[:300])
    A = ann(user, now)
    check("annotation moved to the new book and visible to incremental sync", A.get("repl-a-0001", {}).get("book_id") == a_new, str(A)[:300])
    st, r = user.call("POST", "progress", {"book_id": a_new, "cfi": "epubcfi(/6/10!/4/2)", "percentage": 50, "updated_at": now + 10})
    check("progress route works on the moved row", st == 200 and r["progress"]["percentage"] == 50)
    st, _ = admin.call("GET", "book-file", query=f"&id={a_old}")
    check("unreferenced old book is garbage-collected", st == 404)
    st, _ = admin.call("GET", "book-cover", query=f"&id={a_new}")
    check("cover copied to the new book", st == 200)

    # 2. merge: new book already in the library with its own progress/annotation/meta
    st, r = upload(user, make_variant(tmp, "b-old"), "کتاب ب"); b_old = r["book"]["id"]
    st, r = upload(user, make_variant(tmp, "b-new"), "کتاب ب"); b_new = r["book"]["id"]
    user.call("POST", "progress", {"book_id": b_old, "cfi": "old", "percentage": 10, "updated_at": now})
    user.call("POST", "progress", {"book_id": b_new, "cfi": "new", "percentage": 70, "updated_at": now + 100})
    user.call("POST", "annotations", {"items": [
        {"uid": "repl-b-0001", "book_id": b_old, "type": "bookmark", "cfi": "epubcfi(/6/2!/4/2)", "updated_at": now},
        {"uid": "repl-b-0002", "book_id": b_new, "type": "bookmark", "cfi": "epubcfi(/6/4!/4/2)", "updated_at": now}]})
    user.call("POST", "library-meta", {"items": [{"book_id": b_old, "status": "finished", "shelves": [], "updated_at": now + 200},
                                                 {"book_id": b_new, "status": "want", "shelves": ["shelf-0001"], "updated_at": now + 50}]})
    st, r = user.call("POST", "book-replace", {"old_book_id": b_old, "new_book_id": b_new})
    L = lib(user); nb = L.get(b_new, {})
    check("merge: replace into a book already in the library", st == 200 and b_old not in L and b_new in L, str(r)[:200])
    check("merge: newer progress kept, newer status/shelves kept", (nb.get("progress") or {}).get("percentage") == 70
          and nb.get("status") == "finished" and nb.get("shelves") == [], str(nb)[:300])
    A = ann(user)
    check("merge: both annotations kept on the new book", A.get("repl-b-0001", {}).get("book_id") == b_new and A.get("repl-b-0002", {}).get("book_id") == b_new)

    # 3. non-admin replacing a catalog book: only their own library moves
    st, r = upload(admin, make_variant(tmp, "c-old"), "کتاب ج", "نویسنده ج", init_extra={"publisher": "نشر ج"}); c_old = r["book"]["id"]
    admin.call("POST", "catalog-submit", {"book_id": c_old, "description": "توضیح ج", "category": "رمان"})
    user.call("POST", "catalog-add", {"book_id": c_old}); third.call("POST", "catalog-add", {"book_id": c_old})
    third.call("POST", "progress", {"book_id": c_old, "cfi": "epubcfi(/6/6!/4/2)", "percentage": 33, "updated_at": now})
    third.call("POST", "annotations", {"items": [{"uid": "repl-c-0001", "book_id": c_old, "type": "bookmark", "cfi": "epubcfi(/6/6!/4/2)", "updated_at": now}]})
    st, r = upload(user, make_variant(tmp, "c-user"), "کتاب ج"); c_user = r["book"]["id"]
    st, r = user.call("POST", "book-replace", {"old_book_id": c_old, "new_book_id": c_user})
    check("non-admin replaces a catalog book in own library", st == 200 and r["catalog_moved"] is False and r["migrated_users"] == 0, str(r)[:200])
    cat = [b["id"] for b in third.call("GET", "catalog")[1]["books"]]
    L3 = lib(third)
    check("non-admin replace leaves catalog and other users alone", c_old in cat and c_user not in cat and c_old in L3
          and (L3[c_old].get("progress") or {}).get("percentage") == 33)
    check("non-admin: own library switched", c_user in lib(user) and c_old not in lib(user))

    # 4. admin replacing a catalog book: catalog row and other users move
    st, r = upload(admin, make_variant(tmp, "c-admin"), "کتاب ج"); c_new = r["book"]["id"]
    st, r = admin.call("POST", "book-replace", {"old_book_id": c_old, "new_book_id": c_new})
    check("admin replaces a catalog book", st == 200 and r["catalog_moved"] is True and r["migrated_users"] == 1, str(r)[:200])
    cb = {b["id"]: b for b in third.call("GET", "catalog")[1]["books"]}
    check("catalog entry now points at the new file (fields kept)", c_new in cb and c_old not in cb and cb[c_new]["description"] == "توضیح ج"
          and cb[c_new]["category"] == "رمان" and cb[c_new]["publisher"] == "نشر ج" and cb[c_new]["author"] == "نویسنده ج", str(cb)[:300])
    L3 = lib(third)
    check("other user's library and progress moved", c_new in L3 and c_old not in L3 and (L3[c_new].get("progress") or {}).get("percentage") == 33)
    A3 = ann(third, now)
    check("other user's annotation moved (seen by incremental sync)", A3.get("repl-c-0001", {}).get("book_id") == c_new, str(A3)[:200])
    st, _ = admin.call("GET", "book-file", query=f"&id={c_old}")
    check("replaced catalog book garbage-collected", st == 404)
    check("user's own cleaned copy untouched", c_user in lib(user))

    # 5. validation
    st, r = user.call("POST", "book-replace", {"old_book_id": a_new, "new_book_id": a_new})
    check("book-replace same ids -> 400", st == 400 and r.get("error") == "same_book")
    st, r = user.call("POST", "book-replace", {"old_book_id": 999999, "new_book_id": a_new})
    check("book-replace missing old book -> 404", st == 404)
    st, r = user.call("POST", "book-replace", {"old_book_id": a_new, "new_book_id": 999999})
    check("book-replace missing new book -> 404", st == 404)
    st, r = third.call("POST", "book-replace", {"old_book_id": a_new, "new_book_id": c_new})
    check("book-replace old book not in library -> 404", st == 404)
    st, r = user.call("POST", "book-replace", {"old_book_id": a_new, "new_book_id": c_new})
    check("book-replace new book not in library -> 404", st == 404 and a_new in lib(user))
    st, r = user.call("POST", "book-replace", {"old_book_id": a_new, "new_book_id": b_new}, csrf=False)
    check("book-replace needs CSRF", st == 403 and a_new in lib(user))
    st, r = user.call("POST", "book-replace", {"old_book_id": "x", "new_book_id": b_new})
    check("book-replace bad id -> 400", st == 400)

def run_errors(base, admin, user):
    """v8.4 client error reports: anonymous + logged-in submit, dedupe, scrubbing, rate limits, admin list/clear, cap."""
    anon = Client(base)
    item = lambda msg, **kw: {"kind": "error", "message": msg, "source": "https://h.io/borg/assets/js/app.js:10:5", "app_version": "8.4.0",
                              "stack": "Error: x\n    at f (https://h.io/borg/assets/js/app.js:10:5)", "path": "/borg/", **kw}
    st, r = anon.call("POST", "client-error", {"items": [item("anon boom")]})
    check("anonymous visitor can report an error (no CSRF)", st == 200 and r.get("accepted") == 1, str(r))
    st, r = user.call("POST", "client-error", {"items": [item("user boom", kind="sync")]}, csrf=False)
    check("logged-in report without CSRF -> 403", st == 403)
    st, r = user.call("POST", "client-error", {"items": [item("user boom", kind="sync")]})
    check("logged-in user reports with CSRF", st == 200 and r.get("accepted") == 1, str(r))
    st, r = anon.call("POST", "client-error", {"items": [item("anon boom"), item("anon boom", count=3)]})
    st2, L = admin.call("GET", "errors")
    g = [e for e in L.get("errors", []) if e["message"] == "anon boom"]
    check("same fingerprint on the same day increments count (1 + 1 + 3)", st == 200 and len(g) == 1 and g[0]["count"] == 5 and g[0]["anonymous"], str(g))
    u = [e for e in L.get("errors", []) if e["message"] == "user boom"]
    check("logged-in report is attributed to a user", len(u) == 1 and u[0]["users"] == 1 and not u[0]["anonymous"] and u[0]["kind"] == "sync", str(u))
    secret = {"kind": "Weird Kind!", "app_version": "8.4.0<script>", "message": "fail for a@b.io " + "متن خطا " * 80,
              "source": "https://h.io/borg/api/?r=login&token=abc:1:2", "path": "/borg/reader?book=secret#frag",
              "stack": "at f (app.js:1:2)\n" * 300, "user_agent": "Mozilla/5.0 " * 40}
    st, r = anon.call("POST", "client-error", {"items": [secret, {"message": ""}, "junk", {"message": "csrf=" + "f" * 64 + " sid: 1234567890abcdef"}]})
    check("invalid items skipped, valid ones accepted", st == 200 and r.get("accepted") == 2 and r.get("skipped") == 2, str(r))
    st, L = admin.call("GET", "errors")
    e = [x for x in L["errors"] if x["message"].startswith("fail for")]
    e = e[0] if e else {}
    check("fields truncated to their limits", len(e.get("message", "")) == 500 and len(e.get("stack", "")) == 4000 and len(e.get("user_agent", "")) == 300, str({k: len(str(v)) for k, v in e.items()}))
    check("e-mail, query string and fragment stripped; kind/version sanitised",
          "a@b.io" not in e.get("message", "") and "[email]" in e.get("message", "") and e.get("url_path") == "/borg/reader"
          and "token" not in e.get("source", "") and "?" not in e.get("source", "") and e.get("source", "").endswith(":1:2")
          and e.get("kind") == "other" and e.get("app_version") == "8.4.0script", str({k: e.get(k) for k in ("kind", "app_version", "source", "url_path")}))
    t = [x for x in L["errors"] if x["message"].startswith("csrf=")]
    check("token-like values redacted", t and "f" * 32 not in t[0]["message"] and "1234567890abcdef" not in t[0]["message"], str(t)[:200])
    st, _ = anon.call("POST", "client-error", {"items": [item(f"m{i}") for i in range(21)]})
    check("more than 20 items -> 400", st == 400)
    st, _ = anon.call("POST", "client-error", {"items": "nope"})
    check("items must be a list -> 400", st == 400)
    st, _ = anon.call("POST", "client-error", raw=b"message=x", ctype="application/x-www-form-urlencoded")
    check("client-error rejects non-JSON bodies", st == 415)
    st, _ = anon.call("GET", "client-error")
    check("client-error is POST only", st == 405)
    # Filters
    st, L = admin.call("GET", "errors", query="&kind=sync")
    check("errors filter by kind", st == 200 and [x["message"] for x in L["errors"]] == ["user boom"] and "sync" in L["kinds"], str(L)[:200])
    st, L = admin.call("GET", "errors", query="&version=0.0.1")
    check("errors filter by version", st == 200 and L["errors"] == [] and L["total"] == 0 and "8.4.0" in L["versions"])
    st, L = admin.call("GET", "errors", query="&per=1&page=2")
    st0, L0 = admin.call("GET", "errors")
    check("errors paginated, newest last_seen first", st == 200 and len(L["errors"]) == 1 and L["total"] == L0["total"] >= 4
          and L["errors"][0]["id"] == L0["errors"][1]["id"] and all(a["last_seen"] >= b["last_seen"] for a, b in zip(L0["errors"], L0["errors"][1:])))
    st, r = admin.call("GET", "admin-stats")
    check("admin-stats counts error groups", st == 200 and r.get("client_errors") == L0["total"], str(r))
    # Non-admins
    st, _ = user.call("GET", "errors")
    st2, _ = anon.call("GET", "errors")
    st3, _ = user.call("POST", "errors-clear", {"all": True})
    st4, _ = anon.call("POST", "errors-clear", {"all": True})
    check("non-admin / anonymous cannot list or clear errors", st == 403 and st2 == 401 and st3 == 403 and st4 == 401)
    st, _ = admin.call("POST", "errors-clear", {"all": True}, csrf=False)
    check("errors-clear needs CSRF", st == 403)
    st, _ = admin.call("POST", "errors-clear", {})
    check("errors-clear without a target -> 400", st == 400)
    fp = g[0]["fingerprint"] if g else ""
    st, r = admin.call("POST", "errors-clear", {"fingerprint": fp})
    st2, L = admin.call("GET", "errors")
    check("admin clears one error group by fingerprint", st == 200 and r["deleted"] == 1 and not [x for x in L["errors"] if x["fingerprint"] == fp])
    one_id = L["errors"][0]["id"]
    st, r = admin.call("POST", "errors-clear", {"id": one_id})
    st2, L2 = admin.call("GET", "errors")
    check("admin clears one error by id", st == 200 and r["deleted"] == 1 and L2["total"] == L["total"] - 1)
    # Table cap (ERR_CAP rows): the oldest rows are deleted
    for k in range(2):
        st, r = user.call("POST", "client-error", {"items": [item(f"cap {k}-{i}") for i in range(20)]})
    st2, L = admin.call("GET", "errors", query="&per=100")
    msgs = [x["message"] for x in L["errors"]]
    check(f"table capped at {ERR_CAP} rows, newest kept", st == 200 and L["total"] == ERR_CAP and "cap 1-19" in msgs and "user boom" not in msgs, f"{L['total']} {msgs[-3:]}")
    st, r = admin.call("POST", "errors-clear", {"all": True})
    st2, L = admin.call("GET", "errors")
    check("admin clears all errors", st == 200 and r["deleted"] == ERR_CAP and L["total"] == 0 and L["errors"] == [])
    # Rate limit: anonymous 30 requests/hour per IP (this run already sent 6, rejected ones count too)
    codes = [anon.call("POST", "client-error", {"items": [item("rl")]})[0] for _ in range(25)]
    check("anonymous reports rate limited per IP (30/hour)", codes[:24] == [200] * 24 and codes[24] == 429, str(codes))
    st, _ = user.call("POST", "client-error", {"items": [item("still ok")]})
    check("logged-in user has a separate (per-user) limit", st == 200)

# ---------- v8.7 api-book: API keys, machine upload into the public catalog, server-side cleanup ----------
TOKEN_RE = r"^borg_[A-Za-z0-9_-]{40}$"
PNG_BYTES = __import__("base64").b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

def api_book(base, token=None, fields=None, file=None, header="bearer", raw=None, ctype=None, method="POST"):
    """POST api-book like the owner's program: multipart/form-data, no cookies, key in a header."""
    import uuid
    headers = {}
    if token is not None:
        if header == "bearer": headers["Authorization"] = f"Bearer {token}"
        else: headers["X-Borg-Token"] = token
    if raw is not None:
        data = raw; headers["Content-Type"] = ctype or "application/json"
    else:
        b = "----borg" + uuid.uuid4().hex; parts = []
        for k, v in (fields or {}).items():
            parts += [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode(), str(v).encode(), b"\r\n"]
        if file is not None:
            name, content = file
            parts += [f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/epub+zip\r\n\r\n'.encode(), content, b"\r\n"]
        parts.append(f"--{b}--\r\n".encode())
        data = b"".join(parts); headers["Content-Type"] = f"multipart/form-data; boundary={b}"
    req = urllib.request.Request(f"{base}?r=api-book", data=data if method == "POST" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r: status, payload = r.status, r.read()
    except urllib.error.HTTPError as e: status, payload = e.code, e.read()
    try: return status, json.loads(payload or b"{}")
    except ValueError: return status, {"raw": payload[:300]}

def make_clean_epub(tmp, name="api-clean"):
    """The text of tests/e2e_test.py sections 17 (Telegram) and 25 (whole line) in one book, plus a phrase and a cover."""
    import zipfile
    p = tmp / f"{name}.epub"
    special = ('<p id="p1">کانال ما: https://t.me/testchannel متن عادی بعد از لینک برای هایلایت است.</p>'
               '<p>برای پرسش‌ها @test_channel را دنبال کنید یا به info@example.com بنویسید.</p>'
               '<p>لینک: <a href="https://t.me/x">https://t.me/x</a> پایان پاراگراف.</p>'
               '<p id="ad">پیش از آگهی. این متن تبلیغاتی است پس از آگهی.</p>')
    normal = "".join(f"<p>این پاراگراف شماره {k} از فصل اول در کتاب {name} است.</p>" for k in range(120))
    long = ("<p id='long'>A very long paragraph that quotes This article was downloaded by zlibrary from somewhere in the middle of real text. "
            + "Real content continues here. " * 30 + "</p>")
    dl = ('<p id="dl" class="x">This article was downloaded by <a href="https://z-lib.example/">zlibrary</a> from '
          '<a href="https://www.economist.com/leaders/2026/10/15/what-if">https://www.economist.com/leaders/2026/10/15/what-if</a></p>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        D = zipfile.ZIP_DEFLATED
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', D)
        z.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">{name}</dc:identifier><dc:title>کتاب   برنامهٔ مالک</dc:title><dc:creator>نویسندهٔ OPF</dc:creator><dc:language>fa</dc:language><dc:publisher>نشر OPF</dc:publisher><dc:date>2020-03-04T00:00:00Z</dc:date><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="text/c1.xhtml" media-type="application/xhtml+xml"/><item id="cov" href="images/cover.png" media-type="image/png" properties="cover-image"/></manifest><spine page-progression-direction="rtl"><itemref idref="c1"/></spine></package>', D)
        z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol><li><a href="text/c1.xhtml">فصل اول</a></li></ol></nav></body></html>', D)
        z.writestr("OEBPS/text/c1.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل اول</title></head><body><h1>فصل اول</h1><p id="keep">Normal text stays.</p>{special}{normal}{long}{dl}</body></html>', D)
        z.writestr("OEBPS/images/cover.png", PNG_BYTES, D)
    return p

def run_api_books(base, tmp, admin, user):
    import re, zipfile, io
    import xml.dom.minidom as minidom
    anon = Client(base)
    st, r = admin.call("POST", "invites", {"note": "api reader"})
    reader = Client(base)
    reader.call("POST", "register", {"invite": r["code"], "email": "reader@x.io", "password": "password6", "name": "خواننده"})
    st, h = anon.call("GET", "health")
    check("api: health reports zip and migration 6", st == 200 and h.get("zip") is True and h.get("schema_version", 0) >= 6, str({k: h.get(k) for k in ("zip", "dom", "schema_version")}))

    # Admin-only routes
    codes = [user.call("GET", "api-tokens")[0], user.call("POST", "api-token-create", {"name": "x"})[0], user.call("POST", "api-token-revoke", {"id": 1})[0],
             user.call("GET", "clean-rules")[0], user.call("POST", "clean-rules", {"rules": []})[0]]
    check("api: non-admin cannot use the key / cleanup-rule routes (403)", codes == [403] * 5, str(codes))
    codes = [anon.call("GET", "api-tokens")[0], anon.call("POST", "api-token-create", {"name": "x"})[0], anon.call("GET", "clean-rules")[0]]
    check("api: anonymous -> 401", codes == [401] * 3, str(codes))
    st, _ = admin.call("POST", "api-token-create", {"name": "no csrf"}, csrf=False)
    check("api-token-create needs CSRF", st == 403)
    st1, _ = admin.call("POST", "api-token-create", {"name": ""})
    st2, _ = admin.call("POST", "api-token-create", {"name": "x" * 81})
    check("api-token-create name must be 1–80 characters", st1 == 400 and st2 == 400)

    # Create / list / revoke
    st, r = admin.call("POST", "api-token-create", {"name": "برنامهٔ آپلود"})
    tok, tok_id = r.get("token", ""), r.get("id")
    check("api-token-create returns the key once (borg_ + 40 url-safe chars)", st == 201 and re.match(TOKEN_RE, tok) and isinstance(tok_id, int) and r.get("require_review") is False, str(r))
    st, r = admin.call("POST", "api-token-create", {"name": "با بازبینی", "require_review": True})
    tok_rev = r.get("token", "")
    st, r = admin.call("POST", "api-token-create", {"name": "باطل‌شدنی"})
    tok_bad, bad_id = r.get("token", ""), r.get("id")
    st, r = admin.call("POST", "api-token-create", {"name": "rate"})
    tok_rl = r.get("token", "")
    st, L = admin.call("GET", "api-tokens")
    T = {t["id"]: t for t in L.get("tokens", [])}
    dump = json.dumps(L)
    check("api-tokens lists keys without the key or its hash", st == 200 and len(T) == 4 and T[tok_id]["name"] == "برنامهٔ آپلود"
          and set(T[tok_id]) == {"id", "name", "created_at", "last_used_at", "uses", "revoked", "require_review"}
          and tok not in dump and hashlib.sha256(tok.encode()).hexdigest() not in dump and "borg_" not in dump, dump[:300])
    check("api-tokens: require_review flag and fresh counters", any(t["require_review"] for t in T.values()) and T[tok_id]["uses"] == 0 and T[tok_id]["last_used_at"] == 0 and T[tok_id]["revoked"] is False)
    st, _ = admin.call("POST", "api-token-revoke", {"id": bad_id})
    st2, L = admin.call("GET", "api-tokens")
    check("api-token-revoke marks the key revoked", st == 200 and {t["id"]: t for t in L["tokens"]}[bad_id]["revoked"] is True)
    st, _ = admin.call("POST", "api-token-revoke", {"id": 999999})
    check("api-token-revoke unknown id -> 404", st == 404)

    # Authentication of api-book
    book = make_clean_epub(tmp)
    orig = book.read_bytes()
    results401 = [api_book(base, None, file=("b.epub", orig)), api_book(base, "borg_" + "x" * 40, file=("b.epub", orig)),
                  api_book(base, "not-a-key", file=("b.epub", orig)), api_book(base, tok_bad, file=("b.epub", orig)),
                  api_book(base, tok_bad, file=("b.epub", orig), header="x-borg-token")]
    check("api-book: missing / wrong / malformed / revoked key -> 401", [s for s, _ in results401] == [401] * 5
          and results401[0][1].get("error") == "missing_token" and results401[3][1].get("error") == "bad_token", str(results401))
    st, r = api_book(base, tok, method="GET")
    check("api-book is POST only", st == 405)
    st, r = api_book(base, tok, raw=b'{"x":1}', ctype="application/json")
    check("api-book needs multipart/form-data -> 415", st == 415, str(r))
    st, r = api_book(base, tok, fields={"category": "رمان"})
    check("api-book without a file -> 400 missing_file", st == 400 and r.get("error") == "missing_file", str(r))
    st, r = api_book(base, tok, file=("bad.epub", (FIX / "corrupt.epub").read_bytes()))
    check("api-book invalid EPUB -> 400 not_epub", st == 400 and r.get("error") == "not_epub", str(r))
    st, r = api_book(base, tok, file=("big.epub", make_big_epub(tmp).read_bytes()))
    check("api-book file over PHP upload_max_filesize -> 413 with a clear error", st == 413 and r.get("error") == "file_too_large_for_php" and "upload_max_filesize" in r.get("message", ""), str(r))
    st, r = api_book(base, tok, raw=b"x" * (9 * 1024 * 1024), ctype="multipart/form-data; boundary=zzz")
    check("api-book request over PHP post_max_size -> 413 post_too_large", st == 413 and r.get("error") == "post_too_large", str(r))
    st, r = api_book(base, tok, fields={"category": "x" * 81}, file=("b.epub", orig))
    check("api-book category too long -> 400", st == 400 and r.get("error") == "too_long_category", str(r))

    # Cleanup rules (admin «پاک‌سازی متن»)
    st, r = admin.call("GET", "clean-rules")
    check("clean-rules empty by default", st == 200 and r.get("rules") == [], str(r))
    bad_sets = [{"rules": "x"}, {"rules": [{"id": f"r{i}", "type": "telegram"} for i in range(51)]},
                {"rules": [{"id": "a1", "type": "phrase", "text": "abc"}]}, {"rules": [{"id": "a1", "type": "line", "text": "x" * 201}]},
                {"rules": [{"id": "a1", "type": "telegram", "text": "t.me"}]}, {"rules": [{"id": "a1", "type": "regex", "text": "abcd"}]},
                {"rules": [{"id": "a1", "type": "phrase", "text": "abcd"}, {"id": "a1", "type": "phrase", "text": "efgh"}]},
                {"rules": [{"type": "phrase", "text": "abcd"}]}, {"rules": [{"id": "a1", "type": "phrase", "text": "abcd"}, {"id": "a2", "type": "phrase", "text": "abcd"}]},
                {"rules": [{"id": "a1", "type": "phrase"}]}, {"rules": ["x"]}]
    codes = [admin.call("POST", "clean-rules", b)[0] for b in bad_sets]
    check("clean-rules validation (list, ≤50, text 4–200, telegram without text, known type, unique ids/rules) -> 400", codes == [400] * len(bad_sets), str(codes))
    st, _ = admin.call("POST", "clean-rules", {"rules": []}, csrf=False)
    check("clean-rules POST needs CSRF", st == 403)
    rules = [{"id": "rule-tg", "type": "telegram"}, {"id": "rule-ph", "type": "phrase", "text": "  این متن تبلیغاتی است  "},
             {"id": "rule-ln", "type": "line", "text": "This article was downloaded by zlibrary from"},
             {"id": "rule-broad", "type": "phrase", "text": "این پاراگراف شماره"}]
    st, r = admin.call("POST", "clean-rules", {"rules": rules})
    st2, r2 = admin.call("GET", "clean-rules")
    check("clean-rules stored (text trimmed, telegram without text)", st == 200 and r2.get("rules") == [
        {"id": "rule-tg", "type": "telegram"}, {"id": "rule-ph", "type": "phrase", "text": "این متن تبلیغاتی است"},
        {"id": "rule-ln", "type": "line", "text": "This article was downloaded by zlibrary from"},
        {"id": "rule-broad", "type": "phrase", "text": "این پاراگراف شماره"}], str(r2))
    st, r = admin.call("POST", "clean-rules", {"rules": [{"id": f"r{i:02d}", "type": "line", "text": f"{i:02d}" + "ل" * 198} for i in range(50)]})
    st2, r2 = admin.call("GET", "clean-rules")
    check("clean-rules holds 50 rules of 200 characters", st == 200 and len(r2.get("rules", [])) == 50 and len(r2["rules"][49]["text"]) == 200)
    admin.call("POST", "clean-rules", {"rules": rules})

    # Happy path: approved catalog entry, metadata from the OPF, cover, cleaned file, nobody's library
    files_before = len(list((tmp / "storage" / "books").rglob("*.epub")))
    st, r = api_book(base, tok, fields={"category": "  رمان ", "description": "از برنامهٔ مالک"}, file=("owner-book.epub", orig))
    b = r.get("book") or {}
    check("api-book happy path -> 201 approved, cleaned", st == 201 and r.get("catalog_status") == "approved" and r.get("duplicate") is False
          and r.get("cleanup") == "applied" and r.get("cleaned") == 5 and r.get("skipped_rules") == ["rule-broad"], str(r)[:400])
    check("api-book metadata read from the OPF", b.get("title") == "کتاب برنامهٔ مالک" and b.get("author") == "نویسندهٔ OPF" and b.get("language") == "fa"
          and b.get("publisher") == "نشر OPF" and b.get("published") == "2020-03-04" and b.get("has_cover") is True, str(b))
    check("api-book stores the cleaned file (new hash)", b.get("sha256") and b["sha256"] != hashlib.sha256(orig).hexdigest())
    bid = b.get("id")
    st, C = reader.call("GET", "catalog")
    cb = {x["id"]: x for x in C.get("books", [])}.get(bid, {})
    check("api-book: visible in another user's catalog with category, description, submitter", cb.get("category") == "رمان" and cb.get("description") == "از برنامهٔ مالک"
          and cb.get("submitter") == "مدیر" and cb.get("in_library") is False and cb.get("title") == "کتاب برنامهٔ مالک", str(cb)[:300])
    libs = [bid in [x["id"] for x in c.call("GET", "library")[1]["books"]] for c in (admin, user, reader)]
    st, BA = admin.call("GET", "books-all")
    ba = {x["id"]: x for x in BA["books"]}.get(bid, {})
    check("api-book: not added to anyone's library", libs == [False, False, False] and ba.get("readers") == 0 and ba.get("uploader") == "مدیر", f"{libs} {ba}")
    st, cov = reader.call("GET", "book-cover", query=f"&id={bid}")
    check("api-book: cover extracted from the OPF cover-image", st == 200 and cov == PNG_BYTES)
    st, data = reader.call("GET", "book-file", query=f"&id={bid}")
    check("api-book: stored file downloadable by another user, hash matches", st == 200 and hashlib.sha256(data).hexdigest() == b.get("sha256"))
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = z.infolist()
    check("cleaned EPUB: mimetype first and STORED, other entries deflated", infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
          and z.read("mimetype") == b"application/epub+zip" and all(i.compress_type == zipfile.ZIP_DEFLATED for i in infos[1:])
          and sorted(i.filename for i in infos) == sorted(zipfile.ZipFile(book).namelist()), str([(i.filename, i.compress_type) for i in infos]))
    src = z.read("OEBPS/text/c1.xhtml").decode()
    dom = minidom.parseString(src.encode())
    txt = "".join(n.data for n in _texts(dom.getElementsByTagName("body")[0]))
    ids = {e.getAttribute("id") for e in dom.getElementsByTagName("p")}
    anchors = [a.getAttribute("href") for a in dom.getElementsByTagName("a")]
    # Same expectations as tests/e2e_test.py section 17 (Telegram rule) ...
    check("server cleanup: Telegram links and handles gone, link with empty text removed (e2e 17)",
          "t.me" not in txt and "@test_channel" not in txt and not any("t.me" in h for h in anchors), txt[:200])
    check("server cleanup: normal text intact (e2e 17)", "کانال ما:" in txt and "متن عادی بعد از لینک" in txt and "info@example.com" in txt
          and "پایان پاراگراف." in txt and "این پاراگراف شماره 5 از فصل اول" in txt)
    check("server cleanup: too-broad rule skipped, phrase removed exactly", "این متن تبلیغاتی است" not in txt and "پیش از آگهی.  پس از آگهی." in txt, txt[:400])
    # ... and section 25 (whole-line rule).
    check("server cleanup: download line removed, normal text and the long paragraph kept (e2e 25)",
          "dl" not in ids and "keep" in ids and "long" in ids and "downloaded by zlibrary from https" not in txt and "economist.com" not in "".join(anchors), str(sorted(ids))[:200])
    check("server cleanup: exact serialisation of the cleaned paragraphs (as the browser's XMLSerializer)",
          '<p id="p1">کانال ما:  متن عادی بعد از لینک برای هایلایت است.</p><p>برای پرسش‌ها  را دنبال کنید یا به info@example.com بنویسید.</p><p>لینک:  پایان پاراگراف.</p>' in src
          and src.startswith('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل اول</title></head>'), src[:300])
    check("server cleanup: untouched entries byte-identical", z.read("OEBPS/content.opf") == zipfile.ZipFile(book).read("OEBPS/content.opf")
          and z.read("OEBPS/images/cover.png") == PNG_BYTES)
    st, L = admin.call("GET", "api-tokens")
    t = {x["id"]: x for x in L["tokens"]}[tok_id]
    check("api-tokens: uses and last_used_at updated", t["uses"] == 1 and t["last_used_at"] > 0, str(t))

    # Duplicate: the same original again (also via X-Borg-Token) -> same cleaned bytes -> one book
    st, r = api_book(base, tok, fields={"category": "داستان"}, file=("again.epub", orig), header="x-borg-token")
    check("api-book duplicate -> 201 duplicate:true, same book", st == 201 and r.get("duplicate") is True and (r.get("book") or {}).get("id") == bid
          and r.get("catalog_status") == "approved", str(r)[:300])
    st, BA = admin.call("GET", "books-all")
    st, CA = admin.call("GET", "catalog-all")
    check("duplicate: one book, one catalog entry, one stored file", [x["sha256"] for x in BA["books"]].count(b.get("sha256")) == 1
          and [x["id"] for x in CA["books"]].count(bid) == 1 and len(list((tmp / "storage" / "books").rglob("*.epub"))) == files_before + 1
          and {x["id"]: x for x in CA["books"]}[bid]["category"] == "داستان")
    check("duplicate: still in nobody's library", not any(bid in [x["id"] for x in c.call("GET", "library")[1]["books"]] for c in (admin, user, reader)))

    # require_review -> pending; request fields override the OPF
    var = make_variant(tmp, "api-review").read_bytes()
    st, r = api_book(base, tok_rev, fields={"title": "عنوان   درخواست", "author": "نویسندهٔ درخواست", "published": "1401"}, file=("rev.epub", var))
    rb = r.get("book") or {}
    check("api-book with a require_review key -> pending", st == 201 and r.get("catalog_status") == "pending", str(r)[:300])
    check("api-book request fields override the OPF metadata", rb.get("title") == "عنوان درخواست" and rb.get("author") == "نویسندهٔ درخواست" and rb.get("published") == "1401", str(rb))
    st, C = reader.call("GET", "catalog")
    st2, P = admin.call("GET", "catalog-pending")
    check("pending api-book not in the public catalog, in the admin queue", rb.get("id") not in [x["id"] for x in C["books"]]
          and [x["submitter"] for x in P["books"] if x["id"] == rb.get("id")] == ["مدیر"])
    st, _ = reader.call("GET", "book-file", query=f"&id={rb.get('id')}")
    check("pending api-book not readable by other users", st == 404)

    # No rules -> stored unchanged
    admin.call("POST", "clean-rules", {"rules": []})
    var2 = make_clean_epub(tmp, "api-norules").read_bytes()
    st, r = api_book(base, tok, file=("n.epub", var2))
    check("api-book without rules: cleanup no-rules, file stored unchanged", st == 201 and r.get("cleanup") == "no-rules" and r.get("cleaned") == 0
          and (r.get("book") or {}).get("sha256") == hashlib.sha256(var2).hexdigest(), str(r)[:300])

    # Rate limit: 60 requests per key per hour (rejected ones count); other keys unaffected
    codes = [api_book(base, tok_rl, fields={"x": "1"})[0] for _ in range(61)]
    check("api-book rate limited per key (60/hour)", codes[:60] == [400] * 60 and codes[60] == 429, str(codes[-5:]))
    st, r = api_book(base, tok, fields={"x": "1"})
    check("rate limit is per key", st == 400 and r.get("error") == "missing_file")

def _texts(node):
    out = []
    for c in node.childNodes:
        if c.nodeType == c.TEXT_NODE: out.append(c)
        elif c.nodeType == c.ELEMENT_NODE: out.extend(_texts(c))
    return out

def run(base, tmp):
    PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    anon = Client(base)
    st, h = anon.call("GET", "health")
    check("health ok, configured, needs setup", st == 200 and h["configured"] and h["database"] and h["needs_setup"], json.dumps(h)[:200])
    check("migration 5 applied (schema_version >= 5)", h.get("schema_version", 0) >= 5, str(h.get("schema_version")))
    st, r = anon.call("GET", "me"); check("me without session -> null user", st == 200 and r["user"] is None)
    st, _ = anon.call("GET", "library"); check("library requires login", st == 401)

    admin = Client(base)
    st, r = admin.call("POST", "setup", {"setup_key": "wrong-key-wrong-key-wrong", "email": "a@x.io", "password": "password1", "name": "Admin"})
    check("setup with wrong key refused", st == 403)
    st, r = admin.call("POST", "setup", {"setup_key": SETUP_KEY, "email": "Admin@X.io", "password": "password1", "name": "مدیر"})
    check("setup creates admin and session", st == 201 and r["user"]["role"] == "admin" and r["user"]["email"] == "admin@x.io", str(r)[:150])
    st, r = Client(base).call("POST", "setup", {"setup_key": SETUP_KEY, "email": "b@x.io", "password": "password1", "name": "B"})
    check("setup only works once", st == 409)

    st, r = admin.call("POST", "invites", {"note": "for reader"}, csrf=False)
    check("invite creation needs CSRF token", st == 403)
    st, r = admin.call("POST", "invites", {"note": "for reader", "days": 7})
    code = r.get("code", "")
    check("admin creates invite", st == 201 and len(code) == 10)

    user = Client(base)
    st, r = user.call("POST", "register", {"invite": "BADCODE123", "email": "u@x.io", "password": "password1", "name": "U"})
    check("register with bad invite refused", st == 403)
    st, r = user.call("POST", "register", {"invite": code.lower(), "email": "u@x.io", "password": "short", "name": "U"})
    check("weak password refused", st == 400)
    st, r = user.call("POST", "register", {"invite": code.lower(), "email": "u@x.io", "password": "password2", "name": "کاربر"})
    check("register with invite (case-insensitive)", st == 201 and r["user"]["role"] == "user")
    st, r = Client(base).call("POST", "register", {"invite": code, "email": "v@x.io", "password": "password3", "name": "V"})
    check("invite cannot be reused", st == 403)
    st, r = user.call("POST", "invites", {"note": "x"})
    check("non-admin cannot create invites", st == 403)

    # Upload: small book (single chunk) with cover, then dedupe
    st, r = upload(user, FIX / "sample-fa.epub", "کتاب آزمایشی", "نویسنده تست", cover=PNG,
                   init_extra={"publisher": "نشر نی", "published": "1399", "language": "fa"})
    book1 = (r.get("book") or {}).get("id")
    check("upload book with cover", st == 201 and book1 and r["book"]["has_cover"], str(r)[:200])
    check("upload-init publisher/published/language stored", r.get("book", {}).get("publisher") == "نشر نی"
          and r["book"]["published"] == "1399" and r["book"]["language"] == "fa", str(r)[:300])
    st, r = upload(admin, FIX / "sample-fa.epub", "dup")
    check("same file by another user is deduplicated (no re-upload)", st == 200 and r.get("exists") and r["book"]["id"] == book1)
    st, r = user.call("GET", "library")
    check("library lists the book", st == 200 and [b["id"] for b in r["books"]] == [book1])
    check("library returns publisher/published", r["books"][0]["publisher"] == "نشر نی" and r["books"][0]["published"] == "1399", str(r["books"][0])[:300])

    # Multi-chunk upload
    big = make_big_epub(tmp)
    st, r = upload(user, big, "کتاب بزرگ", init_extra={"publisher": "قدیمی"},
                   complete_extra={"publisher": "انتشارات خوارزمی", "published": "2021-05-01", "language": "fa-IR"})
    book2 = (r.get("book") or {}).get("id")
    check("multi-chunk upload of a 3 MB book", st == 201 and book2, str(r)[:200])
    check("upload-complete metadata overrides upload-init", r.get("book", {}).get("publisher") == "انتشارات خوارزمی"
          and r["book"]["published"] == "2021-05-01" and r["book"]["language"] == "fa-IR", str(r)[:300])
    st, data = user.call("GET", "book-file", query=f"&id={book2}")
    check("download returns identical bytes", st == 200 and hashlib.sha256(data).hexdigest() == hashlib.sha256(big.read_bytes()).hexdigest())

    # Invalid uploads
    bad = (FIX / "corrupt.epub").read_bytes()
    st, r = user.call("POST", "upload-init", {"sha256": hashlib.sha256(bad).hexdigest(), "size": len(bad), "title": "bad"})
    uid = r.get("upload_id")
    user.call("POST", "upload-chunk", raw=bad, query=f"&upload_id={uid}&offset=0")
    st, r = user.call("POST", "upload-complete", {"upload_id": uid})
    check("non-EPUB file rejected", st == 422, str(r))
    data = (FIX / "sample-fa-2.epub").read_bytes()
    st, r = user.call("POST", "upload-init", {"sha256": "1" * 64, "size": len(data), "title": "x", "publisher": ["bad"]})
    check("upload-init with non-string publisher -> 400", st == 400, str(r))
    st, r = user.call("POST", "upload-init", {"sha256": "1" * 64, "size": len(data), "title": "x", "published": "1" * 41})
    check("upload-init with too-long published -> 400", st == 400 and r.get("error") == "too_long_published", str(r))
    st, r = user.call("POST", "upload-init", {"sha256": "0" * 64, "size": len(data), "title": "liar"})
    uid = r.get("upload_id")
    user.call("POST", "upload-chunk", raw=data, query=f"&upload_id={uid}&offset=0")
    st, r = user.call("POST", "upload-complete", {"upload_id": uid})
    check("hash mismatch rejected", st == 400 and r.get("error") == "hash_mismatch")
    st, r = user.call("POST", "upload-chunk", raw=b"x", query=f"&upload_id={'f'*32}&offset=0")
    check("chunk for unknown upload rejected", st == 404)

    # Access control
    st, _ = admin.call("GET", "book-file", query=f"&id={book2}")
    check("admin can read any book", st == 200)
    other = Client(base)
    st, r = admin.call("POST", "invites", {"note": "other"})
    other.call("POST", "register", {"invite": r["code"], "email": "o@x.io", "password": "password4", "name": "دیگری"})
    st, _ = other.call("GET", "book-file", query=f"&id={book2}")
    check("other user cannot download a private book", st == 404)
    st, _ = other.call("GET", "book-cover", query=f"&id={book1}")
    check("other user cannot see a private cover", st == 404)
    st, _ = other.call("POST", "book-meta", {"book_id": book1, "title": "hacked"})
    check("other user cannot change metadata", st == 404)
    st, _ = user.call("GET", "book-cover", query=f"&id={book1}")
    check("owner can see the cover", st == 200)

    # Metadata correction
    st, r = user.call("POST", "book-meta", {"book_id": book1, "title": "عنوان درست", "author": "نویسندهٔ درست", "cover": PNG})
    check("uploader corrects metadata", st == 200 and r["book"]["title"] == "عنوان درست" and r["book"]["author"] == "نویسندهٔ درست")
    st, r = admin.call("POST", "book-meta", {"book_id": book1, "title": "کتاب آزمایشی", "author": "نویسنده تست"})
    check("admin can correct any book", st == 200 and r["book"]["title"] == "کتاب آزمایشی")
    st, r = user.call("POST", "book-meta", {"book_id": book1, "publisher": "نشر چشمه", "published": "1400", "language": "fa"})
    check("owner updates publisher via book-meta; absent keys untouched", st == 200 and r["book"]["publisher"] == "نشر چشمه"
          and r["book"]["published"] == "1400" and r["book"]["title"] == "کتاب آزمایشی" and r["book"]["author"] == "نویسنده تست", str(r)[:300])
    st, r = user.call("POST", "book-meta", {"book_id": book1, "publisher": "x" * 301})
    check("book-meta publisher too long -> 400", st == 400)
    st, r = user.call("POST", "book-meta", {"book_id": book1, "published": {"y": 1}})
    check("book-meta non-string published -> 400", st == 400)
    st, _ = other.call("POST", "book-meta", {"book_id": book1, "publisher": "hacked"})
    check("other user cannot change publisher", st == 404)
    # admin has book1 in library (dedupe link) but is not the uploader; still allowed as admin

    # Progress: last write wins by client timestamp
    now = int(time.time() * 1000)
    st, r = user.call("POST", "progress", {"book_id": book1, "cfi": "epubcfi(/6/4!/4/2)", "percentage": 30, "updated_at": now})
    check("progress saved", st == 200 and r["progress"]["percentage"] == 30)
    st, r = user.call("POST", "progress", {"book_id": book1, "cfi": "epubcfi(/6/2!/4/2)", "percentage": 10, "updated_at": now - 5000})
    check("older progress does not overwrite newer", r["progress"]["percentage"] == 30)
    st, r = user.call("POST", "progress", {"book_id": book1, "cfi": "epubcfi(/6/6!/4/2)", "percentage": 55, "updated_at": now + 1000})
    check("newer progress overwrites", r["progress"]["percentage"] == 55)
    st, r = other.call("POST", "progress", {"book_id": book1, "cfi": "x", "percentage": 1})
    check("progress refused for a book not in library", st == 404)
    st, r = user.call("GET", "library")
    p = next(b for b in r["books"] if b["id"] == book1)["progress"]
    check("library includes progress", p and p["percentage"] == 55)

    # Catalog with moderation
    st, r = user.call("POST", "catalog-submit", {"book_id": book2, "description": "یک کتاب خوب", "category": "x" * 81})
    check("catalog-submit category too long -> 400", st == 400 and r.get("error") == "too_long_category", str(r))
    st, r = user.call("POST", "catalog-submit", {"book_id": book2, "description": "یک کتاب خوب", "category": "  رمان  "})
    check("user submission goes to pending", st == 201 and r["status"] == "pending")
    st, r = other.call("GET", "catalog")
    check("pending book not visible in catalog", st == 200 and r["books"] == [])
    st, r = user.call("POST", "catalog-review", {"book_id": book2, "approve": True})
    check("non-admin cannot approve", st == 403)
    st, r = admin.call("GET", "catalog-pending")
    check("admin sees pending queue", st == 200 and [b["id"] for b in r["books"]] == [book2] and r["books"][0]["submitter"] == "کاربر")
    check("pending queue includes category", r["books"][0].get("category") == "رمان")
    st, r = admin.call("POST", "catalog-review", {"book_id": book2, "approve": True})
    check("admin approves", st == 200 and r["status"] == "approved")
    st, r = other.call("GET", "catalog")
    check("approved book visible to everyone", [b["id"] for b in r["books"]] == [book2] and not r["books"][0]["in_library"])
    cb = r["books"][0]
    check("catalog includes category (trimmed), publisher, published", cb.get("category") == "رمان" and cb.get("publisher") == "انتشارات خوارزمی"
          and cb.get("published") == "2021-05-01", str(cb)[:300])
    st, _ = other.call("GET", "book-file", query=f"&id={book2}")
    check("catalog book readable by everyone", st == 200)
    st, r = other.call("POST", "catalog-add", {"book_id": book2})
    st, r = other.call("GET", "library")
    check("catalog-add puts it in the library", [b["id"] for b in r["books"]] == [book2])
    st, r = other.call("POST", "catalog-submit", {"book_id": book1})
    check("cannot submit a book you do not have", st == 404)
    st, r = admin.call("POST", "catalog-submit", {"book_id": book1})
    check("admin submission is auto-approved", st == 201 and r["status"] == "approved")
    st, r = other.call("GET", "catalog")
    check("catalog-submit without category -> empty string", [b["category"] for b in r["books"] if b["id"] == book1] == [""])
    st, r = admin.call("POST", "catalog-review", {"book_id": book1, "approve": False})
    check("cannot review a non-pending entry", st == 404)

    # Removal and garbage collection
    st, r = user.call("POST", "library-remove", {"book_id": book1})
    st, r = user.call("GET", "library")
    check("remove from own library", book1 not in [b["id"] for b in r["books"]])
    st, _ = other.call("GET", "book-file", query=f"&id={book1}")
    check("book still in catalog stays readable", st == 200)
    admin.call("POST", "catalog-remove", {"book_id": book1})
    admin.call("POST", "library-remove", {"book_id": book1})
    st, _ = admin.call("GET", "book-file", query=f"&id={book1}")
    check("unreferenced book is deleted", st == 404)
    files = list((tmp / "storage" / "books").rglob("*.epub"))
    check("its file is removed from storage", len(files) == 1, str(files))

    # Session lifecycle and brute force
    st, r = user.call("POST", "logout")
    st, _ = user.call("GET", "library")
    check("logout ends the session", st == 401)
    st, r = user.call("POST", "login", {"email": "U@X.IO", "password": "password2"})
    check("login (email case-insensitive)", st == 200 and r["user"]["name"] == "کاربر")
    st, r = user.call("POST", "password", {"current": "password2", "new": "password22"})
    check("change password", st == 200)
    codes = [Client(base).call("POST", "login", {"email": "u@x.io", "password": "wrong-pass"})[0] for _ in range(11)]
    check("login rate limited after repeated failures", codes[-1] == 429 and codes[0] == 401, str(codes))
    st, r = admin.call("GET", "users")
    check("admin lists users", st == 200 and len(r["users"]) == 3)
    st, r = anon.call("POST", "login", raw=b"email=a&password=b", ctype="application/x-www-form-urlencoded")
    check("form-encoded (CSRF-able) bodies rejected", st == 415)
    # ---------- Annotations ----------
    now = int(time.time() * 1000)
    st, r = user.call("POST", "annotations", {"items": [
        {"uid": "aaaaaaaa-0001", "book_id": book2, "type": "highlight", "cfi": "epubcfi(/6/4!/4/2,/1:0,/1:5)", "quote": "متن", "note": "یادداشت", "color": "yellow", "updated_at": now},
        {"uid": "aaaaaaaa-0002", "book_id": book2, "type": "bookmark", "cfi": "epubcfi(/6/4!/4/2)", "updated_at": now},
        {"uid": "aaaaaaaa-0003", "book_id": 999999, "type": "bookmark", "cfi": "x", "updated_at": now},
        {"uid": "bad", "book_id": book2, "type": "bookmark", "cfi": "x"},
    ]})
    check("annotations saved (invalid items skipped)", st == 200 and r["saved"] == 2, str(r))
    st, r = user.call("POST", "annotations", {"items": [{"uid": "aaaaaaaa-0001", "book_id": book2, "type": "highlight", "cfi": "c", "note": "old", "updated_at": now - 10}]})
    check("older annotation edit ignored", r["saved"] == 0)
    st, r = user.call("POST", "annotations", {"items": [{"uid": "aaaaaaaa-0002", "book_id": book2, "type": "bookmark", "cfi": "epubcfi(/6/4!/4/2)", "deleted": True, "updated_at": now + 5}]})
    st, r = user.call("GET", "annotations", query="&since=0")
    items = {a["uid"]: a for a in r["annotations"]}
    check("annotations listed with deletions as tombstones", len(items) == 2 and items["aaaaaaaa-0002"]["deleted"] and items["aaaaaaaa-0001"]["note"] == "یادداشت")
    st, r2 = user.call("GET", "annotations", query=f"&since={now + 1}")
    check("annotations since timestamp", [a["uid"] for a in r2["annotations"]] == ["aaaaaaaa-0002"])
    st, r = other.call("GET", "annotations", query="&since=0")
    check("annotations are private per user", r["annotations"] == [])

    # ---------- Shelves, reading status, stats (v7.3) ----------
    st, r = user.call("POST", "shelves", {"items": [{"uid": "shelf-0001", "name": "فلسفه", "position": 1, "updated_at": now},
                                                    {"uid": "x", "name": "bad"}]})
    check("shelf saved (invalid skipped)", st == 200 and r["saved"] == 1)
    st, r = user.call("POST", "shelves", {"items": [{"uid": "shelf-0001", "name": "فلسفهٔ قدیم", "updated_at": now - 100}]})
    check("older shelf rename ignored", r["saved"] == 0)
    st, r = user.call("GET", "shelves")
    check("shelves listed", [x["name"] for x in r["shelves"]] == ["فلسفه"])
    st, r = user.call("POST", "library-meta", {"items": [{"book_id": book2, "status": "reading", "shelves": ["shelf-0001"], "updated_at": now},
                                                         {"book_id": book2, "status": "weird", "updated_at": now + 1}]})
    check("reading status and shelves saved", st == 200 and r["saved"] == 1)
    st, r = user.call("POST", "library-meta", {"items": [{"book_id": book2, "status": "want", "shelves": [], "updated_at": now - 1}]})
    check("older status ignored", r["saved"] == 0)
    st, r = user.call("GET", "library")
    b2 = [b for b in r["books"] if b["id"] == book2][0]
    check("library returns status and shelves", b2["status"] == "reading" and b2["shelves"] == ["shelf-0001"])
    st, r = other.call("POST", "library-meta", {"items": [{"book_id": book2, "status": "finished", "updated_at": now + 50}]})
    st, r2 = user.call("GET", "library")
    check("status is per user", [b for b in r2["books"] if b["id"] == book2][0]["status"] == "reading")
    user.call("POST", "stats", {"device": "device-aaaa", "days": [{"day": "2026-10-18", "seconds": 600}]})
    user.call("POST", "stats", {"device": "device-bbbb", "days": [{"day": "2026-10-18", "seconds": 300}]})
    user.call("POST", "stats", {"device": "device-aaaa", "days": [{"day": "2026-10-18", "seconds": 120}]})
    st, r = user.call("GET", "stats")
    day = [d for d in r["days"] if d["day"] == "2026-10-18"]
    check("stats summed across devices, never decrease", day and day[0]["seconds"] == 900, str(r)[:200])

    # ---------- Admin management ----------
    st, r = admin.call("GET", "admin-stats")
    check("admin stats", st == 200 and r["users"] == 3 and r["books"] >= 1 and r["storage_bytes"] > 0, str(r))
    st, _ = user.call("GET", "admin-stats")
    check("stats are admin-only", st == 403)
    st, r = admin.call("GET", "users")
    ids = {u["email"]: u["id"] for u in r["users"]}
    st, r = admin.call("POST", "user-update", {"user_id": ids["admin@x.io"], "disabled": True})
    check("admin cannot disable self", st == 400)
    st, r = admin.call("POST", "user-update", {"user_id": ids["o@x.io"], "disabled": True})
    st2, _ = other.call("GET", "library")
    check("disabled user is logged out", st == 200 and st2 == 401)
    st, _ = Client(base).call("POST", "login", {"email": "o@x.io", "password": "password4"})
    check("disabled user cannot log in", st == 401)
    admin.call("POST", "user-update", {"user_id": ids["o@x.io"], "disabled": False})
    st, r = admin.call("POST", "user-reset-password", {"user_id": ids["o@x.io"]})
    newpass = r.get("password", "")
    st2, _ = other.call("POST", "login", {"email": "o@x.io", "password": newpass})
    check("reset password works", st == 200 and len(newpass) == 12 and st2 == 200)
    st, r = admin.call("POST", "user-update", {"user_id": ids["o@x.io"], "role": "admin"})
    check("promote to admin", st == 200 and r["user"]["role"] == "admin")
    st, r = admin.call("POST", "user-update", {"user_id": ids["admin@x.io"], "role": "user"})
    check("cannot demote yourself", st == 400)
    admin.call("POST", "user-update", {"user_id": ids["o@x.io"], "role": "user"})
    st, r = admin.call("POST", "invites", {"note": "to revoke"})
    st, r = admin.call("GET", "invites")
    open_inv = [i for i in r["invites"] if i["note"] == "to revoke"][0]
    st, _ = admin.call("POST", "invite-revoke", {"id": open_inv["id"]})
    st2, r = admin.call("GET", "invites")
    check("revoke invite", st == 200 and not [i for i in r["invites"] if i["note"] == "to revoke"])
    st, r = admin.call("GET", "settings")
    check("settings: approval on by default", st == 200 and r["catalog_requires_approval"] is True)
    admin.call("POST", "settings", {"catalog_requires_approval": False})
    st, r = upload(user, FIX / "sample-fa-2.epub", "کتاب سوم")
    book3 = r["book"]["id"]
    st, r = user.call("POST", "catalog-submit", {"book_id": book3})
    check("approval off: user submission published directly", r.get("status") == "approved")
    admin.call("POST", "settings", {"catalog_requires_approval": True})
    st, r = admin.call("GET", "catalog-all")
    check("admin lists whole catalog", st == 200 and book3 in [b["id"] for b in r["books"]])
    check("catalog-all includes category", all("category" in b for b in r["books"]))
    st, _ = user.call("POST", "catalog-update", {"book_id": book3, "category": "hack"})
    check("non-admin cannot catalog-update", st == 403)
    st, r = admin.call("POST", "catalog-update", {"book_id": book3, "category": "شعر", "publisher": "نشر مرکز", "published": "1380", "language": "fa"})
    st2, r2 = admin.call("GET", "catalog-all")
    b3 = [b for b in r2["books"] if b["id"] == book3][0]
    check("admin catalog-update category+publisher+published+language", st == 200 and b3["category"] == "شعر" and b3["publisher"] == "نشر مرکز"
          and b3["published"] == "1380" and b3["language"] == "fa" and b3["title"] == "کتاب سوم", str(b3)[:300])
    st, r = admin.call("POST", "catalog-update", {"book_id": book3, "description": "نباید ذخیره شود", "category": ["bad"]})
    st2, r2 = admin.call("GET", "catalog-all")
    b3 = [b for b in r2["books"] if b["id"] == book3][0]
    check("catalog-update invalid category -> 400, nothing written", st == 400 and b3["category"] == "شعر" and b3["description"] != "نباید ذخیره شود")
    st, _ = admin.call("POST", "catalog-update", {"book_id": book3, "description": "توضیح مدیر", "title": "عنوان مدیر"})
    st2, r = other.call("GET", "catalog")
    b3 = [b for b in r["books"] if b["id"] == book3][0]
    check("admin edits catalog entry", st == 200 and b3["description"] == "توضیح مدیر" and b3["title"] == "عنوان مدیر")
    check("catalog-update without category keeps it", b3["category"] == "شعر" and b3["publisher"] == "نشر مرکز")
    st, r = admin.call("GET", "books-all")
    check("admin lists all books", st == 200 and book3 in [b["id"] for b in r["books"]])
    st, _ = admin.call("POST", "book-delete", {"book_id": book3})
    st2, r = user.call("GET", "library")
    check("admin deletes a book everywhere", st == 200 and book3 not in [b["id"] for b in r["books"]])
    st, _ = admin.call("POST", "user-delete", {"user_id": ids["o@x.io"]})
    st2, r = admin.call("GET", "users")
    check("admin deletes a user", st == 200 and len(r["users"]) == 2)
    st, _ = admin.call("POST", "user-delete", {"user_id": ids["admin@x.io"]})
    check("admin cannot delete self", st == 400)

    run_replace(base, tmp, admin, user)
    run_errors(base, admin, user)
    run_api_books(base, tmp, admin, user)

    st, r = anon.call("GET", "nope")
    check("unknown route 404", st == 404)
    st, r = anon.call("GET", "login")
    check("wrong method 405", st == 405)

def main():
    if not shutil.which("php"):
        print("php not found"); sys.exit(1)
    tmp = Path(tempfile.mkdtemp(prefix="borg-api-"))
    proc, base = start_php(tmp)
    try:
        run(base, tmp)
    finally:
        proc.terminate(); proc.wait(5); shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
