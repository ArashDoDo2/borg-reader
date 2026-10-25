#!/usr/bin/env python3
"""End-to-end tests for Borg (Playwright + Chromium).

Serves src/ under a sub-path (default /borg/) on localhost, blocks every
non-localhost request, and checks the real user flows: import, read, navigate,
progress, bookmark, settings, offline, broken input, mobile layout.

Usage:  python3 tests/e2e_test.py [--src src] [--base /borg/]
Exit code is non-zero when any check fails.
"""
import argparse, asyncio, os, sys
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
results = []

def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)
    if not ok and os.environ.get("GITHUB_ACTIONS"):
        # Surfaces as an annotation on the run, readable without downloading logs.
        print(f"::error title=e2e failed::{name} [{str(info)[:300]}]", flush=True)

SETUP_KEY = "e2e-setup-key-0123456789abcdef"

class PhpServer:
    """php -S with src/ mounted at the base path and a throwaway SQLite config (real API, like production)."""
    def __init__(self, src, base):
        import json, shutil, socket, subprocess, tempfile, time
        self.tmp = Path(tempfile.mkdtemp(prefix="borg-e2e-"))
        mount = self.tmp / "www" / base.strip("/")
        mount.parent.mkdir(parents=True)
        mount.symlink_to(Path(src).resolve(), target_is_directory=True)
        cfg = self.tmp / "config.php"
        cfg.write_text("<?php return ['db_dsn' => " + json.dumps("sqlite:" + str(self.tmp / "db.sqlite")) +
                       ", 'setup_key' => " + json.dumps(SETUP_KEY) + ", 'storage_dir' => " + json.dumps(str(self.tmp / "storage")) +
                       ", 'client_errors_ip_per_hour' => 10000];\n")  # every test browser is anonymous from 127.0.0.1
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        env = dict(os.environ, BORG_CONFIG=str(cfg), PHP_CLI_SERVER_WORKERS="6")
        self.proc = subprocess.Popen(["php", "-S", f"127.0.0.1:{self.port}", "-t", str(self.tmp / "www")],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try: socket.create_connection(("127.0.0.1", self.port), 0.2).close(); break
            except OSError: time.sleep(0.1)
        self._shutil = shutil
    def shutdown(self):
        self.proc.terminate(); self.proc.wait(5); self._shutil.rmtree(self.tmp, ignore_errors=True)

def start_server(src, base):
    srv = PhpServer(src, base)
    return srv, f"http://localhost:{srv.port}{base}"

VISIBLE_TEXT_JS = """()=>{const f=document.querySelector('#viewer iframe');if(!f)return null;
  const d=f.contentDocument;const r=f.getBoundingClientRect();
  const box=document.querySelector('#viewer').getBoundingClientRect();
  if(box.height<300||r.height<300)return null;
  const v=[...d.querySelectorAll('p,h1')].find(p=>{const x=p.getBoundingClientRect().left+r.left;return x>=box.left-2&&x<box.right});
  return v?v.textContent.slice(0,30):null}"""

async def visible_text(pg): return await pg.evaluate(VISIBLE_TEXT_JS)

async def poll(fn, timeout=10000, step=200):
    """Wait until async fn() is truthy. Used instead of wait_for_function, which needs eval (blocked by our CSP)."""
    for _ in range(max(1, timeout // step)):
        try:
            if await fn(): return True
        except Exception:
            pass
        await asyncio.sleep(step / 1000)
    raise TimeoutError("condition not met")

async def open_settings(pg):
    """Open the reader settings panel if it is not already open (it stays open across a book reopen)."""
    if not await pg.is_visible("#settingsPanel"):
        await pg.click("#settingsToggle"); await pg.wait_for_timeout(200)

async def _contains(pg, sel, text): return text in (await pg.inner_text(sel))
async def _count_gt(pg, sel, n): return await pg.locator(sel).count() > n

async def page_pos(pg):
    """Position of the first visible block: -1 for a chapter heading, else the paragraph number."""
    import re
    t = await visible_text(pg) or ""
    m = re.search(r"شماره (\d+)", t)
    return int(m.group(1)) if m else (-1 if t.startswith("فصل") else None)

async def open_sample(pg, url, sample):
    await pg.goto(url); await pg.wait_for_timeout(800)
    await pg.set_input_files("#epubInput", sample); await pg.wait_for_timeout(1800)
    await pg.click(".book-card"); await pg.wait_for_timeout(2500)

def make_epub(path, title, ident, author, publisher="", date="", direction="rtl", lang="fa", paras=40):
    """A small two-chapter Persian EPUB like the fixtures, with optional publisher and date metadata."""
    import zipfile
    extra = (f"<dc:publisher>{publisher}</dc:publisher>" if publisher else "") + (f"<dc:date>{date}</dc:date>" if date else "")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        D = zipfile.ZIP_DEFLATED
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', D)
        z.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">{ident}</dc:identifier><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:language>{lang}</dc:language>{extra}<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/></manifest><spine{f' page-progression-direction="{direction}"' if direction else ''}><itemref idref="c1"/><itemref idref="c2"/></spine></package>', D)
        z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol><li><a href="c1.xhtml">فصل اول</a></li><li><a href="c2.xhtml">فصل دوم</a></li></ol></nav></body></html>', D)
        for i, n in ((1, "اول"), (2, "دوم")):
            p = "".join(f"<p>این پاراگراف شماره {k} از فصل {n} در کتاب {title} است.</p>" for k in range(paras))
            z.writestr(f"OEBPS/c{i}.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل {n}</title></head><body><h1>فصل {n}</h1>{p}</body></html>', D)
    return str(path)

IDB_JS = """([store]) => new Promise(res => { const r = indexedDB.open('ketabkhane-db'); r.onsuccess = () => {
    const q = r.result.transaction(store).objectStore(store).getAll();
    q.onsuccess = () => { res(q.result.map(x => { const o = { ...x }; delete o.file; delete o.fileBuffer; delete o.cover; delete o.locations; return o; })); r.result.close(); }; }; })"""

async def swipe(cdp, x0, x1, y):
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y}]})
    for i in range(1, 6):
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x0 + (x1 - x0) * i / 5, "y": y}]})
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

# v8.2: the reader shows a first-run gesture guide over the page; contexts mark it as seen unless show_guide=True.
GUIDE_SEEN_JS = "try{localStorage.setItem('borg-guide-seen','1');localStorage.setItem('borg-guide-seen-test','1')}catch(e){}"

async def categories_fallback(ctx):
    """v8.10: a server built before the `categories` route answers 404 (logged by Chromium as a failed request); answer
    it the way a server without categories does, so the older sections stay independent of the server version."""
    async def handle(route):
        resp = await route.fetch()
        if resp.status == 404: await route.fulfill(status=200, content_type="application/json", body='{"categories": []}')
        else: await route.fulfill(response=resp)
    await ctx.route(lambda u: "/api/?r=categories" in u, handle)

async def new_ctx(browser, url, external, errors, show_guide=False, **kw):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, **kw)
    await categories_fallback(ctx)
    if not show_guide:
        await ctx.add_init_script(GUIDE_SEEN_JS)
    async def block(route):
        external.append(route.request.url); await route.abort()
    await ctx.route(lambda u: not u.startswith("http://localhost"), block)
    pg = await ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    return ctx, pg

async def run(url):
    suffix = "-test" if "/borg-test/" in url else ""
    sample, corrupt, sample2 = str(FIX / "sample-fa.epub"), str(FIX / "corrupt.epub"), str(FIX / "sample-fa-2.epub")
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
        check("metadata title/author", await pg.locator("#bookGrid .book-card h3").all_inner_texts() == ["کتاب آزمایشی"]
              and await pg.locator("#bookGrid .book-card p").all_inner_texts() == ["نویسنده تست"])
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
        await pg.click("#settingsToggle"); await pg.click("#fontUp"); await pg.click("#fontUp"); await pg.wait_for_timeout(300)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(200)
        await pg.click(".toc-link >> nth=1"); await pg.wait_for_timeout(800)
        check("toc navigation to chapter 2", await pg.inner_text("#chapterLabel") == "فصل دوم")
        await pg.click("#closeReader"); await pg.wait_for_timeout(400)
        await pg.reload(); await pg.wait_for_timeout(1000)
        await pg.click(".book-card"); await pg.wait_for_timeout(2500)
        check("reopen restores position", (await visible_text(pg) or "").startswith("فصل دوم"), await visible_text(pg))
        fs = await pg.evaluate(f"JSON.parse(localStorage.getItem('ketabkhane-reader-preferences{suffix}')||'{{}}').fontSize")
        check("font size persisted", fs == 120, str(fs))
        dbs = await pg.evaluate("indexedDB.databases().then(d=>d.map(x=>x.name))")
        check("uses the right database for this path", dbs == ["ketabkhane-db" + suffix], str(dbs))
        await pg.click("#closeReader")

        # 2. Offline after first visit
        await ctx.set_offline(True)
        await pg.reload(); await pg.wait_for_timeout(1200)
        check("offline: page loads from cache", await pg.locator("#bookGrid .book-card").count() == 1)
        check("offline: libraries available", await pg.evaluate("typeof JSZip==='function' && typeof ePub==='function'"))
        await pg.click(".book-card"); await pg.wait_for_timeout(2500)
        check("offline: book opens", bool(await visible_text(pg)))
        await pg.click("#closeReader")
        await pg.set_input_files("#epubInput", sample2); await pg.wait_for_timeout(2000)
        check("offline: import works", await pg.locator("#bookGrid .book-card").count() == 2)
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
        check("corrupt EPUB: clear message, nothing saved", "خراب" in t and await pg.locator("#bookGrid .book-card").count() == 0, t)
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
        # v8.13.28: on a phone with books, the library comes first (promo cards below the grid, shortened) and
        # «ادامهٔ مطالعه» is a compact row, so the first book is on the first screen.
        await pg.keyboard.press("ArrowLeft"); await pg.wait_for_timeout(1200)   # RTL book: next page
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(900)       # closes the reader
        check("mobile: Escape closes the reader", await pg.is_visible("#bookGrid"))
        row = await pg.locator("#continueShelf .book-card").first.bounding_box()
        grid = await pg.locator("#bookGrid").bounding_box()
        teaser = await pg.locator("#catalogTeaser").bounding_box()
        check("mobile: continue-reading card is a compact row", row is not None and row["height"] < 130 and row["width"] > 300, str(row))
        check("mobile: promo cards sit below the book grid when the library has books",
              teaser is not None and grid is not None and teaser["y"] > grid["y"] + grid["height"] - 1, f"teaser={teaser} grid={grid}")
        check("mobile: first book starts on the first screen", grid is not None and grid["y"] < 844, str(grid))
        check("mobile: promo description hidden in the compact strip", not await pg.is_visible("#catalogTeaser .teaser-desc"))
        await ctx.close()

        # 6. Desktop navigation: keyboard, edge taps, immersive mode
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await open_sample(pg, url, sample)
        check("desktop: TOC toggle hidden", not await pg.is_visible("#tocToggle"))
        await pg.click("#readerTitle")
        p0 = await page_pos(pg)
        await pg.keyboard.press("ArrowLeft"); await pg.wait_for_timeout(500)
        p1 = await page_pos(pg)
        check("key ArrowLeft = next page (RTL)", p1 is not None and p0 is not None and p1 > p0, f"{p0}->{p1}")
        await pg.keyboard.press("ArrowRight"); await pg.wait_for_timeout(500)
        check("key ArrowRight = previous page (RTL)", await page_pos(pg) == p0)
        fr = pg.frame_locator("#viewer iframe")
        await fr.locator("body").press("ArrowLeft"); await pg.wait_for_timeout(500)
        p2 = await page_pos(pg)
        check("keys work with focus inside the book", p2 is not None and p2 > p0, f"{p0}->{p2}")
        await pg.keyboard.press("PageUp"); await pg.wait_for_timeout(500)
        check("PageUp = previous page", await page_pos(pg) == p0)
        box = await pg.locator("#viewer").bounding_box()
        cy = box["y"] + box["height"] / 2
        await pg.mouse.click(box["x"] + box["width"] * 0.1, cy); await pg.wait_for_timeout(500)
        p3 = await page_pos(pg)
        check("tap left edge = next page (RTL)", p3 is not None and p3 > p0, f"{p0}->{p3}")
        await pg.mouse.click(box["x"] + box["width"] * 0.9, cy); await pg.wait_for_timeout(500)
        check("tap right edge = previous page (RTL)", await page_pos(pg) == p0)
        h0 = box["height"]
        await pg.mouse.click(box["x"] + box["width"] * 0.5, cy); await pg.wait_for_timeout(800)
        h1 = (await pg.locator("#viewer").bounding_box())["height"]
        check("tap middle = immersive (bars hidden, viewer taller)",
              not await pg.is_visible(".reader-toolbar") and not await pg.is_visible(".page-nav") and h1 > h0 + 100, f"{h0:.0f}->{h1:.0f}")
        check("immersive: text still visible", bool(await visible_text(pg)))
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(600)
        check("Escape leaves immersive", await pg.is_visible(".reader-toolbar"))
        await pg.mouse.click(box["x"] + box["width"] * 0.5, cy); await pg.wait_for_timeout(600)
        box2 = await pg.locator("#viewer").bounding_box()
        await pg.mouse.click(box2["x"] + box2["width"] * 0.5, box2["y"] + box2["height"] / 2); await pg.wait_for_timeout(600)
        check("tap middle again restores bars", await pg.is_visible(".reader-toolbar"))
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(500)
        check("Escape closes the reader", not await pg.is_visible("#reader"))
        check("desktop navigation: no JS errors", len(errs) == 0, str(errs[:2]))
        await ctx.close()

        # 7. Mobile navigation: TOC drawer and swipe
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs, is_mobile=True, has_touch=True)
        await pg.set_viewport_size({"width": 390, "height": 844})
        await open_sample(pg, url, sample)
        cdp = await ctx.new_cdp_session(pg)
        panel = pg.locator("#tocPanel")
        check("mobile: TOC toggle visible", await pg.is_visible("#tocToggle"))
        check("mobile: drawer starts off-screen", (await panel.bounding_box())["x"] >= 389)
        await pg.click("#tocToggle"); await pg.wait_for_timeout(400)
        pb = await panel.bounding_box()
        check("mobile: drawer opens inside the screen", pb["x"] >= 0 and pb["x"] + pb["width"] <= 391, str(pb))
        check("mobile: aria-expanded true", await pg.get_attribute("#tocToggle", "aria-expanded") == "true")
        await pg.click(".toc-link >> nth=1"); await pg.wait_for_timeout(900)
        check("mobile: TOC link opens chapter and closes drawer",
              await pg.inner_text("#chapterLabel") == "فصل دوم" and (await panel.bounding_box())["x"] >= 389)
        await pg.click("#tocToggle"); await pg.wait_for_timeout(400)
        await pg.mouse.click(20, 420); await pg.wait_for_timeout(400)
        check("mobile: backdrop closes drawer", (await panel.bounding_box())["x"] >= 389)
        await pg.click("#tocToggle"); await pg.wait_for_timeout(400)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(400)
        check("mobile: Escape closes drawer", (await panel.bounding_box())["x"] >= 389 and await pg.is_visible("#reader"))
        vb = await pg.locator("#viewer").bounding_box()
        y = vb["y"] + vb["height"] / 2
        q0 = await page_pos(pg)
        await swipe(cdp, vb["x"] + 80, vb["x"] + 300, y); await pg.wait_for_timeout(700)
        q1 = await page_pos(pg)
        check("swipe right = next page (RTL)", q1 is not None and q0 is not None and q1 > q0, f"{q0}->{q1}")
        check("swipe does not toggle immersive", await pg.is_visible(".reader-toolbar"))
        await swipe(cdp, vb["x"] + 300, vb["x"] + 80, y); await pg.wait_for_timeout(700)
        check("swipe left = previous page (RTL)", await page_pos(pg) == q0)
        await swipe(cdp, vb["x"] + 190, vb["x"] + 200, y); await pg.wait_for_timeout(500)
        check("tiny movement is not a swipe", await page_pos(pg) == q0)
        # v8.13.25 regression: Chrome on iOS delivers the click it synthesises after a touch *late* (queued behind the
        # chapter re-layout that entering immersive mode starts), past any time-based guard. That click must not undo
        # the immersive toggle. Reproduced here: a touch tap in the middle, then the "synthetic" click 900 ms later.
        mx, my = vb["x"] + vb["width"] / 2, y
        # The 10 px "tiny movement" above counts as a middle tap and may have entered immersive mode; start from a known state
        # and outside its 650 ms duplicate-tap window.
        await pg.wait_for_timeout(800)
        if not await pg.is_visible(".reader-toolbar"):
            await pg.keyboard.press("Escape"); await pg.wait_for_timeout(600)
        check("mobile: bars visible before the immersive tap", await pg.is_visible(".reader-toolbar"))
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": mx, "y": my}]})
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        await pg.wait_for_timeout(900)
        imm_before = not await pg.is_visible(".reader-toolbar")
        LATE_CLICK_JS = """([x, y]) => { const f = document.querySelector('#viewer iframe'); if (!f) return 'no-frame';
            const w = f.contentWindow, r = f.getBoundingClientRect(), el = w.document.elementFromPoint(x - r.left, y - r.top) || w.document.body;
            el.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true, view: w, clientX: x - r.left, clientY: y - r.top }));
            return el.tagName; }"""
        late = await pg.evaluate(LATE_CLICK_JS, [mx, my]); await pg.wait_for_timeout(500)
        check("mobile: touch tap in the middle enters immersive mode", imm_before)
        check("mobile: a late synthetic click after the touch does not leave immersive mode (iOS Chrome)",
              imm_before and not await pg.is_visible(".reader-toolbar"), f"late click target={late}")
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": mx, "y": my}]})
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        await pg.wait_for_timeout(900)
        check("mobile: a second touch tap leaves immersive mode", await pg.is_visible(".reader-toolbar"))
        await pg.mouse.click(mx, my); await pg.wait_for_timeout(600)
        check("mobile: a real mouse click after touch input still toggles immersive mode", not await pg.is_visible(".reader-toolbar"))
        await pg.mouse.click(mx, my); await pg.wait_for_timeout(600)
        check("mobile navigation: no JS errors", len(errs) == 0, str(errs[:2]))
        await ctx.close()

        # 8. Settings panel, reader themes, library shelf, sorting, duplicates, storage info
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await open_sample(pg, url, sample)
        check("settings panel hidden by default", not await pg.is_visible("#settingsPanel"))
        await pg.click("#settingsToggle"); await pg.wait_for_timeout(200)
        check("settings panel opens", await pg.is_visible("#settingsPanel") and await pg.get_attribute("#settingsToggle", "aria-expanded") == "true")
        await pg.click('[data-theme="sepia"]'); await pg.wait_for_timeout(500)
        bg = await pg.evaluate("getComputedStyle(document.querySelector('#viewer iframe').contentDocument.body).backgroundColor")
        check("sepia theme applied to the book", bg == "rgb(244, 236, 216)", bg)
        check("sepia button pressed", await pg.get_attribute('[data-theme="sepia"]', "aria-pressed") == "true")
        await pg.click('[data-align="right"]'); await pg.wait_for_timeout(400)
        ta = await pg.evaluate("getComputedStyle(document.querySelector('#viewer iframe').contentDocument.querySelector('p')).textAlign")
        check("text alignment applied", ta in ("right", "start"), ta)
        await pg.click("#fontUp"); await pg.wait_for_timeout(200)
        check("font size label updates", "۱۱۰" in await pg.inner_text("#fontSizeLabel"), await pg.inner_text("#fontSizeLabel"))
        await pg.click("#readerTitle"); await pg.wait_for_timeout(200)
        check("click outside closes settings", not await pg.is_visible("#settingsPanel"))
        await pg.click("#settingsToggle"); await pg.wait_for_timeout(200)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(200)
        check("Escape closes settings, reader stays", not await pg.is_visible("#settingsPanel") and await pg.is_visible("#reader"))
        await pg.click("#settingsToggle"); await pg.wait_for_timeout(200)
        box = await pg.locator("#viewer").bounding_box()
        p_before = await page_pos(pg)
        # Tap the book where the (left-anchored) panel does not cover it: middle-right, which would normally toggle immersive mode.
        await pg.mouse.click(box["x"] + box["width"] * 0.62, box["y"] + box["height"] / 2); await pg.wait_for_timeout(500)
        check("tap in book while settings open only closes the panel", not await pg.is_visible("#settingsPanel") and await page_pos(pg) == p_before and await pg.is_visible(".reader-toolbar"))
        await pg.click("#nextPage"); await pg.wait_for_timeout(1500)
        await pg.click("#closeReader"); await pg.wait_for_timeout(800)
        check("continue-reading shelf shows the book", await pg.locator("#continueShelf .book-card").count() == 1)
        # v8.13.29 desktop: the unfinished book is a wide «در حال خواندن» card with chapter, progress and a button; the
        # promo cards sit below the grid side by side; the stats card is one row; the lone tab bar is hidden.
        nr = await pg.locator("#continueShelf .now-reading").bounding_box()
        check("desktop: now-reading card is wide", nr is not None and nr["width"] > 600 and 150 < nr["height"] < 320, str(nr))
        meta = await pg.inner_text("#continueShelf .now-reading-meta")
        check("desktop: now-reading card shows chapter, percent and last-read time", "فصل" in meta and "٪" in meta and "پیش" in meta or "همین حالا" in meta, meta)
        check("desktop: now-reading button visible", await pg.is_visible("#continueShelf .now-reading-btn"))
        t1 = await pg.locator("#catalogTeaser").bounding_box(); t2 = await pg.locator("#aiTeaser").bounding_box(); g = await pg.locator("#bookGrid").bounding_box()
        check("desktop: promo cards below the grid, side by side", t1 and t2 and g and t1["y"] > g["y"] + g["height"] - 1 and abs(t1["y"] - t2["y"]) < 2 and t1["width"] < 800, f"{t1} {t2}")
        sc = await pg.locator("#statsCard").bounding_box()
        check("desktop: stats card is a single row", sc is not None and sc["height"] < 90, str(sc))
        check("desktop: tab bar hidden when only one tab", not await pg.is_visible(".view-tabs"))
        await pg.click("#continueShelf .now-reading-btn"); await pg.wait_for_timeout(2500)
        check("desktop: now-reading card opens the book", await pg.is_visible("#reader") and bool(await visible_text(pg)))
        await pg.click("#closeReader"); await pg.wait_for_timeout(800)
        badge = await pg.inner_text("#bookGrid .book-badge")
        check("progress shown on the card", "٪" in badge or "شروع" in badge, badge)
        await pg.reload(); await pg.wait_for_timeout(1000)
        await pg.click("#bookGrid .book-card"); await pg.wait_for_timeout(2500)
        bg = await pg.evaluate("getComputedStyle(document.querySelector('#viewer iframe').contentDocument.body).backgroundColor")
        check("theme persists after reload", bg == "rgb(244, 236, 216)", bg)
        await pg.click("#closeReader"); await pg.wait_for_timeout(500)
        await pg.set_input_files("#epubInput", sample); await pg.wait_for_timeout(2000)
        t = await pg.inner_text("#toast")
        check("duplicate import refused", "قبلاً" in t and await pg.locator("#bookGrid .book-card").count() == 1, t)
        await pg.set_input_files("#epubInput", sample2); await pg.wait_for_timeout(2000)
        check("different book imports", await pg.locator("#bookGrid .book-card").count() == 2)
        await pg.select_option("#librarySort", "title"); await pg.wait_for_timeout(400)
        titles = await pg.locator("#bookGrid .book-card h3").all_inner_texts()
        check("sort by title", titles == ["الف کتاب دوم", "کتاب آزمایشی"], str(titles))
        await pg.select_option("#librarySort", "read"); await pg.wait_for_timeout(400)
        titles = await pg.locator("#bookGrid .book-card h3").all_inner_texts()
        check("sort by last read", titles[0] == "کتاب آزمایشی", str(titles))
        await pg.select_option("#librarySort", "added"); await pg.wait_for_timeout(400)
        titles = await pg.locator("#bookGrid .book-card h3").all_inner_texts()
        check("sort by newest added", titles[0] == "الف کتاب دوم", str(titles))
        # 8.13.30 regression: with a second unfinished book the wide card keeps its row (8.13.29 squeezed it beside
        # fixed-width small cards); the other unfinished books scroll in a shelf below it. (Both books exist here; the
        # second one is opened last on purpose, after the sort checks above.)
        await pg.click("#bookGrid .book-card >> nth=0"); await pg.wait_for_timeout(2500)
        await pg.keyboard.press("ArrowLeft"); await pg.wait_for_timeout(900)
        await pg.click("#closeReader"); await pg.wait_for_timeout(900)
        nr2 = await pg.locator("#continueShelf .now-reading").bounding_box()
        small = await pg.locator("#continueShelf .continue-rest .book-card").first.bounding_box()
        check("desktop: with two unfinished books the wide card keeps its full row", nr2 is not None and nr2["width"] > 600 and 150 < nr2["height"] < 320, str(nr2))
        check("desktop: the other unfinished book sits in the shelf below the wide card", small is not None and nr2 is not None and small["y"] >= nr2["y"] + nr2["height"] - 1 and small["width"] < 200, f"{small}")
        check("storage usage shown", "مگابایت" in await pg.inner_text("#storageInfo"), await pg.inner_text("#storageInfo"))
        check("settings/library: no JS errors", len(errs) == 0, str(errs[:2]))
        await ctx.close()

        # 9. Mobile toolbar is compact and the settings sheet fits the screen
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs, is_mobile=True, has_touch=True)
        await pg.set_viewport_size({"width": 390, "height": 844})
        await open_sample(pg, url, sample)
        th = (await pg.locator(".reader-toolbar").bounding_box())["height"]
        check("mobile: toolbar is one compact row", th <= 72, f"{th:.0f}px")
        await pg.click("#settingsToggle"); await pg.wait_for_timeout(300)
        sb = await pg.locator("#settingsPanel").bounding_box()
        check("mobile: settings sheet inside the screen", sb["x"] >= 0 and sb["x"] + sb["width"] <= 391 and sb["y"] + sb["height"] <= 845, str(sb))
        check("mobile settings: no JS errors", len(errs) == 0, str(errs[:2]))
        await ctx.close()

        # 10. Accounts and cloud: setup admin, upload, invite, catalog, second device sync
        async def wait_synced(pg, timeout=20000):
            try:
                await poll(lambda: _contains(pg, "#syncStatus", "همگام شد"), timeout)
                return True
            except Exception:
                return False
        async def auth(pg, mode, **fields):
            await pg.click("#accountButton"); await pg.wait_for_timeout(600)
            await pg.click(f'[data-auth="{mode}"]')
            for k, v in fields.items(): await pg.fill(f'#authForm [name="{k}"]', v)
            await pg.click("#authSubmit")
        ext, errsA = [], []
        ctxA, A = await new_ctx(b, url, ext, errsA)
        await A.goto(url); await A.wait_for_timeout(800)
        await A.set_input_files("#epubInput", sample); await A.wait_for_timeout(2000)
        await A.click("#accountButton"); await A.wait_for_timeout(800)
        check("first visit offers initial setup", await A.get_attribute('[data-auth="setup"]', "aria-pressed") == "true")
        await A.click("#accountClose")
        await auth(A, "setup", setup_key=SETUP_KEY, name="مدیر بُرگ", email="admin@example.com", password="password1")
        check("setup logs in as admin", await wait_synced(A) and await A.inner_text("#accountButton") == "مدیر بُرگ")
        check("admin sees catalog and admin tabs", await A.is_visible('[data-view="catalog"]') and await A.is_visible('[data-view="admin"]'))
        await A.click('[data-view="admin"]'); await A.wait_for_timeout(800)
        await A.click('[data-admin-tab="invites"]'); await A.wait_for_timeout(300)  # v8.9: admin sub-tabs
        await A.fill("#inviteNote", "دوست"); await A.click("#createInvite"); await A.wait_for_timeout(800)
        code = (await A.inner_text("#inviteResult")).split(":")[-1].strip()
        check("admin creates an invite code", len(code) == 10, code)
        await A.click('[data-view="mine"]'); await A.wait_for_timeout(300)
        await A.click("#bookGrid .share-book"); await A.wait_for_timeout(1500)
        check("admin shares the book to the public catalog", "عمومی" in await A.inner_text("#bookGrid .book-badge"))
        await A.click("#bookGrid .book-card"); await A.wait_for_timeout(2500)
        for _ in range(3):
            await A.click("#nextPage"); await A.wait_for_timeout(500)
        posA = await page_pos(A)
        await A.wait_for_timeout(5500)  # progress push is debounced (4 s)
        await A.click("#closeReader"); await A.wait_for_timeout(500)

        # Same account on a second device
        ext, errsB = [], []
        ctxB, B = await new_ctx(b, url, ext, errsB)
        await B.goto(url); await B.wait_for_timeout(800)
        await auth(B, "login", email="admin@example.com", password="password1")
        check("second device: login and sync", await wait_synced(B))
        await B.wait_for_timeout(500)
        check("second device: book downloaded automatically", await B.locator("#bookGrid .book-card").count() == 1)
        await B.click("#bookGrid .book-card"); await B.wait_for_timeout(2500)
        posB = await page_pos(B)
        check("second device: opens at the same position", posB == posA and posA is not None and posA > 0, f"A={posA} B={posB}")
        await B.click("#closeReader")

        # Invited user: register, browse catalog, add book
        ext, errsC = [], []
        ctxC, C = await new_ctx(b, url, ext, errsC)
        await C.goto(url); await C.wait_for_timeout(800)
        await auth(C, "register", invite=code, name="دوست", email="friend@example.com", password="password2")
        check("invited user registers", await wait_synced(C) and await C.inner_text("#accountButton") == "دوست")
        check("invited user has no admin tab", not await C.is_visible('[data-view="admin"]'))
        check("invited user library starts empty", await C.locator("#bookGrid .book-card").count() == 0)
        await C.click('[data-view="catalog"]'); await C.wait_for_timeout(1200)
        check("catalog shows the shared book", await C.locator("#catalogGrid .book-card").count() == 1)
        await C.click("[data-catalog-add]")
        await wait_synced(C); await C.wait_for_timeout(800)
        await C.click('[data-view="mine"]'); await C.wait_for_timeout(300)
        check("catalog book lands in the user's library", await C.locator("#bookGrid .book-card").count() == 1)
        await C.click("#bookGrid .book-card"); await C.wait_for_timeout(2500)
        check("user can read the catalog book", bool(await visible_text(C)))
        await C.click("#closeReader")
        await C.click("#accountButton"); await C.wait_for_timeout(300)
        await C.click("#logoutButton"); await C.wait_for_timeout(600)
        check("logout returns to local mode, books stay", await C.inner_text("#accountButton") == "ورود" and await C.locator("#bookGrid .book-card").count() == 1)
        allerrs = errsA + errsB + errsC
        check("accounts flow: no JS errors", len(allerrs) == 0, str(allerrs[:3]))
        for c in (ctxA, ctxB, ctxC): await c.close()

        # 11. In-book search, metadata repair, files shared from other apps
        import base64
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await open_sample(pg, url, sample)
        await pg.click("#searchToggle"); await pg.wait_for_timeout(200)
        check("search panel opens", await pg.is_visible("#searchPanel"))
        await pg.fill("#bookSearchInput", "شماره 35 از فصل دوم"); await pg.press("#bookSearchInput", "Enter")
        await poll(lambda: _count_gt(pg, "#bookSearchResults button", 0), 10000)
        n = await pg.locator("#bookSearchResults button").count()
        check("search finds the text", n == 1, str(n))
        check("search result is highlighted", await pg.locator("#bookSearchResults mark").count() >= 1)
        await pg.click("#bookSearchResults button"); await pg.wait_for_timeout(1200)
        pos = await page_pos(pg)
        check("clicking a result jumps there", await pg.inner_text("#chapterLabel") == "فصل دوم" and pos is not None and 26 <= pos <= 35, f"{await pg.inner_text('#chapterLabel')} pos={pos}")
        check("search panel closes after jumping", not await pg.is_visible("#searchPanel"))
        await pg.click("#searchToggle"); await pg.fill("#bookSearchInput", "نیست-در-کتاب"); await pg.press("#bookSearchInput", "Enter")
        await poll(lambda: _contains(pg, "#bookSearchStatus", "پیدا نشد"), 10000)
        check("search reports no results", True)
        await pg.keyboard.press("Escape"); await pg.click("#closeReader"); await pg.wait_for_timeout(400)
        # Simulate a book imported by an old version: filename as title, unknown author, never checked.
        await pg.evaluate("""() => new Promise((res, rej) => {
            const r = indexedDB.open('ketabkhane-db'); r.onsuccess = () => {
              const tx = r.result.transaction('books', 'readwrite'); const st = tx.objectStore('books');
              st.getAll().onsuccess = e => { for (const b of e.target.result) { b.title = 'my_old_file_name'; b.author = 'نویسنده نامشخص'; delete b.metaChecked; st.put(b); } };
              tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error); }; })""")
        await pg.reload(); await pg.wait_for_timeout(1000)
        check("legacy title shown before repair", "my_old_file_name" in " ".join(await pg.locator("#bookGrid h3").all_inner_texts()))
        await pg.wait_for_timeout(3500)
        titles = await pg.locator("#bookGrid h3").all_inner_texts()
        authors = await pg.locator("#bookGrid .book-meta p").all_inner_texts()
        check("metadata repaired from the file", titles == ["کتاب آزمایشی"] and authors[0] == "نویسنده تست", f"{titles} {authors[:1]}")
        manifest = await pg.evaluate("fetch('manifest.webmanifest').then(r => r.json())")
        check("manifest declares share target and file handler", manifest.get("share_target", {}).get("method") == "POST" and bool(manifest.get("file_handlers")))
        b64 = base64.b64encode(open(sample2, "rb").read()).decode()
        status = await pg.evaluate("""async b64 => {
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
            const fd = new FormData(); fd.append('epub', new File([bytes], 'shared-book.epub', { type: 'application/epub+zip' }));
            const r = await fetch('share-target', { method: 'POST', body: fd, redirect: 'manual' });
            return r.type + ':' + r.status; }""", b64)
        check("service worker accepts a shared file", status.startswith("opaqueredirect") or status.endswith(":303") or status.endswith(":200"), status)
        await pg.goto(url + "?shared=1"); await pg.wait_for_timeout(3000)
        titles = await pg.locator("#bookGrid h3").all_inner_texts()
        check("shared file imported into the library", "الف کتاب دوم" in titles, str(titles))
        check("shared URL cleaned up", "shared" not in pg.url, pg.url)
        check("search/repair/share: no JS errors", len(errs) == 0, str(errs[:3]))
        await ctx.close()

        # 12. v7.2: IndexedDB migration, highlights/notes/bookmarks, annotation sync, catalog details, admin panel
        SELECT_JS = """([idx, len]) => { const d = document.querySelector('#viewer iframe').contentDocument;
            const p = d.querySelectorAll('p')[idx]; const r = d.createRange(); r.setStart(p.firstChild, 0); r.setEnd(p.firstChild, len);
            const sel = d.getSelection(); sel.removeAllRanges(); sel.addRange(r); d.dispatchEvent(new Event('selectionchange')); }"""
        ANN_COUNT_JS = """() => new Promise(res => { const r = indexedDB.open('ketabkhane-db'); r.onsuccess = () => {
            const q = r.result.transaction('annotations').objectStore('annotations').getAll();
            q.onsuccess = () => res(q.result.filter(a => !a.deleted).map(a => a.type + ':' + (a.note || ''))); }; })"""
        # Migration: a v1 database with an old-style single bookmark.
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await pg.goto(url + "offline.html")
        b64 = base64.b64encode(open(sample, "rb").read()).decode()
        await pg.evaluate("""b64 => new Promise((res, rej) => {
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
            const r = indexedDB.open('ketabkhane-db', 1);
            r.onupgradeneeded = () => { r.result.createObjectStore('books', { keyPath: 'id' }); r.result.createObjectStore('progress', { keyPath: 'bookId' }); };
            r.onsuccess = () => { const tx = r.result.transaction(['books', 'progress'], 'readwrite');
              tx.objectStore('books').put({ id: 'old-1', title: 'کتاب آزمایشی', author: 'نویسنده تست', cover: null,
                file: new File([bytes], 'old.epub', { type: 'application/epub+zip' }), size: bytes.length, addedAt: 1 });
              tx.objectStore('progress').put({ bookId: 'old-1', cfi: 'epubcfi(/6/4!/4/2/1:0)', percentage: 0, updatedAt: 1, bookmark: 'epubcfi(/6/4!/4/2/1:0)' });
              tx.oncomplete = () => { r.result.close(); res(); }; tx.onerror = () => rej(tx.error); }; })""", b64)
        await pg.goto(url); await pg.wait_for_timeout(1500)
        version = await pg.evaluate("new Promise(res => { const r = indexedDB.open('ketabkhane-db'); r.onsuccess = () => { res(r.result.version); r.result.close(); }; })")
        anns = await pg.evaluate(ANN_COUNT_JS)
        check("v1 database upgraded to the current schema without losing the book", version >= 3 and await pg.locator("#bookGrid .book-card").count() == 1, str(version))
        check("old bookmark migrated into annotations", anns == ["bookmark:"], str(anns))
        await pg.click("#bookGrid .book-card"); await pg.wait_for_timeout(2500)
        await pg.click('[data-ptab="notes"]'); await pg.wait_for_timeout(300)
        check("migrated bookmark listed in the notes panel", await pg.locator("#notesList .note-item").count() == 1)
        # Highlight
        await pg.evaluate(SELECT_JS, [0, 12]); await pg.wait_for_timeout(800)
        check("selecting text shows the highlight toolbar", await pg.is_visible("#selectionBar"))
        await pg.click('[data-hl="yellow"]'); await pg.wait_for_timeout(600)
        check("highlight drawn in the book", await pg.locator("#viewer svg g.borg-hl, #viewer svg .borg-hl").count() >= 1)
        anns = await pg.evaluate(ANN_COUNT_JS)
        check("highlight saved", sorted(anns) == ["bookmark:", "highlight:"], str(anns))
        # Highlight with a note (prompt)
        pg.once("dialog", lambda d: asyncio.ensure_future(d.accept("یادداشت تست")))
        await pg.evaluate(SELECT_JS, [1, 12]); await pg.wait_for_timeout(800)
        await pg.click('[data-sel="note"]'); await pg.wait_for_timeout(800)
        anns = await pg.evaluate(ANN_COUNT_JS)
        check("highlight with note saved", "highlight:یادداشت تست" in anns, str(anns))
        check("note shown in the notes panel", "یادداشت تست" in await pg.inner_text("#notesList"))
        # Bookmark toggle on this page
        before = len(await pg.evaluate(ANN_COUNT_JS))
        await pg.click("#bookmarkButton"); await pg.wait_for_timeout(500)
        mid = await pg.evaluate(ANN_COUNT_JS)
        check("bookmark button toggles off the existing bookmark on this page", len(mid) == before - 1 and "ذخیره" not in await pg.inner_text("#bookmarkButton"), str(mid))
        await pg.click("#bookmarkButton"); await pg.wait_for_timeout(500)
        check("bookmark button adds it back", len(await pg.evaluate(ANN_COUNT_JS)) == before and "ذخیره" in await pg.inner_text("#bookmarkButton"))
        await pg.click("#notesList .note-del >> nth=0"); await pg.wait_for_timeout(400)
        check("delete from notes panel", len(await pg.evaluate(ANN_COUNT_JS)) == before - 1)
        check("annotations: no JS errors", len(errs) == 0, str(errs[:3]))
        await ctx.close()

        # Annotation sync between two devices of the same account (admin from scenario 10)
        ext, errsD = [], []
        ctxD, D = await new_ctx(b, url, ext, errsD)
        await D.goto(url); await D.wait_for_timeout(800)
        await auth(D, "login", email="admin@example.com", password="password1")
        await wait_synced(D)
        await D.click("#bookGrid .book-card"); await D.wait_for_timeout(2500)
        await D.evaluate(SELECT_JS, [0, 10]); await D.wait_for_timeout(800)
        await D.click('[data-hl="green"]'); await D.wait_for_timeout(3500)  # debounced push
        await D.click("#closeReader")
        ext, errsE = [], []
        ctxE, E = await new_ctx(b, url, ext, errsE)
        await E.goto(url); await E.wait_for_timeout(800)
        await auth(E, "login", email="admin@example.com", password="password1")
        await wait_synced(E)
        await E.click("#bookGrid .book-card"); await E.wait_for_timeout(2500)
        await E.click('[data-ptab="notes"]'); await E.wait_for_timeout(300)
        check("highlight synced to the other device", await E.locator("#notesList .note-item").count() >= 1)
        check("synced highlight drawn on the other device", await E.locator("#viewer svg g.borg-hl, #viewer svg .borg-hl").count() >= 1)
        await E.click("#closeReader")
        # Catalog search and details
        await E.click('[data-view="catalog"]'); await E.wait_for_timeout(1200)
        # v8.9: a paged server gets the query after a 300 ms debounce, so wait for the result instead of a fixed delay
        await E.fill("#catalogSearch", "چیزی-که-نیست")
        try: await poll(lambda: _contains(E, "#catalogGrid", "پیدا نشد"), 5000)
        except TimeoutError: pass
        check("catalog search filters", await E.locator("#catalogGrid .book-card").count() == 0)
        await E.fill("#catalogSearch", "آزمایشی")
        try: await poll(lambda: _count_gt(E, "#catalogGrid .book-card", 0), 5000)
        except TimeoutError: pass
        await E.click("#catalogGrid .book-card"); await E.wait_for_timeout(400)
        check("catalog book page opens", await E.is_visible("#viewBook") and await E.inner_text("#detailTitle") == "کتاب آزمایشی")
        await E.click("#detailBack"); await E.wait_for_timeout(300)
        # Admin panel
        E.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await E.click('[data-view="admin"]'); await E.wait_for_timeout(1500)
        check("admin stats shown", await E.locator("#adminStats .stat").count() == 7)  # v8.4: + client errors
        await E.click('[data-admin-tab="settings"]'); await E.wait_for_timeout(600)  # v8.9: admin sub-tabs
        check("approval setting on by default", await E.is_checked("#settingApproval"))
        await E.click("#settingApproval"); await E.wait_for_timeout(1200)
        check("approval setting saved", not await E.is_checked("#settingApproval"))
        await E.click("#settingApproval"); await E.wait_for_timeout(1200)
        await E.click('[data-admin-tab="users"]'); await E.wait_for_timeout(800)
        friend = E.locator('#userList .admin-item:has-text("friend@example.com")')
        await friend.locator('[data-user-act="disable"]').click(); await E.wait_for_timeout(1200)
        check("admin disables a user", await E.locator('#userList .admin-item:has-text("friend@example.com") .status-chip.off').count() == 1)
        await E.locator('#userList .admin-item:has-text("friend@example.com") [data-user-act="enable"]').click(); await E.wait_for_timeout(1200)
        check("admin re-enables the user", await E.locator('#userList .admin-item:has-text("friend@example.com") .status-chip.off').count() == 0)
        await E.click('[data-admin-tab="books"]')  # v8.9: one paged books table replaces the catalog / all-books lists
        try: await poll(lambda: _count_gt(E, "#admBooks tr.adm-row", 0), 8000)
        except TimeoutError: pass
        check("whole catalog and server books listed", await E.locator('#admBooks tr.adm-row:has-text("کتاب آزمایشی") .adm-c-st .st-approved').count() == 1
              and "کتاب" in await E.inner_text("#admBooksSummary"), await E.inner_text("#admBooks"))
        allerrs = errsD + errsE
        check("v7.2 sync/admin: no JS errors", len(allerrs) == 0, str(allerrs[:3]))
        await ctxD.close(); await ctxE.close()

        # 13. v7.3: stats/goals, reading status, shelves, time left, quote card, status+shelf sync
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs, accept_downloads=True)
        await open_sample(pg, url, sample)
        for _ in range(2):
            await pg.wait_for_timeout(3500); await pg.click("#nextPage")
        await pg.wait_for_timeout(500)
        tl = await pg.inner_text("#timeLeft")
        check("time left in chapter shown", "دقیقه" in tl or "آخرین" in tl, tl)
        await pg.evaluate(SELECT_JS, [0, 30]); await pg.wait_for_timeout(800)
        async with pg.expect_download(timeout=15000) as dl:
            await pg.click('[data-sel="quote"]')
        d = await dl.value
        path = await d.path()
        sig = open(path, "rb").read(8) if path else b""
        check("quote card produced as PNG", sig.startswith(b"\x89PNG"), d.suggested_filename)
        await pg.click("#closeReader"); await pg.wait_for_timeout(600)
        check("opening a book marks it as reading", "status:reading" in " ".join([await x.get_attribute("data-filter") for x in await pg.locator("#libraryFilters button").all()]))
        await pg.click('[data-filter="status:reading"]'); await pg.wait_for_timeout(300)
        check("filter: reading shows the book", await pg.locator("#bookGrid .book-card").count() == 1)
        await pg.click('[data-filter="status:want"]'); await pg.wait_for_timeout(300)
        check("filter: want-to-read is empty", await pg.locator("#bookGrid .book-card").count() == 0)
        await pg.click('[data-filter="all"]'); await pg.wait_for_timeout(300)
        await pg.click("#bookGrid [data-options]"); await pg.wait_for_timeout(300)
        check("book options dialog opens", await pg.is_visible("#bookOptions"))
        await pg.click('#bookOptions [data-status="finished"]'); await pg.wait_for_timeout(300)
        await pg.fill("#newShelfName", "فلسفه"); await pg.click("#addShelf"); await pg.wait_for_timeout(400)
        check("new shelf created and checked", await pg.locator('#shelfChecks input[type=checkbox]:checked').count() == 1)
        await pg.click("#bookOptionsClose"); await pg.wait_for_timeout(500)
        check("status shown on the card", "خوانده‌شده" in await pg.inner_text("#bookGrid .book-badge"))
        check("finished counter", await pg.inner_text("#statFinished") == "۱")
        await pg.click('#libraryFilters button:has-text("فلسفه")'); await pg.wait_for_timeout(300)
        check("shelf filter shows the book", await pg.locator("#bookGrid .book-card").count() == 1)
        await pg.click('[data-filter="all"]')
        # Stats: today 30 min, yesterday 10 min -> streak 2; goal 30 -> full ring
        await pg.evaluate("""() => { const z = n => String(n).padStart(2, '0'); const k = d => `${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())}`;
            const t = new Date(), y = new Date(); y.setDate(y.getDate() - 1);
            localStorage.setItem('borg-reading-days', JSON.stringify({ [k(t)]: 1800, [k(y)]: 600 })); }""")
        pg.once("dialog", lambda dd: asyncio.ensure_future(dd.accept("30")))
        await pg.click("#goalButton"); await pg.wait_for_timeout(500)
        check("stats: today minutes", await pg.inner_text("#statToday") == "۳۰", await pg.inner_text("#statToday"))
        check("stats: streak of consecutive days", await pg.inner_text("#statStreak") == "۲")
        check("stats: week total", await pg.inner_text("#statWeek") == "۴۰")
        check("goal updated", "۳۰" in await pg.inner_text("#goalButton") and (await pg.get_attribute("#goalArc", "stroke-dasharray")).startswith("100"))
        await pg.reload(); await pg.wait_for_timeout(1200)
        check("status and shelf survive reload", "خوانده‌شده" in await pg.inner_text("#bookGrid .book-badge") and await pg.locator('#libraryFilters button:has-text("فلسفه")').count() == 1)
        check("v7.3 local features: no JS errors", len(errs) == 0, str(errs[:3]))
        await ctx.close()

        # Status + shelves sync across devices (admin account)
        ext, errsF = [], []
        ctxF, F = await new_ctx(b, url, ext, errsF)
        await F.goto(url); await F.wait_for_timeout(800)
        await auth(F, "login", email="admin@example.com", password="password1"); await wait_synced(F)
        await F.click("#bookGrid [data-options]"); await F.wait_for_timeout(300)
        await F.click('#bookOptions [data-status="want"]')
        await F.fill("#newShelfName", "رمان"); await F.click("#addShelf"); await F.wait_for_timeout(300)
        await F.click("#bookOptionsClose"); await F.wait_for_timeout(3500)
        ext, errsG = [], []
        ctxG, G = await new_ctx(b, url, ext, errsG)
        await G.goto(url); await G.wait_for_timeout(800)
        await auth(G, "login", email="admin@example.com", password="password1"); await wait_synced(G)
        await G.wait_for_timeout(500)
        check("status synced to the other device", "می‌خواهم بخوانم" in await G.inner_text("#bookGrid .book-badge"))
        check("shelf synced to the other device", await G.locator('#libraryFilters button:has-text("رمان")').count() == 1)
        allerrs = errsF + errsG
        check("v7.3 sync: no JS errors", len(allerrs) == 0, str(allerrs[:3]))
        await ctxF.close(); await ctxG.close()

        # 14. v8 reading features: footnotes, images, lookup/copy, digits, layout/themes, read-aloud
        notes = str(FIX / "sample-notes.epub")
        TTS_STUB = """(() => { const spoken = []; window.__spoken = spoken;
          class U { constructor(t) { this.text = t; this.onend = null; this.onerror = null; } }
          const synth = { _t: null,
            getVoices() { return [{ name: 'Test Farsi', lang: 'fa-IR', voiceURI: 'test-fa', default: true }]; },
            speak(u) { spoken.push(u.text); this._t = setTimeout(() => { u.onend && u.onend({}); }, 15); },
            cancel() { clearTimeout(this._t); }, pause() {}, resume() {}, addEventListener() {}, removeEventListener() {} };
          Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
          window.SpeechSynthesisUtterance = U; })();"""
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs)
        await ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        await ctx.add_init_script(TTS_STUB)
        await pg.goto(url); await pg.wait_for_timeout(800)
        await pg.set_input_files("#epubInput", notes); await pg.wait_for_timeout(2000)
        await pg.click("#bookGrid .book-card"); await pg.wait_for_timeout(2500)
        fr = pg.frame_locator("#viewer iframe")
        await fr.locator("#r1").click(); await pg.wait_for_timeout(1200)
        check("EPUB3 footnote opens in a popup", await pg.is_visible("#notePopup") and "پانویس اول" in await pg.inner_text("#notePopupText"), await pg.inner_text("#notePopupText"))
        check("footnote click does not navigate away", await pg.inner_text("#chapterLabel") == "متن")
        await pg.click("#notePopupClose"); await pg.wait_for_timeout(200)
        await fr.locator("#r2").click(); await pg.wait_for_timeout(800)
        check("classic #fn footnote opens in a popup", await pg.is_visible("#notePopup") and "پانویس دوم" in await pg.inner_text("#notePopupText"))
        await pg.click("#notePopupGo"); await pg.wait_for_timeout(800)
        check("'go to footnote' closes the popup", not await pg.is_visible("#notePopup"))
        p3 = await fr.locator("#p3").inner_text()
        check("RLM used as half-space is shown as a real half-space", "صیقل\u200cیافته" in p3 and "\u200f" not in p3, repr(p3))
        await open_settings(pg); await pg.keyboard.press("Escape")
        await pg.click("#searchToggle"); await pg.fill("#bookSearchInput", "می\u200cکنم"); await pg.press("#bookSearchInput", "Enter")
        await poll(lambda: _count_gt(pg, "#bookSearchResults button", 0), 10000)
        check("search matches old-style spelling (ى + RLM) with a standard query", await pg.locator("#bookSearchResults button").count() >= 1)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(200)
        pic = await pg.evaluate("""()=>{const d=document.querySelector('#viewer iframe').contentDocument,i=d.getElementById('pic'),p=i.parentElement;
          const r=i.getBoundingClientRect(),q=p.getBoundingClientRect();return {cls:i.classList.contains('borg-blockimg'),disp:d.defaultView.getComputedStyle(i).display,
          off:Math.round(Math.abs((r.left+r.right)/2-(q.left+q.right)/2))}}""")
        check("v8.6: an image alone in its paragraph is centred as a block", pic["cls"] and pic["disp"] == "block" and pic["off"] <= 2, str(pic))
        await fr.locator("#pic").click(); await pg.wait_for_timeout(500)
        check("tapping an image opens the viewer", await pg.is_visible("#imageViewer") and bool(await pg.get_attribute("#imageViewerImg", "src")))
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(300)
        check("Escape closes the image viewer, reader stays", not await pg.is_visible("#imageViewer") and await pg.is_visible("#reader"))
        await pg.evaluate(SELECT_JS, [0, 10]); await pg.wait_for_timeout(800)
        await pg.click('[data-sel="define"]'); await pg.wait_for_timeout(300)
        links = await pg.locator("#lookupLinks a").count()
        href = await pg.get_attribute("#lookupLinks a >> nth=0", "href")
        check("meaning lookup offers dictionaries", await pg.is_visible("#lookupBox") and links >= 3 and "abadis" in href, f"{links} {href}")
        await pg.click("#lookupClose")
        await pg.evaluate(SELECT_JS, [0, 10]); await pg.wait_for_timeout(800)
        await pg.click('[data-sel="copy"]'); await pg.wait_for_timeout(400)
        clip = await pg.evaluate("navigator.clipboard.readText()")
        check("copy puts the selection on the clipboard", clip.strip() == "این جمله ی"[:len(clip.strip())] and len(clip.strip()) >= 5, repr(clip))
        await open_settings(pg); await pg.click("#prefDigits"); await pg.wait_for_timeout(3000)
        txt = await pg.frame_locator("#viewer iframe").locator("#p1").inner_text()
        check("Persian digits option converts numbers in the text", "۱۴۰۴" in txt and "1404" not in txt, txt[-30:])
        await open_settings(pg); await pg.click("#prefDigits"); await pg.wait_for_timeout(3000)
        await pg.click("#closeReader"); await pg.wait_for_timeout(500)
        check("footnotes/images/lookup: no JS errors", len(errs) == 0, str(errs[:3]))

        # Layout and themes on the long sample
        await pg.set_input_files("#epubInput", sample); await pg.wait_for_timeout(2000)
        await pg.click('#bookGrid .book-card:has-text("کتاب آزمایشی")'); await pg.wait_for_timeout(2500)
        await open_settings(pg); await pg.wait_for_timeout(200)
        await pg.click('[data-theme="black"]'); await pg.wait_for_timeout(400)
        bg = await pg.evaluate("getComputedStyle(document.querySelector('#viewer iframe').contentDocument.body).backgroundColor")
        check("black (OLED) theme", bg == "rgb(0, 0, 0)", bg)
        await pg.click('[data-margin="l"]'); await pg.wait_for_timeout(600)
        pad = await pg.evaluate("parseFloat(getComputedStyle(document.getElementById('viewer')).paddingLeft)")
        check("wide margins applied", pad > 20 and bool(await visible_text(pg)), str(pad))
        await pg.click('[data-para="l"]'); await pg.wait_for_timeout(400)
        mb = await pg.evaluate("parseFloat(getComputedStyle(document.querySelector('#viewer iframe').contentDocument.querySelector('p')).marginBottom)")
        check("paragraph spacing applied", mb > 20, str(mb))
        await pg.click('[data-margin="m"]'); await pg.click('[data-para="m"]')
        await pg.click('[data-spread="none"]'); await pg.wait_for_timeout(3000)
        await pg.click("#readerTitle"); await pg.wait_for_timeout(300)
        ratio = await pg.evaluate("""() => { const f = document.querySelector('#viewer iframe'); const p = f.contentDocument.querySelector('p');
            return p.getBoundingClientRect().width / document.getElementById('viewer').getBoundingClientRect().width; }""")
        check("single-column layout uses the full width", ratio > 0.7, f"{ratio:.2f}")
        await open_settings(pg); await pg.click('[data-spread="auto"]'); await pg.wait_for_timeout(3000)
        await open_settings(pg); await pg.click('[data-flow="scrolled"]'); await pg.wait_for_timeout(3000)
        scrollable = await pg.evaluate("(() => { const c = document.querySelector('#viewer .epub-container'); return c ? c.scrollHeight > c.clientHeight : false; })()")
        check("scrolled mode renders a scrollable chapter", scrollable)
        await open_settings(pg); await pg.click('[data-flow="paginated"]'); await pg.wait_for_timeout(3000)
        await open_settings(pg); await pg.click('[data-theme="light"]'); await pg.keyboard.press("Escape")
        tl = await pg.inner_text("#timeLeft")
        check("page number within chapter shown", "صفحهٔ" in tl and "از" in tl, tl)
        # Read aloud (speech engine stubbed)
        await pg.click("#ttsToggle"); await pg.wait_for_timeout(1500)
        spoken = await pg.evaluate("window.__spoken.slice()")
        check("read-aloud starts from the visible page", len(spoken) >= 3 and await pg.is_visible("#ttsBar"), str(spoken[:2]))
        check("sentence being read is highlighted", await pg.locator("#viewer svg .tts-hl, #viewer svg g.tts-hl").count() >= 1)
        try:
            await poll(lambda: pg.evaluate("window.__spoken.some(t => t.includes('فصل دوم'))"), 30000)
            reached = True
        except TimeoutError:
            reached = False
        check("read-aloud turns pages and continues into the next chapter", reached and await pg.inner_text("#chapterLabel") == "فصل دوم", await pg.inner_text("#chapterLabel"))
        await pg.click("#ttsPlay"); await pg.wait_for_timeout(300)
        n1 = await pg.evaluate("window.__spoken.length"); await pg.wait_for_timeout(600)
        n2 = await pg.evaluate("window.__spoken.length")
        check("pause stops speaking", n1 == n2)
        await pg.click("#ttsPlay"); await pg.wait_for_timeout(600)
        check("resume continues", await pg.evaluate("window.__spoken.length") > n2)
        await pg.click("#ttsStop"); await pg.wait_for_timeout(300)
        check("stop hides the bar and the highlight", not await pg.is_visible("#ttsBar") and await pg.locator("#viewer svg .tts-hl, #viewer svg g.tts-hl").count() == 0)
        check("v8 reading features: no JS errors", len(errs) == 0, str(errs[:3]))
        await ctx.close()
        # Auto theme follows the system colour scheme
        ext, errs = [], []
        ctx, pg = await new_ctx(b, url, ext, errs, color_scheme="dark")
        await open_sample(pg, url, sample)
        await open_settings(pg); await pg.click('[data-theme="auto"]'); await pg.wait_for_timeout(400)
        bg = await pg.evaluate("getComputedStyle(document.querySelector('#viewer iframe').contentDocument.body).backgroundColor")
        check("auto theme uses dark when the system is dark", bg == "rgb(31, 38, 35)", bg)
        await ctx.close()

        # 15. v8.1: catalog book page, online preview, author/category filters, add with shelves, EPUB publisher metadata
        import tempfile
        tmpd = Path(tempfile.mkdtemp(prefix="borg-e2e-epub-"))
        third = make_epub(tmpd / "third.epub", "کتاب سوم", "test-3", "نویسندهٔ دیگر", "نشر آزمایش", "2019-05-01T00:00:00Z")
        fourth = make_epub(tmpd / "fourth.epub", "کتاب چهارم", "test-4", "نویسندهٔ دیگر")
        long_desc = "این توضیح بلند برای آزمایش «بیشتر بخوانید» نوشته شده است. " * 25
        ext, errsH = [], []
        ctxH, H = await new_ctx(b, url, ext, errsH)
        answers = []
        H.on("dialog", lambda d: asyncio.ensure_future(d.accept(answers.pop(0) if answers else "")))
        await H.goto(url); await H.wait_for_timeout(800)
        await auth(H, "login", email="admin@example.com", password="password1"); await wait_synced(H)
        await H.set_input_files("#epubInput", third); await H.wait_for_timeout(1500)
        await H.set_input_files("#epubInput", fourth); await H.wait_for_timeout(1500)
        local = {x["title"]: x for x in await H.evaluate(IDB_JS, ["books"])}
        t3 = local.get("کتاب سوم", {})
        check("EPUB publisher/date/language kept on the local book", t3.get("publisher") == "نشر آزمایش" and t3.get("published") == "2019-05-01" and t3.get("language") == "fa", str({k: t3.get(k) for k in ("publisher", "published", "language")}))
        try:
            await poll(lambda: _count_gt(H, '#bookGrid .book-card:has-text("کتاب سوم") .share-book, #bookGrid .book-card:has-text("کتاب چهارم") .share-book', 1), 20000)
            uploaded = True
        except TimeoutError:
            uploaded = False
        check("new books uploaded", uploaded)
        answers[:] = [long_desc, "شعر"]
        await H.click('#bookGrid .book-card:has-text("کتاب سوم") .share-book'); await H.wait_for_timeout(1200)
        answers[:] = ["", ""]
        await H.click('#bookGrid .book-card:has-text("کتاب چهارم") .share-book'); await H.wait_for_timeout(1200)
        check("books shared to the catalog with a category", "عمومی" in await H.inner_text('#bookGrid .book-card:has-text("کتاب سوم") .book-badge'))
        await H.click('[data-view="admin"]'); await H.wait_for_timeout(800)
        await H.click('[data-admin-tab="books"]')  # v8.9: the catalog editor is the book details panel of the books tab
        try: await poll(lambda: _count_gt(H, '#admBooks tr.adm-row:has-text("کتاب آزمایشی")', 0), 8000)
        except TimeoutError: pass
        await H.click('#admBooks tr.adm-row:has-text("کتاب آزمایشی") .adm-c-title'); await H.wait_for_timeout(500)
        check("admin catalog editor opens with the current values", await H.is_visible("#admDetail") and await H.input_value('#admDetailForm [name="title"]') == "کتاب آزمایشی")
        # v8.10: a category now exists ("شعر", created by the admin's share above), so the details panel shows the picker
        await H.fill("#admDetailCats .cp-search", "شعر"); await H.wait_for_timeout(150)
        await H.click('#admDetailCats .cp-opt:has-text("شعر")')
        await H.fill('#admDetailForm [name="publisher"]', "نشر تست")
        await H.fill('#admDetailForm [name="published"]', "1402")
        await H.click("#admDetailSave"); await H.wait_for_timeout(1500)
        check("admin edit saves category/publisher/date", not await H.is_visible("#admDetail")
              and await H.locator('#admBooks tr.adm-row:has-text("کتاب آزمایشی") .adm-c-cat:has-text("شعر")').count() == 1)
        check("v8.1 admin/upload: no JS errors", len(errsH) == 0, str(errsH[:3]))
        await ctxH.close()

        # Invited user browses the catalog (their library already holds "کتاب آزمایشی" from scenario 10)
        ext, errsI = [], []
        ctxI, I = await new_ctx(b, url, ext, errsI)
        await I.goto(url); await I.wait_for_timeout(800)
        await auth(I, "login", email="friend@example.com", password="password2"); await wait_synced(I)
        await I.wait_for_timeout(500)
        lib_before = await I.locator("#bookGrid .book-card").count()
        await I.click('[data-view="catalog"]'); await I.wait_for_timeout(1500)
        check("catalog lists all shared books", await I.locator("#catalogGrid .book-card").count() == 3)
        # v8.10: the flat category chips are gone; without a category list there is no navigator either
        # The admin's share above created the top-level category "شعر" (legacy name → category), so the navigator shows it
        check("no flat category chips; the category navigator lists the category made from the shared book", await I.locator("#catalogFilters [data-cat-filter]").count() == 0
              and await I.is_visible("#catNav") and "شعر" in await I.inner_text("#catNav"))
        # Book page
        await I.click('#catalogGrid .book-card:has-text("کتاب سوم")'); await I.wait_for_timeout(800)
        meta = await I.inner_text("#detailMeta") if await I.is_visible("#detailMeta") else ""
        check("book page opens with title and author", await I.is_visible("#viewBook") and not await I.is_visible("#viewCatalog")
              and await I.inner_text("#detailTitle") == "کتاب سوم" and await I.inner_text("#detailAuthor") == "نویسندهٔ دیگر")
        check("book page metadata table", all(x in meta for x in ("شعر", "فارسی", "نشر آزمایش", "۲۰۱۹-۰۵-۰۱", "کیلوبایت", "مدیر بُرگ")), meta.replace("\n", " | "))
        crumbs = await I.inner_text("#detailCrumbs")
        check("breadcrumb: catalog / category / title", "کتابخانهٔ عمومی" in crumbs and "شعر" in crumbs and "کتاب سوم" in crumbs, crumbs)
        book3 = I.url.split("#")[-1]
        check("book page is deep-linkable", book3.startswith("catalog/book/"), I.url)
        h0 = (await I.locator("#detailDesc").bounding_box())["height"]
        check("long description collapsed with 'read more'", await I.is_visible("#detailMore") and "بیشتر بخوانید" in await I.inner_text("#detailMore"))
        await I.click("#detailMore"); await I.wait_for_timeout(200)
        h1 = (await I.locator("#detailDesc").bounding_box())["height"]
        check("'read more' expands the description", h1 > h0 + 20, f"{h0:.0f}->{h1:.0f}")
        related = await I.locator("#detailRelated .book-card h3").all_inner_texts()
        check("similar books: same author first", await I.is_visible("#detailRelatedSection") and related[:1] == ["کتاب چهارم"], str(related))
        # Online reading without adding
        await I.click("#detailRead")
        try:
            await poll(lambda: visible_text(I), 15000); opened = True
        except TimeoutError:
            opened = False
        check("read online opens the book", opened and (await visible_text(I) or "").startswith("فصل اول") and await I.is_visible("#previewBanner"), await visible_text(I))
        await I.click("#nextPage"); await I.wait_for_timeout(600)
        await I.click("#bookmarkButton"); await I.wait_for_timeout(300)
        check("preview: bookmarks are not saved", "کتابخانه" in await I.inner_text("#toast") and len(await I.evaluate(IDB_JS, ["annotations"])) == 0)
        await I.click("#closeReader"); await I.wait_for_timeout(600)
        books = await I.evaluate(IDB_JS, ["books"]); progress = await I.evaluate(IDB_JS, ["progress"])
        check("closing the preview returns to the book page", await I.is_visible("#viewBook") and not await I.is_visible("#reader"))
        check("read online does not add to the library or save progress",
              len(books) == lib_before and not any(str(p_.get("bookId", "")).startswith("preview") for p_ in progress), f"{len(books)} books, {len(progress)} progress")
        # Author filter
        await I.click("#detailAuthor"); await I.wait_for_timeout(500)
        titles = sorted(await I.locator("#catalogGrid .book-card h3").all_inner_texts())
        check("author link filters the catalog by author", await I.is_visible("#viewCatalog") and titles == ["کتاب سوم", "کتاب چهارم"]
              and "نویسندهٔ دیگر" in await I.inner_text("#catalogFilters [data-clear-author]"), str(titles))
        await I.click("#catalogFilters [data-clear-author]"); await I.wait_for_timeout(300)
        check("author filter chip is removable", await I.locator("#catalogGrid .book-card").count() == 3 and await I.locator("#catalogFilters [data-clear-author]").count() == 0)
        # Add with a shelf
        await I.click('#catalogGrid .book-card:has-text("کتاب چهارم")'); await I.wait_for_timeout(600)
        await I.click("#detailAddMore"); await I.wait_for_timeout(300)
        check("add menu opens", await I.is_visible("#detailAddMenu"))
        await I.fill("#detailNewShelf", "شعرها"); await I.click("#detailNewShelfAdd"); await I.wait_for_timeout(300)
        check("new shelf created in the add menu and selected", await I.locator("#detailShelfChecks input:checked").count() == 1)
        await I.click("#detailAddConfirm")
        try:
            await poll(lambda: I.is_visible("#detailInLibrary"), 20000); added = True
        except TimeoutError:
            added = False
        check("book page shows 'in your library' after adding", added)
        # Add from inside the preview
        await I.click('[data-view="catalog"]'); await I.wait_for_timeout(800)
        await I.click('#catalogGrid .book-card:has-text("کتاب سوم")'); await I.wait_for_timeout(600)
        await I.click("#detailRead")
        await poll(lambda: visible_text(I), 15000)
        await I.click("#previewAdd")
        try:
            await poll(lambda: I.is_hidden("#previewBanner"), 20000); padded = True
        except TimeoutError:
            padded = False
        check("'add to library' from the preview", padded)
        await I.click("#nextPage"); await I.wait_for_timeout(1200)
        await I.click("#closeReader"); await I.wait_for_timeout(600)
        await I.click('[data-view="mine"]'); await I.wait_for_timeout(600)
        titles = await I.locator("#bookGrid .book-card h3").all_inner_texts()
        check("added books are in the library", "کتاب چهارم" in titles and "کتاب سوم" in titles and len(titles) == lib_before + 2, str(titles))
        await I.click('#libraryFilters button:has-text("شعرها")'); await I.wait_for_timeout(300)
        titles = await I.locator("#bookGrid .book-card h3").all_inner_texts()
        check("book added with a shelf is on that shelf", titles == ["کتاب چهارم"], str(titles))
        await I.click('[data-filter="all"]')
        progress = await I.evaluate(IDB_JS, ["progress"])
        b3 = next((x for x in await I.evaluate(IDB_JS, ["books"]) if x["title"] == "کتاب سوم"), {})
        check("a book added from the preview keeps its reading position", any(p_.get("bookId") == b3.get("id") and p_.get("cfi") for p_ in progress))
        # Deep link and back
        await I.goto(url + "#" + book3); await I.wait_for_timeout(2000)
        try:
            await poll(lambda: _contains(I, "#detailTitle", "کتاب سوم"), 10000); deep = True
        except TimeoutError:
            deep = False
        check("deep link opens the book page", deep and await I.is_visible("#viewBook"))
        await I.click("#detailBack"); await I.wait_for_timeout(800)
        check("back goes to the catalog", await I.is_visible("#viewCatalog") and not await I.is_visible("#viewBook"))
        # Phone width: the page fits the screen
        await I.set_viewport_size({"width": 390, "height": 844})
        await I.click('#catalogGrid .book-card:has-text("کتاب سوم")'); await I.wait_for_timeout(600)
        sw = await I.evaluate("document.documentElement.scrollWidth")
        cb = await I.locator("#detailCover").bounding_box()
        check("mobile: book page fits the screen", sw <= 391 and cb["width"] > 100 and cb["x"] >= 0, f"scrollWidth={sw}")
        check("v8.1 catalog: no JS errors", len(errsI) == 0, str(errsI[:3]))
        await ctxI.close()
        import shutil; shutil.rmtree(tmpd, ignore_errors=True)

        # 14. v8.1.1: a Persian book with no page-progression-direction (and a wrong lang) still opens TOC chapters at their first page
        tmpd = Path(tempfile.mkdtemp(prefix="borg-e2e-dir-"))
        nodir = make_epub(tmpd / "nodir.epub", "کتاب بی‌جهت", "test-nodir", "نویسنده", direction=None, lang="en")
        errsD, extD = [], []
        ctxD, D_ = await new_ctx(b, url, extD, errsD)
        await D_.set_viewport_size({"width": 1600, "height": 900})
        await D_.goto(url); await D_.wait_for_timeout(1200)
        await D_.set_input_files("#epubInput", nodir); await D_.wait_for_timeout(2500)
        await D_.click("#bookGrid .book-card"); await D_.wait_for_timeout(3000)
        BODY_DIR = "()=>{const d=document.querySelector('#viewer iframe').contentDocument;return d.defaultView.getComputedStyle(d.body).direction}"
        H1_VISIBLE = """(t)=>{const f=document.querySelector('#viewer iframe');if(!f)return false;const r=f.getBoundingClientRect();
          const box=document.querySelector('#viewer').getBoundingClientRect();const h=[...f.contentDocument.querySelectorAll('h1')].find(x=>x.textContent.includes(t));
          if(!h)return false;const x=h.getBoundingClientRect();return x.right+r.left>box.left&&x.left+r.left<box.right}"""
        check("no-direction book: first chapter opens at its start", await D_.evaluate(H1_VISIBLE, "فصل اول"))
        await D_.locator("#tocList >> text=فصل دوم").first.click(); await D_.wait_for_timeout(2000)
        check("no-direction book: TOC jump shows the chapter heading", await D_.evaluate(H1_VISIBLE, "فصل دوم"))
        t0 = await visible_text(D_)
        await D_.click("#nextPage"); await D_.wait_for_timeout(1200); t1 = await visible_text(D_)
        await D_.click("#prevPage"); await D_.wait_for_timeout(1200)
        check("no-direction book: next moves forward and prev comes back", t1 and t1 != t0 and await D_.evaluate(H1_VISIBLE, "فصل دوم"), f"{t0!r} → {t1!r}")
        veil = await D_.evaluate("""()=>{const d=document.querySelector('#viewer iframe').contentDocument;
          return {veil: !!d.getElementById('borg-veil'), vis: d.defaultView.getComputedStyle(d.documentElement).visibility}}""")
        check("v8.1.2: chapter veil is lifted once styles load", not veil["veil"] and veil["vis"] == "visible", str(veil))
        check("Persian book tagged lang=en is laid out RTL", await D_.evaluate(BODY_DIR) == "rtl")
        # English book (no declared direction): detected as LTR from its text; nested TOC entries are listed
        import zipfile
        en = tmpd / "english.epub"
        with zipfile.ZipFile(en, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">en-1</dc:identifier><dc:title>English Weekly</dc:title><dc:language>en</dc:language></metadata><manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="a" href="a.html" media-type="application/xhtml+xml"/><item id="b" href="b.html" media-type="application/xhtml+xml"/></manifest><spine toc="ncx"><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr("toc.ncx", '<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>English Weekly</text></docTitle><navMap><navPoint id="n1" playOrder="1"><navLabel><text>Leaders</text></navLabel><content src="a.html"/><navPoint id="n2" playOrder="2"><navLabel><text>Second article</text></navLabel><content src="b.html"/></navPoint></navPoint></navMap></ncx>')
            for f, h in (("a.html", "Leaders"), ("b.html", "Second article")):
                body = "".join(f"<p>This is paragraph {k} of the section called {h}, written in plain English.</p>" for k in range(40))
                z.writestr(f, f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{h}</title></head><body><h1>{h}</h1>{body}</body></html>')
        await D_.click("#closeReader") if await D_.is_visible("#closeReader") else None
        await D_.goto(url); await D_.wait_for_timeout(1200)
        await D_.set_input_files("#epubInput", str(en)); await D_.wait_for_timeout(2500)
        await D_.click('#bookGrid .book-card:has-text("English Weekly")'); await D_.wait_for_timeout(3000)
        check("English book is laid out LTR", await D_.evaluate(BODY_DIR) == "ltr")
        check("nested TOC entries are listed", await D_.locator("#tocList .toc-sub", has_text="Second article").count() == 1)
        await D_.locator("#tocList .toc-sub", has_text="Second article").click(); await D_.wait_for_timeout(2000)
        check("English book: nested TOC entry opens its section at the start", await D_.evaluate(H1_VISIBLE, "Second article"))
        await D_.set_viewport_size({"width": 390, "height": 844}); await D_.wait_for_timeout(600)
        icons = await D_.evaluate("""()=>['ttsToggle','searchToggle','tocToggle'].map(id=>{const b=document.getElementById(id);const i=b&&b.querySelector('svg.ico');
          if(!i)return [id,0,0];const r=i.getBoundingClientRect(),q=b.getBoundingClientRect();return [id,Math.round(r.width),Math.round(Math.min(q.width,q.height))]})""")
        check("v8.1.5 mobile: toolbar icons are drawn at a readable size", all(w >= 18 and bw >= 38 for _, w, bw in icons), str(icons))
        check("v8.1.1 direction: no JS errors", len(errsD) == 0, str(errsD[:3]))
        await ctxD.close(); shutil.rmtree(tmpd, ignore_errors=True)

        # 15. v8.2 nav: chapter title from the TOC in the header, TOC active entry, whole-book seek bar
        import zipfile
        tmpn = Path(tempfile.mkdtemp(prefix="borg-e2e-nav-"))
        # A longer Persian book: six chapter files, the third with two #fragment sub-entries (#a and #b, both after its first page).
        big = tmpn / "big.epub"
        names = ["اول", "دوم", "سوم", "چهارم", "پنجم", "ششم"]
        with zipfile.ZipFile(big, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            D = zipfile.ZIP_DEFLATED
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', D)
            items = "".join(f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(1, 7))
            refs = "".join(f'<itemref idref="c{i}"/>' for i in range(1, 7))
            z.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">nav-big-1</dc:identifier><dc:title>کتاب بلند</dc:title><dc:creator>نویسنده</dc:creator><dc:language>fa</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{items}</manifest><spine page-progression-direction="rtl">{refs}</spine></package>', D)
            lis = "".join((f'<li><a href="text/c{i}.xhtml">فصل {n}</a><ol><li><a href="text/c3.xhtml#a">بخش الف</a></li><li><a href="text/c3.xhtml#b">بخش ب</a></li></ol></li>' if i == 3 else f'<li><a href="text/c{i}.xhtml">فصل {n}</a></li>') for i, n in enumerate(names, 1))
            z.writestr("OEBPS/nav.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol>{lis}</ol></nav></body></html>', D)
            for i, n in enumerate(names, 1):
                ps = [f"<p>پاراگراف {k} از فصل {n}: این متن برای آزمایش نوار پیشرفت کتاب و نام فصل در سربرگ نوشته شده است.</p>" for k in range(90)]
                if i == 3:
                    ps.insert(30, '<h2 id="a">بخش الف</h2>'); ps.insert(61, '<h2 id="b">بخش ب</h2>')
                z.writestr(f"OEBPS/text/c{i}.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل {n}</title></head><body><h1>فصل {n}</h1>{"".join(ps)}</body></html>', D)
        LABEL_GEOM = """()=>{const l=document.getElementById('chapterLabel'),n=document.querySelector('.page-nav');const r=l.getBoundingClientRect(),q=n.getBoundingClientRect();
          return {dir:l.getAttribute('dir'),w:r.width,inside:r.left>=q.left-1&&r.right<=q.right+1&&r.height>0,cut:l.scrollWidth>l.clientWidth+1,ov:getComputedStyle(l).textOverflow}}"""
        ACTIVE = "()=>[...document.querySelectorAll('#tocList .toc-link.active')].map(b=>[b.textContent.trim(),b.getAttribute('aria-current')])"
        SEEK = """()=>{const s=document.getElementById('bookSeek'),r=s.getBoundingClientRect();return {dis:s.disabled,v:+s.value,dir:s.dir,
          cdir:getComputedStyle(s).direction,w:r.width,h:r.height,top:r.top,vt:s.getAttribute('aria-valuetext'),al:s.getAttribute('aria-label'),
          info:document.getElementById('seekInfo').textContent,infoVis:document.getElementById('seekInfo').getBoundingClientRect().width>0}}"""
        faint = lambda t: int("".join("0123456789"["۰۱۲۳۴۵۶۷۸۹".index(c)] if c in "۰۱۲۳۴۵۶۷۸۹" else c for c in t if c in "۰۱۲۳۴۵۶۷۸۹0123456789") or -1)
        errsN, extN = [], []
        ctxN, N = await new_ctx(b, url, extN, errsN)
        await open_sample(N, url, sample)
        lg = await N.evaluate(LABEL_GEOM)
        check("v8.2: header shows the TOC chapter title after opening", await N.inner_text("#chapterLabel") == "فصل اول" and lg["inside"] and lg["dir"] == "auto", str(lg))
        check("v8.2: TOC entry of the current chapter is marked active", await N.evaluate(ACTIVE) == [["فصل اول", "true"]], str(await N.evaluate(ACTIVE)))
        await N.click(".toc-link >> nth=1"); await N.wait_for_timeout(1000)
        check("v8.2: header shows the chapter title after a TOC jump", await N.inner_text("#chapterLabel") == "فصل دوم", await N.inner_text("#chapterLabel"))
        check("v8.2: active TOC entry follows the jump", await N.evaluate(ACTIVE) == [["فصل دوم", "true"]], str(await N.evaluate(ACTIVE)))
        await poll(lambda: N.evaluate("!document.getElementById('bookSeek').disabled"), 15000)
        sk = await N.evaluate(SEEK)
        check("v8.2: seek bar is enabled once locations are ready, visible, RTL for a Persian book",
              not sk["dis"] and sk["w"] > 200 and sk["h"] > 10 and sk["dir"] == "rtl" and sk["cdir"] == "rtl" and sk["al"] == "پیشرفت کتاب", str(sk))
        check("v8.2: book percentage and time left in the book are shown", sk["infoVis"] and "از کتاب" in sk["info"] and "دقیقه تا پایان کتاب" in sk["info"] and "درصد" in (sk["vt"] or ""), sk["info"])
        # A longer book: fragment entries, drag preview without navigating, release navigates to the middle
        await N.click("#closeReader"); await N.wait_for_timeout(400)
        await N.set_input_files("#epubInput", str(big)); await N.wait_for_timeout(2500)
        await N.click('#bookGrid .book-card:has-text("کتاب بلند")'); await N.wait_for_timeout(3000)
        check("v8.2: TOC entries pointing into a text/ folder match the spine (first chapter title)", await N.inner_text("#chapterLabel") == "فصل اول", await N.inner_text("#chapterLabel"))
        await poll(lambda: N.evaluate("!document.getElementById('bookSeek').disabled"), 20000)
        before = await visible_text(N)
        await N.evaluate("()=>{const s=document.getElementById('bookSeek');s.value='500';s.dispatchEvent(new Event('input',{bubbles:true}))}")
        await N.wait_for_timeout(300)
        pv = await N.evaluate("""()=>{const p=document.getElementById('seekPreview'),r=p.getBoundingClientRect();return {hid:p.hidden,t:p.textContent,
          vis:r.width>0&&r.left>=0&&r.right<=innerWidth&&r.top>=0}}""")
        check("v8.2: dragging shows a preview (chapter + Persian percentage) without navigating",
              not pv["hid"] and pv["vis"] and "٪۵۰" in pv["t"] and "فصل" in pv["t"] and await visible_text(N) == before and await N.inner_text("#chapterLabel") == "فصل اول", str(pv))
        await N.evaluate("()=>document.getElementById('bookSeek').dispatchEvent(new Event('change',{bubbles:true}))")
        await N.wait_for_timeout(1800)
        pct = faint(await N.inner_text("#progressText")); lab = await N.inner_text("#chapterLabel"); sk = await N.evaluate(SEEK)
        check("v8.2: releasing the seek bar at 50% jumps to the middle of the book",
              35 <= pct <= 65 and lab in ("فصل سوم", "بخش الف", "بخش ب", "فصل چهارم") and await visible_text(N) != before and await N.is_hidden("#seekPreview"), f"{pct}% {lab} v={sk['v']} {sk['info']}")
        check("v8.2: info shows the new book percentage", f"٪{str(pct).translate(str.maketrans('0123456789','۰۱۲۳۴۵۶۷۸۹'))} از کتاب" in sk["info"], sk["info"])
        await N.locator("#tocList .toc-link", has_text="بخش ب").click(); await N.wait_for_timeout(1500)
        check("v8.2: a #fragment TOC entry is used for the header once its target is on the page",
              await N.inner_text("#chapterLabel") == "بخش ب" and await N.evaluate(ACTIVE) == [["بخش ب", "true"]], f"{await N.inner_text('#chapterLabel')} {await N.evaluate(ACTIVE)}")
        await N.locator("#tocList .toc-link", has_text="فصل سوم").click(); await N.wait_for_timeout(1500)
        check("v8.2: at the start of the file (before #a's heading) the chapter entry is used",
              await N.inner_text("#chapterLabel") == "فصل سوم" and await N.evaluate(ACTIVE) == [["فصل سوم", "true"]], f"{await N.inner_text('#chapterLabel')} {await N.evaluate(ACTIVE)}")
        await N.focus("#bookSeek"); await N.keyboard.press("End"); await N.wait_for_timeout(2000)
        pct = faint(await N.inner_text("#progressText"))
        check("v8.2: seek bar works from the keyboard (End = end of book)", pct >= 90 and await N.inner_text("#chapterLabel") == "فصل ششم", f"{pct}% {await N.inner_text('#chapterLabel')}")
        check("v8.2: keyboard on the seek bar does not turn pages by itself", await N.evaluate("document.activeElement.id") == "bookSeek")
        box = await N.locator("#viewer").bounding_box()
        await N.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2); await N.wait_for_timeout(800)
        check("v8.2: seek bar is hidden in immersive mode", await N.is_visible("#reader") and not await N.is_visible("#bookSeek") and not await N.is_visible(".page-nav"))
        await N.keyboard.press("Escape"); await N.wait_for_timeout(600)
        check("v8.2: seek bar comes back after immersive mode", await N.is_visible("#bookSeek"))
        for theme in ("sepia", "dark", "black"):
            await open_settings(N); await N.click(f'[data-theme="{theme}"]'); await N.wait_for_timeout(300)
        await N.keyboard.press("Escape"); await N.wait_for_timeout(300)
        await open_settings(N); await N.click('[data-theme="light"]'); await N.keyboard.press("Escape")
        # The same book on a phone: the bar fits, no horizontal scroll, the TOC drawer scrolls to the active entry
        await N.set_viewport_size({"width": 390, "height": 844}); await N.wait_for_timeout(900)
        mob = await N.evaluate("""()=>{const w=document.querySelector('.reader-seek').getBoundingClientRect(),s=document.getElementById('bookSeek').getBoundingClientRect(),
          i=document.getElementById('seekInfo'),ir=i.getBoundingClientRect(),v=document.getElementById('viewer').getBoundingClientRect();
          return {sw:document.documentElement.scrollWidth,rw:document.getElementById('reader').scrollWidth,iw:innerWidth,l:w.left,r:w.right,sl:s.left,sr:s.right,sh:s.height,
            il:ir.left,ir:ir.right,cut:i.scrollWidth>i.clientWidth+1,below:s.top>=v.bottom-1,vh:v.height}}""")
        check("v8.2 mobile: seek bar visible and inside the screen, no horizontal overflow",
              await N.is_visible("#bookSeek") and mob["sw"] <= mob["iw"] and mob["rw"] <= mob["iw"] and mob["l"] >= 0 and mob["r"] <= 390 and mob["sl"] >= 0 and mob["sr"] <= 390
              and mob["sr"] - mob["sl"] > 250 and mob["sh"] >= 20 and mob["below"] and mob["vh"] > 400, str(mob))
        check("v8.2 mobile: book progress text fits without being cut", not mob["cut"] and mob["il"] >= 0 and mob["ir"] <= 390, str(mob))
        await N.click("#tocToggle"); await N.wait_for_timeout(500)
        act = await N.evaluate("""()=>{const a=document.querySelector('#tocList .toc-link.active'),p=document.getElementById('tocPanel');if(!a)return null;
          const r=a.getBoundingClientRect(),q=p.getBoundingClientRect();return {t:a.textContent.trim(),inView:r.top>=q.top&&r.bottom<=q.bottom&&r.left>=0&&r.right<=391,focus:document.activeElement===a}}""")
        check("v8.2 mobile: opening the TOC shows (and focuses) the active entry", act and act["t"] == "فصل ششم" and act["inView"] and act["focus"], str(act))
        await N.keyboard.press("Escape"); await N.wait_for_timeout(400)
        check("v8.2: no requests outside the site", len(extN) == 0, str(extN[:3]))
        check("v8.2: no JS errors (Persian books)", len(errsN) == 0, str(errsN[:3]))
        await ctxN.close()
        # English book with a nested NCX and a long article title: LTR seek bar, English title in the header with an ellipsis
        en2 = tmpn / "english-nav.epub"
        long_t = "Second article with a remarkably long headline that cannot possibly fit in the reader header"
        with zipfile.ZipFile(en2, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">en-nav-1</dc:identifier><dc:title>English Navigator</dc:title><dc:language>en</dc:language></metadata><manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="a" href="a.html" media-type="application/xhtml+xml"/><item id="b" href="b.html" media-type="application/xhtml+xml"/></manifest><spine toc="ncx"><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr("toc.ncx", f'<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>English Navigator</text></docTitle><navMap><navPoint id="n1" playOrder="1"><navLabel><text>Leaders</text></navLabel><content src="a.html"/><navPoint id="n2" playOrder="2"><navLabel><text>{long_t}</text></navLabel><content src="b.html"/></navPoint></navPoint></navMap></ncx>')
            for f, h in (("a.html", "Leaders"), ("b.html", "Second article")):
                body = "".join(f"<p>This is paragraph {k} of the section called {h}, written in plain English.</p>" for k in range(60))
                z.writestr(f, f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{h}</title></head><body><h1>{h}</h1>{body}</body></html>')
        errsE, extE = [], []
        ctxE, E_ = await new_ctx(b, url, extE, errsE, is_mobile=True, has_touch=True)
        await E_.set_viewport_size({"width": 390, "height": 844})
        await E_.goto(url); await E_.wait_for_timeout(1000)
        await E_.set_input_files("#epubInput", str(en2)); await E_.wait_for_timeout(2500)
        await E_.click('#bookGrid .book-card:has-text("English Navigator")'); await E_.wait_for_timeout(3000)
        check("v8.2 English: header shows the English chapter title", await E_.inner_text("#chapterLabel") == "Leaders", await E_.inner_text("#chapterLabel"))
        await poll(lambda: E_.evaluate("!document.getElementById('bookSeek').disabled"), 15000)
        sk = await E_.evaluate(SEEK)
        check("v8.2 English: seek bar runs left-to-right", sk["dir"] == "ltr" and sk["cdir"] == "ltr" and not sk["dis"], str(sk))
        await E_.click("#tocToggle"); await E_.wait_for_timeout(500)
        await E_.locator("#tocList .toc-sub").click(); await E_.wait_for_timeout(2000)
        # v8.6: on phones the chapter title is the toolbar's second line; the page-nav row is only kept for screen readers.
        lg = await E_.evaluate("""()=>{const l=document.getElementById('readerChapter'),t=document.querySelector('.reader-toolbar'),n=document.querySelector('.page-nav');
          const r=l.getBoundingClientRect(),q=t.getBoundingClientRect(),nb=n.getBoundingClientRect(),ti=document.getElementById('readerTitle').getBoundingClientRect();
          return {text:l.textContent,dir:l.getAttribute('dir'),inside:r.left>=q.left-1&&r.right<=q.right+1&&r.top>=q.top&&r.bottom<=q.bottom+1&&r.height>0,
            below:r.top>=ti.bottom-2,cut:l.scrollWidth>l.clientWidth+1,ov:getComputedStyle(l).textOverflow,navH:nb.height,
            navBtns:!!document.getElementById('prevPage')&&!!document.getElementById('nextPage')}}""")
        check("v8.6 English (phone): nested TOC jump shows the long article title under the book title in the toolbar, cut with an ellipsis",
              await E_.inner_text("#chapterLabel") == long_t and lg["text"] == long_t and lg["inside"] and lg["below"] and lg["cut"] and lg["ov"] == "ellipsis" and lg["dir"] == "auto", str(lg))
        check("v8.6 phone: prev/next row gives its height to the book (kept in the DOM)", lg["navH"] <= 1 and lg["navBtns"], str(lg))
        tb = await E_.evaluate("""()=>{const c=document.getElementById('closeReader').getBoundingClientRect(),h=document.querySelector('.reader-heading').getBoundingClientRect(),
          bm=document.getElementById('bookmarkButton'),br=bm.getBoundingClientRect();
          const btns=[...document.querySelectorAll('.reader-toolbar>button,.reader-tools>button')].filter(b=>b.offsetParent).map(b=>{const r=b.getBoundingClientRect();return Math.min(r.width,r.height)});
          return {cw:c.width,ch:c.height,label:document.getElementById('closeReader').getAttribute('aria-label'),hw:h.width,minBtn:Math.min(...btns),
            bw:br.width,bh:br.height,bmName:bm.textContent.trim(),pressed:bm.getAttribute('aria-pressed')}}""")
        check("v8.6 phone: back is an icon button (44 px, labelled), the title gets the space, toolbar targets ≥ 44 px",
              tb["cw"] <= 48 and tb["ch"] >= 44 and tb["label"] == "بازگشت به کتابخانه" and tb["hw"] >= 90 and tb["minBtn"] >= 44, str(tb))
        check("v8.6 phone: bookmark is a 44 px star button with an accessible name and aria-pressed",
              tb["bw"] >= 44 and tb["bh"] >= 44 and "نشانک" in tb["bmName"] and tb["pressed"] in ("true", "false"), str(tb))
        check("v8.2 English: active TOC entry is the nested article", await E_.evaluate(ACTIVE) == [[long_t, "true"]], str(await E_.evaluate(ACTIVE)))
        check("v8.2 English mobile: no horizontal overflow", await E_.evaluate("document.documentElement.scrollWidth<=innerWidth"))
        check("v8.2 English: no JS errors", len(errsE) == 0, str(errsE[:3]))
        await ctxE.close(); shutil.rmtree(tmpn, ignore_errors=True)
        # 16. v8.2 help: first-run gesture guide and «برگشت به صفحهٔ قبل» chip after jumps
        tmpd = Path(tempfile.mkdtemp(prefix="borg-e2e-help-"))
        helpbook = make_epub(tmpd / "help.epub", "کتاب راهنما", "test-help", "نویسنده", paras=300)
        errsJ, extJ = [], []
        ctxJ, J = await new_ctx(b, url, extJ, errsJ, show_guide=True)
        await J.goto(url); await J.wait_for_timeout(1200)
        await J.set_input_files("#epubInput", helpbook); await J.wait_for_timeout(2500)
        await J.click("#bookGrid .book-card"); await J.wait_for_timeout(3000)
        GUIDE_GEOM = """()=>{const g=document.getElementById('readerGuide');if(!g||g.hidden)return null;
          const r=g.getBoundingClientRect(),v=document.getElementById('viewer').getBoundingClientRect();
          const hit=document.elementFromPoint(v.left+v.width/2,v.top+v.height/2);
          return {covers:r.left<=v.left+1&&r.right>=v.right-1&&r.top<=v.top+1&&r.bottom>=v.bottom-1,
                  onTop:!!(hit&&g.contains(hit)),focus:document.activeElement&&document.activeElement.id,
                  modal:g.getAttribute('role')==='dialog'&&g.getAttribute('aria-modal')==='true'}}"""
        gg = await J.evaluate(GUIDE_GEOM)
        check("v8.2 guide: shown over the page on the first open", bool(gg) and gg["covers"] and gg["onTop"] and gg["modal"] and await J.is_visible("#readerGuideOk"), str(gg))
        check("v8.2 guide: focus is on «متوجه شدم»", bool(gg) and gg["focus"] == "readerGuideOk", str(gg))
        nxt = await J.inner_text('#readerGuideList [data-guide="next"]') if await J.locator('#readerGuideList [data-guide="next"]').count() else ""
        prv = await J.inner_text('#readerGuideList [data-guide="prev"]') if await J.locator('#readerGuideList [data-guide="prev"]').count() else ""
        edges = await J.evaluate("""()=>['readerGuideLeft','readerGuideRight'].map(id=>{const e=document.getElementById(id);const r=e.getBoundingClientRect();return [e.textContent,Math.round(r.width)]})""")
        check("v8.2 guide: RTL book — left edge is next, right edge is previous",
              "چپ" in nxt and "صفحهٔ بعد" in nxt and "راست" in prv and "صفحهٔ قبل" in prv
              and "بعد" in edges[0][0] and "قبل" in edges[1][0] and edges[0][1] > 50, f"{nxt!r} / {prv!r} / {edges}")
        check("v8.2 guide: mentions full screen, swipe and «Aa»",
              await J.locator('#readerGuideList [data-guide="middle"]').count() == 1 and await J.locator('#readerGuideList [data-guide="swipe"]').count() == 1
              and "Aa" in await J.inner_text("#readerGuideList"))
        await J.click("#readerGuideOk"); await J.wait_for_timeout(300)
        seen = await J.evaluate(f"localStorage.getItem('borg-guide-seen{suffix}')")
        check("v8.2 guide: «متوجه شدم» closes it and remembers it", not await J.is_visible("#readerGuide") and seen == "1", str(seen))
        await J.click("#closeReader"); await J.wait_for_timeout(800)
        await J.click("#bookGrid .book-card"); await J.wait_for_timeout(2500)
        check("v8.2 guide: not shown again when the book is reopened", await J.is_visible("#viewer") and not await J.is_visible("#readerGuide"))
        await J.reload(); await J.wait_for_timeout(1200)
        await J.click("#bookGrid .book-card"); await J.wait_for_timeout(2500)
        check("v8.2 guide: not shown again after a reload", await J.is_visible("#viewer") and not await J.is_visible("#readerGuide"))
        await open_settings(J)
        check("v8.2 guide: settings has «نمایش راهنمای ورق زدن»", await J.is_visible("#showGuide"))
        await J.click("#showGuide"); await J.wait_for_timeout(300)
        gg2 = await J.evaluate(GUIDE_GEOM)
        check("v8.2 guide: can be reopened from settings", bool(gg2) and gg2["covers"] and gg2["onTop"] and not await J.is_visible("#settingsPanel"), str(gg2))
        await J.keyboard.press("Escape"); await J.wait_for_timeout(300)
        check("v8.2 guide: Escape closes it and keeps the reader open", not await J.is_visible("#readerGuide") and await J.is_visible("#viewer"))
        # Back chip
        for _ in range(2):
            await J.click("#nextPage"); await J.wait_for_timeout(900)
        t0 = await visible_text(J)
        check("v8.2 back chip: hidden before any jump", not await J.is_visible("#backChip"))
        await J.locator("#tocList >> text=فصل دوم").first.click(); await J.wait_for_timeout(1800)
        t1 = await visible_text(J)
        CHIP_GEOM = """()=>{const c=document.getElementById('backChip');if(!c||c.hidden)return null;const r=c.getBoundingClientRect();
          const v=document.getElementById('viewer').getBoundingClientRect(),bb=document.querySelector('.reader-bottom').getBoundingClientRect();
          const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
          return {w:Math.round(r.width),h:Math.round(r.height),inMiddle:r.left>=v.left+v.width*0.25-1&&r.right<=v.right-v.width*0.25+1,
                  centered:Math.abs((r.left+r.right)/2-(v.left+v.right)/2)<4,aboveBar:bb.height===0||r.bottom<=bb.top+1,
                  inView:r.top>=v.top&&r.bottom<=v.bottom+1,onTop:hit===c||c.contains(hit),text:c.textContent}}"""
        cg = await J.evaluate(CHIP_GEOM)
        check("v8.2 back chip: visible after a TOC jump, centered above the bottom bar", bool(cg) and cg["w"] > 60 and cg["h"] >= 30 and cg["centered"] and cg["aboveBar"] and cg["inView"] and cg["onTop"] and "برگشت" in cg["text"], f"{cg} {t0!r}→{t1!r}")
        check("v8.2 back chip: stays out of the edge tap zones", bool(cg) and cg["inMiddle"], str(cg))
        check("v8.2 guide: not shown on a chapter load", not await J.is_visible("#readerGuide"))
        await J.set_viewport_size({"width": 390, "height": 844}); await J.wait_for_timeout(1200)
        cgm = await J.evaluate(CHIP_GEOM)
        check("v8.2 back chip (phone): compact, in the middle, above the bottom bar", bool(cgm) and cgm["inMiddle"] and cgm["aboveBar"] and cgm["onTop"] and cgm["h"] < 60, str(cgm))
        await J.set_viewport_size({"width": 1280, "height": 800}); await J.wait_for_timeout(1200)
        await J.click("#backChip"); await J.wait_for_timeout(1800)
        t2 = await visible_text(J)
        check("v8.2 back chip: returns to the page before the jump and hides", t0 and t2 == t0 and t1 != t0 and not await J.is_visible("#backChip"), f"{t0!r} / {t1!r} / {t2!r}")
        await J.locator("#tocList >> text=فصل دوم").first.click(); await J.wait_for_timeout(1800)
        shown_again = await J.is_visible("#backChip")
        states = []
        for _ in range(6):
            await J.click("#nextPage"); await J.wait_for_timeout(900)
            states.append(await J.is_visible("#backChip"))
        check("v8.2 back chip: hides after turning 6 pages away from the jump", shown_again and states[0] and not states[-1], str(states))
        check("v8.2 help: no JS errors, no external requests", len(errsJ) == 0 and len(extJ) == 0, str(errsJ[:3] + extJ[:3]))
        await ctxJ.close(); shutil.rmtree(tmpd, ignore_errors=True)

        # 17. v8.3 text cleanup: rules, search preview, apply to the EPUB file, re-anchored highlight, auto-clean on import
        import zipfile, json as _json
        tmpd = Path(tempfile.mkdtemp(prefix="borg-e2e-clean-"))
        def clean_epub(path, title, ident, special):
            with zipfile.ZipFile(path, "w") as z:
                z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                D = zipfile.ZIP_DEFLATED
                z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', D)
                z.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">{ident}</dc:identifier><dc:title>{title}</dc:title><dc:creator>نویسنده</dc:creator><dc:language>fa</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="text/c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine page-progression-direction="rtl"><itemref idref="c1"/></spine></package>', D)
                z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol><li><a href="text/c1.xhtml">فصل اول</a></li></ol></nav></body></html>', D)
                normal = "".join(f"<p>این پاراگراف شماره {k} از فصل اول در کتاب {title} است.</p>" for k in range(120))
                z.writestr("OEBPS/text/c1.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل اول</title></head><body><h1>فصل اول</h1>{special}{normal}</body></html>', D)
            return str(path)
        SPECIAL1 = ('<p id="p1">کانال ما: https://t.me/testchannel متن عادی بعد از لینک برای هایلایت است.</p>'
                    '<p>برای پرسش‌ها @test_channel را دنبال کنید یا به info@example.com بنویسید.</p>'
                    '<p>لینک: <a href="https://t.me/x">https://t.me/x</a> پایان پاراگراف.</p>')
        book1 = clean_epub(tmpd / "clean1.epub", "کتاب پاک‌سازی", "test-clean-1", SPECIAL1)
        book2 = clean_epub(tmpd / "clean2.epub", "کتاب دوم پاک‌سازی", "test-clean-2",
                           '<p>عضو شوید: https://t.me/second_chan و همچنین @second_chan جملهٔ سالم.</p>')
        errsK, extK = [], []
        ctxK, K = await new_ctx(b, url, extK, errsK)
        await K.goto(url); await K.wait_for_timeout(1200)
        await K.set_input_files("#epubInput", book1); await K.wait_for_timeout(2500)
        check("v8.3 clean: no rules → import toast has no removal count", "حذف شد" not in await K.inner_text("#toast"))
        await K.click("#bookGrid .book-card"); await K.wait_for_timeout(3000)
        HL = "متن عادی بعد از لینک"
        await K.evaluate("""(q)=>{const d=document.querySelector('#viewer iframe').contentDocument;const p=d.getElementById('p1');
            const t=p.firstChild;const i=t.nodeValue.indexOf(q);const r=d.createRange();r.setStart(t,i);r.setEnd(t,i+q.length);
            const s=d.getSelection();s.removeAllRanges();s.addRange(r);d.dispatchEvent(new Event('selectionchange'));}""", HL)
        await K.wait_for_timeout(800)
        await K.click('[data-hl="yellow"]'); await K.wait_for_timeout(800)
        anns0 = [a for a in await K.evaluate(IDB_JS, ["annotations"]) if a["type"] == "highlight"]
        check("v8.3 clean: highlight after the phrase saved", len(anns0) == 1 and anns0[0]["quote"] == HL, str(anns0))
        await K.click("#closeReader"); await K.wait_for_timeout(800)
        books0 = await K.evaluate(IDB_JS, ["books"]); prog0 = await K.evaluate(IDB_JS, ["progress"])
        # Dialog from the library toolbar
        check("v8.3 clean: «پاک‌سازی متن» button visible in the library toolbar", await K.is_visible("#cleanOpen"))
        await K.click("#cleanOpen"); await K.wait_for_timeout(300)
        DLG = """()=>{const d=document.getElementById('cleanDialog');const r=d.getBoundingClientRect();
          const hit=document.elementFromPoint(r.left+r.width/2,r.top+30);
          return {open:d.open,inView:r.top>=0&&r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight,w:Math.round(r.width),onTop:!!(hit&&d.contains(hit))}}"""
        dg = await K.evaluate(DLG)
        check("v8.3 clean: dialog opens in the viewport, on top", dg["open"] and dg["inView"] and dg["onTop"] and dg["w"] > 300, str(dg))
        await K.fill("#cleanPhrase", "ab@"); await K.click("#cleanAdd"); await K.wait_for_timeout(200)
        rules = await K.evaluate(f"localStorage.getItem('borg-clean-rules{suffix}')")
        check("v8.3 clean: rule shorter than 4 characters is rejected with a visible message",
              await K.is_visible("#cleanError") and "۴" in await K.inner_text("#cleanError") and not rules, str(rules))
        await K.click("#cleanPresetTelegram"); await K.wait_for_timeout(200)
        rules = _json.loads(await K.evaluate(f"localStorage.getItem('borg-clean-rules{suffix}')") or "[]")
        check("v8.3 clean: Telegram preset added and stored",
              [r["type"] for r in rules] == ["telegram"] and "لینک‌های تلگرام" in await K.inner_text("#cleanRules") and await K.is_disabled("#cleanPresetTelegram"), str(rules))
        await K.fill("#cleanPhrase", "این پاراگراف شماره"); await K.click("#cleanAdd"); await K.wait_for_timeout(200)
        await K.click("#cleanSearch")
        await poll(lambda: K.is_visible("#cleanApply"), 15000)
        bk = K.locator(".clean-book")
        cnt = await bk.first.locator(".clean-count").inner_text() if await bk.count() else ""
        snip = await bk.first.locator(".clean-snips").inner_text() if await bk.count() else ""
        marks = await K.locator(".clean-snips mark").all_inner_texts()
        check("v8.3 clean: search shows the book with the right count (3)", await bk.count() == 1 and "۳ مورد" in cnt and "کتاب پاک‌سازی" in await bk.first.inner_text(), cnt)
        check("v8.3 clean: preview snippets highlight the matches, e-mail untouched",
              "https://t.me/testchannel" in marks and "@test_channel" in marks and "https://t.me/x" in marks and not any("example" in m for m in marks)
              and await K.locator(".clean-snips mark").first.is_visible() and "کانال ما" in snip, str(marks))
        broad = K.locator(".clean-broad")
        check("v8.3 clean: a rule matching too much is flagged «احتمالاً بیش از حد کلی است» and excluded by default",
              await broad.count() == 1 and await broad.first.is_visible() and "احتمالاً بیش از حد کلی است" in await broad.first.inner_text()
              and "این پاراگراف شماره" in await broad.first.inner_text() and not await K.is_checked("[data-clean-broad]") and "این پاراگراف" not in "".join(marks), str(marks))
        check("v8.3 clean: per-book checkbox checked by default", await K.is_checked("[data-clean-book]"))
        await K.click("#cleanApply")
        await poll(lambda: _contains(K, "#cleanStatus", "حذف شد"), 20000)
        toast = await K.inner_text("#toast")
        check("v8.3 clean: apply toasts «۳ مورد حذف شد»", "۳ مورد حذف شد" in toast, toast)
        await K.click("#cleanClose"); await K.wait_for_timeout(300)
        books1 = await K.evaluate(IDB_JS, ["books"]); prog1 = await K.evaluate(IDB_JS, ["progress"])
        anns1 = [a for a in await K.evaluate(IDB_JS, ["annotations"]) if a["type"] == "highlight"]
        check("v8.3 clean: same book record kept (id/title), contentHash changed, locations cleared",
              len(books1) == 1 and books1[0]["id"] == books0[0]["id"] and books1[0]["title"] == books0[0]["title"]
              and books1[0]["contentHash"] != books0[0]["contentHash"] and len(books1[0]["contentHash"] or "") == 64
              and "locations" not in (await K.evaluate("""()=>new Promise(res=>{const r=indexedDB.open('ketabkhane-db');r.onsuccess=()=>{const q=r.result.transaction('books').objectStore('books').getAll();q.onsuccess=()=>res(Object.keys(q.result[0]))}})""")), str(books1)[:200])
        check("v8.3 clean: progress kept", len(prog1) == 1 and prog1[0]["cfi"] == prog0[0]["cfi"], f"{prog0} {prog1}")
        check("v8.3 clean: highlight re-anchored (new CFI, same quote, marked for sync)",
              len(anns1) == 1 and anns1[0]["cfi"] != anns0[0]["cfi"] and anns1[0]["quote"] == HL and anns1[0]["updatedAt"] > anns0[0]["updatedAt"] and anns1[0]["dirty"], f"{anns0} {anns1}")
        check("v8.3 clean: book still in the library", await K.locator("#bookGrid .book-card").count() == 1 and await K.locator("#bookGrid .book-card").first.is_visible())
        await K.reload(); await K.wait_for_timeout(1200)
        await K.click("#bookGrid .book-card"); await K.wait_for_timeout(3000)
        txt = await K.evaluate("()=>document.querySelector('#viewer iframe').contentDocument.body.textContent")
        check("v8.3 clean: phrases gone from the rendered text",
              "t.me" not in txt and "@test_channel" not in txt and await K.evaluate("()=>document.querySelector('#viewer iframe').contentDocument.querySelectorAll('a[href*=\"t.me\"]').length") == 0, txt[:200])
        check("v8.3 clean: normal text intact", "کانال ما:" in txt and HL in txt and "info@example.com" in txt and "پایان پاراگراف." in txt and "این پاراگراف شماره 5 از فصل اول" in txt)
        HLGEOM = """([cfi,q])=>{const f=document.querySelector('#viewer iframe');const d=f.contentDocument;let txt=null;
          try{txt=new ePub.CFI(cfi).toRange(d).toString()}catch(e){txt='ERR '+e}
          const p=d.getElementById('p1').firstChild;const i=p.nodeValue.indexOf(q);const r=d.createRange();r.setStart(p,i);r.setEnd(p,i+q.length);
          const fr=f.getBoundingClientRect();const tr=r.getBoundingClientRect();
          const hs=[...document.querySelectorAll('#viewer svg .borg-hl rect, #viewer svg g.borg-hl rect')].map(x=>x.getBoundingClientRect()).filter(x=>x.width>0);
          const cx=fr.left+(tr.left+tr.right)/2, cy=fr.top+(tr.top+tr.bottom)/2;
          return {txt,n:hs.length,over:hs.some(h=>cx>=h.left-2&&cx<=h.right+2&&cy>=h.top-2&&cy<=h.bottom+2)}}"""
        hg = await K.evaluate(HLGEOM, [anns1[0]["cfi"] if anns1 else "", HL])
        check("v8.3 clean: highlight points at the same quote text and is drawn over it", hg["txt"] == HL and hg["n"] >= 1 and hg["over"], str(hg))
        await K.click("#closeReader"); await K.wait_for_timeout(800)
        # Auto-clean on import with the rule present
        await K.set_input_files("#epubInput", book2)
        await poll(lambda: _contains(K, "#toast", "حذف شد"), 15000)
        toast2 = await K.inner_text("#toast")
        check("v8.3 clean: importing with a rule present toasts «۲ مورد حذف شد»", "۲ مورد حذف شد" in toast2, toast2)
        await K.wait_for_timeout(800)
        await K.locator("#bookGrid .book-card", has_text="کتاب دوم").click(); await K.wait_for_timeout(3000)
        txt2 = await K.evaluate("()=>document.querySelector('#viewer iframe').contentDocument.body.textContent")
        check("v8.3 clean: imported book text is cleaned; the too-broad rule was skipped", "t.me" not in txt2 and "@second_chan" not in txt2 and "جملهٔ سالم" in txt2 and "عضو شوید:" in txt2
              and "این پاراگراف شماره 7 از فصل اول" in txt2, txt2[:120])
        await K.click("#closeReader"); await K.wait_for_timeout(500)
        await K.set_input_files("#epubInput", book2); await K.wait_for_timeout(2500)
        check("v8.3 clean: re-importing the original file is still detected as a duplicate", "قبلاً" in await K.inner_text("#toast") and await K.locator("#bookGrid .book-card").count() == 2)
        check("v8.3 clean: no JS errors, no external requests", len(errsK) == 0 and len(extK) == 0, str(errsK[:3] + extK[:3]))
        await ctxK.close(); shutil.rmtree(tmpd, ignore_errors=True)

        # 18. v8.4 TOC hrefs that go up a folder (nav in nav/, chapters in text/): epub.js said "No Section Found"
        import zipfile
        tmpt = Path(tempfile.mkdtemp(prefix="borg-e2e-toc-"))
        upbook = tmpt / "up.epub"
        names = ["اول", "دوم", "سوم"]
        with zipfile.ZipFile(upbook, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            D = zipfile.ZIP_DEFLATED
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', D)
            items = "".join(f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(1, 4))
            refs = "".join(f'<itemref idref="c{i}"/>' for i in range(1, 4))
            z.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">toc-up-1</dc:identifier><dc:title>کتاب پوشه‌دار</dc:title><dc:creator>نویسنده</dc:creator><dc:language>fa</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{items}</manifest><spine page-progression-direction="rtl">{refs}</spine></package>', D)
            z.writestr("OEBPS/nav/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol>'
                       '<li><a href="../text/c1.xhtml">فصل اول</a></li><li><a href="../text/c2.xhtml">فصل دوم</a><ol><li><a href="../text/c3.xhtml">فصل سوم</a></li>'
                       '<li><a href="../text/c3.xhtml#deep">بخش ژرف</a></li></ol></li></ol></nav></body></html>', D)
            for i, n in enumerate(names, 1):
                ps = [f"<p>پاراگراف {k} از فصل {n}: متنی برای آزمایش پیوندهای فهرست در پوشه‌ای دیگر.</p>" for k in range(60)]
                if i == 3: ps.insert(40, '<h2 id="deep">بخش ژرف</h2>')
                z.writestr(f"OEBPS/text/c{i}.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل {n}</title></head><body><h1>فصل {n}</h1>{"".join(ps)}</body></html>', D)
        # Same layout for an EPUB2 book: the NCX lives in toc/ and points at ../text/
        upen = tmpt / "up-en.epub"
        with zipfile.ZipFile(upen, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("OPS/book.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">toc-up-en</dc:identifier><dc:title>Folder Atlas</dc:title><dc:language>en</dc:language></metadata><manifest><item id="ncx" href="toc/toc.ncx" media-type="application/x-dtbncx+xml"/><item id="a" href="text/a.html" media-type="application/xhtml+xml"/><item id="b" href="text/b.html" media-type="application/xhtml+xml"/></manifest><spine toc="ncx"><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr("OPS/toc/toc.ncx", '<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>Folder Atlas</text></docTitle><navMap><navPoint id="n1" playOrder="1"><navLabel><text>Opening</text></navLabel><content src="../text/a.html"/><navPoint id="n2" playOrder="2"><navLabel><text>Far Article</text></navLabel><content src="../text/b.html"/></navPoint></navPoint></navMap></ncx>')
            for f, h in (("a.html", "Opening"), ("b.html", "Far Article")):
                body = "".join(f"<p>This is paragraph {k} of the part called {h}, kept in a text folder.</p>" for k in range(50))
                z.writestr(f"OPS/text/{f}", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{h}</title></head><body><h1>{h}</h1>{body}</body></html>')
        H_IN_VIEW = """(t)=>{const f=document.querySelector('#viewer iframe');if(!f)return false;const r=f.getBoundingClientRect();
          const box=document.querySelector('#viewer').getBoundingClientRect();const h=[...f.contentDocument.querySelectorAll('h1,h2')].find(x=>x.textContent.trim()===t);
          if(!h)return false;const x=h.getBoundingClientRect();return x.width>0&&x.right+r.left>box.left&&x.left+r.left<box.right&&x.bottom+r.top>box.top&&x.top+r.top<box.bottom}"""
        errsT, extT = [], []
        ctxT, T = await new_ctx(b, url, extT, errsT)
        await T.goto(url); await T.wait_for_timeout(1000)
        await T.set_input_files("#epubInput", str(upbook)); await T.wait_for_timeout(2500)
        await T.click('#bookGrid .book-card:has-text("کتاب پوشه‌دار")'); await T.wait_for_timeout(3000)
        hrefs = await T.evaluate("()=>[...document.querySelectorAll('#tocList .toc-link')].map(b=>b.dataset.href)")
        check("v8.4 TOC: entries from nav/nav.xhtml resolve to the spine hrefs (text/…, #fragment kept)",
              hrefs == ["text/c1.xhtml", "text/c2.xhtml", "text/c3.xhtml", "text/c3.xhtml#deep"], str(hrefs))
        for label, heading in (("فصل دوم", "فصل دوم"), ("فصل سوم", "فصل سوم"), ("بخش ژرف", "بخش ژرف"), ("فصل اول", "فصل اول")):
            await T.locator("#tocList .toc-link", has_text=label).first.click(); await T.wait_for_timeout(1800)
            check(f"v8.4 TOC: «{label}» (../text/ href) opens its chapter, heading inside the viewer, title in the header",
                  await T.evaluate(H_IN_VIEW, heading) and await T.inner_text("#chapterLabel") == label, await T.inner_text("#chapterLabel"))
        check("v8.4 TOC: no «No Section Found» / JS errors (nav document)", len(errsT) == 0, str(errsT[:3]))
        await T.click("#closeReader"); await T.wait_for_timeout(500)
        await T.set_input_files("#epubInput", str(upen)); await T.wait_for_timeout(2500)
        await T.click('#bookGrid .book-card:has-text("Folder Atlas")'); await T.wait_for_timeout(3000)
        await T.locator("#tocList .toc-sub", has_text="Far Article").click(); await T.wait_for_timeout(1800)
        check("v8.4 TOC: nested NCX entry (toc/toc.ncx → ../text/b.html) opens its article with the title in the header",
              await T.evaluate(H_IN_VIEW, "Far Article") and await T.inner_text("#chapterLabel") == "Far Article", await T.inner_text("#chapterLabel"))
        check("v8.4 TOC: no JS errors, no external requests", len(errsT) == 0 and len(extT) == 0, str(errsT[:3] + extT[:3]))
        await ctxT.close(); shutil.rmtree(tmpt, ignore_errors=True)
        # 19. v8.4 error reports: client errors are queued, batched to the API and listed for the admin (admin from scenario 10)
        QKEY = "borg-error-queue" + suffix
        QUEUE_JS = "([k]) => JSON.parse(localStorage.getItem(k) || '[]')"
        HIDE_JS = "() => { Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true }); document.dispatchEvent(new Event('visibilitychange')); }"
        extX, errsX = [], []
        ctxX, X = await new_ctx(b, url, extX, errsX)
        await X.goto(url); await X.wait_for_timeout(1000)
        for _ in range(2):
            await X.evaluate("() => { setTimeout(() => { throw new Error('borg-e2e-error 19 for mail@example.com') }, 0); }")
        await X.evaluate("() => { Promise.reject(new Error('borg-e2e-rejection 19')); }")
        await X.wait_for_timeout(400)
        q = await X.evaluate(QUEUE_JS, [QKEY])
        qe = [x for x in q if "borg-e2e-error 19" in x["message"]]
        check("v8.4 errors: uncaught error queued once with count 2 (client dedupe), e-mail scrubbed",
              len(qe) == 1 and qe[0]["count"] == 2 and qe[0]["kind"] == "error" and "mail@example.com" not in qe[0]["message"]
              and qe[0]["app_version"] and qe[0]["path"] == "/" + url.split("/", 3)[3] and "borg-e2e-error 19" in qe[0]["stack"], str(q)[:300])
        check("v8.4 errors: unhandled rejection queued", any(x["kind"] == "rejection" and "borg-e2e-rejection 19" in x["message"] for x in q), str(q)[:300])
        await X.evaluate(HIDE_JS)
        try:
            await poll(lambda: X.evaluate("([k]) => !localStorage.getItem(k)", [QKEY]), 8000); sent = True
        except Exception:
            sent = False
        check("v8.4 errors: queue sent and cleared when the page is hidden (anonymous visitor)", sent)
        check("v8.4 errors: only the triggered errors, no external requests", len(extX) == 0
              and all("borg-e2e" in e for e in errsX), str(errsX[:3] + extX[:3]))
        await ctxX.close()
        extY, errsY = [], []
        ctxY, Y = await new_ctx(b, url, extY, errsY)
        Y.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await Y.goto(url); await Y.wait_for_timeout(800)
        await auth(Y, "login", email="admin@example.com", password="password1"); await wait_synced(Y)
        await Y.click('[data-view="admin"]')
        await poll(lambda: _count_gt(Y, "#adminStats .stat", 0), 8000)
        stats_ok = await Y.locator("#adminStats .stat-errors").is_visible() and "خطای کاربران" in await Y.inner_text("#adminStats")
        await Y.click('[data-admin-tab="errors"]')  # v8.9: admin sub-tabs
        await poll(lambda: _count_gt(Y, "#errorsList .err-item", 0), 8000)
        item = Y.locator('#errorsList .err-item:has-text("borg-e2e-error 19")')
        check("v8.4 errors: admin sees the error with its count and version", await item.count() == 1 and await item.is_visible()
              and "× ۲" in await item.inner_text() and await item.locator(".cat-chip").count() == 1, (await Y.inner_text("#errorsList"))[:300])
        check("v8.4 errors: error badge in the admin stats and on the «خطاها» tab", stats_ok and await Y.is_visible("#admBadgeErrors")
              and int((await Y.inner_text("#admBadgeErrors")).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))) >= 2)
        await item.locator("summary").click(); await Y.wait_for_timeout(200)
        check("v8.4 errors: details expand to stack / user agent", await item.locator(".err-meta").is_visible()
              and "مرورگر" in await item.locator(".err-meta").inner_text() and await item.locator(".err-stack").is_visible(), await item.inner_text())
        await item.locator("[data-err-clear]").click()
        async def _gone(): return await Y.locator('#errorsList .err-item:has-text("borg-e2e-error 19")').count() == 0
        await poll(_gone, 6000)
        check("v8.4 errors: clear one removes it, others stay", await Y.locator('#errorsList .err-item:has-text("borg-e2e-error 19")').count() == 0
              and await Y.locator('#errorsList .err-item:has-text("borg-e2e-rejection 19")').count() == 1)
        # A logged-in user's report goes through the CSRF-protected path
        await Y.evaluate("() => { setTimeout(() => { throw new Error('borg-e2e-admin-error 19') }, 0); }")
        await Y.wait_for_timeout(300); await Y.evaluate(HIDE_JS)
        await poll(lambda: Y.evaluate("([k]) => !localStorage.getItem(k)", [QKEY]), 8000)
        await Y.click("#errorsRefresh")
        await poll(lambda: _count_gt(Y, '#errorsList .err-item:has-text("borg-e2e-admin-error 19")', 0), 6000)
        adm = Y.locator('#errorsList .err-item:has-text("borg-e2e-admin-error 19")')
        await adm.locator("summary").click(); await Y.wait_for_timeout(200)
        check("v8.4 errors: logged-in report (CSRF) listed as from a user", "۱ کاربر" in await adm.inner_text(), await adm.inner_text())
        await Y.click("#errorsClearAll")
        await poll(lambda: _contains(Y, "#errorsList", "خطایی ثبت نشده است"), 6000)
        check("v8.4 errors: clear all empties the list", await Y.locator("#errorsList .err-item").count() == 0 and await Y.is_visible("#errorsList"))
        check("v8.4 errors admin: no unexpected JS errors, no external requests", len(extY) == 0 and all("borg-e2e" in e for e in errsY), str(errsY[:3] + extY[:3]))
        await ctxY.close()
        # 21. v8.5 library search: title/author/publisher/shelf, Persian normalisation, count, empty state, clear, "/" and mobile
        import tempfile as _tf21, shutil as _sh21
        tmp21 = Path(_tf21.mkdtemp(prefix="borg-e2e-search-"))
        bk21 = [make_epub(tmp21 / "s1.epub", "سفر به ستاره‌ها", "search-21-a", "مریم کیانی", publisher="نشر چشمه"),
                make_epub(tmp21 / "s2.epub", "باغ انار", "search-21-b", "علی یکتا", publisher="نشر ثالث"),
                make_epub(tmp21 / "s3.epub", "Harbor Lights", "search-21-c", "John Mercer", publisher="Penguin Books", direction="ltr", lang="en")]
        ext21, errs21 = [], []
        ctx21, S = await new_ctx(b, url, ext21, errs21)
        await S.goto(url); await S.wait_for_timeout(800)
        for i, f in enumerate(bk21, 1):
            await S.set_input_files("#epubInput", f)
            await poll(lambda i=i: _count_gt(S, "#bookGrid .book-card", i - 1), 10000)
        async def titles21(): return await S.locator("#bookGrid .book-card h3").all_inner_texts()
        async def search21(q, n):
            await S.fill("#searchInput", q)
            try: await poll(lambda: S.evaluate("(n)=>document.querySelectorAll('#bookGrid .book-card').length===n && !!document.querySelector('#bookGrid .book-card, #bookGrid .lib-search-empty')", n) if n else S.locator("#bookGrid .lib-search-empty").is_visible(), 4000)
            except TimeoutError: pass
            await S.wait_for_timeout(250)  # past the debounce: no later render changes it
            return await titles21()
        check("v8.5 search: placeholder and field in the library toolbar",
              await S.get_attribute("#searchInput", "placeholder") == "جستجو در عنوان، نویسنده، ناشر…" and await S.is_visible("#searchInput")
              and await S.locator("#bookGrid .book-card").count() == 3 and not await S.is_visible("#librarySearchCount"))
        t = await search21("ستاره", 1)
        check("v8.5 search: part of the title finds one book, count «۱ کتاب پیدا شد»", t == ["سفر به ستاره‌ها"]
              and await S.is_visible("#librarySearchCount") and (await S.inner_text("#librarySearchCount")).strip() == "۱ کتاب پیدا شد", str(t))
        check("v8.5 search: matched part highlighted in the title", (await S.locator("#bookGrid .book-card h3 mark").all_inner_texts()) == ["ستاره"]
              and await S.is_visible("#librarySearchClear"))
        t = await search21("ستارهها", 1)
        check("v8.5 search: half-space (ZWNJ) in the title is ignored", t == ["سفر به ستاره‌ها"], str(t))
        t = await search21("كياني", 1)
        check("v8.5 search: author typed with Arabic ك/ي matches Persian ک/ی", t == ["سفر به ستاره‌ها"], str(t))
        t = await search21("يكتا", 1)
        check("v8.5 search: second author with Arabic letters", t == ["باغ انار"], str(t))
        t = await search21("ثالث", 1)
        check("v8.5 search: publisher", t == ["باغ انار"], str(t))
        t = await search21("نشر", 2)
        check("v8.5 search: two matches, count «۲ کتاب پیدا شد»", sorted(t) == sorted(["سفر به ستاره‌ها", "باغ انار"])
              and (await S.inner_text("#librarySearchCount")).strip() == "۲ کتاب پیدا شد", str(t))
        t = await search21("PENGUIN", 1)
        check("v8.5 search: Latin publisher is case-insensitive", t == ["Harbor Lights"], str(t))
        await S.fill("#searchInput", ""); await poll(lambda: S.evaluate("()=>document.querySelectorAll('#bookGrid .book-card').length===3"), 4000)
        await S.click('#bookGrid .book-card:has-text("باغ انار") [data-options]'); await S.wait_for_timeout(300)
        await S.fill("#newShelfName", "کلاسیک"); await S.click("#addShelf"); await S.wait_for_timeout(400)
        await S.click("#bookOptionsClose"); await S.wait_for_timeout(400)
        t = await search21("كلاسيك", 1)
        check("v8.5 search: shelf name", t == ["باغ انار"], str(t))
        t = await search21("نشر", 2)
        await S.click('#libraryFilters button:has-text("کلاسیک")'); await S.wait_for_timeout(400)
        check("v8.5 search: combines with the shelf filter", await titles21() == ["باغ انار"]
              and (await S.inner_text("#librarySearchCount")).strip() == "۱ کتاب پیدا شد", str(await titles21()))
        await S.click('[data-filter="all"]'); await S.wait_for_timeout(300)
        await search21("کتاب ناموجود ۱۲۳", 0)
        check("v8.5 search: empty state «کتابی با این عبارت پیدا نشد» with a clear button, no count",
              await S.is_visible("#bookGrid .lib-search-empty") and "کتابی با این عبارت پیدا نشد" in await S.inner_text("#bookGrid")
              and await S.is_visible("#bookGrid [data-search-clear]") and not await S.is_visible("#librarySearchCount"))
        await S.click("#bookGrid [data-search-clear]"); await S.wait_for_timeout(300)
        check("v8.5 search: clear restores all books", await S.locator("#bookGrid .book-card").count() == 3
              and await S.input_value("#searchInput") == "" and not await S.is_visible("#librarySearchClear") and not await S.is_visible("#librarySearchCount"))
        await search21("باغ", 1); await S.click("#librarySearchClear"); await S.wait_for_timeout(300)
        check("v8.5 search: × button clears", await S.locator("#bookGrid .book-card").count() == 3 and await S.input_value("#searchInput") == "")
        await search21("باغ", 1); await S.press("#searchInput", "Escape"); await S.wait_for_timeout(300)
        check("v8.5 search: Escape in the field clears it", await S.locator("#bookGrid .book-card").count() == 3 and await S.input_value("#searchInput") == "")
        await search21("باغ", 1); await S.reload(); await S.wait_for_timeout(1200)
        check("v8.5 search: query kept in sessionStorage across a reload, not in localStorage",
              await S.input_value("#searchInput") == "باغ" and await titles21() == ["باغ انار"]
              and await S.evaluate("()=>sessionStorage.getItem('borg-library-search')==='باغ' && !JSON.stringify(localStorage).includes('باغ')"))
        await S.fill("#searchInput", ""); await S.wait_for_timeout(400)
        await S.evaluate("()=>document.activeElement && document.activeElement.blur()")
        await S.keyboard.press("/"); await S.wait_for_timeout(150)
        check("v8.5 search: «/» focuses the field (and types nothing)",
              await S.evaluate("()=>document.activeElement && document.activeElement.id==='searchInput'") and await S.input_value("#searchInput") == "")
        await S.click('#bookGrid .book-card:has-text("Harbor Lights")'); await S.wait_for_timeout(2000)
        await S.evaluate("()=>document.activeElement && document.activeElement.blur()")
        await S.keyboard.press("/"); await S.wait_for_timeout(150)
        check("v8.5 search: «/» does nothing while the reader is open",
              await S.evaluate("()=>document.activeElement && document.activeElement.id!=='searchInput'"))
        await S.click("#closeReader"); await S.wait_for_timeout(600)
        await S.set_viewport_size({"width": 390, "height": 844}); await S.wait_for_timeout(400)
        box = await S.locator("#searchInput").bounding_box()
        check("v8.5 search mobile: field visible and wide enough at 390px", box and box["width"] >= 200 and box["x"] >= 0 and box["x"] + box["width"] <= 390, str(box))
        t = await search21("mercer", 1)
        check("v8.5 search mobile: typing filters, clear button inside the box, no horizontal overflow", t == ["Harbor Lights"]
              and await S.evaluate("""()=>{const c=document.querySelector('#librarySearchClear').getBoundingClientRect(),s=document.querySelector('#librarySearchBox').getBoundingClientRect();
                return c.width>0&&c.left>=s.left&&c.right<=s.right&&document.documentElement.scrollWidth<=innerWidth}"""), str(t))
        check("v8.5 search: no JS errors, no external requests", len(errs21) == 0 and len(ext21) == 0, str(errs21[:3] + ext21[:3]))
        await ctx21.close(); _sh21.rmtree(tmp21, ignore_errors=True)

        # 20. v8.5 speed + sync status: opening indicator, cached direction and locations, sync chip
        import tempfile, time, zipfile
        def make_big_epub(path, chapters=40, paras=30):
            """A larger book without page-progression-direction and with a wrong lang tag (forces text detection)."""
            with zipfile.ZipFile(path, "w") as z:
                z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                D = zipfile.ZIP_DEFLATED
                z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>', D)
                man = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(chapters))
                z.writestr("OEBPS/content.opf", f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">big-20</dc:identifier><dc:title>کتاب بزرگ ۲۰</dc:title><dc:creator>آزمون</dc:creator><dc:language>en</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{man}</manifest><spine>{"".join(f"<itemref idref=\"c{i}\"/>" for i in range(chapters))}</spine></package>', D)
                z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol>' + "".join(f'<li><a href="c{i}.xhtml">فصل {i+1}</a></li>' for i in range(chapters)) + '</ol></nav></body></html>', D)
                for i in range(chapters):
                    p = "".join(f"<p>این پاراگراف شماره {k} از فصل {i+1} است و متن آن برای آزمودن سرعت باز شدن کتاب‌های بزرگ کمی بلند نوشته شده است تا صفحه‌ها پر شوند.</p>" for k in range(paras))
                    z.writestr(f"OEBPS/c{i}.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>فصل {i+1}</title></head><body><h1>فصل {i+1}</h1>{p}</body></html>', D)
            return str(path)
        big20 = make_big_epub(Path(tempfile.mkdtemp(prefix="borg-e2e-big-")) / "big-20.epub")
        # The admin exists after section 10; create it when this section runs on its own (no-op otherwise).
        ctxS, S = await new_ctx(b, url, [], [])
        await S.request.post(url + "api/?r=setup", data={"setup_key": SETUP_KEY, "name": "مدیر بُرگ", "email": "admin@example.com", "password": "password1"})
        await ctxS.close()
        extQ, errsQ = [], []
        ctxQ, Q = await new_ctx(b, url, extQ, errsQ)
        await ctxQ.add_init_script("try{localStorage.setItem('borg-debug','{\"stats\":1}')}catch(e){}")
        await Q.goto(url); await Q.wait_for_timeout(800)
        await Q.set_input_files("#epubInput", big20)
        await poll(lambda: _count_gt(Q, "#bookGrid .book-card", 0), 20000); await Q.wait_for_timeout(800)
        WATCH_OPEN_JS = """() => { window.__open = []; const el = document.getElementById('openingIndicator');
          const rec = () => { const r = el.getBoundingClientRect(), f = document.querySelector('#viewer iframe');
            window.__open.push({ hidden: el.hidden, vis: !el.hidden && r.width > 0 && r.height > 0, text: el.textContent,
              role: el.getAttribute('role'), frame: !!f }); };
          new MutationObserver(rec).observe(el, { attributes: true, attributeFilter: ['hidden'] }); }"""
        TEXT_SHOWN_JS = "() => { const f = document.querySelector('#viewer iframe'); return !!f && !!f.contentDocument.body && !f.contentDocument.getElementById('borg-veil'); }"
        async def open_big():
            await Q.evaluate(WATCH_OPEN_JS)
            t0 = time.perf_counter()
            await Q.click("#bookGrid .book-card")
            async def shown(): return bool(await visible_text(Q)) and await Q.evaluate(TEXT_SHOWN_JS)
            await poll(shown, 15000, 50)
            dt = time.perf_counter() - t0
            await poll(lambda: Q.is_hidden("#openingIndicator"), 4000, 50)
            await poll(lambda: Q.is_enabled("#bookSeek"), 20000)
            return dt
        t1 = await open_big()
        rec = await Q.evaluate("() => window.__open")
        check("v8.5 open: «در حال باز کردن کتاب…» shown (visible, role=status) while the book opens",
              any(r["vis"] and "در حال باز کردن کتاب" in r["text"] and r["role"] == "status" for r in rec), str(rec)[:300])
        check("v8.5 open: indicator hidden once the first page's text is visible",
              await Q.is_hidden("#openingIndicator") and bool(await visible_text(Q)) and rec and rec[-1]["hidden"] and rec[-1]["frame"], str(rec)[:300])
        stats = await Q.evaluate("() => ({ d: document.documentElement.dataset.borgDetect, l: document.documentElement.dataset.borgLocGen })")
        rec20 = next((x for x in await Q.evaluate(IDB_JS, ["books"]) if x["title"] == "کتاب بزرگ ۲۰"), {})
        dc = rec20.get("dirCache") or {}
        check("v8.5 open: detected direction cached on the book record (per file + detector version)",
              dc.get("dir") == "rtl" and dc.get("v") == 1 and dc.get("key") == rec20.get("contentHash") and stats == {"d": "1", "l": "1"}, f"{dc} {stats}")
        await Q.click("#closeReader"); await Q.wait_for_timeout(600)
        t2 = await open_big()
        stats2 = await Q.evaluate("() => ({ d: document.documentElement.dataset.borgDetect, l: document.documentElement.dataset.borgLocGen })")
        check("v8.5 open: second open skips direction detection and locations generation, and is not slower",
              stats2 == {"d": "1", "l": "1"} and t2 <= max(t1 * 1.3, t1 + 0.3) and await Q.is_enabled("#bookSeek")
              and await Q.evaluate("() => getComputedStyle(document.querySelector('#viewer iframe').contentDocument.body).direction") == "rtl",
              f"first {t1:.2f}s, second {t2:.2f}s, {stats2}")
        await Q.click("#closeReader"); await Q.wait_for_timeout(500)
        # Sync chip: hidden while logged out
        check("v8.5 sync chip: hidden when not logged in", await Q.is_hidden("#syncStatus") and await Q.is_hidden("#readerSync"))
        WATCH_CHIP_JS = """() => { window.__chip = []; const el = document.getElementById('syncStatus');
          const rec = () => { const s = el.dataset.state; if (window.__chip[window.__chip.length - 1] !== s) window.__chip.push(s); };
          new MutationObserver(rec).observe(el, { attributes: true, childList: true, characterData: true, subtree: true }); }"""
        await Q.evaluate(WATCH_CHIP_JS)
        await auth(Q, "login", email="admin@example.com", password="password1")
        async def chip_is(state, text):
            return await Q.get_attribute("#syncStatus", "data-state") == state and text in await Q.inner_text("#syncStatus") and await Q.is_visible("#syncStatus")
        try: await poll(lambda: chip_is("synced", "همگام شد"), 30000); ok20 = True
        except Exception: ok20 = False
        seq = await Q.evaluate("() => window.__chip")
        box = await Q.locator("#syncStatus").bounding_box()
        live = await Q.evaluate("() => { const w = document.getElementById('syncStatus').closest('[role=status]'); return w && w.getAttribute('aria-live'); }")
        check("v8.5 sync chip: logged in shows «در حال همگام‌سازی…» then «همگام شد» with a relative time",
              ok20 and "syncing" in seq and seq.index("syncing") < len(seq) - 1 and seq[-1] == "synced"
              and "همین حالا" in await Q.inner_text("#syncStatus") and box and box["height"] > 10 and live == "polite",
              f"{seq} {await Q.inner_text('#syncStatus')} {live}")
        # Offline with a pending change (reading position) → «در انتظار اتصال», in the library and in the reader
        await ctxQ.set_offline(True); await Q.wait_for_timeout(300)
        await Q.click("#bookGrid .book-card")
        await poll(lambda: Q.is_enabled("#bookSeek"), 20000)
        await Q.click("#nextPage"); await Q.wait_for_timeout(700)
        try: await poll(lambda: Q.evaluate("() => document.getElementById('readerSync').dataset.state === 'offline'"), 5000); r_off = True
        except Exception: r_off = False
        await Q.set_viewport_size({"width": 390, "height": 844}); await Q.wait_for_timeout(500)
        tb = await Q.locator(".reader-toolbar").bounding_box(); rb = await Q.locator("#readerSync").bounding_box()
        check("v8.5 sync chip: reader toolbar shows it offline, still one row at 390px",
              r_off and rb and tb["height"] <= 72 and rb["x"] >= 0 and rb["x"] + rb["width"] <= 391 and rb["y"] >= tb["y"] - 1 and rb["y"] + rb["height"] <= tb["y"] + tb["height"] + 1
              and "در انتظار اتصال" in (await Q.get_attribute("#readerSync", "aria-label") or ""), f"{tb} {rb}")
        await Q.set_viewport_size({"width": 1280, "height": 800}); await Q.wait_for_timeout(300)
        await Q.click("#closeReader"); await Q.wait_for_timeout(500)
        try: await poll(lambda: chip_is("offline", "در انتظار اتصال"), 5000); off_ok = True
        except Exception: off_ok = False
        check("v8.5 sync chip: offline with a pending change shows «در انتظار اتصال»", off_ok, await Q.inner_text("#syncStatus"))
        await Q.evaluate(WATCH_CHIP_JS)
        await ctxQ.set_offline(False)
        try: await poll(lambda: chip_is("synced", "همگام شد"), 20000); on_ok = True
        except Exception: on_ok = False
        check("v8.5 sync chip: back online it syncs and returns to «همگام شد»", on_ok and "syncing" in await Q.evaluate("() => window.__chip"),
              f"{await Q.inner_text('#syncStatus')} {await Q.evaluate('() => window.__chip')}")
        # A failed sync shows «خطای همگام‌سازی»; tapping the chip retries
        async def fail_library(route):
            await route.fulfill(status=500, content_type="application/json", body='{"error":"server","message":"x"}')
        await Q.route("**/api/?r=library", fail_library)
        await Q.click("#syncStatus")
        try: await poll(lambda: chip_is("error", "خطای همگام‌سازی"), 10000); err_ok = True
        except Exception: err_ok = False
        await Q.unroute("**/api/?r=library", fail_library)
        await Q.click("#syncStatus")
        try: await poll(lambda: chip_is("synced", "همگام شد"), 15000); retry_ok = True
        except Exception: retry_ok = False
        check("v8.5 sync chip: failed sync shows «خطای همگام‌سازی», tapping it retries", err_ok and retry_ok, await Q.inner_text("#syncStatus"))
        unexpected = [e for e in errsQ if "500" not in e and "ERR_INTERNET_DISCONNECTED" not in e]
        check("v8.5 speed + sync: no JS errors, no external requests", not unexpected and not extQ, str(unexpected[:3] + extQ[:3]))
        await ctxQ.close()
        # 22. v8.6.1: the book iframe allows scripts (Safari runs no listeners otherwise) but book scripts never run
        import zipfile as _zf22
        tmps = Path(tempfile.mkdtemp(prefix="borg-e2e-scr-"))
        evil = tmps / "scripted.epub"
        with _zf22.ZipFile(evil, "w") as z:
            z.writestr(_zf22.ZipInfo("mimetype"), "application/epub+zip", compress_type=_zf22.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">scr-1</dc:identifier><dc:title>کتاب اسکریپت‌دار</dc:title><dc:language>fa</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml" properties="scripted"/><item id="js" href="x.js" media-type="application/javascript"/></manifest><spine><itemref idref="c1"/></spine></package>')
            z.writestr("nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>فهرست</title></head><body><nav epub:type="toc"><ol><li><a href="c1.xhtml">فصل اول</a></li></ol></nav></body></html>')
            z.writestr("x.js", "window.top.__pwned = 'external';")
            paras = "".join(f"<p>پاراگراف {k} متن عادی این کتاب است.</p>" for k in range(30))
            z.writestr("c1.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="fa" dir="rtl"><head><title>فصل اول</title><script src="x.js"></script><script>window.top.__pwned = "inline";</script></head><body><h1 onclick="window.top.__pwned=\'click\'">فصل اول</h1><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" onerror="window.top.__pwned=\'onerror\'" alt=""/><a id="jsl" href="javascript:window.top.__pwned=\'href\'">پیوند</a>' + paras + '</body></html>')
        errsS, extS = [], []
        ctxS, SP = await new_ctx(b, url, extS, errsS)
        await SP.goto(url); await SP.wait_for_timeout(1000)
        await SP.set_input_files("#epubInput", str(evil)); await SP.wait_for_timeout(2500)
        await SP.click('#bookGrid .book-card:has-text("کتاب اسکریپت‌دار")'); await SP.wait_for_timeout(3500)
        info = await SP.evaluate("""()=>{const f=document.querySelector('#viewer iframe');const d=f.contentDocument;
          return {sandbox:f.getAttribute('sandbox')||'', scripts:d.querySelectorAll('script').length, onattr:[...d.querySelectorAll('*')].some(e=>[...e.attributes].some(a=>/^on/i.test(a.name))),
                  js:!!(d.getElementById('jsl')&&/javascript:/i.test(d.getElementById('jsl').getAttribute('href')||'')), pwned: window.__pwned||null}}""")
        check("v8.6.1: book iframe allows scripts (so Safari runs our listeners)", "allow-scripts" in info["sandbox"], info["sandbox"])
        check("v8.6.1: book scripts, inline handlers and javascript: links are stripped and never run",
              info["scripts"] == 0 and not info["onattr"] and not info["js"] and info["pwned"] is None, str(info))
        await SP.click("#nextPage") if await SP.is_visible("#nextPage") else None
        await SP.wait_for_timeout(800)
        check("v8.6.1: still no book script ran after interaction", await SP.evaluate("window.__pwned||null") is None)
        await ctxS.close(); shutil.rmtree(tmps, ignore_errors=True)
        # 23. v8.6.2: every link inside a chapter is handled by the app (never by the browser inside the iframe)
        import zipfile as _zf23
        tmpl = Path(tempfile.mkdtemp(prefix="borg-e2e-links-"))
        lb = tmpl / "links.epub"
        with _zf23.ZipFile(lb, "w") as z:
            z.writestr(_zf23.ZipInfo("mimetype"), "application/epub+zip", compress_type=_zf23.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("OEBPS/content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">links-1</dc:identifier><dc:title>Link Test</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="a" href="feed_1/article_0/a.xhtml" media-type="application/xhtml+xml"/><item id="b" href="feed_4/article_0/b.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>TOC</title></head><body><nav epub:type="toc"><ol><li><a href="feed_1/article_0/a.xhtml">Leaders</a></li><li><a href="feed_4/article_0/b.xhtml">Briefing</a></li></ol></nav></body></html>')
            filler = "".join(f"<p>Filler paragraph {k} with enough words to make this chapter longer than one page.</p>" for k in range(60))
            z.writestr("OEBPS/feed_1/article_0/a.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Leaders</title></head><body><h1>Leaders</h1><p>As we <a id="cross" href="../../feed_4/article_0/b.xhtml#deep">report in our Briefing</a>, see also <a id="ext" href="https://example.com/x">the web</a> and <a id="broken" href="../../nowhere/x.xhtml">a broken link</a>.</p>' + filler + '</body></html>')
            z.writestr("OEBPS/feed_4/article_0/b.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Briefing</title></head><body><h1>Briefing</h1><p><a id="same" href="#deep">Jump down</a></p>' + filler + '<h2 id="deep">Deep section</h2><p>Target text.</p>' + filler + '</body></html>')
        errsL, extL = [], []
        ctxL, LP = await new_ctx(b, url, extL, errsL)
        popups = []
        ctxL.on("page", lambda p_: popups.append(p_))
        await LP.goto(url); await LP.wait_for_timeout(1000)
        await LP.set_input_files("#epubInput", str(lb)); await LP.wait_for_timeout(2500)
        await LP.click('#bookGrid .book-card:has-text("Link Test")'); await LP.wait_for_timeout(3000)
        FR = "()=>{const f=document.querySelector('#viewer iframe');return {url:(f.contentWindow&&f.contentWindow.location.href)||'', h1:(f.contentDocument.querySelector('h1')||{}).textContent||''}}"
        DEEP = """()=>{const f=document.querySelector('#viewer iframe');const r=f.getBoundingClientRect();const box=document.getElementById('viewer').getBoundingClientRect();
          const h=f.contentDocument.getElementById('deep');if(!h)return false;const x=h.getBoundingClientRect();return x.right+r.left>box.left&&x.left+r.left<box.right}"""
        await LP.frame_locator("#viewer iframe").locator("#cross").click(); await LP.wait_for_timeout(2000)
        st = await LP.evaluate(FR)
        check("v8.6.2: cross-folder link opens the target chapter inside the reader", st["h1"] == "Briefing" and st["url"] in ("about:srcdoc", "") and await LP.evaluate(DEEP), str(st))
        check("v8.6.2: the back chip offers the way back after a link", await LP.is_visible("#backChip"))
        await LP.click("#backChip"); await LP.wait_for_timeout(1500)
        check("v8.6.2: back chip returns to the chapter with the link", (await LP.evaluate(FR))["h1"] == "Leaders")
        await LP.frame_locator("#viewer iframe").locator("#broken").click(); await LP.wait_for_timeout(1200)
        st = await LP.evaluate(FR)
        check("v8.6.2: a broken internal link never navigates the reader away", st["h1"] == "Leaders" and st["url"] in ("about:srcdoc", "") and LP.url.startswith(url), str(st))
        await LP.frame_locator("#viewer iframe").locator("#ext").click(); await LP.wait_for_timeout(1500)
        st = await LP.evaluate(FR)
        check("v8.6.2: an external link opens a new tab and leaves the reader in place", len(popups) >= 1 and st["h1"] == "Leaders" and LP.url.startswith(url), f"popups={len(popups)} {st}")
        for p_ in popups:
            try: await p_.close()
            except Exception: pass
        check("v8.6.2 links: no JS errors", not [e for e in errsL if "example.com" not in e], str(errsL[:3]))
        await ctxL.close(); shutil.rmtree(tmpl, ignore_errors=True)
        # 24. v8.6.3: mouse wheel / trackpad turns pages (one page per gesture), can be switched off
        errsW, extW = [], []
        ctxW, WP = await new_ctx(b, url, extW, errsW)
        await WP.set_viewport_size({"width": 1280, "height": 800})
        await WP.goto(url); await WP.wait_for_timeout(1000)
        await WP.set_input_files("#epubInput", sample); await WP.wait_for_timeout(2500)
        await WP.click("#bookGrid .book-card"); await WP.wait_for_timeout(3000)
        SL = "()=>{const c=document.querySelector('#viewer .epub-container');return c?Math.round(c.scrollLeft):null}"
        PW = "()=>{const c=document.querySelector('#viewer .epub-container');return c?c.clientWidth:0}"
        vb = await WP.locator("#viewer").bounding_box()
        await WP.mouse.move(vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
        s0 = await WP.evaluate(SL); pw = await WP.evaluate(PW)
        await WP.mouse.wheel(0, 120); await WP.wait_for_timeout(900)
        s1 = await WP.evaluate(SL)
        check("v8.6.3: one wheel notch down turns exactly one page forward", s0 is not None and abs(abs(s1 - s0) - pw) <= 2, f"{s0} → {s1} (page {pw})")
        for _ in range(12):
            await WP.mouse.wheel(0, 8); await WP.wait_for_timeout(16)
        await WP.wait_for_timeout(900)
        s2 = await WP.evaluate(SL)
        check("v8.6.3: a trackpad burst of tiny deltas turns only one page", abs(abs(s2 - s1) - pw) <= 2, f"{s1} → {s2}")
        await WP.mouse.wheel(0, -120); await WP.wait_for_timeout(900)
        s3 = await WP.evaluate(SL)
        check("v8.6.3: wheel up goes back one page", abs(s3 - s1) <= 2, f"{s2} → {s3} (expected {s1})")
        await open_settings(WP)
        await WP.click("#prefWheel"); await WP.wait_for_timeout(300)
        await WP.keyboard.press("Escape"); await WP.wait_for_timeout(400)
        await WP.mouse.move(vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
        await WP.mouse.wheel(0, 120); await WP.wait_for_timeout(900)
        check("v8.6.3: with the setting off the wheel does not turn pages", abs((await WP.evaluate(SL)) - s3) <= 2)
        check("v8.6.3 wheel: no JS errors", len(errsW) == 0, str(errsW[:3]))
        await ctxW.close()
        # 25. v8.6.4: «کل خطِ شامل این متن» removes the whole line whose rest (e.g. a URL) changes; long blocks are kept
        import zipfile as _zf25
        tmpc = Path(tempfile.mkdtemp(prefix="borg-e2e-line-"))
        lbk = tmpc / "lines.epub"
        with _zf25.ZipFile(lbk, "w") as z:
            z.writestr(_zf25.ZipInfo("mimetype"), "application/epub+zip", compress_type=_zf25.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">line-1</dc:identifier><dc:title>Line Rule Test</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="b" href="b.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr("nav.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>TOC</title></head><body><nav epub:type="toc"><ol><li><a href="a.xhtml">First</a></li><li><a href="b.xhtml">Second</a></li></ol></nav></body></html>')
            for f, h, u in (("a.xhtml", "First", "leaders/2026/10/15/what-if"), ("b.xhtml", "Second", "briefing/2026/10/15/search")):
                long = "<p id='long'>A very long paragraph that quotes This article was downloaded by zlibrary from somewhere in the middle of real text. " + "Real content continues here. " * 30 + "</p>"
                z.writestr(f, f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{h}</title></head><body><h1>{h}</h1><p id="keep">Normal text stays.</p>' + "".join(f"<p>Article body paragraph {k} with ordinary sentences about the economy and science.</p>" for k in range(40))
                           + (long if h == "First" else "")
                           + f'<p id="dl" class="x">This article was downloaded by <a href="https://z-lib.example/">zlibrary</a> from <a href="https://www.economist.com/{u}">https://www.economist.com/{u}</a></p></body></html>')
        errsC, extC = [], []
        ctxC, CP = await new_ctx(b, url, extC, errsC)
        await CP.goto(url); await CP.wait_for_timeout(1000)
        await CP.set_input_files("#epubInput", str(lbk)); await CP.wait_for_timeout(2500)
        await CP.click("#cleanOpen"); await CP.wait_for_timeout(300)
        await CP.fill("#cleanPhrase", "This article was downloaded by zlibrary from")
        await CP.select_option("#cleanMode", "line"); await CP.click("#cleanAdd"); await CP.wait_for_timeout(200)
        check("v8.6.4: a whole-line rule is listed as «کل خطِ شامل …»", "کل خطِ شامل" in await CP.inner_text("#cleanRules"))
        await CP.click("#cleanSearch")
        await poll(lambda: CP.is_visible("#cleanApply"), 15000)
        res = await CP.inner_text("#cleanResults")
        check("v8.6.4: preview counts both lines (the URL differs) and keeps the long paragraph", "۲ مورد" in res and "خیلی بلند" in res and "economist.com" in res, res[:300])
        await CP.click("#cleanApply")
        await poll(lambda: _contains(CP, "#cleanStatus", "حذف شد"), 20000)
        await CP.click("#cleanClose"); await CP.wait_for_timeout(300)
        await CP.click('#bookGrid .book-card:has-text("Line Rule Test")'); await CP.wait_for_timeout(3000)
        DOCQ = "()=>{const d=document.querySelector('#viewer iframe').contentDocument;return {dl:!!d.getElementById('dl'),keep:!!d.getElementById('keep'),long:!!d.getElementById('long'),txt:d.body.textContent.includes('downloaded by zlibrary from https')}}"
        a1 = await CP.evaluate(DOCQ)
        await CP.locator("#tocList .toc-link", has_text="Second").first.click(); await CP.wait_for_timeout(2000)
        a2 = await CP.evaluate(DOCQ)
        check("v8.6.4: the download lines are gone from both chapters, normal text and the long paragraph stay",
              not a1["dl"] and not a2["dl"] and a1["keep"] and a2["keep"] and a1["long"] and not a2["txt"], f"{a1} {a2}")
        check("v8.6.4 line rule: no JS errors", len(errsC) == 0, str(errsC[:3]))
        await ctxC.close(); shutil.rmtree(tmpc, ignore_errors=True)
        # 26. v8.6.5: «جهت این کتاب» overrides the detected direction per book, survives reload, «خودکار» restores it
        errsDir, extDir = [], []
        ctxDir, DR = await new_ctx(b, url, extDir, errsDir)
        await DR.set_viewport_size({"width": 1280, "height": 800})
        await DR.goto(url); await DR.wait_for_timeout(1000)
        await DR.set_input_files("#epubInput", sample); await DR.wait_for_timeout(2500)
        await DR.click("#bookGrid .book-card"); await DR.wait_for_timeout(3000)
        BDIR = "()=>{const f=document.querySelector('#viewer iframe');const d=f&&f.contentDocument;return d?d.defaultView.getComputedStyle(d.body).direction:null}"
        PRESSED = "()=>[...document.querySelectorAll('[data-bookdir]')].filter(b=>b.getAttribute('aria-pressed')==='true').map(b=>b.dataset.bookdir)"
        check("v8.6.5: a Persian book starts RTL with «خودکار» selected", await DR.evaluate(BDIR) == "rtl" and await DR.evaluate(PRESSED) == ["auto"], str(await DR.evaluate(PRESSED)))
        await open_settings(DR)
        await DR.click('[data-bookdir="ltr"]')
        try:
            await poll(lambda: DR.evaluate("()=>{const f=document.querySelector('#viewer iframe');const d=f&&f.contentDocument;return !!d&&d.defaultView.getComputedStyle(d.body).direction==='ltr'}"), 10000)
        except TimeoutError:
            pass
        check("v8.6.5: choosing «چپ‌به‌راست» reopens the book left-to-right", await DR.evaluate(BDIR) == "ltr" and await DR.evaluate(PRESSED) == ["ltr"], f"{await DR.evaluate(BDIR)} {await DR.evaluate(PRESSED)}")
        await DR.reload(); await DR.wait_for_timeout(1500)
        await DR.click("#bookGrid .book-card"); await DR.wait_for_timeout(3000)
        check("v8.6.5: the choice is kept for this book after a reload", await DR.evaluate(BDIR) == "ltr" and await DR.evaluate(PRESSED) == ["ltr"])
        await open_settings(DR)
        await DR.click('[data-bookdir="auto"]')
        try:
            await poll(lambda: DR.evaluate("()=>{const f=document.querySelector('#viewer iframe');const d=f&&f.contentDocument;return !!d&&d.defaultView.getComputedStyle(d.body).direction==='rtl'}"), 10000)
        except TimeoutError:
            pass
        check("v8.6.5: «خودکار» goes back to the detected direction", await DR.evaluate(BDIR) == "rtl" and await DR.evaluate(PRESSED) == ["auto"])
        check("v8.6.5 book direction: no JS errors", len(errsDir) == 0, str(errsDir[:3]))
        await ctxDir.close()
        # 27. v8.7 API keys (admin panel) and cleanup rules mirrored to the server for the admin.
        # The new endpoints are mocked with routes (their server side is covered by api_test.py); login is real.
        import json as _json27, time as _time27
        ext27, errs27 = [], []
        ctx27, K = await new_ctx(b, url, ext27, errs27)
        K.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        now27 = int(_time27.time())
        TOKEN27 = "borg_e2e_token_27_once"
        m27 = {"calls": [], "rules": [{"id": "srv1", "type": "phrase", "text": "کانال سرور ما"}, {"id": "srv2", "type": "telegram"}],
               "tokens": [{"id": 3, "name": "ربات قدیمی", "created_at": now27 - 3 * 86400, "last_used_at": now27 - 3600, "uses": 12, "revoked": False, "require_review": True},
                          {"id": 2, "name": "کلید کنار گذاشته", "created_at": now27 - 9 * 86400, "last_used_at": 0, "uses": 0, "revoked": True, "require_review": False}]}
        async def api27(route):
            req = route.request
            r = req.url.split("r=", 1)[1].split("&", 1)[0]
            body = None
            if req.method == "POST":
                try: body = _json27.loads(req.post_data or "null")
                except Exception: body = None
            m27["calls"].append({"m": req.method, "r": r, "body": body, "csrf": req.headers.get("x-csrf-token")})
            data = {"ok": True}
            if r == "api-tokens": data = {"tokens": m27["tokens"]}
            elif r == "api-token-create":
                m27["tokens"].insert(0, {"id": 7, "name": body["name"], "created_at": now27, "last_used_at": 0, "uses": 0, "revoked": False, "require_review": bool(body.get("require_review"))})
                data = {"id": 7, "token": TOKEN27}
            elif r == "api-token-revoke":
                for t in m27["tokens"]:
                    if t["id"] == body.get("id"): t["revoked"] = True
            elif r == "clean-rules":
                if req.method == "GET": data = {"rules": m27["rules"]}
                else: m27["rules"] = body["rules"]
            await route.fulfill(status=200, content_type="application/json", body=_json27.dumps(data))
        await ctx27.route(lambda u: "/api/?r=api-token" in u or "/api/?r=clean-rules" in u, api27)
        RULES27 = "([k]) => JSON.parse(localStorage.getItem(k) || '[]')"
        CKEY27 = "borg-clean-rules" + suffix
        posts27 = lambda r: [c for c in m27["calls"] if c["m"] == "POST" and c["r"] == r]
        async def admin_login27():
            h = await (await K.request.get(url + "api/?r=health")).json()
            if h.get("needs_setup"):
                await auth(K, "setup", setup_key=SETUP_KEY, name="مدیر بُرگ", email="admin@example.com", password="password1")
            else:
                await auth(K, "login", email="admin@example.com", password="password1")
            await wait_synced(K)
        await K.goto(url); await K.wait_for_timeout(800)
        await admin_login27()
        try:
            await poll(lambda: K.evaluate("([k]) => JSON.parse(localStorage.getItem(k) || '[]').length === 2", [CKEY27]), 8000)
        except TimeoutError:
            pass
        lr = await K.evaluate(RULES27, [CKEY27])
        check("v8.7 rules: admin with no local rules adopts the server's rules on login",
              [(x["id"], x["type"], x.get("text")) for x in lr] == [("srv1", "phrase", "کانال سرور ما"), ("srv2", "telegram", None)]
              and not posts27("clean-rules"), str(lr))
        await K.click("#cleanOpen"); await K.wait_for_timeout(300)
        check("v8.7 rules: the cleanup dialog lists the adopted rules and tells the admin they apply on the server",
              await K.is_visible("#cleanServerNote") and "کتاب‌های ارسالی با API" in await K.inner_text("#cleanServerNote")
              and "روی سرور ذخیره شد" in await K.inner_text("#cleanSyncState") and "کانال سرور ما" in await K.inner_text("#cleanRules"),
              await K.inner_text("#cleanServerNote"))
        await K.fill("#cleanPhrase", "متن تبلیغ آزمایشی"); await K.click("#cleanAdd")
        try:
            await poll(lambda: asyncio.sleep(0, len(posts27("clean-rules")) >= 1), 6000)
        except TimeoutError:
            pass
        p1 = posts27("clean-rules")
        lr = await K.evaluate(RULES27, [CKEY27])
        exp = [{"id": "srv1", "type": "phrase", "text": "کانال سرور ما"}, {"id": "srv2", "type": "telegram"}, {"id": lr[-1]["id"], "type": "phrase", "text": "متن تبلیغ آزمایشی"}]
        check("v8.7 rules: adding a rule as admin POSTs the full list once (debounced) with the CSRF header",
              len(p1) == 1 and p1[0]["body"] == {"rules": exp} and bool(p1[0]["csrf"]), str(p1)[:400])
        await K.click('#cleanRules [data-clean-del="srv2"]')
        try:
            await poll(lambda: asyncio.sleep(0, len(posts27("clean-rules")) >= 2), 6000)
            await poll(lambda: _contains(K, "#cleanSyncState", "روی سرور ذخیره شد"), 4000)
        except TimeoutError:
            pass
        p2 = posts27("clean-rules")
        check("v8.7 rules: deleting a rule pushes the shorter list; the dialog shows it was saved",
              len(p2) == 2 and [x["id"] for x in p2[1]["body"]["rules"]] == ["srv1", lr[-1]["id"]]
              and "روی سرور ذخیره شد" in await K.inner_text("#cleanSyncState"), str(p2[-1:])[:300])
        await K.click("#cleanClose")
        # API keys in the admin panel
        await K.click('[data-view="admin"]'); await K.click('[data-admin-tab="api"]')  # v8.9: admin sub-tabs
        try:
            await poll(lambda: _count_gt(K, "#apiTokenList .api-item", 1), 8000)
        except TimeoutError:
            pass
        items = K.locator("#apiTokenList .api-item")
        old, dead = items.nth(0), items.nth(1)
        old_t, dead_t = await old.inner_text(), await dead.inner_text()
        check("v8.7 keys: the admin sees «کلیدهای API» with the list (uses, last use, review flag, status)",
              await K.is_visible("#apiBlock") and "کلیدهای API" in await K.inner_text("#apiBlock h3") and await items.count() == 2
              and "ربات قدیمی" in old_t and "۱۲ بار" in old_t and "منتظر تأیید" in old_t and "فعال" in old_t and "هرگز" not in old_t
              and await old.locator("[data-token-revoke]").is_visible(), old_t)
        dead_op = await dead.evaluate("e => getComputedStyle(e).opacity")
        check("v8.7 keys: a revoked key is greyed, says «لغوشده» and «هرگز», has no revoke button",
              "لغوشده" in dead_t and "هرگز" in dead_t and float(dead_op) < 1 and await dead.locator("[data-token-revoke]").count() == 0, f"{dead_t} {dead_op}")
        curl = await K.inner_text("#apiCurl")
        check("v8.7 keys: usage guide and a curl example with this site's API URL",
              "docs/api-books.md" in await K.inner_text("#apiBlock") and "Authorization: Bearer" in curl and url + "api/?r=api-book" in curl
              and await K.is_visible("#apiCurl"), curl)
        await K.click("#apiTokenCreate"); await K.wait_for_timeout(300)
        check("v8.7 keys: a key needs a name (nothing is sent without one)",
              bool((await K.inner_text("#apiTokenError")).strip()) and not posts27("api-token-create"))
        await K.fill("#apiTokenName", "ربات تست"); await K.check("#apiTokenReview"); await K.click("#apiTokenCreate")
        try:
            await poll(lambda: K.is_visible("#apiTokenNew"), 6000)
            await poll(lambda: _count_gt(K, "#apiTokenList .api-item", 2), 6000)
        except TimeoutError:
            pass
        pc = posts27("api-token-create")
        check("v8.7 keys: creating sends the name and review flag, then shows the key once with the warning",
              len(pc) == 1 and pc[0]["body"] == {"name": "ربات تست", "require_review": True} and bool(pc[0]["csrf"])
              and await K.input_value("#apiTokenValue") == TOKEN27 and await K.get_attribute("#apiTokenValue", "readonly") is not None
              and "فقط همین یک بار" in await K.inner_text("#apiTokenNew") and await K.is_visible("#apiTokenCopy"), str(pc))
        check("v8.7 keys: the new key appears in the list", "ربات تست" in await items.nth(0).inner_text() and await items.count() == 3)
        try: await ctx27.grant_permissions(["clipboard-read", "clipboard-write"], origin=url.split("/borg")[0])
        except Exception: pass
        await K.click("#apiTokenCopy"); await K.wait_for_timeout(400)
        sel = await K.evaluate("() => { const f = document.getElementById('apiTokenValue'); return document.activeElement === f && f.selectionStart === 0 && f.selectionEnd === f.value.length; }")
        try: clip = await K.evaluate("() => navigator.clipboard.readText()")
        except Exception: clip = None
        check("v8.7 keys: «کپی» selects the whole key and copies it (or asks to copy when the clipboard is blocked)",
              sel and ("کپی شد" in await K.inner_text("#toast") and clip in (TOKEN27, None) or "انتخاب شد" in await K.inner_text("#toast")), f"{sel} {clip!r} {await K.inner_text('#toast')}")
        await K.click('[data-view="mine"]'); await K.wait_for_timeout(200); await K.click('[data-view="admin"]'); await K.wait_for_timeout(600)
        check("v8.7 keys: the key is not shown again after leaving the admin view",
              not await K.is_visible("#apiTokenNew") and await K.input_value("#apiTokenValue") == "")
        try: await poll(lambda: _count_gt(K, "#apiTokenList .api-item", 2), 6000)
        except TimeoutError: pass
        await K.locator('#apiTokenList [data-token-revoke="7"]').click()
        try: await poll(lambda: K.locator('#apiTokenList .api-item.revoked[data-token="7"]').count(), 6000)
        except TimeoutError: pass
        pr = posts27("api-token-revoke")
        check("v8.7 keys: «لغو» (after confirmation) calls api-token-revoke and the key turns revoked",
              len(pr) == 1 and pr[0]["body"] == {"id": 7} and await K.locator('#apiTokenList .api-item.revoked[data-token="7"]').count() == 1, str(pr))
        # Phone width: the section fits without horizontal scrolling
        await K.set_viewport_size({"width": 390, "height": 844}); await K.wait_for_timeout(400)
        await K.locator("#apiBlock").scroll_into_view_if_needed()
        geo = await K.evaluate("""() => { const r = e => document.querySelector(e).getBoundingClientRect();
            const all = ['#apiBlock', '#apiTokenName', '#apiTokenCreate', '#apiCurl', '#apiTokenList'].map(r);
            return { ok: all.every(b => b.left >= -1 && b.right <= innerWidth + 1 && b.width > 0), sw: document.documentElement.scrollWidth,
                     btn: r('#apiTokenCreate').height, inp: r('#apiTokenName').height }; }""")
        check("v8.7 keys mobile 390px: section, form, list and curl fit, touch-sized controls, no horizontal scroll",
              geo["ok"] and geo["sw"] <= 390 and geo["btn"] >= 40 and geo["inp"] >= 40, str(geo))
        await K.set_viewport_size({"width": 1280, "height": 800})
        # Logged out: rules stay local only; logging in again with a different server list pushes the local one (local wins)
        await K.click("#accountButton"); await K.wait_for_timeout(400); await K.click("#logoutButton"); await K.wait_for_timeout(600)
        n_before = len(posts27("clean-rules"))
        await K.click("#cleanOpen"); await K.wait_for_timeout(200)
        await K.fill("#cleanPhrase", "فقط روی این دستگاه"); await K.click("#cleanAdd"); await K.wait_for_timeout(1800)
        check("v8.7 rules: without an admin session nothing is sent and the server note is hidden",
              len(posts27("clean-rules")) == n_before and not await K.is_visible("#cleanServerNote"))
        await K.click("#cleanClose")
        m27["rules"] = [{"id": "other", "type": "phrase", "text": "قانون دستگاه دیگر"}]
        await admin_login27()
        try: await poll(lambda: asyncio.sleep(0, len(posts27("clean-rules")) > n_before), 6000)
        except TimeoutError: pass
        lr = await K.evaluate(RULES27, [CKEY27])
        pl = posts27("clean-rules")
        check("v8.7 rules: when both lists exist and differ, the local list wins and is pushed",
              len(pl) == n_before + 1 and [x["id"] for x in pl[-1]["body"]["rules"]] == [x["id"] for x in lr]
              and "other" not in [x["id"] for x in lr] and any(x.get("text") == "فقط روی این دستگاه" for x in lr), str(pl[-1:])[:300])
        check("v8.7 API keys: no JS errors, no external requests", len(errs27) == 0 and len(ext27) == 0, str(errs27[:3] + ext27[:3]))
        await ctx27.close()
        # 28. v8.8 PDF: text-based PDF magazines open in their original layout (pdf.js, lazily loaded, canvas + text layer).
        # Fixtures are real text PDFs printed by Chromium itself (page.pdf with an outline): 3 English pages and 3 Persian
        # (RTL, Vazirmatn) pages with headings, plus a 120-page one for the memory/responsiveness check.
        tmpp = Path(tempfile.mkdtemp(prefix="borg-e2e-pdf-"))
        EN28, FA28 = ["Alpha", "Bravo", "Zeppelin"], ["نخست", "دوم", "سوم"]
        def pdf_html(lang, title, heads, para, n=12):
            pages = "".join(f'<section style="page-break-after:always"><h1>{h}</h1>' + "".join(f"<p>{para(i, k)}</p>" for k in range(n)) + "</section>" for i, h in enumerate(heads))
            return (f'<!doctype html><html lang="{lang}" dir="{"rtl" if lang == "fa" else "ltr"}"><head><meta charset="utf-8"><title>{title}</title>'
                    f'<style>@font-face{{font-family:Vazirmatn;src:url({url}assets/fonts/Vazirmatn-Variable.woff2)}}body{{font-family:Vazirmatn,sans-serif;font-size:18px}}</style></head><body>{pages}</body></html>')
        ctxG = await b.new_context()
        GP = await ctxG.new_page()
        await GP.goto(url + "offline.html")
        async def make_pdf(path, html):
            await GP.set_content(html); await GP.evaluate("document.fonts.ready")
            await GP.pdf(path=str(path), format="A4", outline=True, tagged=True)  # Chromium builds the outline from the tagged headings
            return str(path)
        en_pdf = await make_pdf(tmpp / "magazine-en.pdf", pdf_html("en", "Test Magazine", [f"Chapter {w}" for w in EN28], lambda i, k: f"Paragraph {k} on the page about {EN28[i]} with plain English words for the reader."))
        fa_pdf = await make_pdf(tmpp / "majale-fa.pdf", pdf_html("fa", "مجلهٔ آزمایشی", [f"فصل {w}" for w in FA28], lambda i, k: f"این پاراگراف شماره {k} از فصل {FA28[i]} در مجلهٔ آزمایشی است و متن فارسی دارد."))
        big_pdf = await make_pdf(tmpp / "big.pdf", pdf_html("en", "Big Magazine", [f"Article {k}" for k in range(1, 121)], lambda i, k: f"Article {i + 1} paragraph {k} of the big magazine.", n=6))
        await ctxG.close()
        PAGES = "()=>[...document.querySelectorAll('#viewer .pdf-page')].map(p=>+p.dataset.page)"
        PIX = """()=>{const c=document.querySelector('#viewer .pdf-page canvas');if(!c||!c.dataset.ready)return null;
          const v=document.getElementById('viewer').getBoundingClientRect(),r=c.getBoundingClientRect(),d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
          let dark=0,light=0;for(let i=0;i<d.length;i+=32){const s=d[i]+d[i+1]+d[i+2];if(s<300)dark++;else if(s>600)light++}
          return {dark,light,inside:r.width>100&&r.height>100&&r.left>=v.left-1&&r.right<=v.right+1&&r.top>=v.top-1,w:c.width,cssW:Math.round(r.width),cssH:Math.round(r.height),dpr:devicePixelRatio}}"""
        LAYER = "()=>[...document.querySelectorAll('#viewer .textLayer')].map(t=>t.textContent).join(' ')"
        async def pages_are(pg, want, timeout=6000):
            """Wait until the viewer shows exactly these pages; returns what it shows."""
            async def ok(): return await pg.evaluate(PAGES) == want
            try: await poll(ok, timeout)
            except TimeoutError: pass
            return await pg.evaluate(PAGES)
        async def drawn(pg, timeout=10000):
            try: await poll(lambda: pg.evaluate(PIX), timeout)
            except TimeoutError: pass
            return await pg.evaluate(PIX)
        ext28, errs28 = [], []
        ctx28, Q = await new_ctx(b, url, ext28, errs28)
        Q.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await Q.add_init_script(f"try{{const k='ketabkhane-reader-preferences{suffix}';const p=JSON.parse(localStorage.getItem(k)||'{{}}');if(!p.__pdf28){{p.spread='none';p.__pdf28=1;localStorage.setItem(k,JSON.stringify(p))}}}}catch(e){{}}")
        await Q.goto(url); await Q.wait_for_timeout(1000)
        await Q.reload(); await Q.wait_for_timeout(800)
        shell = await Q.evaluate("caches.keys().then(ks=>Promise.all(ks.map(k=>caches.open(k).then(c=>c.keys())))).then(a=>a.flat().map(r=>new URL(r.url).pathname))")
        check("v8.8 PDF: pdf.js is not in the precached app shell", not any("pdfjs" in k for k in shell) and any(k.endswith("app.js") for k in shell), str([k for k in shell if "pdfjs" in k]))
        mods0 = await Q.evaluate("performance.getEntriesByType('resource').filter(e=>e.name.includes('pdfjs')).length")
        check("v8.8 PDF: pdf.js is not loaded before a PDF is used", mods0 == 0, str(mods0))
        await Q.set_input_files("#epubInput", en_pdf)
        card = Q.locator('#bookGrid .book-card:has-text("Test Magazine")')
        try: await poll(lambda: card.count(), 15000)
        except TimeoutError: pass
        cov = await card.evaluate("c=>{const i=c.querySelector('.book-cover-thumb img');const r=i&&i.getBoundingClientRect();return {src:i?i.getAttribute('src').slice(0,23):'',w:r?r.width:0,badge:(c.querySelector('.format-badge')||{}).textContent||''}}") if await card.count() else {}
        check("v8.8 PDF: import shows a card titled from the PDF metadata with a page-1 JPEG cover and a PDF badge",
              cov.get("src") == "data:image/jpeg;base64," and cov.get("w", 0) > 50 and cov.get("badge") == "PDF" and "اضافه شد" in await Q.inner_text("#toast"), str(cov))
        rec = [x for x in await Q.evaluate(IDB_JS, ["books"]) if x.get("title") == "Test Magazine"]
        check("v8.8 PDF: stored as a book record with format pdf, page count and SHA-256", len(rec) == 1 and rec[0].get("format") == "pdf" and rec[0].get("pages") == 3 and len(rec[0].get("contentHash") or "") == 64, str(rec)[:300])
        await Q.set_input_files("#epubInput", en_pdf); await Q.wait_for_timeout(1500)
        check("v8.8 PDF: the same PDF again is refused as a duplicate", "قبلاً" in await Q.inner_text("#toast") and await card.count() == 1)
        await card.click()
        px = await drawn(Q)
        check("v8.8 PDF: page 1 is drawn on a canvas inside the viewer (non-blank pixels, devicePixelRatio)",
              bool(px) and px["inside"] and px["dark"] > 200 and px["light"] > 1000 and px["w"] >= px["cssW"] * px["dpr"] - 2, str(px))
        try: await poll(lambda: _contains(Q, "#viewer .textLayer", "Chapter Alpha"), 6000)
        except TimeoutError: pass
        lay = await Q.evaluate(LAYER)
        check("v8.8 PDF: the text layer holds the page text (selectable, searchable)", "Chapter Alpha" in lay and "plain English words" in lay and "Bravo" not in lay, lay[:120])
        sel = await Q.evaluate("""()=>{const s=document.querySelector('#viewer .textLayer');if(!s)return '';
          const r=document.createRange();r.selectNodeContents(s);const g=getSelection();g.removeAllRanges();g.addRange(r);const t=String(g);g.removeAllRanges();return t.slice(0,60)}""")
        check("v8.8 PDF: text on the page can be selected", "Alpha" in sel, sel)
        check("v8.8 PDF: TOC from the PDF outline and the chapter title in the header",
              await Q.locator("#tocList .toc-link").all_inner_texts() == [f"Chapter {w}" for w in EN28] and await Q.inner_text("#chapterLabel") == "Chapter Alpha"
              and await Q.is_disabled("#prevPage") and "صفحهٔ ۱ از ۳" in await Q.inner_text("#timeLeft"), await Q.inner_text("#chapterLabel"))
        check("v8.8 PDF: an English PDF is detected LTR (seek bar LTR)", await Q.get_attribute("#bookSeek", "dir") == "ltr")
        await Q.click("#nextPage"); p1 = await pages_are(Q, [2])
        await Q.click("#prevPage"); p2 = await pages_are(Q, [1])
        check("v8.8 PDF: next/prev buttons turn one page", p1 == [2] and p2 == [1], f"{p1} {p2}")
        await Q.keyboard.press("ArrowRight"); k1 = await pages_are(Q, [2])
        await Q.keyboard.press("ArrowLeft"); k2 = await pages_are(Q, [1])
        await Q.keyboard.press("PageDown"); k3 = await pages_are(Q, [2])
        await Q.keyboard.press("PageUp"); k4 = await pages_are(Q, [1])
        check("v8.8 PDF: arrows follow the LTR book (→ next), PageDown/PageUp", (k1, k2, k3, k4) == ([2], [1], [2], [1]), f"{k1} {k2} {k3} {k4}")
        vb = await Q.locator("#viewer").bounding_box()
        await Q.mouse.click(vb["x"] + vb["width"] * 0.92, vb["y"] + vb["height"] * 0.5); t1 = await pages_are(Q, [2])
        await Q.mouse.click(vb["x"] + vb["width"] * 0.08, vb["y"] + vb["height"] * 0.5); t2 = await pages_are(Q, [1])
        check("v8.8 PDF: edge taps (LTR: right edge = next, left edge = previous)", t1 == [2] and t2 == [1], f"{t1} {t2}")
        await Q.mouse.move(vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
        await Q.mouse.wheel(0, 120); w1 = await pages_are(Q, [2]); await Q.wait_for_timeout(400)
        for _ in range(10):
            await Q.mouse.wheel(0, 8); await Q.wait_for_timeout(16)
        w2 = await pages_are(Q, [3]); await Q.wait_for_timeout(400)
        await Q.mouse.wheel(0, -120); w3 = await pages_are(Q, [2])
        check("v8.8 PDF: mouse wheel turns one page per gesture (down = next, up = back)", (w1, w2, w3) == ([2], [3], [2]), f"{w1} {w2} {w3}")
        await Q.evaluate("()=>{const r=document.getElementById('bookSeek');r.value='1000';r.dispatchEvent(new Event('input'));r.dispatchEvent(new Event('change'))}")
        s1 = await pages_are(Q, [3])
        try: await poll(lambda: Q.is_visible("#backChip"), 4000)
        except TimeoutError: pass
        chip = await Q.is_visible("#backChip")
        check("v8.8 PDF: seek bar jumps to the last page («صفحهٔ ۳ از ۳», next disabled) and offers the way back",
              s1 == [3] and chip and await Q.is_disabled("#nextPage") and "۳ از ۳" in await Q.inner_text("#timeLeft") and await Q.inner_text("#chapterLabel") == "Chapter Zeppelin", f"{s1} chip={chip}")
        await Q.click("#backChip"); b1 = await pages_are(Q, [2])
        check("v8.8 PDF: the back chip returns to the page before the jump", b1 == [2], str(b1))
        await Q.locator("#tocList .toc-link", has_text="Chapter Alpha").click(); c1 = await pages_are(Q, [1])
        check("v8.8 PDF: a TOC entry opens its page and becomes the active entry",
              c1 == [1] and await Q.locator("#tocList .toc-link.active").inner_text() == "Chapter Alpha", str(c1))
        await Q.click("#nextPage"); await pages_are(Q, [2]); await Q.wait_for_timeout(600)
        prog = [x for x in await Q.evaluate(IDB_JS, ["progress"]) if x.get("bookId") == rec[0]["id"]]
        check("v8.8 PDF: progress is stored as pdf:<page> with percentage page/pages", prog and prog[0]["cfi"] == "pdf:2" and prog[0]["percentage"] == 67, str(prog))
        await Q.click("#bookmarkButton"); await Q.wait_for_timeout(500)
        anns = [a for a in await Q.evaluate(IDB_JS, ["annotations"]) if a.get("bookId") == rec[0]["id"] and not a.get("deleted")]
        check("v8.8 PDF: a page bookmark is saved as an annotation pdf:<page>",
              len(anns) == 1 and anns[0]["type"] == "bookmark" and anns[0]["cfi"] == "pdf:2" and await Q.get_attribute("#bookmarkButton", "aria-pressed") == "true", str(anns))
        await Q.reload(); await Q.wait_for_timeout(1000)
        await Q.click('#bookGrid .book-card:has-text("Test Magazine")')
        r1 = await pages_are(Q, [2], 10000)
        try: await poll(lambda: _count_gt(Q, "#tocList .toc-link", 2), 6000)
        except TimeoutError: pass
        check("v8.8 PDF: reopening after a reload restores the page (pdf:2)", r1 == [2] and await Q.inner_text("#chapterLabel") == "Chapter Bravo", str(r1))
        cached = await Q.evaluate("caches.keys().then(ks=>Promise.all(ks.map(k=>caches.open(k).then(c=>c.keys())))).then(a=>a.flat().map(r=>new URL(r.url).pathname))")
        check("v8.8 PDF: after first use pdf.js (module + worker) sits in the runtime cache for offline use",
              any(k.endswith("pdfjs/pdf.min.mjs") for k in cached) and any(k.endswith("pdfjs/pdf.worker.min.mjs") for k in cached), str([k for k in cached if "pdfjs" in k][:4]))
        # Search: a word on page 3, jump with the match marked in the text layer
        await Q.click("#searchToggle"); await Q.fill("#bookSearchInput", "zeppelin"); await Q.press("#bookSearchInput", "Enter")
        try: await poll(lambda: _count_gt(Q, "#bookSearchResults button", 0), 10000)
        except TimeoutError: pass
        res = await Q.locator("#bookSearchResults button").all_inner_texts()
        check("v8.8 PDF: search finds the word on page 3 (case-insensitive) with an excerpt", len(res) >= 13 and all("صفحهٔ ۳" in r for r in res) and "Zeppelin" in res[0], str(res[:2]))
        await Q.locator("#bookSearchResults button").first.click()
        s3 = await pages_are(Q, [3])
        try: await poll(lambda: Q.locator("#viewer .textLayer .pdf-hit").count(), 6000)
        except TimeoutError: pass
        hit = await Q.evaluate("""()=>{const hs=[...document.querySelectorAll('#viewer .textLayer .pdf-hit')];if(!hs.length)return null;const h=hs[0],r=h.getBoundingClientRect(),v=document.getElementById('viewer').getBoundingClientRect();
          return {t:hs.map(x=>x.textContent).join(''),vis:r.width>2&&r.top>=v.top&&r.bottom<=v.bottom&&r.left>=v.left&&r.right<=v.right,bg:getComputedStyle(h).backgroundColor}}""")
        check("v8.8 PDF: the search result opens page 3 with every match highlighted in the text layer",
              s3 == [3] and bool(hit) and hit["t"].lower() == "zeppelin" * 13 and hit["vis"] and hit["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), f"{s3} {hit}")
        try: await poll(lambda: Q.is_visible("#backChip"), 3000)
        except TimeoutError: pass
        check("v8.8 PDF: the back chip appears after the search jump", await Q.is_visible("#backChip"), await Q.evaluate("()=>{const c=document.getElementById('backChip');return [c.hidden,c.getBoundingClientRect().height,getComputedStyle(c).display]}"))
        # Settings: EPUB-only rows hidden, PDF rows shown; zoom buttons; fit toggle; double click resets
        await open_settings(Q)
        vis = {sid: await Q.is_visible(sid) for sid in ["#readerFont", "#fontUp", "#readerLineHeight", '[data-align="justify"]', '[data-margin="s"]', '[data-para="s"]', '[data-flow="paginated"]', "#prefDigits", "#pdfZoomIn", '[data-pdffit="width"]', "#prefPdfInvert", '[data-theme="dark"]', '[data-spread="auto"]', '[data-bookdir="auto"]', "#prefTaps"]}
        check("v8.8 PDF: «Aa» hides font/size/line height/align/paragraph/margin/flow/digits and shows zoom, fit and invert",
              not any(vis[k] for k in ["#readerFont", "#fontUp", "#readerLineHeight", '[data-align="justify"]', '[data-margin="s"]', '[data-para="s"]', '[data-flow="paginated"]', "#prefDigits"])
              and all(vis[k] for k in ["#pdfZoomIn", '[data-pdffit="width"]', "#prefPdfInvert", '[data-theme="dark"]', '[data-spread="auto"]', '[data-bookdir="auto"]', "#prefTaps"])
              and not await Q.is_visible("#ttsToggle"), str(vis))
        before = await drawn(Q)
        await Q.click("#pdfZoomIn"); await Q.click("#pdfZoomIn")
        try:
            async def zoomed(): x = await Q.evaluate(PIX); return x and x["cssW"] > before["cssW"] * 1.5
            await poll(zoomed, 6000)
        except TimeoutError: pass
        after = await drawn(Q)
        pan = await Q.evaluate("()=>{const s=document.querySelector('#viewer .pdf-stage');const a=[s.scrollLeft,s.scrollTop];s.scrollTop=40;return {can:s.scrollHeight>s.clientHeight+100,moved:s.scrollTop===40,a}}")
        check("v8.8 PDF: «+» zooms the page (bigger canvas, sharper pixels, «۱۵۶٪») and the zoomed page pans",
              after and abs(after["cssW"] / before["cssW"] - 1.5625) < 0.03 and after["w"] > before["w"] * 1.5 and await Q.inner_text("#pdfZoomLabel") == "۱۵۶٪" and pan["can"] and pan["moved"], f"{before} → {after} {pan}")
        await Q.keyboard.press("Escape"); await Q.wait_for_timeout(300)
        vb = await Q.locator("#viewer").bounding_box()
        await Q.mouse.click(vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
        await Q.wait_for_timeout(120)
        await Q.mouse.click(vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
        await Q.wait_for_timeout(900)
        reset = await drawn(Q)
        check("v8.8 PDF: a double tap/click on the zoomed page resets the zoom (no immersive toggle)",
              reset and abs(reset["cssW"] - before["cssW"]) <= 2 and not await Q.evaluate("document.getElementById('reader').classList.contains('immersive')"), str(reset))
        await Q.mouse.move(vb["x"] + vb["width"] / 2, vb["y"] + vb["height"] / 2)
        await Q.keyboard.down("Control"); await Q.mouse.wheel(0, -300); await Q.keyboard.up("Control")
        try:
            async def zoomed2(): x = await Q.evaluate(PIX); return x and x["cssW"] > before["cssW"] * 1.3
            await poll(zoomed2, 4000)
        except TimeoutError: pass
        cz = await drawn(Q)
        check("v8.8 PDF: ctrl+wheel zooms the page (not the browser) and keeps the page", cz and cz["cssW"] > before["cssW"] * 1.3 and await Q.evaluate(PAGES) == [3]
              and await Q.evaluate("Math.round(visualViewport.scale*100)") == 100, str(cz))
        await open_settings(Q)
        await Q.click('[data-pdffit="width"]'); await Q.wait_for_timeout(900)
        fw = await Q.evaluate("()=>{const s=document.querySelector('#viewer .pdf-stage'),p=document.querySelector('#viewer .pdf-page');return {s:s.clientWidth,p:p.getBoundingClientRect().width,z:document.getElementById('pdfZoomLabel').textContent}}")
        check("v8.8 PDF: «عرض صفحه» fits the page to the viewer width (zoom back to 100%)", abs(fw["s"] - fw["p"]) <= 2 and fw["z"] == "۱۰۰٪", str(fw))
        await Q.click('[data-pdffit="page"]'); await Q.wait_for_timeout(900)
        fp = await Q.evaluate("()=>{const s=document.querySelector('#viewer .pdf-stage').getBoundingClientRect(),p=document.querySelector('#viewer .pdf-page').getBoundingClientRect();return p.top>=s.top&&p.bottom<=s.bottom+1&&p.height>s.height*0.85}")
        check("v8.8 PDF: «اندازهٔ صفحه» shows the whole page", fp)
        await Q.click('[data-theme="dark"]'); await Q.wait_for_timeout(300)
        filt = await Q.evaluate("getComputedStyle(document.querySelector('#viewer .pdf-page canvas')).filter")
        bg = await Q.evaluate("getComputedStyle(document.querySelector('.reader-toolbar')).backgroundColor")
        await Q.click("#prefPdfInvert"); await Q.wait_for_timeout(200)
        filt2 = await Q.evaluate("getComputedStyle(document.querySelector('#viewer .pdf-page canvas')).filter")
        await Q.click("#prefPdfInvert"); await Q.click('[data-theme="light"]'); await Q.wait_for_timeout(200)
        check("v8.8 PDF: dark theme inverts the page (optional) while the chrome follows the theme", "invert(1)" in filt and filt2 == "none" and bg != "rgb(255, 255, 255)", f"{filt} | {filt2} | {bg}")
        await Q.click('[data-spread="auto"]'); await Q.wait_for_timeout(300)
        await Q.keyboard.press("Escape"); await Q.wait_for_timeout(300)
        await Q.locator("#tocList .toc-link", has_text="Chapter Bravo").click()
        sp = await pages_are(Q, [2, 3])
        geo = await Q.evaluate("()=>[...document.querySelectorAll('#viewer .pdf-page')].map(p=>Math.round(p.getBoundingClientRect().left))")
        check("v8.8 PDF: on a wide landscape viewer «ستون‌ها: خودکار» shows two pages side by side (LTR: 2 left of 3)", sp == [2, 3] and geo[0] < geo[1], f"{sp} {geo}")
        await open_settings(Q); await Q.click('[data-spread="none"]'); await Q.keyboard.press("Escape"); await Q.wait_for_timeout(500)
        # Text cleanup never touches PDFs (and never feeds them to JSZip)
        await Q.click("#closeReader"); await Q.wait_for_timeout(400)
        await Q.set_input_files("#epubInput", sample); await Q.wait_for_timeout(2500)
        size0 = [x for x in await Q.evaluate(IDB_JS, ["books"]) if x.get("format") == "pdf"][0]["size"]
        await Q.click("#cleanOpen"); await Q.wait_for_timeout(300)
        await Q.fill("#cleanPhrase", "plain English words"); await Q.click("#cleanAdd"); await Q.wait_for_timeout(200)
        await Q.click("#cleanSearch")
        try: await poll(lambda: _contains(Q, "#cleanStatus", "بررسی شد"), 15000)
        except TimeoutError: pass
        cst, cres = await Q.inner_text("#cleanStatus"), await Q.inner_text("#cleanResults")
        size1 = [x for x in await Q.evaluate(IDB_JS, ["books"]) if x.get("format") == "pdf"][0]["size"]
        check("v8.8 PDF: «پاک‌سازی متن» skips PDFs (only the EPUB is scanned, the PDF is untouched, no errors)",
              "۱ کتاب بررسی شد" in cst and "Test Magazine" not in cres and size0 == size1 and not errs28, f"{cst} | {cres[:80]} | {errs28[:2]}")
        await Q.click("#cleanClose")
        await Q.evaluate(f"localStorage.removeItem('borg-clean-rules{suffix}')")
        # A big magazine: 120 pages stay responsive and only the pages around the current one keep a canvas
        t0 = await Q.evaluate("Date.now()")
        await Q.set_input_files("#epubInput", big_pdf)
        bigc = Q.locator('#bookGrid .book-card:has-text("Big Magazine")')
        try: await poll(lambda: bigc.count(), 20000)
        except TimeoutError: pass
        await bigc.click()
        await drawn(Q, 15000)
        await Q.evaluate("()=>{const r=document.getElementById('bookSeek');r.value='840';r.dispatchEvent(new Event('input'));r.dispatchEvent(new Event('change'))}")
        bp = await pages_are(Q, [101], 8000)
        pxb = await drawn(Q, 8000)
        lag = await Q.evaluate("()=>new Promise(r=>{const t=performance.now();setTimeout(()=>r(Math.round(performance.now()-t-50)),50)})")
        await Q.click("#searchToggle"); await Q.fill("#bookSearchInput", "Article 117 paragraph 3"); await Q.press("#bookSearchInput", "Enter")
        try: await poll(lambda: _count_gt(Q, "#bookSearchResults button", 0), 20000)
        except TimeoutError: pass
        bres = await Q.locator("#bookSearchResults button").all_inner_texts()
        await Q.keyboard.press("Escape")
        nc = await Q.evaluate("document.querySelectorAll('#viewer canvas').length")
        check("v8.8 PDF: a 120-page PDF opens, seeks to page 101 and searches all pages; the main thread stays responsive",
              bp == [101] and bool(pxb) and pxb["dark"] > 50 and lag < 250 and len(bres) == 1 and "صفحهٔ ۱۱۷" in bres[0] and nc == 1,
              f"{bp} lag={lag} res={bres[:1]} canvases={nc} t={await Q.evaluate('Date.now()') - t0}ms")
        await Q.click("#closeReader"); await Q.wait_for_timeout(300)
        check("v8.8 PDF (desktop): no JS errors, no external requests", not errs28 and not ext28, str(errs28[:3] + ext28[:3]))
        await ctx28.close()
        # Phone (390px, touch): Persian RTL PDF — fit to width, RTL edge taps and swipes, pinch zoom + pan, Persian search
        ext28m, errs28m = [], []
        ctxM, M = await new_ctx(b, url, ext28m, errs28m, has_touch=True, is_mobile=True)
        await M.set_viewport_size({"width": 390, "height": 844})
        await M.goto(url); await M.wait_for_timeout(1000)
        await M.set_input_files("#epubInputSmall", fa_pdf)
        cardm = M.locator('#bookGrid .book-card:has-text("مجلهٔ آزمایشی")')
        try: await poll(lambda: cardm.count(), 15000)
        except TimeoutError: pass
        await cardm.click()
        pxm = await drawn(M)
        geo = await M.evaluate("""()=>{const v=document.getElementById('viewer').getBoundingClientRect(),p=document.querySelector('#viewer .pdf-page').getBoundingClientRect();
          return {vw:Math.round(v.width),pw:Math.round(p.width),pr:Math.round(p.right),sw:document.documentElement.scrollWidth,vh:Math.round(v.height)}}""")
        check("v8.8 PDF mobile 390px: the Persian page fits the viewer width, nothing overflows the screen",
              bool(pxm) and pxm["dark"] > 100 and abs(geo["pw"] - geo["vw"]) <= 4 and geo["pr"] <= 390 and geo["sw"] <= 390 and geo["vh"] > 400, f"{geo} {pxm}")
        check("v8.8 PDF: a Persian PDF is detected RTL (seek bar RTL) and titled from its outline",
              await M.get_attribute("#bookSeek", "dir") == "rtl" and await M.inner_text("#readerChapter") == "فصل نخست", await M.inner_text("#readerChapter"))
        mb = await M.locator("#viewer").bounding_box()
        async def tap(rx):
            await M.touchscreen.tap(mb["x"] + mb["width"] * rx, mb["y"] + mb["height"] * 0.5); await M.wait_for_timeout(900)
        await tap(0.1); m1 = await pages_are(M, [2])
        await tap(0.9); m2 = await pages_are(M, [1])
        check("v8.8 PDF: Persian PDF taps follow RTL (left edge = next, right edge = previous)", m1 == [2] and m2 == [1], f"{m1} {m2}")
        cdp = await ctxM.new_cdp_session(M)
        y = mb["y"] + mb["height"] * 0.5
        await swipe(cdp, mb["x"] + 80, mb["x"] + 300, y); sw1 = await pages_are(M, [2]); await M.wait_for_timeout(600)
        await swipe(cdp, mb["x"] + 300, mb["x"] + 80, y); sw2 = await pages_are(M, [1]); await M.wait_for_timeout(600)
        check("v8.8 PDF: swipes follow RTL (finger to the right = next page)", sw1 == [2] and sw2 == [1], f"{sw1} {sw2}")
        await M.click("#searchToggle"); await M.fill("#bookSearchInput", "فصل سوم"); await M.press("#bookSearchInput", "Enter")
        try: await poll(lambda: _count_gt(M, "#bookSearchResults button", 0), 10000)
        except TimeoutError: pass
        fres = await M.locator("#bookSearchResults button").all_inner_texts()
        check("v8.8 PDF: Persian search finds «فصل سوم» on page 3 (also when the PDF stores glyphs in visual order)",
              len(fres) >= 1 and all("صفحهٔ ۳" in r for r in fres), str(fres[:2]))
        await M.locator("#bookSearchResults button").first.click(); fp3 = await pages_are(M, [3])
        try: await poll(lambda: M.locator("#viewer .textLayer .pdf-hit").count(), 6000)
        except TimeoutError: pass
        check("v8.8 PDF: the Persian match is marked on page 3", fp3 == [3] and await M.locator("#viewer .textLayer .pdf-hit").count() >= 1, str(fp3))
        await M.click("#backChip"); await pages_are(M, [1])
        pw0 = (await drawn(M))["cssW"]
        cx, cy = mb["x"] + mb["width"] / 2, mb["y"] + mb["height"] / 2
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": cx - 40, "y": cy, "id": 1}, {"x": cx + 40, "y": cy, "id": 2}]})
        for i in range(1, 7):
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": cx - 40 - 12 * i, "y": cy, "id": 1}, {"x": cx + 40 + 12 * i, "y": cy, "id": 2}]})
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        try:
            async def pinched(): x = await M.evaluate(PIX); return x and x["cssW"] > pw0 * 1.5
            await poll(pinched, 5000)
        except TimeoutError: pass
        pz = await drawn(M)
        canpan = await M.evaluate("()=>{const s=document.querySelector('#viewer .pdf-stage');return s.scrollWidth>s.clientWidth+2}")
        check("v8.8 PDF: pinch with two fingers zooms the page (re-rendered, pannable), the page stays", pz and pz["cssW"] > pw0 * 1.5 and canpan and await M.evaluate(PAGES) == [1]
              and await M.evaluate("Math.round(visualViewport.scale*100)") == 100, f"{pw0} → {pz and pz['cssW']}")
        sl0 = await M.evaluate("document.querySelector('#viewer .pdf-stage').scrollLeft")
        await swipe(cdp, mb["x"] + 300, mb["x"] + 80, y); await M.wait_for_timeout(700)
        sl1 = await M.evaluate("document.querySelector('#viewer .pdf-stage').scrollLeft")
        check("v8.8 PDF: on a zoomed page a swipe pans instead of turning", await M.evaluate(PAGES) == [1] and sl1 != sl0, f"{sl0} → {sl1}")
        await M.touchscreen.tap(cx, cy); await M.wait_for_timeout(120); await M.touchscreen.tap(cx, cy); await M.wait_for_timeout(900)
        dz = await drawn(M)
        check("v8.8 PDF: double tap resets the zoom", dz and abs(dz["cssW"] - pw0) <= 2 and not await M.evaluate("document.getElementById('reader').classList.contains('immersive')"), str(dz and dz["cssW"]))
        check("v8.8 PDF (phone): no JS errors, no external requests", not errs28m and not ext28m, str(errs28m[:3] + ext28m[:3]))
        await ctxM.close(); shutil.rmtree(tmpp, ignore_errors=True)
        # 29. v8.8.1: several files at once: progress, one summary (added / duplicate / failed), all cards appear
        errsM, extM = [], []
        ctxM, MP = await new_ctx(b, url, extM, errsM)
        await MP.goto(url); await MP.wait_for_timeout(1000)
        multi_ok = await MP.evaluate("[...document.querySelectorAll('input[type=file]')].every(i=>i.multiple)")
        check("v8.8.1: every file picker allows selecting several files", multi_ok)
        await MP.set_input_files("#epubInputSmall" if await MP.is_visible("#epubInputSmall") else "#epubInput",
                                 [str(FIX / "sample-fa.epub"), str(FIX / "sample-fa-2.epub"), str(FIX / "sample-fa.epub"), str(FIX / "corrupt.epub")])
        try:
            await poll(lambda: _contains(MP, "#toast", "کتاب اضافه شد"), 30000)
        except TimeoutError:
            pass
        toast = await MP.inner_text("#toast")
        cards = await MP.locator("#bookGrid .book-card").count()
        check("v8.8.1: summary counts added, duplicate and failed files", "۲ کتاب اضافه شد" in toast and "۱ تکراری" in toast and "۱ ناموفق" in toast and "corrupt.epub" in toast, toast)
        check("v8.8.1: both new books appear in the library", cards == 2, f"cards={cards}")
        unexpected = [e for e in errsM if "Metadata extraction failed" not in e]  # the corrupt file is expected to fail
        check("v8.8.1 multi import: no unexpected JS errors", not unexpected, str(unexpected[:3]))
        await ctxM.close()
        # 30. v8.9 admin: sub-tabs with #admin/<tab> deep links, a server-paged books table (filters in the hash),
        # bulk actions (ids or the whole filter), details panel, review queue with keyboard shortcuts, phone layout.
        # admin-books / admin-bulk / catalog-pending / catalog-review / catalog-update / book-meta are mocked. The login is real but uses
        # the invited user from scenario 10 with role "admin" patched into login/me (the admin's e-mail has used up its login rate
        # limit, 10 per 15 min, by now); the few admin-only routes this section touches are mocked too.
        import json as _json30, time as _time30
        from urllib.parse import urlparse as _up30, parse_qs as _pq30
        ext30, errs30 = [], []
        ctx30, Q = await new_ctx(b, url, ext30, errs30)
        dlg30 = []
        def _dlg30(d):
            dlg30.append(d.message); asyncio.ensure_future(d.accept())
        Q.on("dialog", _dlg30)
        now30 = int(_time30.time())
        CATS30 = ["رمان", "شعر", "تاریخ"]
        books30 = [{"id": 9000 + i, "sha256": "", "size": 150000 + i * 5000, "title": f"کتاب نمونهٔ {i}", "author": f"نویسنده {i % 7}",
                    "language": "fa", "publisher": "", "published": "", "has_cover": i % 10 == 0, "format": "pdf" if i % 9 == 0 else "epub",
                    "catalog_status": [None, "approved", "pending", "rejected", "approved"][i % 5], "category": "" if i % 4 == 0 else CATS30[i % 3],
                    "description": "توضیح" if i % 2 else "", "submitter": "مدیر بُرگ", "uploader_id": 1 if i % 3 else 0,
                    "uploader_name": "مدیر بُرگ" if i % 3 else "", "created_at": now30 - i * 3600, "source": "app" if i % 3 else "api",
                    "in_libraries": i % 4} for i in range(1, 231)]
        pend30 = [{"id": 9501 + k, "sha256": "", "size": 200000, "title": t, "author": "نویسندهٔ صف", "language": "fa", "publisher": "نشر صف",
                   "published": "1401", "has_cover": False, "format": "epub", "description": "یک توضیح کوتاه برای بررسی.", "category": "",
                   "submitter": "دوست", "submitted_at": now30 - k * 60} for k, t in enumerate(["صف یکم", "صف دوم", "صف سوم"])]
        m30 = {"calls": []}
        PNG30 = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                              "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e2b1e9a00000000049454e44ae426082")
        def books_resp30(qs):
            g = lambda k, d="": qs.get(k, [d])[0]
            rows = books30
            if g("q"): rows = [x for x in rows if g("q") in x["title"]]
            if g("status") and g("status") != "any": rows = [x for x in rows if (x["catalog_status"] or "none") == g("status")]
            if g("category"): rows = [x for x in rows if (x["category"] == "" if g("category") == "__none__" else x["category"] == g("category"))]
            if g("format"): rows = [x for x in rows if x["format"] == g("format")]
            if g("uploader"): rows = [x for x in rows if (x["source"] == "api" if g("uploader") == "api" else str(x["uploader_id"]) == g("uploader"))]
            if g("no_cover") == "1": rows = [x for x in rows if not x["has_cover"]]
            page, per = int(g("page", "1")), min(100, int(g("per", "50")))
            st = {k: sum(1 for x in books30 if (x["catalog_status"] or "none") == k) for k in ("approved", "pending", "rejected", "none")}
            return {"books": rows[(page - 1) * per: page * per], "total": len(rows), "page": page, "per": per,
                    "facets": {"status": st, "categories": [{"name": c, "count": sum(1 for x in books30 if x["category"] == c)} for c in CATS30],
                               "uploaders": [{"id": 1, "name": "مدیر بُرگ", "count": 153}], "formats": {"epub": 205, "pdf": 25}}}
        async def api30(route):
            req = route.request
            pu = _up30(req.url); qs = _pq30(pu.query)
            r = qs.get("r", [""])[0]
            if r == "book-cover":
                await route.fulfill(status=200, content_type="image/png", body=PNG30); return
            body = None
            if req.method == "POST":
                try: body = _json30.loads(req.post_data or "null")
                except Exception: body = None
            m30["calls"].append({"m": req.method, "r": r, "qs": qs, "body": body, "csrf": req.headers.get("x-csrf-token")})
            data = {"ok": True}
            if r == "admin-books": data = books_resp30(qs)
            elif r == "admin-bulk": data = {"affected": len(body.get("ids") or []) or 230, "skipped": 2 if body.get("filter") else 0}
            elif r == "catalog-pending": data = {"books": pend30}
            elif r == "admin-stats": data = {"users": 2, "active_users": 2, "books": 230, "storage_bytes": 52428800, "catalog_approved": 92,
                                             "catalog_pending": len(pend30), "open_invites": 0, "client_errors": 0}
            elif r == "settings": data = {"catalog_requires_approval": True}
            elif r == "clean-rules": data = {"rules": []}
            elif r == "users": data = {"users": []}
            elif r == "catalog-review":
                pend30[:] = [x for x in pend30 if x["id"] != body["book_id"]]
                data = {"status": "approved" if body.get("approve") else "rejected"}
            await route.fulfill(status=200, content_type="application/json", body=_json30.dumps(data))
        MOCK30 = ("admin-books", "admin-bulk", "catalog-pending", "catalog-review", "catalog-update", "book-meta", "admin-stats", "settings", "clean-rules", "users")
        await ctx30.route(lambda u: any(f"/api/?r={x}" in u for x in MOCK30) or ("/api/?r=book-cover" in u and "id=9" in u), api30)
        async def as_admin30(route):
            resp = await route.fetch()
            try:
                j = await resp.json()
                if isinstance(j, dict) and j.get("user"): j["user"]["role"] = "admin"
                await route.fulfill(response=resp, json=j)
            except Exception:
                await route.fulfill(response=resp)
        await ctx30.route(lambda u: u.endswith("/api/?r=login") or u.endswith("/api/?r=me"), as_admin30)
        # This section covers the books tab without a category list (the free-text category flows); v8.10 trees are section 32
        async def no_cats30(route): await route.fulfill(status=200, content_type="application/json", body='{"categories": []}')
        await ctx30.route(lambda u: "/api/?r=categories" in u, no_cats30)
        calls30 = lambda r, m=None: [c for c in m30["calls"] if c["r"] == r and (m is None or c["m"] == m)]
        last_books30 = lambda: (calls30("admin-books") or [{"qs": {}}])[-1]["qs"]
        qv30 = lambda qs, k: qs.get(k, [None])[0]
        await Q.goto(url); await Q.wait_for_timeout(800)
        h30 = await (await Q.request.get(url + "api/?r=health")).json()
        if h30.get("needs_setup"):  # this section run on its own
            await auth(Q, "setup", setup_key=SETUP_KEY, name="مدیر بُرگ", email="admin@example.com", password="password1")
        else:
            await auth(Q, "login", email="friend@example.com", password="password2")
        await wait_synced(Q)
        # Tabs + overview
        await Q.click('[data-view="admin"]')
        try: await poll(lambda: _count_gt(Q, "#adminStats .stat", 6), 8000)
        except TimeoutError: pass
        tabs = await Q.locator("#adminTabs [role=tab]").all_inner_texts()
        check("v8.9 admin: nine sub-tabs (v8.10 «دسته‌ها»), overview first with the stats cards, hash #admin/overview",
              len(tabs) == 9 and tabs[0].strip() == "نمای کلی" and "صف تأیید" in tabs[1] and tabs[3].strip() == "دسته‌ها" and "خطاها" in tabs[7]
              and await Q.locator("#adminStats .stat").count() == 7 and await Q.is_visible("#adminShortcuts")
              and await Q.evaluate("location.hash") == "#admin/overview", str(tabs))
        check("v8.9 admin: only the active tab's data is loaded (no books/queue/keys requests on the overview)",
              not calls30("admin-books") and not calls30("catalog-pending") and await Q.locator("#apiTokenList .api-item").count() == 0, str([c["r"] for c in m30["calls"]]))
        # Books tab: 50 rows of 230, paging text, request params
        await Q.click('[data-admin-tab="books"]')
        try: await poll(lambda: _count_gt(Q, "#admBooks tr.adm-row", 49), 8000)
        except TimeoutError: pass
        q1 = last_books30()
        summ = await Q.inner_text("#admBooksSummary")
        check("v8.9 books: 50 rows from a 230-book result, «صفحهٔ ۱ از ۵ · ۲۳۰ کتاب», request page=1 per=50 sort=date desc",
              await Q.locator("#admBooks tr.adm-row").count() == 50 and "صفحهٔ ۱ از ۵ · ۲۳۰ کتاب" in summ
              and qv30(q1, "page") == "1" and qv30(q1, "per") == "50" and qv30(q1, "sort") == "date" and qv30(q1, "dir") == "desc"
              and await Q.evaluate("location.hash") == "#admin/books", f"{summ} {q1}")
        row0 = Q.locator("#admBooks tr.adm-row").first
        r0 = await row0.inner_text()
        check("v8.9 books: a row shows title, author, format chip, category, status, uploader, Persian date, size, libraries",
              "کتاب نمونهٔ 1" in r0 and "نویسنده" in r0 and "EPUB" in r0 and "شعر" in r0 and "عمومی" in r0
              and "مدیر بُرگ" in r0 and "KB" in r0 and await row0.locator(".adm-c-date").inner_text() != "" and await row0.locator(".adm-thumb").is_visible(), r0)
        api_row = Q.locator('#admBooks tr[data-id="9003"]')
        check("v8.9 books: books sent with an API key show an «API» chip as uploader; lazy cover images",
              await api_row.locator(".adm-c-up .adm-chip.api").inner_text() == "API"
              and await Q.locator('#admBooks tr[data-id="9010"] img[loading="lazy"]').count() == 1)
        # Next page
        await Q.click("#admNext")
        try: await poll(lambda: _contains(Q, "#admBooksSummary", "صفحهٔ ۲ از ۵"), 6000)
        except TimeoutError: pass
        check("v8.9 books: «بعدی» loads page 2 from the server and puts it in the hash",
              qv30(last_books30(), "page") == "2" and "صفحهٔ ۲ از ۵" in await Q.inner_text("#admBooksSummary")
              and "page=2" in await Q.evaluate("location.hash") and await Q.locator('#admBooks tr[data-id="9051"]').count() == 1)
        # Debounced search
        n_before = len(calls30("admin-books"))
        await Q.click("#admBookSearch"); await Q.keyboard.type("نمونهٔ 12", delay=40); await Q.wait_for_timeout(700)
        sq = calls30("admin-books")[n_before:]
        check("v8.9 books: search is debounced (one request after typing) and sends q, back on page 1",
              len(sq) == 1 and qv30(sq[0]["qs"], "q") == "نمونهٔ 12" and qv30(sq[0]["qs"], "page") == "1"
              and await Q.locator("#admBooks tr.adm-row").count() == 11, f"{len(sq)} {sq[-1]['qs'] if sq else ''}")
        await Q.fill("#admBookSearch", ""); await Q.wait_for_timeout(600)
        # Filters
        await Q.select_option("#admFStatus", "pending"); await Q.wait_for_timeout(500)
        await Q.select_option("#admFCategory", "__none__"); await Q.wait_for_timeout(500)
        await Q.select_option("#admFFormat", "epub"); await Q.wait_for_timeout(500)
        await Q.select_option("#admFUploader", "api"); await Q.wait_for_timeout(500)
        await Q.check("#admFNoCover"); await Q.wait_for_timeout(500)
        await Q.select_option("#admSort", "title:asc"); await Q.wait_for_timeout(600)
        fq = last_books30()
        check("v8.9 books: filters and sort are sent as status/category(__none__)/format/uploader(api)/no_cover/sort/dir",
              qv30(fq, "status") == "pending" and qv30(fq, "category") == "__none__" and qv30(fq, "format") == "epub" and qv30(fq, "uploader") == "api"
              and qv30(fq, "no_cover") == "1" and qv30(fq, "sort") == "title" and qv30(fq, "dir") == "asc" and qv30(fq, "q") is None, str(fq))
        cat_opts = await Q.locator("#admFCategory option").all_inner_texts()
        up_opts = await Q.locator("#admFUploader option").all_inner_texts()
        check("v8.9 books: category and uploader filters come from the facets (incl. «بدون دسته» and «API»)",
              "بدون دسته" in cat_opts and any("رمان" in o for o in cat_opts) and "API" in up_opts and any("مدیر بُرگ" in o for o in up_opts), f"{cat_opts} {up_opts}")
        hash30 = await Q.evaluate("location.hash")
        await Q.reload(); await Q.wait_for_timeout(600)
        try: await poll(lambda: _count_gt(Q, "#admBooks tr.adm-row, #admBooks tr.adm-empty", 0), 10000)
        except TimeoutError: pass
        rq = last_books30()
        check("v8.9 books: refresh keeps the tab and every filter (hash query → controls → request)",
              await Q.evaluate("location.hash") == hash30 and await Q.input_value("#admFStatus") == "pending" and await Q.input_value("#admFCategory") == "__none__"
              and await Q.is_checked("#admFNoCover") and await Q.input_value("#admSort") == "title:asc" and qv30(rq, "uploader") == "api"
              and await Q.get_attribute("#admTab-books", "aria-selected") == "true", f"{hash30} {rq}")
        await Q.click("#admFReset"); await Q.wait_for_timeout(500)
        await Q.select_option("#admSort", "date:desc"); await Q.wait_for_timeout(600)
        check("v8.9 books: «پاک کردن فیلترها» goes back to all 230 books", "۲۳۰ کتاب" in await Q.inner_text("#admBooksSummary")
              and qv30(last_books30(), "status") is None)
        # Selection: shift-click range, page, all results
        await Q.locator('#admBooks tr.adm-row').nth(1).locator(".adm-sel").click()
        await Q.locator('#admBooks tr.adm-row').nth(5).locator(".adm-sel").click(modifiers=["Shift"])
        check("v8.9 bulk: shift-click selects the range (5 rows) and the bar shows the count",
              await Q.locator("#admBooks tr.adm-row.selected").count() == 5 and await Q.is_visible("#admBulkBar")
              and "۵ کتاب انتخاب شد" in await Q.inner_text("#admBulkCount"))
        await Q.click('#admBulkBar [data-bulk="set_category"]'); await Q.wait_for_timeout(300)
        cat_dialog = await Q.is_visible("#admCatDialog")
        await Q.fill("#admCatInput", "رمان"); await Q.press("#admCatInput", "Enter")
        try: await poll(lambda: asyncio.sleep(0, len(calls30("admin-bulk")) >= 1), 5000)
        except TimeoutError: pass
        bc = calls30("admin-bulk")
        check("v8.9 bulk: «تعیین دسته» asks in a dialog (categories suggested) and sends ids + category",
              cat_dialog and len(bc) == 1 and bc[0]["body"]["action"] == "set_category" and bc[0]["body"]["category"] == "رمان"
              and sorted(bc[0]["body"]["ids"]) == [9002, 9003, 9004, 9005, 9006] and "filter" not in bc[0]["body"] and bool(bc[0]["csrf"])
              and await Q.locator("#admCatList option").count() == 3, str(bc[:1]))
        try: await poll(lambda: _contains(Q, "#toast", "انجام شد"), 4000)
        except TimeoutError: pass
        check("v8.9 bulk: toast with the affected count, selection cleared, page reloaded",
              "انجام شد: ۵ کتاب" in await Q.inner_text("#toast") and not await Q.is_visible("#admBulkBar"), await Q.inner_text("#toast"))
        await Q.check("#admSelPage"); await Q.wait_for_timeout(200)
        check("v8.9 bulk: «انتخاب همهٔ این صفحه» selects 50 and offers «انتخاب همهٔ ۲۳۰ نتیجه»",
              await Q.locator("#admBooks tr.adm-row.selected").count() == 50 and "۵۰ کتاب انتخاب شد" in await Q.inner_text("#admBulkCount")
              and await Q.is_visible("#admSelectAll") and "۲۳۰" in await Q.inner_text("#admSelectAll"))
        await Q.click('#admBulkBar [data-bulk="approve"]')
        try: await poll(lambda: asyncio.sleep(0, len(calls30("admin-bulk")) >= 2), 5000)
        except TimeoutError: pass
        bc = calls30("admin-bulk")
        check("v8.9 bulk: approve the page sends its 50 ids (no confirmation needed)",
              len(bc) == 2 and bc[1]["body"]["action"] == "approve" and len(bc[1]["body"]["ids"]) == 50 and len(dlg30) == 0, str(len(bc)))
        await Q.select_option("#admFStatus", "approved"); await Q.wait_for_timeout(600)
        await Q.check("#admSelPage"); await Q.wait_for_timeout(200)
        total_appr = sum(1 for x in books30 if x["catalog_status"] == "approved")
        await Q.click("#admSelectAll"); await Q.wait_for_timeout(200)
        sel_all_txt = await Q.inner_text("#admBulkCount")
        await Q.click('#admBulkBar [data-bulk="delete"]')
        try: await poll(lambda: asyncio.sleep(0, len(calls30("admin-bulk")) >= 3), 5000)
        except TimeoutError: pass
        bc = calls30("admin-bulk")
        fa_total = str(total_appr).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        check("v8.9 bulk: «انتخاب همهٔ N نتیجه» + delete confirms with the exact count and sends the filter instead of ids",
              f"همهٔ {fa_total} نتیجه" in sel_all_txt and len(dlg30) == 1 and fa_total in dlg30[0] and "حذف" in dlg30[0]
              and len(bc) == 3 and bc[2]["body"]["action"] == "delete" and "ids" not in bc[2]["body"] and bc[2]["body"].get("filter") == {"status": "approved"},
              f"{sel_all_txt} {dlg30} {bc[-1:]}")
        try: await poll(lambda: _contains(Q, "#toast", "نادیده"), 4000)
        except TimeoutError: pass
        check("v8.9 bulk: the toast reports skipped items too", "۲ مورد نادیده گرفته شد" in await Q.inner_text("#toast"), await Q.inner_text("#toast"))
        await Q.select_option("#admFStatus", "any"); await Q.wait_for_timeout(600)
        # Details panel (desktop side panel), save through catalog-update / book-meta
        await Q.locator('#admBooks tr[data-id="9002"] .adm-c-title').click(); await Q.wait_for_timeout(400)
        dg = await Q.evaluate("() => { const r = document.getElementById('admDetail').getBoundingClientRect(); return { l: r.left, r: r.right, t: r.top, h: r.height, w: r.width, vh: innerHeight }; }")
        check("v8.9 details: clicking a row opens a side panel (left edge in RTL, full height) with the book's fields",
              await Q.is_visible("#admDetail") and dg["l"] <= 1 and dg["w"] <= 470 and dg["h"] >= dg["vh"] - 2
              and await Q.input_value('#admDetailForm [name="title"]') == "کتاب نمونهٔ 2" and "در انتظار" in await Q.inner_text("#admDetailChips")
              and await Q.locator('#admDetailStatus [data-dst="approve"]').is_visible(), str(dg))
        await Q.fill('#admDetailForm [name="category"]', "تاریخ")
        await Q.fill('#admDetailForm [name="publisher"]', "نشر نمونه")
        await Q.click("#admDetailSave")
        try:
            await poll(lambda: asyncio.sleep(0, len(calls30("catalog-update")) >= 1), 5000)
            await poll(lambda: Q.is_hidden("#admDetail"), 4000)
        except TimeoutError: pass
        cu = calls30("catalog-update")
        check("v8.9 details: «ذخیره» sends every field through catalog-update and closes the panel",
              len(cu) == 1 and cu[0]["body"]["book_id"] == 9002 and cu[0]["body"]["category"] == "تاریخ" and cu[0]["body"]["publisher"] == "نشر نمونه"
              and cu[0]["body"]["title"] == "کتاب نمونهٔ 2" and not await Q.is_visible("#admDetail"), str(cu))
        await Q.locator('#admBooks tr[data-id="9005"] .adm-c-title').click(); await Q.wait_for_timeout(300)
        none_ok = await Q.is_disabled('#admDetailForm [name="category"]') and await Q.is_visible('#admDetailStatus [data-dst="add_catalog"]')
        await Q.fill('#admDetailForm [name="author"]', "نویسندهٔ تازه"); await Q.click("#admDetailSave")
        try: await poll(lambda: asyncio.sleep(0, len(calls30("book-meta")) >= 1), 5000)
        except TimeoutError: pass
        bm = [c for c in calls30("book-meta") if (c["body"] or {}).get("book_id") == 9005]
        check("v8.9 details: a book outside the public catalog saves through book-meta (no category/description) and offers «افزودن به کتابخانهٔ عمومی»",
              none_ok and len(bm) == 1 and bm[0]["body"]["author"] == "نویسندهٔ تازه" and "category" not in bm[0]["body"], str(bm))
        # Review queue with keyboard shortcuts
        await Q.click('[data-admin-tab="queue"]')
        try: await poll(lambda: _contains(Q, "#admReview", "صف یکم"), 6000)
        except TimeoutError: pass
        check("v8.9 queue: review mode shows one pending book (cover, metadata, description, uploader) with shortcut hints",
              "کتاب ۱ از ۳" in await Q.inner_text("#admReview") and "دوست" in await Q.inner_text("#admReview") and "یک توضیح کوتاه" in await Q.inner_text("#admReview")
              and await Q.is_visible("#admReviewKeys") and await Q.locator("#admReview kbd").count() >= 3 and await Q.evaluate("location.hash") == "#admin/queue")
        await Q.keyboard.press("j"); await Q.wait_for_timeout(150)
        j_ok = "کتاب ۲ از ۳" in await Q.inner_text("#admReview") and "صف دوم" in await Q.inner_text("#admReview")
        await Q.keyboard.press("k"); await Q.wait_for_timeout(150)
        k_ok = "صف یکم" in await Q.inner_text("#admReview")
        await Q.keyboard.press("ArrowLeft"); await Q.wait_for_timeout(150)
        al_ok = "صف دوم" in await Q.inner_text("#admReview")
        await Q.keyboard.press("ArrowRight"); await Q.wait_for_timeout(150)
        check("v8.9 queue: J / ← next, K / → previous", j_ok and k_ok and al_ok and "صف یکم" in await Q.inner_text("#admReview"))
        await Q.keyboard.press("a")
        try: await poll(lambda: _contains(Q, "#admReview", "کتاب ۱ از ۲"), 4000)
        except TimeoutError: pass
        rv = calls30("catalog-review")
        check("v8.9 queue: A approves the shown book (catalog-review) and the next one appears",
              len(rv) == 1 and rv[0]["body"] == {"book_id": 9501, "approve": True} and "صف دوم" in await Q.inner_text("#admReview")
              and await Q.inner_text("#admQueueCount") == "۲", str(rv))
        await Q.keyboard.press("c"); await Q.wait_for_timeout(300)
        c_open = await Q.is_visible("#admCatDialog")
        await Q.fill("#admCatInput", "شعر"); await Q.press("#admCatInput", "Enter")
        try: await poll(lambda: _contains(Q, "#admReview .adm-detail-chips", "شعر"), 4000)
        except TimeoutError: pass
        cu = calls30("catalog-update")
        check("v8.9 queue: C opens the category dialog and saves via catalog-update", c_open and cu[-1]["body"] == {"book_id": 9502, "category": "شعر"}
              and "شعر" in await Q.inner_text("#admReview"), str(cu[-1:]))
        await Q.keyboard.press("r")
        try: await poll(lambda: _contains(Q, "#admReview", "کتاب ۱ از ۱"), 4000)
        except TimeoutError: pass
        rv = calls30("catalog-review")
        check("v8.9 queue: R rejects", len(rv) == 2 and rv[1]["body"] == {"book_id": 9502, "approve": False} and "صف سوم" in await Q.inner_text("#admReview"), str(rv))
        await Q.click('[data-queue-mode="list"]'); await Q.wait_for_timeout(200)
        check("v8.9 queue: the compact list view lists the pending books with approve/reject",
              await Q.is_visible("#pendingList") and not await Q.is_visible("#admReview") and await Q.locator("#pendingList [data-review]").count() == 2)
        await Q.click('[data-queue-mode="review"]')
        # Back / forward between tabs, deep link on a fresh load
        await Q.click('[data-admin-tab="users"]'); await Q.wait_for_timeout(500)
        await Q.go_back(); await Q.wait_for_timeout(500)
        back_ok = await Q.get_attribute("#admTab-queue", "aria-selected") == "true" and await Q.is_visible("#admReview")
        await Q.goto(url + "#admin/settings"); await Q.reload(); await Q.wait_for_timeout(300)
        try: await poll(lambda: Q.is_visible("#settingApproval"), 8000)
        except TimeoutError: pass
        check("v8.9 admin: back returns to the previous tab; a #admin/settings link opens that tab directly",
              back_ok and await Q.is_visible("#settingApproval") and await Q.get_attribute("#admTab-settings", "aria-selected") == "true"
              and not await Q.is_visible("#admBooks"))
        sticky = await Q.evaluate("getComputedStyle(document.getElementById('adminTabs')).position")
        check("v8.9 admin: the sub-navigation is sticky", sticky == "sticky", sticky)
        # Phone: list rows, filters sheet, details sheet, no horizontal overflow
        await Q.set_viewport_size({"width": 390, "height": 844}); await Q.wait_for_timeout(200)
        await Q.click('[data-admin-tab="books"]')
        try: await poll(lambda: _count_gt(Q, "#admBooks tr.adm-row", 49), 8000)
        except TimeoutError: pass
        mg = await Q.evaluate("""() => { const tr = document.querySelector('#admBooks tr.adm-row'), r = tr.getBoundingClientRect();
            const vis = s => { const e = tr.querySelector(s); return e && e.getBoundingClientRect().width > 0; };
            return { h: r.height, w: r.width, l: r.left, rr: r.right, disp: getComputedStyle(tr).display, head: document.querySelector('.adm-table thead').getBoundingClientRect().height,
                     date: vis('.adm-c-date'), meta: vis('.adm-m'), sel: tr.querySelector('.adm-sel').getBoundingClientRect().width,
                     sw: document.documentElement.scrollWidth, filt: document.getElementById('admFilters').getBoundingClientRect().height }; }""")
        check("v8.9 mobile 390px: compact list rows (checkbox, cover, title + chips), no table header, filters folded, no horizontal scroll",
              mg["disp"] == "grid" and mg["h"] < 100 and mg["head"] == 0 and not mg["date"] and mg["meta"] and mg["sel"] >= 20
              and mg["rr"] <= 391 and mg["l"] >= -1 and mg["sw"] <= 390 and mg["filt"] == 0 and await Q.is_visible("#admFiltersOpen"), str(mg))
        await Q.click("#admFiltersOpen"); await Q.wait_for_timeout(400)
        sh = await Q.evaluate("() => { const r = document.getElementById('admFilters').getBoundingClientRect(); return { t: r.top, b: r.bottom, l: r.left, r: r.right, vh: innerHeight }; }")
        check("v8.9 mobile: «فیلترها» opens a bottom sheet with the filters", await Q.is_visible("#admFStatus") and abs(sh["b"] - sh["vh"]) <= 2
              and sh["l"] >= -1 and sh["r"] <= 391 and sh["t"] > 100 and await Q.is_visible("#admFApply"), str(sh))
        await Q.select_option("#admFStatus", "rejected"); await Q.wait_for_timeout(500)
        await Q.click("#admFApply"); await Q.wait_for_timeout(400)
        check("v8.9 mobile: a filter chosen in the sheet is sent; the button shows the active-filter count",
              qv30(last_books30(), "status") == "rejected" and not await Q.is_visible("#admFStatus")
              and await Q.inner_text("#admFilterCount") == "۱" and await Q.is_visible("#admFilterCount"))
        await Q.locator("#admBooks tr.adm-row").first.locator(".adm-sel").click(); await Q.wait_for_timeout(200)
        bb = await Q.evaluate("() => { const r = document.getElementById('admBulkBar').getBoundingClientRect(); return { l: r.left, r: r.right, b: r.bottom, vh: innerHeight, sw: document.documentElement.scrollWidth }; }")
        check("v8.9 mobile: the bulk bar fits the screen at the bottom", await Q.is_visible("#admBulkBar") and bb["l"] >= 0 and bb["r"] <= 390 and bb["b"] <= bb["vh"] and bb["sw"] <= 390, str(bb))
        await Q.click("#admBulkClear")
        await Q.locator("#admBooks tr.adm-row").first.locator(".adm-c-title").click(); await Q.wait_for_timeout(400)
        ds = await Q.evaluate("() => { const r = document.getElementById('admDetail').getBoundingClientRect(); return { t: r.top, b: r.bottom, l: r.left, r: r.right, vh: innerHeight }; }")
        check("v8.9 mobile: the details panel is a bottom sheet", await Q.is_visible("#admDetail") and abs(ds["b"] - ds["vh"]) <= 2 and ds["l"] >= -1 and ds["r"] <= 391, str(ds))
        await Q.click("#admDetailClose")
        await Q.click('[data-admin-tab="queue"]'); await Q.wait_for_timeout(600)
        qsw = await Q.evaluate("() => ({ sw: document.documentElement.scrollWidth, r: document.querySelector('#admReview .adm-rv').getBoundingClientRect().right })")
        check("v8.9 mobile: review card and tabs fit (no horizontal page scroll)", qsw["sw"] <= 390 and qsw["r"] <= 391, str(qsw))
        await Q.set_viewport_size({"width": 1280, "height": 800})
        check("v8.9 admin: no JS errors, no external requests", not errs30 and not ext30, str(errs30[:3] + ext30[:3]))
        await ctx30.close()
        # 31. v8.9 catalog paging: the public catalog is loaded page by page (server mocked: 130 books), search/filters/sort
        # go to the server, scroll position survives a book page, similar books need no full list, old servers still work
        import json as _json, re as _re
        from urllib.parse import urlparse as _up, parse_qs as _pq, quote as _quote
        CAT_N = 130
        CAT_CATS = ["رمان", "شعر", "تاریخ"]
        cat_books = [{"id": 9000 + i, "sha256": f"{i:064x}", "size": 40000 + i, "title": f"کتاب نمونه {i}", "author": f"نویسنده {i % 5}",
                      "language": "fa", "publisher": "", "published": "", "has_cover": False, "format": "pdf" if i % 4 == 0 else "epub",
                      "description": f"توضیح {i}", "category": CAT_CATS[i % 3], "submitter": "مدیر", "in_library": False} for i in range(1, CAT_N + 1)]
        cat_state = {"paged": True, "reqs": []}
        async def cat_route(route):
            u = route.request.url
            cat_state["reqs"].append(u)
            qs = {k: v[0] for k, v in _pq(_up(u).query).items()}
            if not cat_state["paged"] or "page" not in qs:
                body = {"books": cat_books}
            else:
                bs = list(cat_books)
                if qs.get("q"): bs = [x for x in bs if qs["q"] in x["title"]]
                if qs.get("author"): bs = [x for x in bs if x["author"] == qs["author"]]
                if qs.get("format"): bs = [x for x in bs if x["format"] == qs["format"]]
                if qs.get("id"): bs = [x for x in bs if str(x["id"]) == qs["id"]]
                facets = [{"name": c, "count": sum(1 for x in bs if x["category"] == c)} for c in CAT_CATS]
                if qs.get("category"): bs = [x for x in bs if x["category"] == qs["category"]]
                if qs.get("sort") == "title": bs.sort(key=lambda x: x["title"])
                elif qs.get("sort") == "author": bs.sort(key=lambda x: (x["author"], x["title"]))
                else: bs.sort(key=lambda x: -x["id"])
                page, per = int(qs["page"]), min(60, int(qs.get("per", 24)))
                body = {"books": bs[(page - 1) * per: page * per], "total": len(bs), "page": page, "per": per, "categories": facets}
            await route.fulfill(status=200, content_type="application/json; charset=utf-8", body=_json.dumps(body, ensure_ascii=False))
        CAT_RE = _re.compile(r"[?&]r=catalog(&|$)")
        def cat_reqs(): return [x for x in cat_state["reqs"]]
        async def cat_ctx(width=1280, height=800):
            e, x = [], []
            c = await b.new_context(viewport={"width": width, "height": height}, service_workers="block")
            await categories_fallback(c)
            await c.add_init_script(GUIDE_SEEN_JS)
            async def block(route):
                x.append(route.request.url); await route.abort()
            await c.route(lambda u: not u.startswith("http://localhost"), block)
            await c.route(CAT_RE, cat_route)
            # v8.10: this section covers the catalog without a category list (the real server may have some by now)
            async def no_cats(route): await route.fulfill(status=200, content_type="application/json", body='{"categories": []}')
            await c.route(lambda u: "/api/?r=categories" in u, no_cats)
            pg = await c.new_page()
            pg.on("pageerror", lambda er: e.append(str(er)))
            pg.on("console", lambda m: m.type == "error" and e.append(m.text))
            await pg.goto(url); await pg.wait_for_timeout(800)
            await pg.click("#accountButton"); await pg.wait_for_timeout(500)
            await pg.click('[data-auth="login"]')
            # The invited user: the admin's e-mail has used up its login rate limit (10 / 15 min) by this point
            await pg.fill('#authForm [name="email"]', "friend@example.com"); await pg.fill('#authForm [name="password"]', "password2")
            await pg.click("#authSubmit")
            await poll(lambda: pg.is_visible('[data-view="catalog"]'), 15000)
            await pg.wait_for_timeout(500)
            return c, pg, e, x
        async def ncards(pg): return await pg.locator("#catalogGrid .book-card").count()
        ctxP, P, errsP, extP = await cat_ctx()
        cat_state["reqs"].clear()
        await P.click('[data-view="catalog"]')
        try: await poll(lambda: _count_gt(P, "#catalogGrid .book-card", 0), 10000)
        except TimeoutError: pass
        await P.wait_for_timeout(300)
        first = cat_reqs()[:1]
        check("v8.9 catalog: first page only (24 of 130)", await ncards(P) == 24 and "۱۳۰ کتاب" in await P.inner_text("#catalogCount"),
              f"cards={await ncards(P)} count={await P.inner_text('#catalogCount')}")
        check("v8.9 catalog: first request asks for page 1, 24 per page", bool(first) and "page=1" in first[0] and "per=24" in first[0], str(first))
        check("v8.9 catalog: «بیشتر» button shown while more books exist", await P.is_visible("#catalogMoreButton") and "بیشتر" in await P.inner_text("#catalogMoreButton"))
        for _ in range(40):
            if await ncards(P) >= CAT_N: break
            await P.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)"); await P.wait_for_timeout(350)
        ids = await P.eval_on_selector_all("#catalogGrid .book-card", "els => els.map(e => e.dataset.detail)")
        check("v8.9 catalog: scrolling loads more until all 130, no duplicates", len(ids) == CAT_N and len(set(ids)) == CAT_N, f"{len(ids)} cards, {len(set(ids))} unique")
        pages = sorted({int(_pq(_up(u).query)["page"][0]) for u in cat_reqs() if "page=" in u})
        check("v8.9 catalog: pages 1..6 requested once each", pages == [1, 2, 3, 4, 5, 6] and len([u for u in cat_reqs() if "page=" in u]) == 6, str(pages))
        check("v8.9 catalog: «بیشتر» hidden at the end", not await P.is_visible("#catalogMore"))
        # Book page and back: position and loaded pages kept
        card = P.locator('#catalogGrid .book-card[data-detail="9070"]')
        await card.scroll_into_view_if_needed(); await P.evaluate("window.scrollBy(0, 120)"); await P.wait_for_timeout(300)
        y0 = await P.evaluate("window.scrollY")
        CARD_TOP = "Math.round(document.querySelector('#catalogGrid .book-card[data-detail=\"9070\"]').getBoundingClientRect().top)"
        t0 = await P.evaluate(CARD_TOP)
        n_before = len(cat_reqs())
        await card.click(); await P.wait_for_timeout(800)
        check("v8.9 catalog: book page opens from a later page", await P.is_visible("#viewBook") and await P.inner_text("#detailTitle") == "کتاب نمونه 70"
              and P.url.endswith("#catalog/book/9070"), P.url)
        try: await poll(lambda: P.is_visible("#detailRelatedSection"), 5000)
        except TimeoutError: pass
        rel = await P.locator("#detailRelated .book-card h3").all_inner_texts()
        rel_reqs = cat_reqs()[n_before:]
        check("v8.9 similar books: small author page then category, no full list",
              any("per=6" in u and "author=" + _quote("نویسنده 0") in u for u in rel_reqs) and all("page=" in u for u in rel_reqs)
              and 0 < len(rel) <= 6 and "کتاب نمونه 70" not in rel, f"{rel} {rel_reqs}")
        n_before = len(cat_reqs())
        await P.go_back(); await P.wait_for_timeout(700)
        y1 = await P.evaluate("window.scrollY"); t1 = await P.evaluate(CARD_TOP)
        check("v8.9 catalog: back from the book page keeps the loaded books and the scroll position",
              await P.is_visible("#viewCatalog") and await ncards(P) == CAT_N and abs(t1 - t0) < 100 and len(cat_reqs()) == n_before,
              f"y {y0}->{y1}, card top {t0}->{t1}, cards={await ncards(P)}, new requests={cat_reqs()[n_before:]}")
        # Search: debounced, sent to the server
        await P.evaluate("window.scrollTo(0, 0)")
        n_before = len(cat_reqs())
        await P.type("#catalogSearch", "نمونه 12", delay=40); await P.wait_for_timeout(900)
        qreqs = [u for u in cat_reqs()[n_before:] if "q=" in u]
        check("v8.9 catalog search: one debounced request with q", len(qreqs) == 1 and "q=" + _quote("نمونه 12") in qreqs[0] and "page=1" in qreqs[0], str(qreqs))
        check("v8.9 catalog search: results and count from the server", await ncards(P) == 11 and "۱۱ کتاب" in await P.inner_text("#catalogCount"),
              f"{await ncards(P)} {await P.inner_text('#catalogCount')}")
        await P.fill("#catalogSearch", "چیزی-که-نیست"); await P.wait_for_timeout(700)
        check("v8.9 catalog: empty result shows a message and a reset button", await ncards(P) == 0 and "پیدا نشد" in await P.inner_text("#catalogGrid")
              and await P.is_visible("[data-catalog-reset]") and not await P.is_visible("#catalogEmpty") and not await P.is_visible("#catalogMore"))
        await P.click("[data-catalog-reset]"); await P.wait_for_timeout(700)
        check("v8.9 catalog: reset brings back the first page", await ncards(P) == 24 and await P.input_value("#catalogSearch") == "" and "۱۳۰ کتاب" in await P.inner_text("#catalogCount"))
        # The old free-text category (v8.10: no flat chips; reached from a book page, shown as a removable chip)
        check("v8.10 catalog: no flat category chips, no navigator without a category list", await P.locator("#catalogFilters [data-cat-filter]").count() == 0
              and not await P.is_visible("#catNav"))
        await P.click('#catalogGrid .book-card[data-detail="9130"]'); await P.wait_for_timeout(600)
        n_before = len(cat_reqs())
        await P.click('#detailMeta [data-meta-cat]'); await P.wait_for_timeout(700)
        creqs = cat_reqs()[n_before:]
        check("v8.9 catalog: a book's category link → category param and a removable chip", any("category=" + _quote("شعر") in u and "page=1" in u for u in creqs)
              and "۴۴ کتاب" in await P.inner_text("#catalogCount") and await P.is_visible("#catalogFilters [data-clear-category]"), str(creqs))
        # Format and sort
        n_before = len(cat_reqs())
        await P.click('#catalogFormat [data-format="pdf"]'); await P.wait_for_timeout(600)
        freqs = cat_reqs()[n_before:]
        badges = await P.locator("#catalogGrid .book-card .format-badge").count()
        check("v8.9 catalog: format filter → format=pdf (combined with the category)", any("format=pdf" in u and "category=" in u for u in freqs)
              and await ncards(P) > 0 and badges == await ncards(P), f"{freqs} cards={await ncards(P)} badges={badges}")
        n_before = len(cat_reqs())
        await P.select_option("#catalogSort", "title"); await P.wait_for_timeout(600)
        sreqs = cat_reqs()[n_before:]
        check("v8.9 catalog: sort select → sort=title", any("sort=title" in u and "format=pdf" in u for u in sreqs), str(sreqs))
        await P.click('#catalogFormat [data-format=""]'); await P.click("#catalogFilters [data-clear-category]"); await P.select_option("#catalogSort", "new")
        await P.wait_for_timeout(700)
        # Author filter from a book page
        await P.click('#catalogGrid .book-card >> nth=0'); await P.wait_for_timeout(700)
        auth_name = await P.inner_text("#detailAuthor")
        n_before = len(cat_reqs())
        await P.click("#detailAuthor"); await P.wait_for_timeout(700)
        areqs = cat_reqs()[n_before:]
        check("v8.9 catalog: author link → author param and a removable chip", await P.is_visible("#viewCatalog")
              and any("author=" + _quote(auth_name) in u and "page=1" in u for u in areqs) and await P.is_visible("#catalogFilters [data-clear-author]")
              and "۲۶ کتاب" in await P.inner_text("#catalogCount"), f"{auth_name} {areqs}")
        await P.click("#catalogFilters [data-clear-author]"); await P.wait_for_timeout(600)
        check("v8.9 catalog: removing the author chip reloads everything", "۱۳۰ کتاب" in await P.inner_text("#catalogCount"))
        # Mobile: toolbar and grid fit 390 px
        await P.set_viewport_size({"width": 390, "height": 844}); await P.wait_for_timeout(400)
        sw = await P.evaluate("document.documentElement.scrollWidth")
        tb = await P.locator(".catalog-toolbar").bounding_box(); ms = await P.locator("#catalogSort").bounding_box()
        check("v8.9 catalog: mobile 390px fits (toolbar, sort, cards)", sw <= 391 and tb and tb["x"] >= 0 and tb["x"] + tb["width"] <= 391
              and ms and ms["x"] >= 0 and ms["x"] + ms["width"] <= 391 and await ncards(P) == 24, f"scrollWidth={sw} toolbar={tb} sort={ms}")
        for _ in range(20):
            if await ncards(P) > 24: break
            await P.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)"); await P.wait_for_timeout(300)
        check("v8.9 catalog: mobile scrolling loads the next page", await ncards(P) >= 48)
        check("v8.9 catalog paging: no JS errors", not errsP and not extP, str((errsP + extP)[:3]))
        await ctxP.close()
        # Cold deep link to a book that no loaded page holds: one small request for that id, never the whole list
        ctxQ, Q, errsQ, extQ = await cat_ctx()
        dreqs = []
        Q.on("request", lambda r_: ("r=catalog" in r_.url) and dreqs.append(r_.url))
        await Q.goto(url + "#catalog/book/9077"); await Q.wait_for_timeout(500)
        try: await poll(lambda: _contains(Q, "#detailTitle", "کتاب نمونه 77"), 10000); deep = True
        except TimeoutError: deep = False
        check("v8.9 catalog: deep link to an unloaded book still opens it", deep and await Q.is_visible("#viewBook"))
        check("v8.9 catalog: the deep link asks for that one book (id=…), not the whole list",
              any("id=9077" in u and "per=1" in u for u in dreqs) and all("page=" in u for u in dreqs), str(dreqs[:4]))
        await Q.click("#detailBack"); await Q.wait_for_timeout(800)
        check("v8.9 catalog: back from a deep link loads the first page", await Q.is_visible("#viewCatalog") and await ncards(Q) == 24)
        check("v8.9 catalog deep link: no JS errors", not errsQ and not extQ, str((errsQ + extQ)[:3]))
        await ctxQ.close()
        # Server without paging (no `total`): the old full-list behaviour
        cat_state["paged"] = False
        ctxR, R, errsR, extR = await cat_ctx()
        await R.click('[data-view="catalog"]')
        try: await poll(lambda: _count_gt(R, "#catalogGrid .book-card", 0), 10000)
        except TimeoutError: pass
        await R.wait_for_timeout(300)
        check("v8.9 catalog fallback: whole list shown when the server sends no total", await ncards(R) == CAT_N and not await R.is_visible("#catalogMore")
              and "۱۳۰ کتاب" in await R.inner_text("#catalogCount"), f"{await ncards(R)}")
        n_before = len(cat_reqs())
        await R.fill("#catalogSearch", "نمونه 12"); await R.wait_for_timeout(150)
        check("v8.9 catalog fallback: search filters locally at once", await ncards(R) == 11 and len(cat_reqs()) == n_before)
        await R.fill("#catalogSearch", "")
        await R.click('#catalogFormat [data-format="pdf"]'); await R.wait_for_timeout(200)
        fb = await R.eval_on_selector_all("#catalogGrid .book-card", "els => els.map(e => +e.dataset.detail)")
        check("v8.9 catalog fallback: format filtered locally", fb and all((i - 9000) % 4 == 0 for i in fb)
              and len(fb) == sum(1 for i in range(1, CAT_N + 1) if i % 4 == 0), str(fb[:5]))
        await R.click('#catalogFormat [data-format=""]')
        await R.select_option("#catalogSort", "title"); await R.wait_for_timeout(200)
        titles = await R.locator("#catalogGrid .book-card h3").all_inner_texts()
        check("v8.9 catalog fallback: sort by title locally", len(titles) == CAT_N and titles == sorted(titles, key=lambda t: t), str(titles[:3]))
        await R.click('#catalogGrid .book-card:has-text("کتاب نمونه 33")'); await R.wait_for_timeout(800)
        rel = await R.locator("#detailRelated .book-card h3").all_inner_texts()
        check("v8.9 catalog fallback: book page and similar books from the whole list", await R.inner_text("#detailTitle") == "کتاب نمونه 33" and len(rel) == 6, str(rel))
        check("v8.9 catalog fallback: no JS errors", not errsR and not extR, str((errsR + extR)[:3]))
        await ctxR.close()
        # 32. v8.10 categories: a two-level list (category → subcategory) kept by the admin, several per book. Public catalog
        # navigator (≥ 900 px) with a subcategory panel, phone sheet with drill-down, #catalog?cat=ID (back, refresh, deep link),
        # book page links, similar books by subcategory, admin «دسته‌ها» tab (add, rename, move, reorder, delete), category
        # picker (details panel, review queue «C», user submission), tree filter and bulk add/remove in the books tab.
        # categories / catalog / category-* / admin-books / admin-bulk / catalog-* are mocked; login is the invited user with
        # role "admin" patched into login/me (as in section 30).
        import json as _j32, time as _t32, re as _re32, tempfile as _tf32
        from urllib.parse import urlparse as _up32, parse_qs as _pq32
        shots32 = os.environ.get("BORG_SHOTS")  # optional: a folder for screenshots of the new UI
        _ns32 = lambda t: _re32.sub(r"\s+", "", t or "")
        ext32, errs32 = [], []
        ctx32, C = await new_ctx(b, url, ext32, errs32)
        dlg32 = []
        C.on("dialog", lambda d: (dlg32.append(d.message), asyncio.ensure_future(d.accept())))
        now32 = int(_t32.time())
        def _c(i, n, p=None, cnt=0, sort=0): return {"id": i, "name": n, "parent_id": p or 0, "sort": sort, "count": cnt, "count_all": cnt + 2}
        cats32 = [_c(1, "ادبیات", None, 60), _c(11, "شعر", 1, 20), _c(12, "داستان", 1, 20), _c(13, "نمایشنامه", 1, 0),
                  _c(2, "تاریخ", None, 20), _c(21, "ایران", 2, 20), _c(22, "جهان", 2, 0),
                  _c(3, "علوم", None, 20), _c(5, "هنر", None, 0), _c(51, "موسیقی", 5), _c(52, "سینما", 5), _c(53, "نقاشی", 5), _c(4, "کودک", None, 0)]
        def cobj32(i):
            c = next(x for x in cats32 if x["id"] == i); p = next((x for x in cats32 if x["id"] == c["parent_id"]), None)
            return {"id": i, "name": c["name"], "parent_id": c["parent_id"], "parent_name": p["name"] if p else ""}
        BOOKCATS32 = {0: [11], 1: [12, 21], 2: [3]}
        books32 = [{"id": 7000 + i, "sha256": f"{i:064x}", "size": 50000 + i, "title": f"کتاب دسته {i}", "author": f"نویسنده {i % 4}",
                    "language": "fa", "publisher": "", "published": "", "has_cover": False, "format": "epub", "description": "", "category": "",
                    "submitter": "مدیر", "in_library": False, "catalog_status": "approved", "uploader_id": 1, "uploader_name": "مدیر بُرگ",
                    "created_at": now32 - i * 60, "source": "app", "in_libraries": 0, "categories": [cobj32(x) for x in BOOKCATS32[i % 3]]}
                   for i in range(1, 61)]
        pend32 = [{**books32[0], "id": 7901, "title": "صف دسته", "catalog_status": "pending", "submitted_at": now32, "categories": []}]
        m32 = {"calls": []}
        def in_cat32(bk, cid):
            cid = int(cid); return any(c["id"] == cid or c["parent_id"] == cid for c in bk["categories"])
        def cat_list32(admin=True):
            out = []
            for t in [c for c in cats32 if not c["parent_id"]]:
                out.append(t); out += [c for c in cats32 if c["parent_id"] == t["id"]]
            return [{k: v for k, v in c.items() if admin or k != "count_all"} for c in out]
        async def api32(route):
            req = route.request; qs = {k: v[0] for k, v in _pq32(_up32(req.url).query).items()}; r = qs.get("r", "")
            body = None
            if req.method == "POST":
                try: body = _j32.loads(req.post_data or "null")
                except Exception: body = None
            m32["calls"].append({"m": req.method, "r": r, "qs": qs, "body": body, "csrf": req.headers.get("x-csrf-token")})
            status, data = 200, {"ok": True}
            if r == "categories": data = {"categories": cat_list32()}
            elif r in ("catalog", "admin-books"):
                bs = list(books32)
                if qs.get("id"): bs = [x for x in bs if str(x["id"]) == qs["id"]]
                if qs.get("cat") == "none": bs = [x for x in bs if not x["categories"]]
                elif qs.get("cat"): bs = [x for x in bs if in_cat32(x, qs["cat"])]
                if qs.get("author"): bs = [x for x in bs if x["author"] == qs["author"]]
                page, per = int(qs.get("page", 1)), int(qs.get("per", 24))
                data = {"books": bs[(page - 1) * per: page * per], "total": len(bs), "page": page, "per": per, "categories": []}
                if r == "admin-books":
                    data["facets"] = {"status": {"approved": len(bs), "pending": 0, "rejected": 0, "none": 0}, "categories": [], "uploaders": [], "formats": {"epub": 60, "pdf": 0}}
            elif r == "category-save":
                name, pid, cid = body.get("name"), body.get("parent_id"), body.get("id")
                if any(c["name"] == name and c["parent_id"] == pid and c["id"] != cid for c in cats32): status, data = 409, {"error": "duplicate", "message": "Duplicate."}
                elif cid:
                    c = next(x for x in cats32 if x["id"] == cid); c["name"] = name; c["parent_id"] = pid; data = {"category": c}
                else:
                    c = _c(100 + len(m32["calls"]), name, pid); cats32.append(c); data = {"category": c}
            elif r == "category-delete":
                cid = body.get("id")
                if any(c["parent_id"] == cid for c in cats32): status, data = 409, {"error": "has_children", "message": "Has children."}
                else:
                    cats32[:] = [c for c in cats32 if c["id"] != cid]; data = {"moved": 20 if body.get("move_to") else 0, "unlinked": 0 if body.get("move_to") else 20}
            elif r == "category-order":
                order = body["ids"]; sub = [c for c in cats32 if c["id"] in order]; rest = [c for c in cats32 if c["id"] not in order]
                cats32[:] = rest + sorted(sub, key=lambda c: order.index(c["id"]))
            elif r == "admin-bulk": data = {"affected": len(body.get("ids") or []), "skipped": 0}
            elif r == "catalog-pending": data = {"books": pend32}
            elif r == "catalog-submit": data = {"status": "pending"}
            elif r == "admin-stats": data = {"users": 2, "active_users": 2, "books": 60, "storage_bytes": 1048576, "catalog_approved": 60, "catalog_pending": 1, "open_invites": 0, "client_errors": 0}
            elif r == "settings": data = {"catalog_requires_approval": True}
            elif r == "clean-rules": data = {"rules": []}
            await route.fulfill(status=status, content_type="application/json; charset=utf-8", body=_j32.dumps(data, ensure_ascii=False))
        MOCK32 = {"categories", "catalog", "admin-books", "category-save", "category-delete", "category-order", "admin-bulk", "catalog-pending",
                  "catalog-update", "catalog-submit", "admin-stats", "settings", "clean-rules"}
        def _is32(u):
            m = _re32.search(r"/api/\?r=([a-z-]+)", u)
            return bool(m) and m.group(1) in MOCK32
        await ctx32.route(_is32, api32)
        async def as_admin32(route):
            resp = await route.fetch()
            try:
                j = await resp.json()
                if isinstance(j, dict) and j.get("user"): j["user"]["role"] = "admin"
                await route.fulfill(response=resp, json=j)
            except Exception:
                await route.fulfill(response=resp)
        await ctx32.route(lambda u: u.endswith("/api/?r=login") or u.endswith("/api/?r=me"), as_admin32)
        calls32 = lambda r, m=None: [c for c in m32["calls"] if c["r"] == r and (m is None or c["m"] == m)]
        posts32 = lambda r: [c["body"] for c in calls32(r, "POST")]
        cat_reqs32 = lambda: [c["qs"] for c in calls32("catalog")]
        async def nposts32(r, n):
            try: await poll(lambda: asyncio.sleep(0, len(posts32(r)) >= n), 5000)
            except TimeoutError: pass
            return posts32(r)
        async def shot32(name):
            if shots32: await C.screenshot(path=str(Path(shots32) / name))
        await C.goto(url); await C.wait_for_timeout(800)
        h32 = await (await C.request.get(url + "api/?r=health")).json()
        await C.click("#accountButton"); await C.wait_for_timeout(500)
        if h32.get("needs_setup"):  # this section run on its own
            await C.click('[data-auth="setup"]')
            for k_, v_ in dict(setup_key=SETUP_KEY, name="مدیر بُرگ", email="admin@example.com", password="password1").items(): await C.fill(f'#authForm [name="{k_}"]', v_)
        else:
            await C.click('[data-auth="login"]')
            await C.fill('#authForm [name="email"]', "friend@example.com"); await C.fill('#authForm [name="password"]', "password2")
        await C.click("#authSubmit")
        await poll(lambda: C.is_visible('[data-view="catalog"]'), 15000)
        await C.wait_for_timeout(600)
        # --- Public catalog, desktop: navigator, panel, hash, back, refresh, deep link ---
        await C.click('[data-view="catalog"]')
        try: await poll(lambda: _count_gt(C, "#catNavList [data-cat-top]", 3), 8000)
        except TimeoutError: pass
        await C.wait_for_timeout(300)
        geo = await C.evaluate("""() => { const r = e => { const b = document.querySelector(e).getBoundingClientRect(); return { l: b.left, r: b.right, t: b.top, w: b.width, h: b.height }; };
            return { nav: r('#catNav'), grid: r('#catalogGrid'), sec: r('#viewCatalog'), sw: document.documentElement.scrollWidth }; }""")
        tops = await C.locator("#catNavList .cat-nav-name").all_inner_texts()
        check("v8.10 catalog: desktop navigator lists «همهٔ کتاب‌ها» + the top categories with counts, on the right of the grid (RTL)",
              await C.is_visible("#catNav") and tops[:4] == ["همهٔ کتاب‌ها", "ادبیات", "تاریخ", "علوم"] and "۶۰" in await C.inner_text('#catNavList [data-cat-top="1"]')
              and geo["nav"]["w"] >= 180 and geo["nav"]["w"] <= 260 and geo["grid"]["r"] <= geo["nav"]["l"] + 1 and abs(geo["nav"]["r"] - geo["sec"]["r"]) <= 2
              and geo["sw"] <= 1280 and await C.locator('#catNavList [data-cat-open="1"] .cat-chev').count() == 1
              and await C.locator('#catNavList [data-cat-pick="3"] .cat-chev').count() == 0, f"{tops} {geo}")
        check("v8.10 catalog: the old flat category chips are gone", await C.locator("#catalogFilters [data-cat-filter]").count() == 0 and not await C.is_visible("#catSheetOpen"))
        await C.hover('#catNavList [data-cat-open="1"]'); await C.wait_for_timeout(250)
        pg32 = await C.evaluate("""() => { const p = document.getElementById('catNavPanel').getBoundingClientRect(), n = document.getElementById('catNav').getBoundingClientRect();
            const it = [...document.querySelectorAll('#catNavPanel .cat-panel-item')].map(e => { const b = e.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top)]; });
            return { l: p.left, r: p.right, t: p.top, w: p.width, nl: n.left, nt: n.top, it }; }""")
        ptxt = await C.inner_text("#catNavPanel") if await C.is_visible("#catNavPanel") else ""
        check("v8.10 catalog: hovering a top category opens its panel to the left with «همهٔ کتاب‌های ادبیات ›» and the subcategories in two columns",
              "همهٔ کتاب‌های ادبیات" in ptxt and all(x in ptxt for x in ("شعر", "داستان", "نمایشنامه")) and pg32["r"] <= pg32["nl"] + 8 and pg32["l"] >= 0
              and abs(pg32["t"] - pg32["nt"]) <= 2 and len(pg32["it"]) == 3 and pg32["it"][0][1] == pg32["it"][1][1] and pg32["it"][0][0] != pg32["it"][1][0]
              and await C.get_attribute('#catNavList [data-cat-open="1"]', "aria-expanded") == "true", f"{ptxt!r} {pg32}")
        await shot32("catalog-navigator-1280.png")
        n0 = len(cat_reqs32())
        await C.click('#catNavPanel [data-cat-pick="11"]'); await C.wait_for_timeout(600)
        rq = cat_reqs32()[n0:]
        crumbs = await C.inner_text("#catCrumbs") if await C.is_visible("#catCrumbs") else ""
        check("v8.10 catalog: choosing «شعر» sends cat=11 (page 1), puts #catalog?cat=11 in the hash and shows «کتابخانهٔ عمومی › ادبیات › شعر»",
              any(q.get("cat") == "11" and q.get("page") == "1" for q in rq) and await C.evaluate("location.hash") == "#catalog?cat=11"
              and _ns32(crumbs).startswith("کتابخانهٔعمومی›ادبیات›شعر") and await C.locator("#catalogGrid .book-card").count() == 20
              and "۲۰ کتاب" in await C.inner_text("#catalogCount") and not await C.is_visible("#catNavPanel")
              and await C.get_attribute('#catNavList [data-cat-open="1"]', "aria-current") == "true", f"{rq} {crumbs!r}")
        await C.hover('#catNavList [data-cat-open="1"]'); await C.wait_for_timeout(200)
        await C.click("#catNavPanel .cat-panel-all"); await C.wait_for_timeout(600)
        check("v8.10 catalog: «همهٔ کتاب‌های ادبیات» filters by the top category (cat=1 includes its subcategories)",
              cat_reqs32()[-1].get("cat") == "1" and await C.evaluate("location.hash") == "#catalog?cat=1" and "۴۰ کتاب" in await C.inner_text("#catalogCount"))
        await C.hover('#catNavList [data-cat-open="1"]'); await C.wait_for_timeout(200)
        panel_opened = await C.is_visible("#catNavPanel")
        await C.mouse.move(60, 760); await C.wait_for_timeout(500)
        panel_closed = not await C.is_visible("#catNavPanel")
        await C.hover('#catNavList [data-cat-open="2"]'); await C.wait_for_timeout(200)
        await C.keyboard.press("Escape"); await C.wait_for_timeout(150)
        check("v8.10 catalog: the panel closes when the pointer leaves the navigator and with Escape", panel_opened and panel_closed and not await C.is_visible("#catNavPanel"))
        await C.go_back(); await C.wait_for_timeout(700)
        check("v8.10 catalog: back returns to the previous category (hash, request, breadcrumb)",
              await C.evaluate("location.hash") == "#catalog?cat=11" and cat_reqs32()[-1].get("cat") == "11" and await C.is_visible("#catCrumbs")
              and await C.locator("#catalogGrid .book-card").count() == 20)
        await C.reload(); await C.wait_for_timeout(600)
        try: await poll(lambda: _count_gt(C, "#catalogGrid .book-card", 0), 10000)
        except TimeoutError: pass
        await C.wait_for_timeout(400)
        check("v8.10 catalog: refresh keeps the category (request cat=11, breadcrumb, highlighted top)",
              await C.evaluate("location.hash") == "#catalog?cat=11" and cat_reqs32()[-1].get("cat") == "11" and "شعر" in await C.inner_text("#catCrumbs")
              and await C.locator("#catalogGrid .book-card").count() == 20 and await C.get_attribute('#catNavList [data-cat-open="1"]', "aria-current") == "true")
        await C.goto(url + "#catalog?cat=21"); await C.wait_for_timeout(300); await C.reload(); await C.wait_for_timeout(600)
        try: await poll(lambda: _contains(C, "#catCrumbs", "ایران"), 10000)
        except TimeoutError: pass
        check("v8.10 catalog: a deep link #catalog?cat=21 opens the catalog filtered by «تاریخ › ایران»",
              cat_reqs32()[-1].get("cat") == "21" and "تاریخ" in await C.inner_text("#catCrumbs") and await C.is_visible("#viewCatalog"))
        await C.click("#catCrumbs .cat-clear"); await C.wait_for_timeout(600)
        check("v8.10 catalog: «همهٔ دسته‌ها» in the breadcrumb clears the category (hash, request without cat)",
              await C.evaluate("location.hash") == "#catalog" and "cat" not in cat_reqs32()[-1] and not await C.is_visible("#catCrumbs")
              and await C.get_attribute('#catNavList [data-cat-pick=""]', "aria-current") == "true")
        await C.click('#catNavList [data-cat-pick="3"]'); await C.wait_for_timeout(600)
        check("v8.10 catalog: a top category without subcategories filters at once", cat_reqs32()[-1].get("cat") == "3" and await C.evaluate("location.hash") == "#catalog?cat=3")
        await C.click('#catNavList [data-cat-pick=""]'); await C.wait_for_timeout(600)
        # --- Book page: category links, similar books by subcategory ---
        n0 = len(cat_reqs32())
        await C.click('#catalogGrid .book-card[data-detail="7004"]'); await C.wait_for_timeout(500)
        try: await poll(lambda: C.is_visible("#detailRelatedSection"), 5000)
        except TimeoutError: pass
        links = await C.locator("#detailMeta [data-meta-catid]").all_inner_texts()
        rel = [q for q in cat_reqs32()[n0:] if q.get("per") == "6"]
        check("v8.10 book page: each category is a link «ادبیات › داستان», «تاریخ › ایران»; breadcrumb shows the first one",
              links == ["ادبیات › داستان", "تاریخ › ایران"] and "داستان" in await C.inner_text("#detailCrumbs") and "ادبیات" in await C.inner_text("#detailCrumbs"), f"{links}")
        rel_ids = await C.eval_on_selector_all("#detailRelated .book-card", "els => els.map(e => +e.dataset.detail)")
        check("v8.10 similar books: the same subcategory is asked first (cat=12) and comes first; the author fills the rest",
              rel[:1] and rel[0].get("cat") == "12" and len(rel_ids) == 6 and 7004 not in rel_ids and all((i - 7000) % 3 == 1 for i in rel_ids[:5])
              and any(q.get("author") for q in rel[1:]), f"{rel} {rel_ids}")
        await C.click('#detailMeta [data-meta-catid="21"]'); await C.wait_for_timeout(600)
        check("v8.10 book page: a category link opens the catalog filtered by it (#catalog?cat=21)",
              await C.is_visible("#viewCatalog") and await C.evaluate("location.hash") == "#catalog?cat=21" and cat_reqs32()[-1].get("cat") == "21" and "ایران" in await C.inner_text("#catCrumbs"))
        # --- Phone: «دسته‌ها» sheet with drill-down ---
        await C.set_viewport_size({"width": 390, "height": 844}); await C.wait_for_timeout(300)
        await C.click('[data-view="catalog"]'); await C.wait_for_timeout(700)
        check("v8.10 mobile: no navigator column; a «دسته‌ها» button instead", not await C.is_visible("#catNav") and await C.is_visible("#catSheetOpen")
              and await C.evaluate("document.documentElement.scrollWidth") <= 390)
        await C.click("#catSheetOpen"); await C.wait_for_timeout(400)
        sh = await C.evaluate("() => { const r = document.getElementById('catSheet').getBoundingClientRect(); return { t: r.top, b: r.bottom, l: r.left, r: r.right, vh: innerHeight }; }")
        lvl0 = await C.locator("#catSheetList .cat-nav-name").all_inner_texts()
        check("v8.10 mobile: the sheet opens at the bottom with «همهٔ کتاب‌ها» and the top categories (chevron for those with subcategories)",
              await C.is_visible("#catSheet") and abs(sh["b"] - sh["vh"]) <= 2 and sh["l"] >= -1 and sh["r"] <= 391 and lvl0[:3] == ["همهٔ کتاب‌ها", "ادبیات", "تاریخ"]
              and await C.locator('#catSheetList [data-cat-drill="1"] .cat-chev').count() == 1 and not await C.is_visible("#catSheetBack"), f"{sh} {lvl0}")
        await shot32("catalog-sheet-390.png")
        await C.click('#catSheetList [data-cat-drill="1"]'); await C.wait_for_timeout(200)
        lvl1 = await C.locator("#catSheetList .cat-nav-name").all_inner_texts()
        check("v8.10 mobile: drilling into «ادبیات» shows «همهٔ کتاب‌های ادبیات» and its subcategories, with a back arrow",
              await C.inner_text("#catSheetTitle") == "ادبیات" and lvl1 == ["همهٔ کتاب‌های ادبیات", "شعر", "داستان", "نمایشنامه"] and await C.is_visible("#catSheetBack"), str(lvl1))
        await shot32("catalog-sheet-drill-390.png")
        await C.click("#catSheetBack"); await C.wait_for_timeout(150)
        back_ok = await C.inner_text("#catSheetTitle") == "دسته‌ها" and not await C.is_visible("#catSheetBack")
        await C.click('#catSheetList [data-cat-drill="1"]'); await C.click('#catSheetList [data-cat-pick="11"]'); await C.wait_for_timeout(600)
        check("v8.10 mobile: back arrow returns to the top level; picking «شعر» closes the sheet, filters (cat=11, hash) and labels the button",
              back_ok and not await C.is_visible("#catSheet") and cat_reqs32()[-1].get("cat") == "11" and await C.evaluate("location.hash") == "#catalog?cat=11"
              and "شعر" in await C.inner_text("#catSheetOpen") and await C.is_visible("#catCrumbs"))
        cw = await C.evaluate("() => ({ sw: document.documentElement.scrollWidth, cr: document.getElementById('catCrumbs').getBoundingClientRect().right, tb: document.querySelector('.catalog-toolbar').getBoundingClientRect().right })")
        check("v8.10 mobile 390px: catalog with a category has no horizontal scroll", cw["sw"] <= 390 and cw["cr"] <= 391 and cw["tb"] <= 391, str(cw))
        await shot32("catalog-filtered-390.png")
        await C.click("#catSheetOpen"); await C.wait_for_timeout(300)
        reopen_lvl = await C.inner_text("#catSheetTitle") == "ادبیات" and await C.get_attribute('#catSheetList [data-cat-pick="11"]', "aria-current") == "true"
        await C.click("#catSheetBack"); await C.click('#catSheetList [data-cat-pick=""]'); await C.wait_for_timeout(500)
        check("v8.10 mobile: the sheet reopens at the chosen category's level; «همهٔ کتاب‌ها» clears the category",
              reopen_lvl and await C.evaluate("location.hash") == "#catalog" and not await C.is_visible("#catCrumbs"))
        await C.set_viewport_size({"width": 1280, "height": 800}); await C.wait_for_timeout(200)
        # --- Admin «دسته‌ها» tab ---
        await C.click('[data-view="admin"]'); await C.wait_for_timeout(500)
        tabs32 = [t.strip() for t in await C.locator("#adminTabs [role=tab]").all_inner_texts()]
        await C.click('[data-admin-tab="cats"]')
        try: await poll(lambda: _count_gt(C, "#catTree .cat-top", 3), 6000)
        except TimeoutError: pass
        tops_txt = await C.locator("#catTree .cat-top > .cat-row .cat-row-name").all_inner_texts()
        check("v8.10 admin: «دسته‌ها» tab right after «کتاب‌ها», #admin/cats, a two-level tree with counts (count_all for admins)",
              tabs32[2:4] == ["کتاب‌ها", "دسته‌ها"] and await C.evaluate("location.hash") == "#admin/cats" and tops_txt[:3] == ["ادبیات", "تاریخ", "علوم"]
              and await C.locator('#catTree [data-cat-id="1"] .cat-kid').count() == 3 and "۶۲ کتاب" in await C.inner_text('#catTree [data-cat-id="1"] > .cat-row'), f"{tabs32} {tops_txt}")
        await shot32("admin-categories-1280.png")
        await C.fill("#catAddTopName", "فلسفه"); await C.press("#catAddTopName", "Enter")
        sv = await nposts32("category-save", 1)
        try: await poll(lambda: _contains(C, "#catTree", "فلسفه"), 4000)
        except TimeoutError: pass
        check("v8.10 admin: «افزودن دستهٔ اصلی» sends category-save {name, parent_id: 0} with CSRF; the tree shows it",
              sv[:1] == [{"name": "فلسفه", "parent_id": 0}] and bool(calls32("category-save", "POST")[0]["csrf"]) and "فلسفه" in await C.inner_text("#catTree"), str(sv))
        await C.fill("#catAddTopName", "ادبیات"); await C.press("#catAddTopName", "Enter")
        await nposts32("category-save", 2); await C.wait_for_timeout(300)
        check("v8.10 admin: a duplicate name (409) is explained in Persian", "همین نام" in await C.inner_text("#catAdmError"), await C.inner_text("#catAdmError"))
        await C.click('#catTree [data-cat-id="2"] > .cat-kids [data-cat-act="addsub"]'); await C.wait_for_timeout(150)
        await C.fill('#catTree [data-cat-addsub="2"] input', "معاصر"); await C.press('#catTree [data-cat-addsub="2"] input', "Enter")
        sv = await nposts32("category-save", 3)
        try: await poll(lambda: _contains(C, '#catTree [data-cat-id="2"]', "معاصر"), 4000)
        except TimeoutError: pass
        check("v8.10 admin: «+ زیردسته» adds a subcategory under its parent (parent_id 2)", sv[2:3] == [{"name": "معاصر", "parent_id": 2}]
              and "معاصر" in await C.inner_text('#catTree [data-cat-id="2"] .cat-kids'), str(sv[2:]))
        await C.click('#catTree [data-cat-id="11"] [data-cat-act="rename"]'); await C.wait_for_timeout(150)
        ren_vis = await C.is_visible('#catTree [data-cat-rename="11"] input') and await C.input_value('#catTree [data-cat-rename="11"] input') == "شعر"
        await C.fill('#catTree [data-cat-rename="11"] input', "شعر و ترانه"); await C.press('#catTree [data-cat-rename="11"] input', "Enter")
        sv = await nposts32("category-save", 4)
        try: await poll(lambda: _contains(C, '#catTree [data-cat-id="11"]', "شعر و ترانه"), 4000)
        except TimeoutError: pass
        check("v8.10 admin: rename inline (input in the row, Enter) sends {id, name, parent_id}", ren_vis and sv[3:4] == [{"id": 11, "name": "شعر و ترانه", "parent_id": 1}]
              and not await C.is_visible('#catTree [data-cat-rename="11"]'), str(sv[3:]))
        await C.select_option('#catTree [data-cat-id="13"] [data-cat-move]', "2")
        sv = await nposts32("category-save", 5)
        try: await poll(lambda: _contains(C, '#catTree [data-cat-id="2"] .cat-kids', "نمایشنامه"), 4000)
        except TimeoutError: pass
        check("v8.10 admin: «انتقال به…» moves a subcategory to another parent", sv[4:5] == [{"id": 13, "name": "نمایشنامه", "parent_id": 2}]
              and "نمایشنامه" in await C.inner_text('#catTree [data-cat-id="2"] .cat-kids'), str(sv[4:]))
        await C.click('#catTree [data-cat-id="1"] > .cat-row [data-cat-act="down"]')
        od = await nposts32("category-order", 1)
        try: await poll(lambda: C.evaluate("document.querySelector('#catTree .cat-top').dataset.catId === '2'"), 4000)
        except TimeoutError: pass
        await C.click('#catTree [data-cat-id="12"] [data-cat-act="up"]')
        od = await nposts32("category-order", 2)
        await C.wait_for_timeout(300)
        kid_order = await C.evaluate("[...document.querySelectorAll('#catTree [data-cat-id=\"1\"] .cat-kid')].map(e => +e.dataset.catId)")
        check("v8.10 admin: ↓/↑ reorder through category-order {parent_id, ids} (top level and inside a parent)",
              len(od) == 2 and od[0]["parent_id"] == 0 and od[0]["ids"][:2] == [2, 1] and len(od[0]["ids"]) == 6
              and od[1] == {"parent_id": 1, "ids": [12, 11]} and kid_order == [12, 11]
              and await C.evaluate("document.querySelector('#catTree .cat-top').dataset.catId") == "2", f"{od} {kid_order}")
        await C.click('#catTree [data-cat-id="3"] > .cat-row [data-cat-act="delete"]'); await C.wait_for_timeout(200)
        del_open = await C.is_visible("#catDelDialog") and "۲۲ کتاب" in await C.inner_text("#catDelInfo")
        await C.select_option("#catDelMoveTo", "21"); await C.click("#catDelOk")
        dl = await nposts32("category-delete", 1)
        try: await poll(lambda: _contains(C, "#toast", "حذف شد"), 4000)
        except TimeoutError: pass
        check("v8.10 admin: delete asks what to do with the books; «منتقل شوند به…» sends {id, move_to} and reports the moved count",
              del_open and dl[:1] == [{"id": 3, "move_to": 21}] and "۲۰ کتاب" in await C.inner_text("#toast") and not await C.is_visible("#catDelDialog")
              and await C.locator('#catTree [data-cat-id="3"]').count() == 0, f"{dl} {await C.inner_text('#toast')}")
        await C.click('#catTree [data-cat-id="22"] [data-cat-act="delete"]'); await C.wait_for_timeout(200)
        await C.click("#catDelOk")
        dl = await nposts32("category-delete", 2); await C.wait_for_timeout(300)
        check("v8.10 admin: «فقط از کتاب‌ها برداشته شود» sends {id} only", dl[1:2] == [{"id": 22}] and "برداشته شد" in await C.inner_text("#toast"), str(dl))
        await C.click('#catTree [data-cat-id="1"] > .cat-row [data-cat-act="delete"]'); await C.wait_for_timeout(200)
        await C.click("#catDelOk")
        dl = await nposts32("category-delete", 3); await C.wait_for_timeout(300)
        check("v8.10 admin: has_children (409) is explained in the dialog, which stays open",
              len(dl) == 3 and await C.is_visible("#catDelDialog") and "زیردسته دارد" in await C.inner_text("#catDelError"), await C.inner_text("#catDelError"))
        await C.click("#catDelClose")
        # --- Books tab: tree filter, details picker, bulk add/remove ---
        await C.click('[data-admin-tab="books"]')
        try: await poll(lambda: _count_gt(C, "#admBooks tr.adm-row", 0), 6000)
        except TimeoutError: pass
        opts = await C.locator("#admFCategory option").all_inner_texts()
        vals = await C.eval_on_selector_all("#admFCategory option", "els => els.map(e => e.value)")
        check("v8.10 books: the category filter is the tree (parents first, children indented under them), plus «بدون دسته»",
              vals[:3] == ["", "none", "2"] and opts[1] == "بدون دسته" and opts[2].startswith("تاریخ") and "—" in opts[3] and "—" not in opts[vals.index("1")]
              and vals.index("2") < vals.index("21") < vals.index("1") < vals.index("12") < vals.index("5") < vals.index("51"), f"{vals} {opts}")
        await C.select_option("#admFCategory", "1"); await C.wait_for_timeout(500)
        bq = calls32("admin-books")[-1]["qs"]
        await C.select_option("#admFCategory", "none"); await C.wait_for_timeout(500)
        bq2 = calls32("admin-books")[-1]["qs"]
        h_none = await C.evaluate("location.hash")
        await C.reload(); await C.wait_for_timeout(600)
        try: await poll(lambda: _count_gt(C, "#admBooks tr.adm-row, #admBooks tr.adm-empty", 0), 8000)
        except TimeoutError: pass
        await C.wait_for_timeout(300)
        check("v8.10 books: the filter sends cat=ID / cat=none (not category), lives in the hash and survives a refresh",
              bq.get("cat") == "1" and "category" not in bq and bq2.get("cat") == "none" and "cat=none" in h_none
              and await C.input_value("#admFCategory") == "none" and calls32("admin-books")[-1]["qs"].get("cat") == "none", f"{bq} {bq2} {h_none}")
        await C.select_option("#admFCategory", ""); await C.wait_for_timeout(500)
        # details panel picker
        await C.locator('#admBooks tr[data-id="7001"] .adm-c-title').click(); await C.wait_for_timeout(300)
        chips0 = await C.locator("#admDetailCats .cp-chip").all_inner_texts()
        check("v8.10 details: the category picker replaces the free-text field and shows the book's categories as chips",
              await C.is_visible("#admDetailCats .cp-search") and not await C.is_visible('#admDetailForm [name="category"]')
              and [c.replace("×", "").strip() for c in chips0] == ["ادبیات › داستان", "تاریخ › ایران"], str(chips0))
        await C.fill("#admDetailCats .cp-search", "موس"); await C.wait_for_timeout(100)
        found = await C.locator("#admDetailCats .cp-opt").all_inner_texts()
        await C.click('#admDetailCats [data-cp="51"]')
        await C.fill("#admDetailCats .cp-search", "")
        await C.click('#admDetailCats [data-cp-del="21"]')
        grouped = await C.evaluate("[...document.querySelectorAll('#admDetailCats .cp-group')].map(g => [...g.querySelectorAll('[data-cp]')].map(o => +o.dataset.cp))")
        check("v8.10 picker: search narrows the options (the parent stays as the group head), options are grouped by parent, × removes a chip",
              [f.strip() for f in found] == ["هنر", "موسیقی"] and grouped[0][0] == 2 and 21 in grouped[0] and [c.replace("×", "").strip() for c in await C.locator("#admDetailCats .cp-chip").all_inner_texts()] == ["ادبیات › داستان", "هنر › موسیقی"],
              f"{found} {grouped}")
        await shot32("picker-details-1280.png")
        await C.click("#admDetailSave")
        cu = await nposts32("catalog-update", 1)
        check("v8.10 details: «ذخیره» sends categories:[ids] through catalog-update (no free-text category)",
              cu[:1] and cu[0]["book_id"] == 7001 and cu[0]["categories"] == [12, 51] and "category" not in cu[0], str(cu))
        await C.wait_for_timeout(300)
        await C.locator('#admBooks tr[data-id="7002"] .adm-c-title').click(); await C.wait_for_timeout(300)
        for _ in range(14):
            el = C.locator('#admDetailCats .cp-opt[aria-selected="false"]:not([disabled])')
            if not await el.count(): break
            await el.first.click()
        nsel = await C.locator("#admDetailCats .cp-chip").count()
        dis = await C.locator('#admDetailCats .cp-opt[aria-selected="false"][disabled]').count()
        free = await C.locator('#admDetailCats .cp-opt[aria-selected="false"]:not([disabled])').count()
        check("v8.10 picker: at most 10 categories; the rest are disabled with a note", nsel == 10 and dis > 0 and free == 0 and "حداکثر" in await C.inner_text("#admDetailCats .cp-note"),
              f"{nsel} {dis} {free}")
        await C.click("#admDetailClose")
        # bulk add / remove one category
        await C.locator("#admBooks tr.adm-row").nth(0).locator(".adm-sel").click()
        await C.locator("#admBooks tr.adm-row").nth(2).locator(".adm-sel").click(modifiers=["Shift"])
        check("v8.10 bulk: «افزودن دسته…» and «برداشتن دسته…» replace the free-text category actions",
              await C.is_visible('#admBulkBar [data-bulk="add_category"]') and await C.is_visible('#admBulkBar [data-bulk="remove_category"]')
              and not await C.is_visible('#admBulkBar [data-bulk="set_category"]') and not await C.is_visible('#admBulkBar [data-bulk="clear_category"]'))
        await C.click('#admBulkBar [data-bulk="add_category"]'); await C.wait_for_timeout(250)
        pick_vis = await C.is_visible("#catPickDialog") and "۳ کتاب" in await C.inner_text("#catPickTitle")
        await C.click('#catPickBox [data-cp="11"]'); await C.click('#catPickBox [data-cp="21"]')
        one = await C.locator("#catPickBox .cp-chip").count()
        await C.click("#catPickOk")
        bk = await nposts32("admin-bulk", 1)
        check("v8.10 bulk: «افزودن دسته…» picks one category and sends add_category {ids, category_id}",
              pick_vis and one == 1 and bk[:1] == [{"action": "add_category", "ids": [7001, 7002, 7003], "category_id": 21}], str(bk))
        await C.wait_for_timeout(500)
        await C.locator("#admBooks tr.adm-row").nth(1).locator(".adm-sel").click()
        await C.click('#admBulkBar [data-bulk="remove_category"]'); await C.wait_for_timeout(250)
        await C.click('#catPickBox [data-cp="12"]'); await C.click("#catPickOk")
        bk = await nposts32("admin-bulk", 2)
        check("v8.10 bulk: «برداشتن دسته…» confirms with the name and count, then sends remove_category",
              bk[1:2] == [{"action": "remove_category", "ids": [7002], "category_id": 12}] and dlg32 and "داستان" in dlg32[-1] and "۱ کتاب" in dlg32[-1], f"{bk} {dlg32[-1:]}")
        # review queue: C opens the picker
        await C.click('[data-admin-tab="queue"]')
        try: await poll(lambda: _contains(C, "#admReview", "صف دسته"), 6000)
        except TimeoutError: pass
        await C.keyboard.press("c"); await C.wait_for_timeout(250)
        c_open = await C.is_visible("#catPickDialog") and await C.locator("#catPickBox .cp-search").count() == 1
        await C.click('#catPickBox [data-cp="11"]'); await C.click('#catPickBox [data-cp="13"]'); await C.click("#catPickOk")
        cu = await nposts32("catalog-update", 2)
        try: await poll(lambda: _contains(C, "#admReview .adm-detail-chips", "نمایشنامه"), 4000)
        except TimeoutError: pass
        check("v8.10 queue: C opens the category picker; «ثبت» sends catalog-update {book_id, categories}",
              c_open and cu[1:2] == [{"book_id": 7901, "categories": [11, 13]}] and "شعر و ترانه" in await C.inner_text("#admReview .adm-detail-chips"), str(cu[1:]))
        # --- Phone: admin tab, picker dialog ---
        await C.set_viewport_size({"width": 390, "height": 844}); await C.wait_for_timeout(200)
        await C.click('[data-admin-tab="cats"]'); await C.wait_for_timeout(400)
        aw = await C.evaluate("() => ({ sw: document.documentElement.scrollWidth, r: Math.max(...[...document.querySelectorAll('#catTree .cat-row')].map(e => e.getBoundingClientRect().right)) })")
        check("v8.10 mobile 390px: the categories tab fits (no horizontal scroll)", aw["sw"] <= 390 and aw["r"] <= 391, str(aw))
        await shot32("admin-categories-390.png")
        await C.click('[data-admin-tab="queue"]'); await C.wait_for_timeout(400)
        await C.keyboard.press("c"); await C.wait_for_timeout(350)
        pd = await C.evaluate("() => { const r = document.getElementById('catPickDialog').getBoundingClientRect(); return { b: r.bottom, l: r.left, r: r.right, vh: innerHeight, sw: document.documentElement.scrollWidth }; }")
        check("v8.10 mobile: the picker is a bottom sheet that fits the screen", await C.is_visible("#catPickDialog") and abs(pd["b"] - pd["vh"]) <= 2 and pd["l"] >= -1 and pd["r"] <= 391 and pd["sw"] <= 390, str(pd))
        await shot32("picker-sheet-390.png")
        await C.click("#catPickClose")
        await C.set_viewport_size({"width": 1280, "height": 800})
        # --- The user's suggestion to the public catalog: description + picker ---
        tmp32 = Path(_tf32.mkdtemp(prefix="borg-e2e-cat-"))
        await C.click('[data-view="mine"]'); await C.wait_for_timeout(300)
        await C.set_input_files("#epubInput", make_epub(tmp32 / "cat.epub", "کتاب دسته‌دار", "test-cat-32", "نویسندهٔ دسته"))
        try: await poll(lambda: _count_gt(C, '#bookGrid .book-card:has-text("کتاب دسته‌دار") .share-book', 0), 25000)
        except TimeoutError: pass
        await C.click('#bookGrid .book-card:has-text("کتاب دسته‌دار") .share-book'); await C.wait_for_timeout(400)
        sub_open = await C.is_visible("#submitDialog") and await C.is_visible("#submitCats .cp-search")
        await C.fill('#submitForm [name="description"]', "توضیح پیشنهاد")
        await C.click('#submitCats [data-cp="12"]'); await C.click('#submitCats [data-cp="2"]')
        await shot32("picker-submit-1280.png")
        await C.click("#submitOk")
        cs = await nposts32("catalog-submit", 1)
        check("v8.10 suggestion: the user's dialog has a description and the picker; it sends categories:[ids]",
              sub_open and len(cs) == 1 and cs[0]["description"] == "توضیح پیشنهاد" and cs[0]["categories"] == [12, 2] and isinstance(cs[0]["book_id"], int)
              and "category" not in cs[0] and not await C.is_visible("#submitDialog"), str(cs))
        errs32 = [e for e in errs32 if "status of 409" not in e]  # the mocked duplicate / has_children answers
        check("v8.10 categories: no JS errors (besides the two mocked 409 answers), no external requests", not errs32 and not ext32, str((errs32 + ext32)[:3]))
        await ctx32.close()
        await b.close()

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
