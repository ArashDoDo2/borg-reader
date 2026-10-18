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

async def new_ctx(browser, url, external, errors, show_guide=False, **kw):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, **kw)
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
        await E.fill("#catalogSearch", "چیزی-که-نیست"); await E.wait_for_timeout(200)
        check("catalog search filters", await E.locator("#catalogGrid .book-card").count() == 0)
        await E.fill("#catalogSearch", "آزمایشی"); await E.wait_for_timeout(200)
        await E.click("#catalogGrid .book-card"); await E.wait_for_timeout(400)
        check("catalog book page opens", await E.is_visible("#viewBook") and await E.inner_text("#detailTitle") == "کتاب آزمایشی")
        await E.click("#detailBack"); await E.wait_for_timeout(300)
        # Admin panel
        E.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await E.click('[data-view="admin"]'); await E.wait_for_timeout(1500)
        check("admin stats shown", await E.locator("#adminStats .stat").count() == 7)  # v8.4: + client errors
        check("approval setting on by default", await E.is_checked("#settingApproval"))
        await E.click("#settingApproval"); await E.wait_for_timeout(1200)
        check("approval setting saved", not await E.is_checked("#settingApproval"))
        await E.click("#settingApproval"); await E.wait_for_timeout(1200)
        friend = E.locator('#userList .admin-item:has-text("friend@example.com")')
        await friend.locator('[data-user-act="disable"]').click(); await E.wait_for_timeout(1200)
        check("admin disables a user", await E.locator('#userList .admin-item:has-text("friend@example.com") .status-chip.off').count() == 1)
        await E.locator('#userList .admin-item:has-text("friend@example.com") [data-user-act="enable"]').click(); await E.wait_for_timeout(1200)
        check("admin re-enables the user", await E.locator('#userList .admin-item:has-text("friend@example.com") .status-chip.off').count() == 0)
        check("whole catalog and server books listed", await E.locator("#catalogAdminList .admin-item").count() >= 1 and await E.locator("#booksAdminList .admin-item").count() >= 1)
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
        await H.click('[data-view="admin"]'); await H.wait_for_timeout(1500)
        await H.click('#catalogAdminList .admin-item:has-text("کتاب آزمایشی") [data-cat-edit]'); await H.wait_for_timeout(500)
        check("admin catalog editor opens with the current values", await H.is_visible("#catalogEditDialog") and await H.input_value('#catalogEditForm [name="title"]') == "کتاب آزمایشی")
        await H.fill('#catalogEditForm [name="category"]', "رمان")
        await H.fill('#catalogEditForm [name="publisher"]', "نشر تست")
        await H.fill('#catalogEditForm [name="published"]', "1402")
        await H.click("#catalogEditSave"); await H.wait_for_timeout(1500)
        check("admin edit saves category/publisher/date", not await H.is_visible("#catalogEditDialog")
              and await H.locator('#catalogAdminList .admin-item:has-text("کتاب آزمایشی") .cat-chip:has-text("رمان")').count() == 1)
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
        chips = await I.locator("#catalogFilters [data-cat-filter]").all_inner_texts()
        check("category chips built from the catalog", chips == ["همهٔ دسته‌ها", "رمان", "شعر"], str(chips))
        await I.click('#catalogFilters [data-cat-filter="شعر"]'); await I.wait_for_timeout(300)
        titles = await I.locator("#catalogGrid .book-card h3").all_inner_texts()
        check("category chip filters the catalog", titles == ["کتاب سوم"], str(titles))
        await I.click('#catalogFilters [data-cat-filter=""]'); await I.wait_for_timeout(300)
        check("'all categories' shows everything again", await I.locator("#catalogGrid .book-card").count() == 3)
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
        await poll(lambda: _count_gt(Y, "#errorsList .err-item", 0), 8000)
        item = Y.locator('#errorsList .err-item:has-text("borg-e2e-error 19")')
        check("v8.4 errors: admin sees the error with its count and version", await item.count() == 1 and await item.is_visible()
              and "× ۲" in await item.inner_text() and await item.locator(".cat-chip").count() == 1, (await Y.inner_text("#errorsList"))[:300])
        check("v8.4 errors: error badge in the admin stats", await Y.locator("#adminStats .stat-errors").is_visible()
              and "خطای کاربران" in await Y.inner_text("#adminStats"))
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
