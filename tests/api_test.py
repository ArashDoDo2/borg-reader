#!/usr/bin/env python3
"""API tests for Borg. Starts `php -S` with src/ mounted at /borg/ and a throwaway config.

Database: SQLite by default; set BORG_TEST_DSN / BORG_TEST_DB_USER / BORG_TEST_DB_PASS to run against MySQL (CI).
Usage: python3 tests/api_test.py
"""
import hashlib, http.client, http.cookiejar, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
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

def spawn_php(tmp):
    """Another `php -S` on the already prepared docroot/config (same database)."""
    docroot, cfg, port = tmp / "www", tmp / "config.php", free_port()
    proc = subprocess.Popen(["php", "-d", "display_errors=0", "-d", "upload_max_filesize=2M", "-d", "post_max_size=8M", "-S", f"127.0.0.1:{port}", "-t", str(docroot)],
                            env=dict(os.environ, BORG_CONFIG=str(cfg)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try: socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError: time.sleep(0.1)
    return proc, f"http://127.0.0.1:{port}/borg/api/"

def start_php(tmp, dsn=None):
    docroot = tmp / "www"; docroot.mkdir()
    (docroot / "borg").symlink_to(ROOT / "src", target_is_directory=True)
    dsn = dsn or os.environ.get("BORG_TEST_DSN") or f"sqlite:{tmp / 'borg.sqlite'}"
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
    except (ConnectionError, http.client.RemoteDisconnected) as e:
        # PHP's built-in test server may drop an over-size request before answering (seen on GitHub runners).
        return 0, {"error": "connection_closed", "detail": type(e).__name__}
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
    # Over-size requests go to a throwaway `php -S`: on GitHub runners the built-in server can die on them
    # (connection reset, then every later request refused). Apache in production answers 413.
    xproc, xbase = spawn_php(tmp)
    st, r = api_book(xbase, tok, file=("big.epub", make_big_epub(tmp).read_bytes()))
    check("api-book file over PHP upload_max_filesize -> 413 with a clear error (or refused by the test server)",
          (st == 413 and r.get("error") == "file_too_large_for_php" and "upload_max_filesize" in r.get("message", "")) or (st == 0 and r.get("error") == "connection_closed"), str(r))
    if st == 0:  # the throwaway server died on the first one: restart it for the second check
        xproc.terminate(); xproc.wait(5); xproc, xbase = spawn_php(tmp)
    st, r = api_book(xbase, tok, raw=b"x" * (9 * 1024 * 1024), ctype="multipart/form-data; boundary=zzz")
    try: xproc.terminate(); xproc.wait(5)
    except Exception: pass
    # Apache answers 413 post_too_large; `php -S` sometimes closes the connection instead. Either way nothing is stored.
    check("api-book request over PHP post_max_size -> 413 post_too_large (or refused by the test server)",
          (st == 413 and r.get("error") == "post_too_large") or (st == 0 and r.get("error") == "connection_closed"), str(r))
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

# ---------- PDF books (text-based magazines opened directly; migration 7 books.format) ----------
def make_pdf(text="Hello from Borg", info="/Title (Test Magazine) /Author (Borg)", xref=True):
    """A minimal valid PDF written by hand: catalog, pages, one page with a Helvetica text stream, xref, trailer + Info."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    if info is not None: objs.append(b"<< " + (info.encode("latin-1") if isinstance(info, str) else info) + b" >>")
    out, offs = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    start = len(out)
    if xref:
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    trailer = f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R" + (f" /Info {len(objs)} 0 R" if info is not None else "") + " >>\n"
    out += trailer.encode() + f"startxref\n{start if xref else 0}\n%%EOF\n".encode()
    return bytes(out)

def raw_get(c, route, query):
    """GET a binary route with the client's session: (status, body bytes, headers)."""
    req = urllib.request.Request(f"{c.base}?r={route}{query}")
    try:
        with c.opener.open(req) as r: return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e: return e.code, e.read(), e.headers

def run_pdfs(base, tmp, admin, user):
    import random
    st, r = admin.call("POST", "invites", {"note": "pdf reader"})
    reader = Client(base)
    reader.call("POST", "register", {"invite": r["code"], "email": "pdf@x.io", "password": "password7", "name": "خوانندهٔ PDF"})
    st, h = reader.call("GET", "health")
    check("pdf: health reports migration 7", h.get("schema_version", 0) >= 7, str(h.get("schema_version")))
    storage = tmp / "storage" / "books"

    # Chunked upload with the "pdf" hint
    pdf = make_pdf("Chunked upload"); sha = hashlib.sha256(pdf).hexdigest()
    p = tmp / "mag.pdf"; p.write_bytes(pdf)
    st, r = upload(user, p, "مجله", "نویسنده", init_extra={"format": "pdf"})
    b = r.get("book") or {}
    check("pdf: chunked upload accepted, format pdf", st == 201 and b.get("format") == "pdf" and b.get("sha256") == sha and b.get("title") == "مجله", str(r)[:300])
    pid = b.get("id")
    check("pdf: stored as <sha>.pdf (no .epub)", (storage / sha[:2] / f"{sha}.pdf").is_file() and not (storage / sha[:2] / f"{sha}.epub").exists())
    st, L = user.call("GET", "library")
    fm = {x["id"]: x.get("format") for x in L["books"]}
    check("pdf: library lists format pdf, EPUBs keep format epub", fm.get(pid) == "pdf" and fm and all(v == "epub" for k, v in fm.items() if k != pid), str(fm))
    st, body, hd = raw_get(user, "book-file", f"&id={pid}")
    check("pdf: book-file serves application/pdf with identical bytes", st == 200 and hd.get("Content-Type") == "application/pdf" and body == pdf
          and ".pdf" in (hd.get("Content-Disposition") or ""), f"{st} {hd.get('Content-Type')} {len(body)}")
    epub_id = next((k for k, v in fm.items() if v == "epub"), None)
    st, body, hd = raw_get(user, "book-file", f"&id={epub_id}")
    check("pdf: EPUB book-file unchanged (application/epub+zip)", st == 200 and hd.get("Content-Type") == "application/epub+zip" and body[:2] == b"PK"
          and ".epub" in (hd.get("Content-Disposition") or ""), str(hd.get("Content-Type")))
    st, r = user.call("POST", "upload-init", {"sha256": sha, "size": len(pdf), "title": "again", "format": "pdf"})
    check("pdf: same PDF again dedupes by SHA-256", st == 200 and r.get("exists") is True and (r.get("book") or {}).get("id") == pid, str(r)[:200])
    PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    st, r = user.call("POST", "book-meta", {"book_id": pid, "cover": PNG})
    st2, cov, hd = raw_get(user, "book-cover", f"&id={pid}")
    check("pdf: client-rendered cover can be stored and served", st == 200 and (r.get("book") or {}).get("has_cover") is True and st2 == 200
          and hd.get("Content-Type") == "image/png", str(r)[:200])

    # Detection from the bytes, not the hint
    p2 = tmp / "nohint.pdf"; p2.write_bytes(make_pdf("No hint"))
    st, r = upload(user, p2, "بدون اشاره")
    check("pdf: detected from the bytes without a hint", st == 201 and (r.get("book") or {}).get("format") == "pdf", str(r)[:200])
    st, r = upload(user, make_variant(tmp, "hint-lies"), "EPUB با اشارهٔ pdf", init_extra={"format": "pdf"})
    hb = r.get("book") or {}
    check("pdf: an EPUB sent with format pdf is stored as EPUB", st == 201 and hb.get("format") == "epub"
          and (storage / hb.get("sha256", "xx")[:2] / f"{hb.get('sha256')}.epub").is_file(), str(r)[:200])
    import zipfile
    emb = tmp / "embeds-pdf.epub"
    src = zipfile.ZipFile(FIX / "sample-fa.epub")
    with zipfile.ZipFile(emb, "w") as z:  # "%PDF-" within the first 1024 bytes of a valid EPUB
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("OEBPS/a.pdf", make_pdf("Embedded")[:300], compress_type=zipfile.ZIP_STORED)
        for info in src.infolist():
            if info.filename != "mimetype": z.writestr(info, src.read(info.filename), compress_type=zipfile.ZIP_DEFLATED)
    st, r = upload(user, emb, "EPUB با PDF درونی")
    check("pdf: an EPUB with an embedded PDF near the start is still an EPUB", st == 201 and (r.get("book") or {}).get("format") == "epub", str(r)[:200])
    user.call("POST", "library-remove", {"book_id": (r.get("book") or {}).get("id", 0)})
    st, r = user.call("POST", "upload-init", {"sha256": "a" * 64, "size": 10, "title": "x", "format": "docx"})
    check("pdf: upload-init unknown format hint -> 400 bad_format", st == 400 and r.get("error") == "bad_format", str(r))

    # Rejections
    fake = tmp / "fake.pdf"; fake.write_bytes(make_pdf("Truncated")[:-40])
    rnd = tmp / "random.pdf"; rnd.write_bytes(random.Random(11).randbytes(5000))
    st1, r1 = upload(user, fake, "ناقص", init_extra={"format": "pdf"})
    st2, r2 = upload(user, rnd, "تصادفی", init_extra={"format": "pdf"})
    st3, r3 = upload(user, rnd, "تصادفی")
    check("pdf: PDF without %%EOF -> 422 not_pdf", st1 == 422 and r1.get("error") == "not_pdf", str(r1))
    check("pdf: random bytes sent as pdf -> 422 not_pdf", st2 == 422 and r2.get("error") == "not_pdf", str(r2))
    check("pdf: random bytes without a hint -> 422 not_epub", st3 == 422 and r3.get("error") == "not_epub", str(r3))
    st, L = user.call("GET", "library")
    check("pdf: rejected uploads leave no book", len(L["books"]) == len(fm) + 2, f"{len(L['books'])} vs {len(fm) + 2}")

    # api-book
    st, r = admin.call("POST", "api-token-create", {"name": "pdf"})
    tok = r.get("token", "")
    apdf = make_pdf("API upload")
    st, r = api_book(base, tok, fields={"category": "مجله"}, file=("weekly-issue.pdf", apdf))
    ab = r.get("book") or {}
    check("api-book pdf -> 201 approved, cleanup not-applicable", st == 201 and r.get("catalog_status") == "approved" and r.get("duplicate") is False
          and r.get("cleanup") == "not-applicable" and r.get("cleaned") == 0 and r.get("skipped_rules") == [], str(r)[:300])
    check("api-book pdf: format pdf, metadata from the Info dictionary, no cover, file unchanged", ab.get("format") == "pdf" and ab.get("title") == "Test Magazine"
          and ab.get("author") == "Borg" and ab.get("has_cover") is False and ab.get("sha256") == hashlib.sha256(apdf).hexdigest(), str(ab))
    st, C = reader.call("GET", "catalog")
    cb = next((x for x in C.get("books", []) if x["id"] == ab.get("id")), {})
    check("api-book pdf: in the catalog with format pdf", cb.get("format") == "pdf" and cb.get("category") == "مجله", str(cb)[:200])
    st, body, hd = raw_get(reader, "book-file", f"&id={ab.get('id')}")
    check("api-book pdf: book-file application/pdf, identical bytes", st == 200 and hd.get("Content-Type") == "application/pdf" and body == apdf)
    st, r = api_book(base, tok, file=("again.pdf", apdf))
    check("api-book pdf duplicate -> duplicate:true, same book", st == 201 and r.get("duplicate") is True and (r.get("book") or {}).get("id") == ab.get("id"), str(r)[:200])
    st, r = api_book(base, tok, fields={"title": "عنوان درخواست", "author": "نویسندهٔ درخواست", "language": "fa"}, file=("x.pdf", make_pdf("Fields")))
    rb = r.get("book") or {}
    check("api-book pdf: request fields win over the Info dictionary", st == 201 and rb.get("title") == "عنوان درخواست" and rb.get("author") == "نویسندهٔ درخواست"
          and rb.get("language") == "fa", str(rb))
    fa_title = "مجلهٔ هفتگی".encode("utf-16-be")
    info = b"/Title <FEFF" + fa_title.hex().upper().encode() + b"> /Author (Caf\\351 \\(Ed.\\) \\\\ Borg)"
    st, r = api_book(base, tok, file=("fa.pdf", make_pdf("Persian", info=info)))
    check("api-book pdf: UTF-16BE hex title and escaped literal author decoded", st == 201 and (r.get("book") or {}).get("title") == "مجلهٔ هفتگی"
          and r["book"].get("author") == "Café (Ed.) \\ Borg", str(r.get("book"))[:300])
    st, r = api_book(base, tok, file=("no-xref.pdf", make_pdf("No xref table", info="/Title (Scanned For) /Author (Nobody)", xref=False)))
    check("api-book pdf: Info found by scanning when there is no xref table", st == 201 and (r.get("book") or {}).get("title") == "Scanned For", str(r.get("book"))[:200])
    st, r = api_book(base, tok, file=("شمارهٔ ۱۲.pdf", make_pdf("No info", info=None)))
    check("api-book pdf without Info: title from the file name", st == 201 and (r.get("book") or {}).get("title") == "شمارهٔ ۱۲" and r["book"].get("author") == "", str(r.get("book"))[:200])
    st1, r1 = api_book(base, tok, file=("fake.pdf", make_pdf("Truncated")[:-40]))
    st2, r2 = api_book(base, tok, file=("random.pdf", random.Random(12).randbytes(4000)))
    check("api-book fake pdf -> 400 not_pdf, random bytes -> 400 not_epub", (st1, r1.get("error"), st2, r2.get("error")) == (400, "not_pdf", 400, "not_epub"), f"{r1} {r2}")

    # Garbage collection removes the .pdf file
    user.call("POST", "library-remove", {"book_id": pid})
    st, _ = admin.call("GET", "book-file", query=f"&id={pid}")
    check("pdf: removing an unreferenced PDF deletes the .pdf file", st == 404 and not (storage / sha[:2] / f"{sha}.pdf").exists())

def run_migration7():
    """Migration 7 on an existing database: a v8.7 schema (no books.format) with an EPUB gets format 'epub' and keeps working."""
    import sqlite3
    tmp2 = Path(tempfile.mkdtemp(prefix="borg-m7-"))
    proc, base2 = start_php(tmp2, dsn=f"sqlite:{tmp2 / 'old.sqlite'}")
    try:
        a = Client(base2)
        a.call("POST", "setup", {"setup_key": SETUP_KEY, "email": "m7@x.io", "password": "password1", "name": "M7"})
        epub = (FIX / "sample-fa.epub").read_bytes()
        st, r = upload(a, FIX / "sample-fa.epub", "قدیمی")
        bid = (r.get("book") or {}).get("id")
        proc.terminate(); proc.wait(5)
        # Turn the database back into a schema-6 one: undo migration 8 (indexes, source, search_key), drop the format
        # column and forget migrations 7 and 8.
        db = sqlite3.connect(tmp2 / "old.sqlite")
        for ix in ("books_created", "books_format", "catalog_status", "catalog_category", "user_books_book"): db.execute(f"DROP INDEX {ix}")
        for col in ("format", "source", "search_key"): db.execute(f"ALTER TABLE books DROP COLUMN {col}")
        db.execute("DELETE FROM schema_migrations WHERE version IN (7, 8)"); db.commit()
        cols = [c[1] for c in db.execute("PRAGMA table_info(books)")]
        db.close()
        proc, base2 = spawn_php(tmp2)
        a.base = base2
        st, h = a.call("GET", "health")
        st2, L = a.call("GET", "library")
        bk = next((x for x in L.get("books", []) if x["id"] == bid), {})
        st3, body, hd = raw_get(a, "book-file", f"&id={bid}")
        check("migrations 7 and 8 applied on an existing database", "format" not in cols and "search_key" not in cols and h.get("schema_version") == 9, f"{cols} {h.get('schema_version')}")
        check("migration 7: existing book reads as format epub and its file is still served", bk.get("format") == "epub" and st3 == 200
              and body == epub and hd.get("Content-Type") == "application/epub+zip", f"{bk} {st3}")
        db = sqlite3.connect(tmp2 / "old.sqlite")
        row = db.execute("SELECT source, search_key FROM books WHERE id = ?", (bid,)).fetchone()
        idx = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        db.close()
        check("migration 8: existing book gets source 'app' and an empty search_key; indexes created", row == ("app", "")
              and {"books_created", "books_format", "catalog_status", "catalog_category", "user_books_book"} <= idx, f"{row} {idx}")
        st, r = a.call("GET", "admin-books", query="&q=" + urllib.parse.quote("قديمي"))
        db = sqlite3.connect(tmp2 / "old.sqlite")
        key = db.execute("SELECT search_key FROM books WHERE id = ?", (bid,)).fetchone()[0]
        db.close()
        check("migration 8: admin-books backfills the old row's search_key and finds it", st == 200 and [b["id"] for b in r.get("books", [])] == [bid]
              and key == "قدیمی", f"{st} {str(r)[:200]} {key!r}")
    finally:
        proc.terminate(); proc.wait(5); shutil.rmtree(tmp2, ignore_errors=True)

def tiny_epub(path, n):
    """The smallest EPUB validate_epub accepts (mimetype + container.xml), unique per n."""
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="c.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("c.xhtml", f"<p>{n}</p>" + "x" * (n * 37 % 500))  # sizes differ, for sort=size
    return path

def search_norm_py(s):
    """Python twin of search_norm() in lib/books.php (and the client's library search folding)."""
    import re
    s = s.translate(str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", **{d: str(i % 10) for i, d in enumerate("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩")}}))
    s = re.sub("[​-‏‪-‮⁦-⁩﻿ـً-ٰٟ]", "", s).lower()
    return re.sub(r"\s+", " ", s).strip()

def run_large_catalog():
    """Migration 8: admin-books (paging, filters, normalised search, sort, facets), admin-bulk, catalog paging, source,
    search_key maintenance and backfill — on a fresh database with ~130 books so every count is exact."""
    import sqlite3
    tmp3 = Path(tempfile.mkdtemp(prefix="borg-large-"))
    dbfile = tmp3 / "large.sqlite"
    proc, base = start_php(tmp3, dsn=f"sqlite:{dbfile}")
    Q = urllib.parse.quote
    PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    try:
        admin, user = Client(base), Client(base)
        st, r = admin.call("POST", "setup", {"setup_key": SETUP_KEY, "email": "la@x.io", "password": "password1", "name": "مدیر بزرگ"})
        A = r["user"]["id"]
        code = admin.call("POST", "invites", {"note": "large"})[1]["code"]
        st, r = user.call("POST", "register", {"invite": code, "email": "lu@x.io", "password": "password2", "name": "کاربر بزرگ"})
        U = r["user"]["id"]
        names = {A: "مدیر بزرگ", U: "کاربر بزرگ"}
        admin.call("POST", "category-save", {"name": "رمان"})  # users' legacy names only match existing categories (migration 9)

        # ---- ~120 books through the normal upload flow; M = the expected state of every book ----
        M = {}
        cats = ["رمان", "شعر", "تاریخ"]
        special = {1: ("كتاب‌هاي قديمي ۱۲۳", None), 2: ("دیوان", "حافظ"), 3: ("Hello WORLD Book", None), 4: ("تخفیف 50% ویژه", None),
                   6: ("a_b test", None), 7: ("axb test", None), 8: ("کِتابِ مُعرَّب", None), 9: ("سال ٢٠٢٤", None)}
        t0 = time.time()
        for i in range(120):
            by = U if i % 3 == 0 else A
            c = user if by == U else admin
            pdf = i % 12 == 5
            title = f"کتاب نمونه {i:03d}"; author = ["حافظ", "سعدی", "مولوی", "فردوسی"][i % 4]; publisher = ["نشر الف", "نشر ب", "نشر ج"][i % 3]
            if i in special: title, author = special[i][0], special[i][1] or author
            if pdf:
                p = tmp3 / f"b{i}.pdf"; p.write_bytes(make_pdf(f"Large {i}"))
            else:
                p = tiny_epub(tmp3 / f"b{i}.epub", i)
            st, r = upload(c, p, title, author, cover=PNG if i % 4 == 1 else None,
                           init_extra={"publisher": publisher, **({"format": "pdf"} if pdf else {})})
            if st != 201: check(f"large: upload {i}", False, str(r)[:200]); continue
            b = r["book"]
            M[b["id"]] = {"title": title, "author": author, "publisher": publisher, "format": "pdf" if pdf else "epub", "by": by,
                          "api": False, "cover": b["has_cover"], "size": b["size"], "status": None, "category": "", "lib": {by}}
            bid = b["id"]
            if by == A and i % 5 != 0:
                cat = "" if i % 7 == 0 else cats[i % 3]
                admin.call("POST", "catalog-submit", {"book_id": bid, "category": cat})
                M[bid].update(status="approved", category=cat)
            elif by == U and i % 2 == 0:
                cat = "رمان" if i % 4 == 0 else ""
                user.call("POST", "catalog-submit", {"book_id": bid, "category": cat})
                M[bid].update(status="pending", category=cat)
                if i % 9 == 0:
                    admin.call("POST", "catalog-review", {"book_id": bid, "approve": False})
                    M[bid]["status"] = "rejected"
        check("large: 120 books uploaded through the normal flow", len(M) == 120, f"{len(M)} in {time.time() - t0:.1f}s")
        # three books from the owner's program (api-book) -> source 'api'
        tok = admin.call("POST", "api-token-create", {"name": "large"})[1]["token"]
        api_ids = []
        for k in range(3):
            st, r = api_book(base, tok, fields={"title": f"کتاب برنامه {k}", "author": "ربات", "category": "مجله"},
                             file=(f"api{k}.epub", tiny_epub(tmp3 / f"api{k}.epub", 1000 + k).read_bytes()))
            if st == 201:
                api_ids.append(r["book"]["id"])
                M[r["book"]["id"]] = {"title": f"کتاب برنامه {k}", "author": "ربات", "publisher": "", "format": "epub", "by": A, "api": True,
                                      "cover": False, "size": r["book"]["size"], "status": "approved", "category": "مجله", "lib": set()}
        check("large: api-book uploads accepted", len(api_ids) == 3)
        sid = {i: next(b for b, m in M.items() if m["title"] == special[i][0]) for i in special}

        def listing(query="", c=None):
            """All pages of admin-books for a query string (per=100)."""
            out, page, tot = [], 1, None
            while True:
                st, r = (c or admin).call("GET", "admin-books", query=f"{query}&per=100&page={page}")
                if st != 200: return st, r, []
                out += r["books"]; tot = r["total"]
                if len(r["books"]) < 100: break
                page += 1
            return st, {"total": tot, "facets": r["facets"]}, out
        def ids(query=""):
            st, r, rows = listing(query)
            return [b["id"] for b in rows] if st == 200 else ("error", st, r)
        def expect(pred, q=""):
            words = [w for w in search_norm_py(q).split(" ") if w]
            return {b for b, m in M.items() if pred(m) and all(w in search_norm_py(f"{m['title']} {m['author']} {m['publisher']}") for w in words)}

        # ---- response shape, paging, per cap ----
        st, r = admin.call("GET", "admin-books")
        b0 = (r.get("books") or [{}])[0]
        check("admin-books: default page 1, per 50, total = all books", st == 200 and r["page"] == 1 and r["per"] == 50 and r["total"] == len(M)
              and len(r["books"]) == 50, str({k: r.get(k) for k in ("page", "per", "total")}))
        check("admin-books: book fields", set(b0) >= {"id", "sha256", "size", "title", "author", "language", "publisher", "published", "has_cover", "format",
              "catalog_status", "category", "description", "submitter", "uploader_id", "uploader_name", "created_at", "source", "in_libraries"}, str(sorted(b0)))
        seen = []
        for page in range(1, 20):
            st, r = admin.call("GET", "admin-books", query=f"&per=7&page={page}")
            seen += [b["id"] for b in r["books"]]
            if not r["books"]: break
        check("admin-books: pages of 7 cover every book exactly once", sorted(seen) == sorted(M) and r["total"] == len(M) and r["page"] == page, f"{len(seen)} {len(set(seen))}")
        st, r = admin.call("GET", "admin-books", query="&per=500")
        check("admin-books: per capped at 100", st == 200 and r["per"] == 100 and len(r["books"]) == 100)
        st, r = admin.call("GET", "admin-books", query="&per=0&page=-3")
        check("admin-books: per/page below 1 -> 1", st == 200 and r["per"] == 1 and r["page"] == 1 and len(r["books"]) == 1)
        st, r = admin.call("GET", "admin-books", query="&page=99999999999999999999")
        check("admin-books: huge page clamped (no 500)", st == 200 and r["page"] == 100000 and r["books"] == [], str(r)[:200])

        # ---- filters ----
        for name, query, pred in [
            ("status=approved", "&status=approved", lambda m: m["status"] == "approved"),
            ("status=pending", "&status=pending", lambda m: m["status"] == "pending"),
            ("status=rejected", "&status=rejected", lambda m: m["status"] == "rejected"),
            ("status=none", "&status=none", lambda m: m["status"] is None),
            ("category=شعر", "&category=" + Q("شعر"), lambda m: m["status"] and m["category"] == "شعر"),
            ("category=__none__", "&category=__none__", lambda m: m["category"] == ""),
            ("format=pdf", "&format=pdf", lambda m: m["format"] == "pdf"),
            ("format=epub", "&format=epub", lambda m: m["format"] == "epub"),
            ("uploader=user", f"&uploader={U}", lambda m: m["by"] == U and not m["api"]),
            ("uploader=admin (app uploads only)", f"&uploader={A}", lambda m: m["by"] == A and not m["api"]),
            ("uploader=api", "&uploader=api", lambda m: m["api"]),
            ("no_cover=1", "&no_cover=1", lambda m: not m["cover"]),
            ("combined status+format+uploader", f"&status=approved&format=pdf&uploader={A}", lambda m: m["status"] == "approved" and m["format"] == "pdf" and not m["api"]),
        ]:
            got = ids(query); want = expect(pred)
            check(f"admin-books filter {name}", isinstance(got, list) and sorted(got) == sorted(want) and len(want) > 0, f"{len(got) if isinstance(got, list) else got} vs {len(want)}")
        codes = [admin.call("GET", "admin-books", query=qq)[0] for qq in ("&status=x", "&format=mobi", "&uploader=bob", "&sort=x", "&dir=up", "&category=" + "x" * 81)]
        check("admin-books: bad status/format/uploader/sort/dir/category -> 400", codes == [400] * 6, str(codes))

        # ---- normalised Persian search (all words, LIKE on search_key) ----
        for q, want in [("کتابهای قدیمی 123", {sid[1]}), ("قديمي", {sid[1]}), ("۱۲۳", {sid[1]}), ("کتاب‌ها", {sid[1]}), ("كتابهاي", {sid[1]}),
                        ("hello world", {sid[3]}), ("HELLO", {sid[3]}), ("50%", {sid[4]}), ("%", {sid[4]}), ("a_b", {sid[6]}),
                        ("کتاب معرب", {sid[8]}), ("2024", {sid[9]}), ("۲۰۲۴", {sid[9]}), ("حافظ دیوان", {sid[2]}), ("دیوان ناموجود", set()),
                        ("  دیـــوان  ", {sid[2]})]:
            got = ids("&q=" + Q(q))
            check(f"admin-books search {q!r}", isinstance(got, list) and set(got) == want and len(got) == len(want), f"{got}")
        for q in ["نمونه 01", "نشر ج", "سعدی نشر", "ربات"]:
            got = ids("&q=" + Q(q)); want = expect(lambda m: True, q)
            check(f"admin-books search {q!r} matches the model", isinstance(got, list) and sorted(got) == sorted(want) and want, f"{len(got)} vs {len(want)}")

        # ---- sort orders ----
        st, r, rows = listing("&sort=date")
        check("sort=date (default desc): newest first, id breaks ties", [(b["created_at"], b["id"]) for b in rows] == sorted([(b["created_at"], b["id"]) for b in rows], reverse=True))
        st, r, rows = listing("&sort=date&dir=asc")
        check("sort=date&dir=asc", [b["id"] for b in rows] == sorted([b["id"] for b in rows]))
        st, r, rows = listing("&sort=title")
        check("sort=title (default asc)", [(b["title"], b["id"]) for b in rows] == sorted((b["title"], b["id"]) for b in rows))
        st, r, rows = listing("&sort=title&dir=desc")
        check("sort=title&dir=desc", [(b["title"], b["id"]) for b in rows] == sorted(((b["title"], b["id"]) for b in rows), reverse=True))
        st, r, rows = listing("&sort=size")
        check("sort=size (default desc)", [(b["size"], b["id"]) for b in rows] == sorted(((b["size"], b["id"]) for b in rows), reverse=True))
        rank = {"pending": 0, "approved": 1, "rejected": 2, None: 3}
        st, r, rows = listing("&sort=status")
        check("sort=status asc: pending, approved, rejected, not in catalog", [rank[b["catalog_status"]] for b in rows] == sorted(rank[b["catalog_status"]] for b in rows))
        st, r, rows = listing("&sort=status&dir=desc")
        check("sort=status desc", [rank[b["catalog_status"]] for b in rows] == sorted((rank[b["catalog_status"]] for b in rows), reverse=True))

        # ---- row values ----
        st, r, rows = listing()
        R = {b["id"]: b for b in rows}
        ok = all(R[b]["catalog_status"] == m["status"] and R[b]["category"] == m["category"] and R[b]["source"] == ("api" if m["api"] else "app")
                 and R[b]["uploader_id"] == m["by"] and R[b]["uploader_name"] == names[m["by"]] and R[b]["in_libraries"] == len(m["lib"])
                 and R[b]["format"] == m["format"] for b, m in M.items())
        check("admin-books rows: status, category, source, uploader, in_libraries, format match", ok)
        pend = next(b for b, m in M.items() if m["status"] == "pending")
        check("admin-books rows: submitter of a user's submission", R[pend]["submitter"] == names[U] and R[next(b for b, m in M.items() if m["status"] is None)]["submitter"] is None)
        user.call("POST", "catalog-add", {"book_id": api_ids[0]}); M[api_ids[0]]["lib"].add(U)
        st, r, rows = listing("&uploader=api")
        check("in_libraries counts user_books", {b["id"]: b["in_libraries"] for b in rows}.get(api_ids[0]) == 1)

        # ---- facets (for q only) ----
        def facets_ok(q=""):
            st, r = admin.call("GET", "admin-books", query=("&q=" + Q(q) if q else "") + "&status=approved&format=pdf")  # other filters ignored
            f = r["facets"]; sel = expect(lambda m: True, q)
            want_status = {k: sum(1 for b in sel if (M[b]["status"] or "none") == k) for k in ("approved", "pending", "rejected", "none")}
            cc = {}
            for b in sel:
                if M[b]["status"] and M[b]["category"]: cc[M[b]["category"]] = cc.get(M[b]["category"], 0) + 1
            up = {}
            for b in sel:
                k = "api" if M[b]["api"] else M[b]["by"]; up[k] = up.get(k, 0) + 1
            fm = {"epub": sum(1 for b in sel if M[b]["format"] == "epub"), "pdf": sum(1 for b in sel if M[b]["format"] == "pdf")}
            got_up = {u["id"]: u["count"] for u in f["uploaders"]}
            return (f["status"] == want_status and {c["name"]: c["count"] for c in f["categories"]} == cc and got_up == up and f["formats"] == fm
                    and all(u["name"] == ("API" if u["id"] == "api" else names[u["id"]]) for u in f["uploaders"])
                    and [c["count"] for c in f["categories"]] == sorted((c["count"] for c in f["categories"]), reverse=True)), f
        ok, f = facets_ok()
        check("facets: status, categories, uploaders (incl. api), formats for all books", ok, json.dumps(f, ensure_ascii=False)[:400])
        ok, f = facets_ok("حافظ")
        check("facets follow q", ok and sum(f["status"].values()) == len(expect(lambda m: True, "حافظ")), json.dumps(f, ensure_ascii=False)[:400])

        # ---- search_key follows metadata edits ----
        x = next(b for b, m in M.items() if m["by"] == A and m["status"] == "approved" and not m["api"])
        old_title = M[x]["title"]
        admin.call("POST", "book-meta", {"book_id": x, "title": "عنوان تازه‌ي ويرايش"})
        M[x]["title"] = "عنوان تازه‌ي ويرايش"
        check("search_key updated by book-meta", ids("&q=" + Q("تازهی ویرایش")) == [x] and x not in ids("&q=" + Q(old_title)))
        admin.call("POST", "catalog-update", {"book_id": x, "author": "نویسندهٔ يكتا", "publisher": "ناشر يكتا"})
        M[x]["author"] = "نویسندهٔ يكتا"; M[x]["publisher"] = "ناشر يكتا"
        check("search_key updated by catalog-update (author, publisher)", ids("&q=" + Q("یکتا ناشر")) == [x])
        st, r = upload(admin, tiny_epub(tmp3 / "replace.epub", 5000), "بدون عنوان")
        nb = r["book"]["id"]
        st, r = admin.call("POST", "book-replace", {"old_book_id": x, "new_book_id": nb})
        M[nb] = dict(M.pop(x), size=r["book"]["size"], cover=r["book"]["has_cover"])
        check("search_key updated by book-replace metadata copy", st == 200 and ids("&q=" + Q("یکتا ناشر")) == [nb], str(r)[:200])

        # ---- admin-bulk: permissions and validation ----
        codes = [user.call("GET", "admin-books")[0], user.call("POST", "admin-bulk", {"action": "approve", "ids": [1]})[0],
                 Client(base).call("GET", "admin-books")[0], admin.call("POST", "admin-bulk", {"action": "approve", "ids": [1]}, csrf=False)[0],
                 admin.call("GET", "admin-bulk")[0]]
        check("non-admin 403, anonymous 401, missing CSRF 403, GET admin-bulk 405", codes == [403, 403, 401, 403, 405], str(codes))
        bad = [admin.call("POST", "admin-bulk", b) for b in (
            {"action": "explode", "ids": [1]}, {"action": "approve"}, {"action": "approve", "ids": "1,2"}, {"action": "approve", "ids": [1, "x"]},
            {"action": "approve", "ids": [0]}, {"action": "set_category", "ids": [1]}, {"action": "set_category", "ids": [1], "category": "x" * 81},
            {"action": "approve", "filter": {"status": "nope"}})]
        check("admin-bulk validation -> 400", [s for s, _ in bad] == [400] * 8 and [r["error"] for _, r in bad] ==
              ["bad_action", "missing_target", "bad_ids", "bad_ids", "bad_ids", "missing_category", "too_long_category", "bad_status"], str([r.get("error") for _, r in bad]))
        st, r = admin.call("POST", "admin-bulk", {"action": "delete", "ids": list(range(1, 5002))})
        check("admin-bulk: more than 5000 ids -> 400 too_many, nothing deleted", st == 400 and r.get("error") == "too_many" and len(ids()) == len(M), str(r))
        st, r = admin.call("POST", "admin-bulk", {"action": "approve", "ids": list(range(10 ** 6, 10 ** 6 + 5000))})
        check("admin-bulk: exactly 5000 ids accepted (unknown ids skipped)", st == 200 and r == {"affected": 0, "skipped": 5000}, str(r))

        def state_ok():
            st, r, rows = listing()
            got = {b["id"]: (b["catalog_status"], b["category"]) for b in rows}
            want = {b: (m["status"], m["category"] if m["status"] else "") for b, m in M.items()}
            return got == want
        def bulk(body):
            return admin.call("POST", "admin-bulk", body)
        def pick(pred, n):
            return [b for b, m in sorted(M.items()) if pred(m)][:n]

        # ---- admin-bulk by ids ----
        pending, approved, none_ = pick(lambda m: m["status"] == "pending", 3), pick(lambda m: m["status"] == "approved" and not m["api"], 2), pick(lambda m: m["status"] is None, 1)
        st, r = bulk({"action": "approve", "ids": pending + approved + none_ + [999999], "filter": {"status": "none"}})
        for b in pending: M[b]["status"] = "approved"
        check("bulk ids approve: pending -> approved; approved/none/unknown skipped (ids win over filter)", r == {"affected": 3, "skipped": 4} and state_ok(), str(r))
        st, r = admin.call("GET", "catalog-pending")
        check("bulk approve sets reviewed status (catalog-pending shrinks)", all(b not in [x["id"] for x in r["books"]] for b in pending))
        st, r = bulk({"action": "reject", "ids": approved + [str(approved[0])]})
        for b in approved: M[b]["status"] = "rejected"
        check("bulk ids reject (string ids accepted, duplicates once)", r == {"affected": 2, "skipped": 0} and state_ok(), str(r))
        tgt = pick(lambda m: m["status"] == "approved" and m["category"] != "علمی" and not m["api"], 3) + pick(lambda m: m["status"] is None, 1)
        st, r = bulk({"action": "set_category", "ids": tgt, "category": " علمی "})
        for b in tgt[:3]: M[b]["category"] = "علمی"
        check("bulk ids set_category (trimmed; book not in catalog skipped)", r == {"affected": 3, "skipped": 1} and state_ok(), str(r))
        st, r = bulk({"action": "set_category", "ids": tgt, "category": "علمی"})
        check("bulk set_category again: unchanged rows skipped", r == {"affected": 0, "skipped": 4}, str(r))
        st, r = bulk({"action": "clear_category", "ids": tgt})
        for b in tgt[:3]: M[b]["category"] = ""
        check("bulk ids clear_category", r == {"affected": 3, "skipped": 1} and state_ok(), str(r))
        rm = pick(lambda m: m["status"] == "approved" and not m["api"], 2)
        st, r = bulk({"action": "remove_catalog", "ids": rm + [api_ids[1]]})
        for b in rm: M[b].update(status=None, category="")
        api_gone = M.pop(api_ids[1])
        st2, _ = admin.call("GET", "book-file", query=f"&id={api_ids[1]}")
        check("bulk ids remove_catalog: rows removed; a book in no library is garbage-collected", r == {"affected": 3, "skipped": 0} and state_ok() and st2 == 404, str(r))
        st, f = admin.call("GET", "book-file", query=f"&id={rm[0]}")
        check("bulk remove_catalog keeps a book that is still in a library", st == 200)
        add = pick(lambda m: m["status"] is None, 3) + pick(lambda m: m["status"] == "pending", 1)
        st, r = bulk({"action": "add_catalog", "ids": add})
        for b in add[:3]: M[b].update(status="approved", category="")
        check("bulk ids add_catalog: approved rows for books not in the catalog only", r == {"affected": 3, "skipped": 1} and state_ok(), str(r))
        st, rr, rows = listing("&status=approved")
        check("bulk add_catalog: submitter is the admin", all(b["submitter"] == names[A] for b in rows if b["id"] in add[:3]))
        dl = pick(lambda m: m["by"] == U and m["status"] == "pending", 1) + pick(lambda m: m["by"] == A and m["status"] is None, 1)
        st, r = bulk({"action": "delete", "ids": dl + [999998]})
        for b in dl: M.pop(b)
        L = [b["id"] for b in user.call("GET", "library")[1]["books"]]
        st2, _ = admin.call("GET", "book-file", query=f"&id={dl[0]}")
        check("bulk ids delete: books gone everywhere (unknown id skipped)", r == {"affected": 2, "skipped": 1} and state_ok() and dl[0] not in L and st2 == 404, str(r))

        # ---- admin-bulk by filter ----
        n_pend = len(expect(lambda m: m["status"] == "pending"))
        st, r = bulk({"action": "approve", "filter": {"status": "pending"}})
        for m in M.values():
            if m["status"] == "pending": m["status"] = "approved"
        st2, rr = admin.call("GET", "admin-books")
        check("bulk filter approve status=pending", r == {"affected": n_pend, "skipped": 0} and n_pend > 0 and rr["facets"]["status"]["pending"] == 0 and state_ok(), str(r))
        sel = expect(lambda m: m["status"] == "rejected")
        st, r = bulk({"action": "reject", "filter": {"status": "rejected"}})
        check("bulk filter reject on already rejected: all skipped", r == {"affected": 0, "skipped": len(sel)}, str(r))
        sel = expect(lambda m: m["status"] == "approved" and m["category"] == "شعر")
        st, r = bulk({"action": "set_category", "filter": {"status": "approved", "category": "شعر"}, "category": "شعر کلاسیک"})
        for b in sel: M[b]["category"] = "شعر کلاسیک"
        check("bulk filter set_category (status+category)", r == {"affected": len(sel), "skipped": 0} and len(sel) > 0 and state_ok(), str(r))
        sel = expect(lambda m: m["status"] and m["category"] == "تاریخ", "حافظ")
        st, r = bulk({"action": "clear_category", "filter": {"q": "حافظ", "category": "تاریخ"}})
        for b in sel: M[b]["category"] = ""
        check("bulk filter clear_category (q+category)", r == {"affected": len(sel), "skipped": 0} and len(sel) > 0 and state_ok(), str(r))
        sel = expect(lambda m: m["format"] == "pdf" and m["status"] is not None)
        alls = expect(lambda m: m["format"] == "pdf")
        st, r = bulk({"action": "remove_catalog", "filter": {"format": "pdf"}})
        for b in sel: M[b].update(status=None, category="")
        check("bulk filter remove_catalog format=pdf", r == {"affected": len(sel), "skipped": len(alls) - len(sel)} and len(sel) > 0 and state_ok(), str(r))
        sel = expect(lambda m: m["status"] is None and m["format"] == "pdf")
        st, r = bulk({"action": "add_catalog", "filter": {"status": "none", "format": "pdf"}})
        for b in sel: M[b].update(status="approved", category="")
        check("bulk filter add_catalog status=none&format=pdf", r == {"affected": len(sel), "skipped": 0} and len(sel) > 0 and state_ok(), str(r))
        sel = expect(lambda m: m["by"] == U and not m["api"] and m["status"] == "rejected")
        st, r = bulk({"action": "delete", "filter": {"uploader": str(U), "status": "rejected"}})
        for b in sel: M.pop(b)
        check("bulk filter delete uploader+status", r == {"affected": len(sel), "skipped": 0} and len(sel) > 0 and state_ok(), str(r))

        # ---- catalog paging (user) ----
        appr = expect(lambda m: m["status"] == "approved")
        st, r = user.call("GET", "catalog")
        check("catalog without page: unchanged full response", st == 200 and set(r) == {"books"} and sorted(b["id"] for b in r["books"]) == sorted(appr)
              and set(r["books"][0]) == {"id", "sha256", "size", "title", "author", "language", "publisher", "published", "has_cover", "format",
                                         "description", "category", "categories", "submitter", "in_library"}, str(sorted(r.get("books", [{}])[0]))[:300])
        full_new = [b["id"] for b in r["books"]]
        st, r = user.call("GET", "catalog", query="&page=1&per=5")
        check("catalog page: total, page, per, same book fields", st == 200 and r["total"] == len(appr) and r["page"] == 1 and r["per"] == 5
              and len(r["books"]) == 5 and set(r["books"][0]) == {"id", "sha256", "size", "title", "author", "language", "publisher", "published",
              "has_cover", "format", "description", "category", "categories", "submitter", "in_library"}, str({k: r.get(k) for k in ("total", "page", "per")}))
        st, r = user.call("GET", "catalog", query="&page=1")
        check("catalog page: per defaults to 24", r["per"] == 24 and len(r["books"]) == min(24, len(appr)))
        st, r = user.call("GET", "catalog", query="&page=1&per=1000")
        check("catalog page: per capped at 60", r["per"] == 60 and len(r["books"]) == min(60, len(appr)))
        got = []
        for page in range(1, 20):
            st, r = user.call("GET", "catalog", query=f"&page={page}&per=24")
            got += [b["id"] for b in r["books"]]
            if len(r["books"]) < 24: break
        check("catalog pages (sort=new) cover the unpaged list exactly once", sorted(got) == sorted(full_new) and len(set(got)) == len(got), f"{len(got)} vs {len(full_new)}")
        def cat_all(query):
            out = []
            for page in range(1, 20):
                st, r = user.call("GET", "catalog", query=f"{query}&page={page}&per=60")
                if st != 200: return ("error", st, r)
                out += r["books"]
                if len(r["books"]) < 60: break
            return out
        rows = cat_all("&sort=title")
        check("catalog sort=title", [(b["title"], b["id"]) for b in rows] == sorted((b["title"], b["id"]) for b in rows) and len(rows) == len(appr))
        rows = cat_all("&sort=author")
        check("catalog sort=author", [(b["author"], b["title"], b["id"]) for b in rows] == sorted((b["author"], b["title"], b["id"]) for b in rows))
        check("catalog page: in_library per user", all(b["in_library"] == (U in M[b["id"]]["lib"]) for b in rows) and any(b["in_library"] for b in rows))
        for name, query, pred, q in [
            ("category", "&category=" + Q("رمان"), lambda m: m["category"] == "رمان", ""),
            ("author", "&author=" + Q("سعدی"), lambda m: m["author"] == "سعدی", ""),
            ("format", "&format=pdf", lambda m: m["format"] == "pdf", ""),
            ("q", "&q=" + Q("كتاب نمونه 1"), lambda m: True, "كتاب نمونه 1"),
            ("q+category+format", "&q=" + Q("نمونه") + "&category=" + Q("رمان") + "&format=epub", lambda m: m["category"] == "رمان" and m["format"] == "epub", "نمونه"),
        ]:
            rows = cat_all(query); want = expect(lambda m: m["status"] == "approved" and pred(m), q)
            check(f"catalog page filter {name}", isinstance(rows, list) and sorted(b["id"] for b in rows) == sorted(want) and want, f"{len(rows)} vs {len(want)}")
        st, r = user.call("GET", "catalog", query="&page=1&q=" + Q("حافظ"))
        cc = {}
        for b in expect(lambda m: m["status"] == "approved" and m["category"], "حافظ"): cc[M[b]["category"]] = cc.get(M[b]["category"], 0) + 1
        check("catalog page: categories (approved, for q)", {c["name"]: c["count"] for c in r["categories"]} == cc and cc, str(r.get("categories"))[:300])
        st, r = user.call("GET", "catalog", query="&page=99")
        check("catalog page past the end: empty list, real total", st == 200 and r["books"] == [] and r["total"] == len(appr))
        codes = [user.call("GET", "catalog", query=qq)[0] for qq in ("&page=1&sort=x", "&page=1&format=x")]
        check("catalog page: bad sort/format -> 400", codes == [400, 400], str(codes))

        # ---- search_key backfill: rows written with an empty key (e.g. before migration 8), 500 per request ----
        db = sqlite3.connect(dbfile)
        now = int(time.time())
        db.executemany("INSERT INTO books (sha256, size, title, author, language, has_cover, uploaded_by, created_at) VALUES (?, 1, ?, 'نويسندهٔ قديم', '', 0, ?, ?)",
                       [(hashlib.sha256(f"bf{k}".encode()).hexdigest(), f"پر شدنی {k}", A, now) for k in range(600)])
        db.commit()
        empty0 = db.execute("SELECT COUNT(*) FROM books WHERE search_key = ''").fetchone()[0]
        db.close()
        st, r = admin.call("GET", "admin-books", query="&q=" + Q("نویسندهٔ قدیم") + "&per=1")
        db = sqlite3.connect(dbfile)
        empty1 = db.execute("SELECT COUNT(*) FROM books WHERE search_key = ''").fetchone()[0]
        db.close()
        check("backfill: one request fills 500 empty keys", empty0 == 600 and empty1 == 100 and r["total"] == 500, f"{empty0} {empty1} {r.get('total')}")
        st, r = user.call("GET", "catalog", query="&page=1")
        db = sqlite3.connect(dbfile)
        empty2 = db.execute("SELECT COUNT(*) FROM books WHERE search_key = ''").fetchone()[0]
        key = db.execute("SELECT search_key FROM books WHERE title = 'پر شدنی 7'").fetchone()[0]
        db.close()
        check("backfill: catalog requests fill the rest; key is normalised", empty2 == 0 and key == "پر شدنی 7 نویسنده قدیم", f"{empty2} {key!r}")
        st, r = bulk({"action": "delete", "filter": {"q": "پر شدنی"}})
        check("bulk filter delete of 600 rows (no files on disk)", r == {"affected": 600, "skipped": 0} and len(ids()) == len(M), str(r))
    finally:
        proc.terminate(); proc.wait(5); shutil.rmtree(tmp3, ignore_errors=True)

def run_migration9():
    """Migration 9 on an existing database: distinct non-empty catalog.category strings become top-level categories
    (trimmed) and their books are linked; the data statements are idempotent."""
    import sqlite3, re
    tmp2 = Path(tempfile.mkdtemp(prefix="borg-m9-"))
    dbfile = tmp2 / "m9.sqlite"
    proc, base2 = start_php(tmp2, dsn=f"sqlite:{dbfile}")
    try:
        a = Client(base2)
        a.call("POST", "setup", {"setup_key": SETUP_KEY, "email": "m9@x.io", "password": "password1", "name": "M9"})
        ids = []
        for i in range(5):
            st, r = upload(a, tiny_epub(tmp2 / f"m9-{i}.epub", 300 + i), f"کتاب م{i}")
            ids.append(r["book"]["id"])
            a.call("POST", "catalog-submit", {"book_id": ids[-1]})
        proc.terminate(); proc.wait(5)
        # Back to a schema-8 database whose catalog rows carry free-text categories.
        db = sqlite3.connect(dbfile)
        db.execute("DROP TABLE catalog_categories"); db.execute("DROP TABLE categories")
        db.execute("DELETE FROM schema_migrations WHERE version = 9")
        for bid, cat in zip(ids, [" رمان ", "رمان", "شعر", "", "  "]):
            db.execute("UPDATE catalog SET category = ? WHERE book_id = ?", (cat, bid))
        db.commit(); db.close()
        proc, base2 = spawn_php(tmp2)
        a.base = base2
        st, h = a.call("GET", "health")
        st2, r = a.call("GET", "categories")
        C = r.get("categories", [])
        check("migration 9 applied on an existing database", h.get("schema_version") == 9, str(h.get("schema_version")))
        check("migration 9: distinct non-empty strings become top-level categories (trimmed) with counts",
              sorted((c["name"], c["parent_id"], c["count"]) for c in C) == [("رمان", 0, 2), ("شعر", 0, 1)], str(C))
        byname = {c["name"]: c["id"] for c in C}
        st, r = a.call("GET", "catalog")
        B = {b["id"]: b for b in r.get("books", [])}
        check("migration 9: books linked; legacy column trimmed; empty ones unlinked",
              [[c["id"] for c in B[i]["categories"]] for i in ids] == [[byname.get("رمان")], [byname.get("رمان")], [byname.get("شعر")], [], []]
              and [B[i]["category"] for i in ids] == ["رمان", "رمان", "شعر", "", ""]
              and B[ids[0]]["categories"][0] == {"id": byname.get("رمان"), "name": "رمان", "parent_id": 0, "parent_name": ""}, str([B[i] for i in ids])[:400])
        # Run the migration's data statements again: nothing changes.
        src = (ROOT / "src" / "api" / "lib" / "core.php").read_text(encoding="utf-8")
        block = re.search(r"\n        9 => \[(.*?)\n        \],", src, re.S).group(1)
        stmts = [x for x in re.findall(r'"((?:INSERT|UPDATE)[^"]*)"', block)]
        db = sqlite3.connect(dbfile)
        before = (db.execute("SELECT * FROM categories ORDER BY id").fetchall(), db.execute("SELECT * FROM catalog_categories ORDER BY 1, 2").fetchall())
        for x in stmts: db.execute(x)
        db.commit()
        after = (db.execute("SELECT * FROM categories ORDER BY id").fetchall(), db.execute("SELECT * FROM catalog_categories ORDER BY 1, 2").fetchall())
        db.close()
        check("migration 9: data statements are idempotent", len(stmts) == 3 and before == after and len(before[1]) == 3, f"{len(stmts)} {before} {after}")
    finally:
        proc.terminate(); proc.wait(5); shutil.rmtree(tmp2, ignore_errors=True)

def run_categories():
    """Migration 9: two-level categories, several per book — routes, counts, filters, book fields, bulk, api-book."""
    tmp4 = Path(tempfile.mkdtemp(prefix="borg-cat-"))
    proc, base = start_php(tmp4, dsn=f"sqlite:{tmp4 / 'cat.sqlite'}")
    Q = urllib.parse.quote
    try:
        admin, user, anon = Client(base), Client(base), Client(base)
        admin.call("POST", "setup", {"setup_key": SETUP_KEY, "email": "ca@x.io", "password": "password1", "name": "مدیر"})
        code = admin.call("POST", "invites", {"note": "cat"})[1]["code"]
        user.call("POST", "register", {"invite": code, "email": "cu@x.io", "password": "password2", "name": "کاربر"})
        n = [0]
        def book(c, title):
            n[0] += 1
            st, r = upload(c, tiny_epub(tmp4 / f"c{n[0]}.epub", 2000 + n[0]), title)
            return r["book"]["id"]
        def save(body, c=None): return (c or admin).call("POST", "category-save", body)
        def cats(c=None): return {x["id"]: x for x in (c or admin).call("GET", "categories")[1]["categories"]}
        def order(c=None): return [x["id"] for x in (c or admin).call("GET", "categories")[1]["categories"]]
        def cat_ids(b): return [x["id"] for x in b["categories"]]
        def catalog_book(bid):
            st, r = admin.call("GET", "catalog-all")
            return next((b for b in r["books"] if b["id"] == bid), None)
        def ccat(bid): return cat_ids(catalog_book(bid))
        def legacy(bid): return catalog_book(bid)["category"]

        # ---- permissions ----
        codes = [user.call("POST", "category-save", {"name": "x"})[0], user.call("POST", "category-delete", {"id": 1})[0],
                 user.call("POST", "category-order", {"parent_id": 0, "ids": [1]})[0],
                 anon.call("POST", "category-save", {"name": "x"})[0], anon.call("GET", "categories")[0],
                 admin.call("POST", "category-save", {"name": "x"}, csrf=False)[0], admin.call("POST", "category-delete", {"id": 1}, csrf=False)[0],
                 admin.call("POST", "category-order", {"parent_id": 0, "ids": [1]}, csrf=False)[0],
                 admin.call("GET", "category-save")[0], admin.call("POST", "categories", {})[0]]
        check("categories: non-admin 403, anonymous 401, missing CSRF 403, wrong method 405", codes == [403, 403, 403, 401, 401, 403, 403, 403, 405, 405], str(codes))
        st, r = user.call("GET", "categories")
        check("GET categories works for a user (empty list)", st == 200 and r == {"categories": []}, str(r))

        # ---- create, validate ----
        st, r = save({"name": "  ادبیات  ", "parent_id": 0})
        L = r.get("category", {}).get("id")
        check("category-save creates a top-level category (trimmed)", st == 200 and r["category"] == {"id": L, "name": "ادبیات", "parent_id": 0, "sort": 0, "parent_name": ""}, str(r))
        H = save({"name": "تاریخ"})[1]["category"]["id"]
        st, r = save({"name": "رمان", "parent_id": L})
        R = r["category"]["id"]
        check("category-save creates a subcategory", st == 200 and r["category"]["parent_id"] == L and r["category"]["parent_name"] == "ادبیات", str(r))
        P = save({"name": "شعر", "parent_id": str(L)})[1]["category"]["id"]
        I = save({"name": "ایران", "parent_id": H})[1]["category"]["id"]
        st, r = save({"name": "رمان", "parent_id": H})
        same_name = r.get("category", {}).get("id")
        check("same name under another parent is allowed", st == 200 and same_name not in (None, R))
        admin.call("POST", "category-delete", {"id": same_name})
        bad = [save({"name": ""}), save({"name": "x" * 81}), save({"name": "ادبیات"}), save({"name": "شعر", "parent_id": L}),
               save({"name": "عمیق", "parent_id": R}), save({"name": "یتیم", "parent_id": 999999}), save({"name": "ادبیات", "id": L, "parent_id": H}),
               save({"name": "خود", "id": H, "parent_id": H}), save({"name": "x", "id": 999999}), save({"name": "x", "parent_id": "abc"})]
        check("category-save errors: missing/too long name 400, duplicate 409, too_deep 400, bad parent 400, has_children 409, unknown id 404",
              [(s_, r_.get("error")) for s_, r_ in bad] == [(400, "missing_name"), (400, "too_long_name"), (409, "duplicate"), (409, "duplicate"),
              (400, "too_deep"), (400, "bad_parent"), (409, "has_children"), (400, "bad_parent"), (404, "not_found"), (400, "bad_parent_id")],
              str([(s_, r_.get("error")) for s_, r_ in bad]))
        check("categories in display order (top level by sort, children after their parent)", order() == [L, R, P, H, I], str(order()))
        st, r = user.call("GET", "categories")
        c0 = r["categories"][0]
        check("GET categories item fields (user: no count_all)", set(c0) == {"id", "name", "parent_id", "sort", "count"}, str(c0))

        # ---- books with several categories ----
        b1, b2, b3, b5 = book(admin, "کتاب یک"), book(admin, "کتاب دو"), book(admin, "کتاب سه"), book(admin, "کتاب پنج")
        nb = book(admin, "خارج از کاتالوگ")
        u1, u2, u3 = book(user, "کتاب کاربر"), book(user, "کتاب کاربر دو"), book(user, "کتاب کاربر سه")
        st, r = admin.call("POST", "catalog-submit", {"book_id": b1, "categories": [P, R]})
        check("admin catalog-submit with categories", st == 201 and r["status"] == "approved", str(r))
        admin.call("POST", "catalog-submit", {"book_id": b2, "categories": [I, R, L, str(R)]})
        admin.call("POST", "catalog-submit", {"book_id": b3, "categories": [I]})
        admin.call("POST", "catalog-submit", {"book_id": b5})
        st, r = user.call("POST", "catalog-submit", {"book_id": u1, "categories": [P]})
        check("user catalog-submit with category ids -> pending", st == 201 and r["status"] == "pending")
        st, r = user.call("POST", "catalog-submit", {"book_id": u2, "categories": [999999]})
        check("user catalog-submit with an unknown category id -> 400", st == 400 and r.get("error") == "unknown_category", str(r))
        st, r = user.call("POST", "catalog-submit", {"book_id": u2, "category": "ناموجود"})
        check("user legacy category that does not exist: ignored, not created", st == 201 and ccat(u2) == [] and legacy(u2) == ""
              and "ناموجود" not in [c["name"] for c in cats().values()])
        st, r = user.call("POST", "catalog-submit", {"book_id": u3, "category": " تاریخ "})
        check("user legacy category matching an existing top-level name links it", st == 201 and ccat(u3) == [H] and legacy(u3) == "تاریخ")
        ref = {L: ("ادبیات", 0, ""), R: ("رمان", L, "ادبیات"), P: ("شعر", L, "ادبیات"), H: ("تاریخ", 0, ""), I: ("ایران", H, "تاریخ")}
        want = lambda ids: [{"id": i, "name": ref[i][0], "parent_id": ref[i][1], "parent_name": ref[i][2]} for i in ids]
        st, r = user.call("GET", "catalog")
        B = {b["id"]: b for b in r["books"]}
        check("catalog (full list): categories in display order with parent names; legacy category = first one's top-level name",
              B[b1]["categories"] == want([R, P]) and B[b2]["categories"] == want([L, R, I]) and B[b3]["categories"] == want([I]) and B[b5]["categories"] == []
              and [B[b]["category"] for b in (b1, b2, b3, b5)] == ["ادبیات", "ادبیات", "تاریخ", ""], json.dumps([B[b] for b in (b1, b2)], ensure_ascii=False)[:500])
        st, r = user.call("GET", "catalog", query=f"&page=1&id={b2}")
        check("catalog deep link (page, id) includes categories", st == 200 and [b["categories"] for b in r["books"]] == [want([L, R, I])], str(r)[:300])
        st, r = admin.call("GET", "catalog-pending")
        check("catalog-pending includes categories", {b["id"]: b["categories"] for b in r["books"]}.get(u1) == want([P]), str(r)[:300])
        st, r = admin.call("GET", "catalog-all")
        check("catalog-all includes categories", all("categories" in b for b in r["books"]) and {b["id"]: cat_ids(b) for b in r["books"]}[b2] == [L, R, I])
        st, r = admin.call("GET", "admin-books", query="&per=100")
        AB = {b["id"]: b for b in r["books"]}
        check("admin-books includes categories (also [] for a book outside the catalog)", AB[b1]["categories"] == want([R, P]) and AB[nb]["categories"] == [] and AB[nb]["category"] == "")

        # ---- counts: approved, distinct, a top level includes its children ----
        C = cats()
        got = {i: (C[i]["count"], C[i]["count_all"]) for i in (L, R, P, H, I)}
        check("GET categories counts (distinct; top level includes children; count_all for admins incl. pending)",
              got == {L: (2, 3), R: (2, 2), P: (1, 2), H: (2, 3), I: (2, 2)}, str(got))
        UC = cats(user)
        check("user counts equal the admin's count, no count_all", {i: UC[i]["count"] for i in UC} == {i: C[i]["count"] for i in C} and "count_all" not in UC[L])

        # ---- filters ----
        def cat_ids_q(query, c=None):
            st, r = (c or user).call("GET", "catalog", query="&page=1&per=60" + query)
            return sorted(b["id"] for b in r["books"]) if st == 200 else (st, r.get("error"))
        def adm_q(query):
            st, r = admin.call("GET", "admin-books", query="&per=100" + query)
            return sorted(b["id"] for b in r["books"]) if st == 200 else (st, r.get("error"))
        got = [cat_ids_q(f"&cat={L}"), cat_ids_q(f"&cat={R}"), cat_ids_q(f"&cat={H}"), cat_ids_q("&cat=none"), cat_ids_q("&cat=999999"), cat_ids_q("&cat=x"),
               cat_ids_q("&category=" + Q("ادبیات")), cat_ids_q("&category=" + Q("شعر")), cat_ids_q(f"&cat={H}&category=" + Q("رمان")), cat_ids_q(f"&cat={L}&q=" + Q("دو"))]
        check("catalog cat=<top id> includes children; cat=<child>; cat=none; unknown id empty; bad cat 400; legacy category= name (column or any linked name)",
              got == [sorted([b1, b2]), sorted([b1, b2]), sorted([b2, b3]), [b5], [], (400, "bad_cat"), sorted([b1, b2]), [b1], [b2], [b2]], str(got))
        st, r = user.call("GET", "catalog", query=f"&page=1&cat={L}")
        check("catalog page with cat keeps its legacy facets", st == 200 and r["total"] == 2 and {c["name"]: c["count"] for c in r["categories"]} == {"ادبیات": 2, "تاریخ": 1}, str(r.get("categories")))
        got = [adm_q(f"&cat={P}"), adm_q(f"&cat={L}&status=pending"), adm_q("&cat=none"), adm_q("&cat=-1"), adm_q("&category=" + Q("ایران"))]
        check("admin-books cat=<id> (any status), cat=none (incl. books outside the catalog), bad cat 400, legacy name",
              got == [sorted([b1, u1]), [u1], sorted([b5, nb, u2]), (400, "bad_cat"), sorted([b2, b3])], str(got))

        # ---- order ----
        st, r = admin.call("POST", "category-order", {"parent_id": 0, "ids": [H, L]})
        check("category-order top level", st == 200 and order() == [H, I, L, R, P], str(order()))
        check("order changes the books' category order and the legacy column", ccat(b2) == [I, L, R] and legacy(b2) == "تاریخ" and legacy(b1) == "ادبیات")
        admin.call("POST", "category-order", {"parent_id": L, "ids": [P, R]})
        check("category-order children", order() == [H, I, L, P, R] and ccat(b1) == [P, R] and cats()[P]["sort"] == 0 and cats()[R]["sort"] == 1)
        bad = [admin.call("POST", "category-order", b) for b in ({"parent_id": 0, "ids": [H, R]}, {"parent_id": L, "ids": [P, P]},
               {"parent_id": 0, "ids": "1,2"}, {"parent_id": 0, "ids": []}, {"parent_id": 0, "ids": [999999]})]
        check("category-order validation -> 400 bad_ids", [(s_, r_.get("error")) for s_, r_ in bad] == [(400, "bad_ids")] * 5, str(bad))

        # ---- rename / move keep the legacy column in sync ----
        st, r = save({"id": L, "name": "ادبیات فارسی"})
        check("rename a top-level category (parent kept when parent_id is absent)", st == 200 and r["category"]["parent_id"] == 0 and r["category"]["name"] == "ادبیات فارسی"
              and legacy(b1) == "ادبیات فارسی", str(r))
        st, r = save({"id": I, "name": "ایران", "parent_id": L})
        check("move a subcategory to another parent: books' legacy column follows", st == 200 and r["category"]["parent_name"] == "ادبیات فارسی"
              and r["category"]["sort"] == 2 and legacy(b3) == "ادبیات فارسی" and order() == [H, L, P, R, I], str(r))
        st, r = save({"id": I, "name": "ایران", "parent_id": 0})
        check("a subcategory can become top level", st == 200 and r["category"]["parent_id"] == 0 and legacy(b3) == "ایران")
        save({"id": I, "name": "ایران", "parent_id": H})
        check("and move back under a parent", cats()[I]["parent_id"] == H and legacy(b3) == "تاریخ")

        # ---- catalog-update: categories replace the set; legacy category string ----
        st, r = admin.call("POST", "catalog-update", {"book_id": b5, "categories": [R, I]})
        check("catalog-update categories replaces the set (display order) and sets the legacy column", st == 200 and ccat(b5) == [I, R] and legacy(b5) == "تاریخ")
        st, r = admin.call("POST", "catalog-update", {"book_id": b5, "description": "نه", "categories": [R, 999999]})
        st2, r2 = admin.call("POST", "catalog-update", {"book_id": b5, "categories": list(range(1, 12))})
        st3, r3 = admin.call("POST", "catalog-update", {"book_id": b5, "categories": ["x"]})
        st4, r4 = admin.call("POST", "catalog-update", {"book_id": b5, "categories": R})
        check("catalog-update categories errors: unknown 400 (nothing written), more than 10 -> 400, bad -> 400",
              (st, r.get("error"), st2, r2.get("error"), st3, r3.get("error"), st4, r4.get("error")) == (400, "unknown_category", 400, "too_many_categories", 400, "bad_categories", 400, "bad_categories")
              and ccat(b5) == [I, R] and catalog_book(b5)["description"] != "نه")
        st, r = admin.call("POST", "catalog-update", {"book_id": b5, "category": " علمی "})
        sci = next((c["id"] for c in cats().values() if c["name"] == "علمی"), None)
        check("catalog-update legacy category: creates a top-level category and replaces the set", st == 200 and sci and ccat(b5) == [sci]
              and legacy(b5) == "علمی" and cats()[sci]["parent_id"] == 0)
        admin.call("POST", "catalog-update", {"book_id": b5, "category": "تاریخ"})
        check("catalog-update legacy category: finds an existing top-level one", ccat(b5) == [H])
        admin.call("POST", "catalog-update", {"book_id": b5, "category": ""})
        check("catalog-update legacy category '' clears", ccat(b5) == [] and legacy(b5) == "")
        admin.call("POST", "catalog-update", {"book_id": b5, "category": "x", "categories": [P]})
        check("catalog-update: categories wins over category", ccat(b5) == [P])
        admin.call("POST", "catalog-update", {"book_id": b5, "description": "فقط توضیح"})
        check("catalog-update without category keys keeps the set", ccat(b5) == [P])
        st, _ = user.call("POST", "catalog-update", {"book_id": b5, "categories": [H]})
        check("catalog-update categories is admin-only", st == 403 and ccat(b5) == [P])

        # ---- admin-bulk: add_category / remove_category / set_category / clear_category ----
        def bulk(body): return admin.call("POST", "admin-bulk", body)
        for b in (b1, b2, b3, b5): admin.call("POST", "catalog-update", {"book_id": b, "categories": []})
        admin.call("POST", "catalog-update", {"book_id": b2, "categories": [I]})
        st, r = bulk({"action": "add_category", "category_id": H, "ids": [b1, b2, b3, b5, nb]})
        check("bulk add_category by ids (book outside the catalog skipped)", r == {"affected": 4, "skipped": 1} and ccat(b2) == [H, I]
              and legacy(b1) == "تاریخ", str(r))
        st, r = bulk({"action": "add_category", "category_id": str(H), "ids": [b1, b2]})
        check("bulk add_category again: all skipped", r == {"affected": 0, "skipped": 2}, str(r))
        st, r = bulk({"action": "remove_category", "category_id": H, "filter": {"cat": str(H), "status": "approved"}})
        check("bulk remove_category by filter (cat + status)", r == {"affected": 4, "skipped": 0} and ccat(b1) == [] and legacy(b1) == ""
              and ccat(b2) == [I] and legacy(b2) == "تاریخ" and ccat(u3) == [H], str(r))
        n_none = len(adm_q("&cat=none&status=approved"))
        st, r = bulk({"action": "add_category", "category_id": R, "filter": {"cat": "none", "status": "approved"}})
        check("bulk add_category by filter cat=none", r == {"affected": n_none, "skipped": 0} and n_none == 3 and adm_q("&cat=none&status=approved") == [], str(r))
        extra = [save({"name": f"دسته {k}"})[1]["category"]["id"] for k in range(10)]
        for k in extra[:9]: bulk({"action": "add_category", "category_id": k, "ids": [b1]})
        st, r = bulk({"action": "add_category", "category_id": extra[9], "ids": [b1]})
        check("bulk add_category respects the 10-categories limit", r == {"affected": 0, "skipped": 1} and len(ccat(b1)) == 10, str(r))
        bad = [bulk(b) for b in ({"action": "add_category", "ids": [b1]}, {"action": "remove_category", "category_id": 999999, "ids": [b1]},
                                 {"action": "add_category", "category_id": "x", "ids": [b1]}, {"action": "add_category", "category_id": H, "filter": {"cat": "x"}})]
        check("bulk category validation -> 400", [(s_, r_.get("error")) for s_, r_ in bad] == [(400, "missing_category_id"), (400, "unknown_category"),
              (400, "bad_category_id"), (400, "bad_cat")], str([(s_, r_.get("error")) for s_, r_ in bad]))
        st, r = admin.call("POST", "admin-bulk", {"action": "remove_category", "category_id": H, "ids": [b1]}, csrf=False)
        st2, _ = user.call("POST", "admin-bulk", {"action": "add_category", "category_id": H, "ids": [u1]})
        check("bulk category actions need admin + CSRF", st == 403 and st2 == 403)
        st, r = bulk({"action": "set_category", "ids": [b1, b2, nb], "category": " تازه "})
        new = next((c["id"] for c in cats().values() if c["name"] == "تازه"), None)
        check("bulk set_category (legacy name): creates the top-level category and replaces the set", r == {"affected": 2, "skipped": 1} and new
              and ccat(b1) == [new] and ccat(b2) == [new] and legacy(b1) == "تازه", str(r))
        st, r = bulk({"action": "set_category", "ids": [b1, b2], "category": "تازه"})
        check("bulk set_category again: skipped", r == {"affected": 0, "skipped": 2}, str(r))
        st, r = bulk({"action": "clear_category", "ids": [b1, b2, b5]})
        check("bulk clear_category unlinks everything", r == {"affected": 3, "skipped": 0} and ccat(b1) == [] and ccat(b5) == [] and legacy(b2) == "", str(r))

        # ---- category-delete ----
        for b, ids in ((b1, [P, R]), (b2, [L, P]), (b3, [I])): admin.call("POST", "catalog-update", {"book_id": b, "categories": ids})
        bad = [admin.call("POST", "category-delete", b) for b in ({"id": L}, {"id": 999999}, {"id": P, "move_to": 999999}, {"id": P, "move_to": P})]
        check("category-delete errors: has_children 409, unknown 404, bad move_to 400",
              [(s_, r_.get("error")) for s_, r_ in bad] == [(409, "has_children"), (404, "not_found"), (400, "unknown_category"), (400, "unknown_category")], str(bad))
        st, r = admin.call("POST", "category-delete", {"id": P, "move_to": R})
        check("category-delete with move_to relinks its books (no duplicates)", st == 200 and r == {"moved": 3, "unlinked": 0}
              and ccat(b1) == [R] and ccat(b2) == [L, R] and ccat(u1) == [R] and P not in cats(), str(r))
        st, r = admin.call("POST", "category-delete", {"id": I})
        check("category-delete without move_to unlinks", st == 200 and r == {"moved": 0, "unlinked": 1} and ccat(b3) == [] and legacy(b3) == "", str(r))
        st, r = admin.call("POST", "category-delete", {"id": R})
        st2, r2 = admin.call("POST", "category-delete", {"id": L})
        check("deleting the last child, then its parent", st == 200 and r["unlinked"] == 3 and st2 == 200 and r2["unlinked"] == 1 and ccat(b2) == [] and legacy(b2) == "", f"{r} {r2}")

        # ---- links follow the catalog row: book-replace moves them, catalog-remove drops them ----
        admin.call("POST", "catalog-update", {"book_id": b2, "categories": [H, new]})
        st, r = upload(admin, make_variant(tmp4, "cat-replace"), "کتاب دو")
        b2n = r["book"]["id"]
        st, r = admin.call("POST", "book-replace", {"old_book_id": b2, "new_book_id": b2n})
        check("book-replace moves the catalog entry's categories", st == 200 and r["catalog_moved"] and ccat(b2n) == [H, new] and legacy(b2n) == "تاریخ", str(r)[:200])
        before = cats()[H]["count"]
        admin.call("POST", "catalog-remove", {"book_id": b2n})
        check("catalog-remove drops the book's links (counts follow)", cats()[H]["count"] == before - 1, f"{before} {cats()[H]['count']}")

        # ---- api-book: paths, auto-create, limits ----
        tok = admin.call("POST", "api-token-create", {"name": "cat"})[1]["token"]
        def api(cat, k):
            return api_book(base, tok, fields={"category": cat} if cat is not None else {}, file=(f"a{k}.epub", tiny_epub(tmp4 / f"a{k}.epub", 7000 + k).read_bytes()))
        snapshot = sorted((c["name"], c["parent_id"]) for c in cats().values())
        st, r = api("علوم/فیزیک; علوم › شیمی\nتاریخ > معاصر ; علوم ;;", 1)
        a1 = (r.get("book") or {}).get("id")
        C = cats(); byname = {(c["name"], c["parent_id"]): c["id"] for c in C.values()}
        sci2 = byname.get(("علوم", 0)); phy = byname.get(("فیزیک", sci2)); chem = byname.get(("شیمی", sci2)); mod = byname.get(("معاصر", H))
        check("api-book: several paths (/ › > separators, ; and newline) create missing categories, parent first",
              st == 201 and sci2 and phy and chem and mod and sorted(ccat(a1)) == sorted([sci2, phy, chem, mod]), f"{st} {str(r)[:200]} {ccat(a1) if a1 else None}")
        check("api-book: existing top-level category reused, legacy column = first category's top-level name",
              list(byname).count(("تاریخ", 0)) == 1 and legacy(a1) == ("تاریخ" if C[H]["sort"] < C[sci2]["sort"] else "علوم") and ccat(a1)[0] in (mod, sci2), str(ccat(a1)))
        snapshot = sorted((c["name"], c["parent_id"]) for c in cats().values())
        bad = [api("الف/ب/ج", 2), api(";".join(f"د{k}" for k in range(11)), 3), api("الف/" + "x" * 81, 4)]
        check("api-book category errors: more than 2 levels 400 too_deep, more than 10 400, segment > 80 400; nothing created",
              [(s_, r_.get("error")) for s_, r_ in bad] == [(400, "too_deep"), (400, "too_many_categories"), (400, "too_long_category")]
              and sorted((c["name"], c["parent_id"]) for c in cats().values()) == snapshot, str(bad))
        st, r = api_book(base, tok, fields={"category": "علوم"}, file=("a1.epub", tiny_epub(tmp4 / "a1.epub", 7001).read_bytes()))
        check("api-book duplicate with category replaces the set", st == 201 and r.get("duplicate") is True and ccat(a1) == [sci2] and legacy(a1) == "علوم", str(r)[:200])
        st, r = api_book(base, tok, fields={"description": "x"}, file=("a1.epub", tiny_epub(tmp4 / "a1.epub", 7001).read_bytes()))
        check("api-book duplicate without category keeps the set", st == 201 and ccat(a1) == [sci2])
        st, r = api("", 5)
        check("api-book without category: no categories", st == 201 and ccat(r["book"]["id"]) == [] and legacy(r["book"]["id"]) == "")
    finally:
        proc.terminate(); proc.wait(5); shutil.rmtree(tmp4, ignore_errors=True)

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
    # Since migration 9 a user's legacy category name only matches an existing top-level category (admins create them).
    admin.call("POST", "category-save", {"name": "رمان", "parent_id": 0})
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
    run_pdfs(base, tmp, admin, user)
    run_migration7()
    run_migration9()
    run_large_catalog()
    run_categories()

    st, r = anon.call("GET", "nope")
    check("unknown route 404", st == 404)
    st, r = anon.call("GET", "login")
    check("wrong method 405", st == 405)

def main():
    gh = os.environ.get("GITHUB_ACTIONS")
    if not shutil.which("php"):
        print("::error title=api test::php not found" if gh else "php not found"); sys.exit(1)
    if gh:  # what the runner's PHP offers (zip/dom/sqlite are needed)
        import subprocess as _sp
        ver = _sp.run(["php", "-r", "echo PHP_VERSION;"], capture_output=True, text=True).stdout
        mods = _sp.run(["php", "-m"], capture_output=True, text=True).stdout.split()
        print(f"::notice title=php::{ver} zip={'zip' in mods} dom={'dom' in mods} pdo_sqlite={'pdo_sqlite' in mods} mbstring={'mbstring' in mods}", flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="borg-api-"))
    proc, base = start_php(tmp)
    try:
        run(base, tmp)
    except Exception as e:  # a crash before/between checks must still be visible in CI annotations
        import traceback
        tb = traceback.format_exc()
        print(tb, flush=True)
        if gh:
            print(f"::error title=api test crashed::{type(e).__name__}: {str(e)[:300]} | {tb.strip().splitlines()[-3][:300] if len(tb.strip().splitlines()) >= 3 else ''}", flush=True)
        results.append(("api test run completed without a crash", False))
    finally:
        proc.terminate(); proc.wait(5); shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
