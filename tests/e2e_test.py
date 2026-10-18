#!/usr/bin/env python3
"""End-to-end tests for Borg (Playwright + Chromium).

Serves src/ under a sub-path (default /borg/) on localhost, blocks every
non-localhost request, and checks the real user flows: import, read, navigate,
progress, bookmark, settings, offline, broken input, mobile layout.

Usage:  python3 tests/e2e_test.py [--src src] [--base /borg/]
Exit code is non-zero when any check fails.
"""
import argparse, asyncio, functools, http.server, os, socketserver, sys, threading
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
results = []

def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)

class SubPathHandler(http.server.SimpleHTTPRequestHandler):
    base = "/borg/"
    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean + "/" == self.base: clean = self.base
        if not clean.startswith(self.base):
            return str(Path(self.directory) / "__not_found__")
        return super().translate_path("/" + clean[len(self.base):])
    def log_message(self, *args): pass

def start_server(src, base):
    SubPathHandler.base = base
    handler = functools.partial(SubPathHandler, directory=str(src))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://localhost:{httpd.server_address[1]}{base}"

VISIBLE_TEXT_JS = """()=>{const f=document.querySelector('#viewer iframe');if(!f)return null;
  const d=f.contentDocument;const r=f.getBoundingClientRect();
  const box=document.querySelector('#viewer').getBoundingClientRect();
  if(box.height<300||r.height<300)return null;
  const v=[...d.querySelectorAll('p,h1')].find(p=>{const x=p.getBoundingClientRect().left+r.left;return x>=box.left-2&&x<box.right});
  return v?v.textContent.slice(0,30):null}"""

async def visible_text(pg): return await pg.evaluate(VISIBLE_TEXT_JS)

async def new_ctx(browser, url, external, errors, **kw):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, **kw)
    async def block(route):
        external.append(route.request.url); await route.abort()
    await ctx.route(lambda u: not u.startswith("http://localhost"), block)
    pg = await ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    return ctx, pg

async def run(url):
    sample, corrupt = str(FIX / "sample-fa.epub"), str(FIX / "corrupt.epub")
    async with async_playwright() as p:
        b = await p.chromium.launch()

        # 1. Full flow with no internet access
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await pg.goto(url); await pg.wait_for_timeout(1000)
        check("service worker active", await pg.evaluate("navigator.serviceWorker.ready.then(r=>!!r.active)"))
        await pg.reload(); await pg.wait_for_timeout(800)
        keys = await pg.evaluate("caches.keys().then(ks=>caches.open(ks[0]).then(c=>c.keys()).then(rs=>rs.map(r=>new URL(r.url).pathname)))")
        need = ["offline.html", "assets/vendor/jszip.min.js", "assets/vendor/epub.min.js", "assets/fonts/Vazirmatn-Variable.woff2", "assets/css/reader-fonts.css"]
        check("app shell cached (vendor, font, offline page)", all(any(k.endswith(n) for k in keys) for n in need), f"{len(keys)} entries")
        check("UI font Vazirmatn loaded", await pg.evaluate("document.fonts.ready.then(()=>document.fonts.check('16px Vazirmatn'))"))
        await pg.set_input_files("#epubInput", sample); await pg.wait_for_timeout(2000)
        check("import shows success", "اضافه شد" in await pg.inner_text("#toast"))
        check("metadata title/author", await pg.locator(".book-card h3").all_inner_texts() == ["کتاب آزمایشی"]
              and await pg.locator(".book-card p").all_inner_texts() == ["نویسنده تست"])
        await pg.click(".book-card"); await pg.wait_for_timeout(2500)
        check("book text rendered and visible", (await visible_text(pg) or "").startswith("فصل اول"))
        check("table of contents", await pg.locator(".toc-link").all_inner_texts() == ["فصل اول", "فصل دوم"])
        ff = await pg.evaluate("""()=>{const d=document.querySelector('#viewer iframe').contentDocument;
           return d.fonts.ready.then(()=>({fam:getComputedStyle(d.querySelector('p')).fontFamily, ok:d.fonts.check('16px Vazirmatn')}))}""")
        check("Vazirmatn applied inside book iframe", ff["ok"] and "Vazirmatn" in ff["fam"], ff["fam"])
        check("prev disabled at start", await pg.is_disabled("#prevPage"))
        for _ in range(3):
            await pg.click("#nextPage"); await pg.wait_for_timeout(500)
        check("next advances pages", (await visible_text(pg) or "").startswith("این پاراگراف شماره 30"), await visible_text(pg))
        check("prev enabled after paging", not await pg.is_disabled("#prevPage"))
        await pg.click("#prevPage"); await pg.wait_for_timeout(500)
        check("prev goes back", (await visible_text(pg) or "").startswith("این پاراگراف شماره 20"), await visible_text(pg))
        await pg.wait_for_timeout(1500)
        prog = await pg.inner_text("#progressText")
        check("progress above 0%", prog not in ("۰٪", "0٪"), prog)
        await pg.click("#bookmarkButton"); await pg.wait_for_timeout(300)
        check("bookmark saved", "ذخیره" in await pg.inner_text("#bookmarkButton"))
        await pg.click("#fontUp"); await pg.click("#fontUp"); await pg.wait_for_timeout(300)
        await pg.click(".toc-link >> nth=1"); await pg.wait_for_timeout(800)
        check("toc navigation to chapter 2", await pg.inner_text("#chapterLabel") == "بخش ۲")
        await pg.click("#closeReader"); await pg.wait_for_timeout(400)
        await pg.reload(); await pg.wait_for_timeout(1000)
        await pg.click(".book-card"); await pg.wait_for_timeout(2500)
        check("reopen restores position", (await visible_text(pg) or "").startswith("فصل دوم"), await visible_text(pg))
        fs = await pg.evaluate("JSON.parse(localStorage.getItem('ketabkhane-reader-preferences')||'{}').fontSize")
        check("font size persisted", fs == 120, str(fs))
        await pg.click("#closeReader")

        # 2. Offline after first visit
        await ctx.set_offline(True)
        await pg.reload(); await pg.wait_for_timeout(1200)
        check("offline: page loads from cache", await pg.locator(".book-card").count() == 1)
        check("offline: libraries available", await pg.evaluate("typeof JSZip==='function' && typeof ePub==='function'"))
        await pg.click(".book-card"); await pg.wait_for_timeout(2500)
        check("offline: book opens", bool(await visible_text(pg)))
        await pg.click("#closeReader")
        await pg.set_input_files("#epubInput", sample); await pg.wait_for_timeout(2000)
        check("offline: import works", await pg.locator(".book-card").count() == 2)
        await ctx.set_offline(False)
        check("no requests outside the site", len(ext) == 0, str(ext[:3]))
        check("no JS errors in console", len(errs) == 0, str(errs[:3]))
        await ctx.close()

        # 3. Corrupt EPUB
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await pg.goto(url); await pg.wait_for_timeout(800)
        await pg.set_input_files("#epubInput", corrupt); await pg.wait_for_timeout(2000)
        t = await pg.inner_text("#toast")
        check("corrupt EPUB: clear message, nothing saved", "خراب" in t and await pg.locator(".book-card").count() == 0, t)
        check("file input reset after rejection", await pg.input_value("#epubInput") == "")
        await ctx.close()

        # 4. JSZip missing
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await ctx.route("**/jszip.min.js", lambda r: r.fulfill(status=404, body=""))
        await pg.goto(url); await pg.wait_for_timeout(800)
        await pg.set_input_files("#epubInput", sample); await pg.wait_for_timeout(1500)
        t = await pg.inner_text("#toast")
        check("JSZip missing: reload message", "تازه‌سازی" in t, t)
        await ctx.close()

        # 5. Mobile width
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs, is_mobile=True, has_touch=True)
        await pg.set_viewport_size({"width": 390, "height": 844})
        await pg.goto(url); await pg.wait_for_timeout(600)
        await pg.set_input_files("#epubInput", sample); await pg.wait_for_timeout(1800)
        await pg.click(".book-card"); await pg.wait_for_timeout(2500)
        check("mobile: book visible", bool(await visible_text(pg)))
        await ctx.close(); await b.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "src"))
    ap.add_argument("--base", default="/borg/")
    a = ap.parse_args()
    httpd, url = start_server(Path(a.src).resolve(), a.base)
    print(f"Serving {a.src} at {url}")
    try:
        asyncio.run(run(url))
    finally:
        httpd.shutdown()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
