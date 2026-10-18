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
        "setup_key": SETUP_KEY, "storage_dir": str(tmp / "storage"),
    }).replace("{", "[").replace("}", "]").replace('":', '" =>') + ";\n")
    port = free_port()
    env = dict(os.environ, BORG_CONFIG=str(cfg))
    proc = subprocess.Popen(["php", "-d", "upload_max_filesize=2M", "-d", "post_max_size=8M", "-S", f"127.0.0.1:{port}", "-t", str(docroot)],
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

def upload(c, path, title, author="", cover=None):
    data = Path(path).read_bytes(); sha = hashlib.sha256(data).hexdigest()
    st, r = c.call("POST", "upload-init", {"sha256": sha, "size": len(data), "title": title, "author": author, "cover": cover})
    if st >= 400: return st, r
    if r.get("exists"): return st, r
    uid, chunk, off = r["upload_id"], r["chunk_bytes"], 0
    while off < len(data):
        part = data[off:off + chunk]
        st, rr = c.call("POST", "upload-chunk", raw=part, query=f"&upload_id={uid}&offset={off}")
        if st != 200: return st, rr
        off += len(part)
    return c.call("POST", "upload-complete", {"upload_id": uid})

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

def run(base, tmp):
    PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    anon = Client(base)
    st, h = anon.call("GET", "health")
    check("health ok, configured, needs setup", st == 200 and h["configured"] and h["database"] and h["needs_setup"], json.dumps(h)[:200])
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
    st, r = upload(user, FIX / "sample-fa.epub", "کتاب آزمایشی", "نویسنده تست", cover=PNG)
    book1 = (r.get("book") or {}).get("id")
    check("upload book with cover", st == 201 and book1 and r["book"]["has_cover"], str(r)[:200])
    st, r = upload(admin, FIX / "sample-fa.epub", "dup")
    check("same file by another user is deduplicated (no re-upload)", st == 200 and r.get("exists") and r["book"]["id"] == book1)
    st, r = user.call("GET", "library")
    check("library lists the book", st == 200 and [b["id"] for b in r["books"]] == [book1])

    # Multi-chunk upload
    big = make_big_epub(tmp)
    st, r = upload(user, big, "کتاب بزرگ")
    book2 = (r.get("book") or {}).get("id")
    check("multi-chunk upload of a 3 MB book", st == 201 and book2, str(r)[:200])
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
    st, r = user.call("POST", "catalog-submit", {"book_id": book2, "description": "یک کتاب خوب"})
    check("user submission goes to pending", st == 201 and r["status"] == "pending")
    st, r = other.call("GET", "catalog")
    check("pending book not visible in catalog", st == 200 and r["books"] == [])
    st, r = user.call("POST", "catalog-review", {"book_id": book2, "approve": True})
    check("non-admin cannot approve", st == 403)
    st, r = admin.call("GET", "catalog-pending")
    check("admin sees pending queue", st == 200 and [b["id"] for b in r["books"]] == [book2] and r["books"][0]["submitter"] == "کاربر")
    st, r = admin.call("POST", "catalog-review", {"book_id": book2, "approve": True})
    check("admin approves", st == 200 and r["status"] == "approved")
    st, r = other.call("GET", "catalog")
    check("approved book visible to everyone", [b["id"] for b in r["books"]] == [book2] and not r["books"][0]["in_library"])
    st, _ = other.call("GET", "book-file", query=f"&id={book2}")
    check("catalog book readable by everyone", st == 200)
    st, r = other.call("POST", "catalog-add", {"book_id": book2})
    st, r = other.call("GET", "library")
    check("catalog-add puts it in the library", [b["id"] for b in r["books"]] == [book2])
    st, r = other.call("POST", "catalog-submit", {"book_id": book1})
    check("cannot submit a book you do not have", st == 404)
    st, r = admin.call("POST", "catalog-submit", {"book_id": book1})
    check("admin submission is auto-approved", st == 201 and r["status"] == "approved")
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
    st, _ = admin.call("POST", "catalog-update", {"book_id": book3, "description": "توضیح مدیر", "title": "عنوان مدیر"})
    st2, r = other.call("GET", "catalog")
    b3 = [b for b in r["books"] if b["id"] == book3][0]
    check("admin edits catalog entry", st == 200 and b3["description"] == "توضیح مدیر" and b3["title"] == "عنوان مدیر")
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
