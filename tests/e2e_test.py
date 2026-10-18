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
                       ", 'setup_key' => " + json.dumps(SETUP_KEY) + ", 'storage_dir' => " + json.dumps(str(self.tmp / "storage")) + "];\n")
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

async def swipe(cdp, x0, x1, y):
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y}]})
    for i in range(1, 6):
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x0 + (x1 - x0) * i / 5, "y": y}]})
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

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
        check("toc navigation to chapter 2", await pg.inner_text("#chapterLabel") == "بخش ۲")
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
              await pg.inner_text("#chapterLabel") == "بخش ۲" and (await panel.bounding_box())["x"] >= 389)
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
        check("text alignment applied", ta == "right", ta)
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
        await pg.mouse.click(box["x"] + box["width"] * 0.1, box["y"] + box["height"] / 2); await pg.wait_for_timeout(500)
        check("tap in book while settings open only closes the panel", not await pg.is_visible("#settingsPanel") and await page_pos(pg) == p_before)
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
