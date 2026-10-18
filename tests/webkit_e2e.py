#!/usr/bin/env python3
"""Safari-engine (WebKit) end-to-end checks that gate the deploy.

Covers the bugs that only showed up on iPad/iPhone: books stored in IndexedDB (Blob refusal, 8.0.1), text laid
out inside the viewer (frame width, 8.0.4), edge taps turning exactly one page (8.0.5), TOC jumps landing on the
chapter heading in the book's direction, the load veil, the seek bar and the back chip, and page errors.
Contexts: iPad (820x1180 touch), iPhone (390x844 touch) and a desktop one (English LTR book).

Usage:  python3 tests/webkit_e2e.py            (WebKit; CI installs it with `playwright install --with-deps webkit`)
Exit code 1 when any check fails. Failures are printed as `::error title=webkit::…` annotations in GitHub Actions
(`::warning` when WEBKIT_ANNOTATE=warning, used for the first attempt before the flake-guard retry).
"""
import argparse, asyncio, os, re, shutil, sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_test import start_server, FIX, poll, VISIBLE_TEXT_JS, GUIDE_SEEN_JS
from playwright.async_api import async_playwright

results = []
LEVEL = "warning" if os.environ.get("WEBKIT_ANNOTATE") == "warning" else "error"

def check(name, ok, info=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)
    if not ok and os.environ.get("GITHUB_ACTIONS"):
        print(f"::{LEVEL} title=webkit::{name} [{str(info)[:300]}]", flush=True)

DEVICES = {
    "iPad": dict(viewport={"width": 820, "height": 1180}, has_touch=True, is_mobile=True),
    "iPhone": dict(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True),
    "desktop": dict(viewport={"width": 1280, "height": 800}),
}

# Layout of the open chapter: frame vs viewer width, whether a paragraph box intersects the viewer box, and the
# paginated container's scroll position (one page turn moves it by exactly one page width).
LAYOUT_JS = """() => {
  const f = document.querySelector('#viewer iframe'); if (!f) return null;
  const v = document.getElementById('viewer').getBoundingClientRect(), fr = f.getBoundingClientRect(), d = f.contentDocument;
  const hit = [...d.querySelectorAll('p,h1,h2')].some(p => { const r = p.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.right + fr.left > v.left + 1 && r.left + fr.left < v.right - 1 && r.bottom + fr.top > v.top && r.top + fr.top < v.bottom; });
  const c = document.querySelector('#viewer .epub-container');
  return { frameW: Math.round(fr.width), viewerW: Math.round(v.width), viewerH: Math.round(v.height), hit,
           sl: c ? c.scrollLeft : null, cw: c ? c.clientWidth : null };
}"""
H_IN_VIEW = """(t) => { const f = document.querySelector('#viewer iframe'); if (!f) return false; const r = f.getBoundingClientRect();
  const box = document.getElementById('viewer').getBoundingClientRect();
  const h = [...f.contentDocument.querySelectorAll('h1,h2')].find(x => x.textContent.trim() === t); if (!h) return false;
  const x = h.getBoundingClientRect();
  return x.width > 0 && x.right + r.left > box.left && x.left + r.left < box.right && x.bottom + r.top > box.top && x.top + r.top < box.bottom }"""
VEIL_JS = """() => { const d = document.querySelector('#viewer iframe').contentDocument;
  return { veil: !!d.getElementById('borg-veil'), vis: d.defaultView.getComputedStyle(d.documentElement).visibility } }"""
BODY_DIR = "() => { const d = document.querySelector('#viewer iframe').contentDocument; return d.defaultView.getComputedStyle(d.body).direction }"

def english_epub(path):
    """English book with no declared direction and a nested NCX (like e2e section 14)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">wk-en-1</dc:identifier><dc:title>English Weekly</dc:title><dc:language>en</dc:language></metadata><manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="a" href="a.html" media-type="application/xhtml+xml"/><item id="b" href="b.html" media-type="application/xhtml+xml"/></manifest><spine toc="ncx"><itemref idref="a"/><itemref idref="b"/></spine></package>')
        z.writestr("toc.ncx", '<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/><docTitle><text>English Weekly</text></docTitle><navMap><navPoint id="n1" playOrder="1"><navLabel><text>Leaders</text></navLabel><content src="a.html"/><navPoint id="n2" playOrder="2"><navLabel><text>Second article</text></navLabel><content src="b.html"/></navPoint></navPoint></navMap></ncx>')
        for f, h in (("a.html", "Leaders"), ("b.html", "Second article")):
            body = "".join(f"<p>This is paragraph {k} of the section called {h}, written in plain English.</p>" for k in range(40))
            z.writestr(f, f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{h}</title></head><body><h1>{h}</h1>{body}</body></html>')
    return str(path)

async def new_ctx(browser, opts, errors, external):
    ctx = await browser.new_context(**opts)
    await ctx.add_init_script(GUIDE_SEEN_JS)
    async def block(route):
        external.append(route.request.url); await route.abort()
    await ctx.route(lambda u: not u.startswith("http://localhost"), block)
    pg = await ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)[:200]))
    return ctx, pg

async def import_and_open(pg, url, path, title):
    await pg.goto(url); await pg.wait_for_timeout(800)
    await pg.set_input_files("#epubInput", path)
    card = pg.locator("#bookGrid .book-card", has_text=title)
    try:
        await poll(lambda: card.first.is_visible(), 15000)
    except TimeoutError:
        return False
    await card.first.click()
    try:
        await poll(lambda: pg.evaluate("()=>{const l=(" + LAYOUT_JS + ")();return !!(l&&l.hit)}"), 15000)
    except TimeoutError:
        pass
    await pg.wait_for_timeout(1200)  # let fonts/veil settle
    return True

async def tap_at(pg, rx, ry=0.5):
    box = await pg.locator("#viewer").bounding_box()
    await pg.touchscreen.tap(box["x"] + box["width"] * rx, box["y"] + box["height"] * ry)
    await pg.wait_for_timeout(1000)  # > the 700 ms synthetic-click guard

async def touch_device(browser, url, name):
    errs, ext = [], []
    ctx, pg = await new_ctx(browser, DEVICES[name], errs, ext)
    sample = str(FIX / "sample-fa.epub")
    ok = await import_and_open(pg, url, sample, "کتاب آزمایشی")
    check(f"{name}: imported book is stored and its card appears", ok)
    if not ok:
        await ctx.close(); return
    lay = await pg.evaluate(LAYOUT_JS)
    check(f"{name}: text is laid out inside the viewer (sane frame width, paragraph intersects the viewer)",
          bool(lay) and lay["hit"] and lay["viewerH"] > 300 and lay["frameW"] < 50 * lay["viewerW"]
          and (await pg.evaluate(VISIBLE_TEXT_JS) or "").startswith("فصل اول"), str(lay))
    # Edge taps (RTL book): the left edge is the next page, the right edge the previous one; exactly one page each.
    a = await pg.evaluate(LAYOUT_JS)
    await tap_at(pg, 0.1)
    b = await pg.evaluate(LAYOUT_JS)
    await tap_at(pg, 0.9)
    c = await pg.evaluate(LAYOUT_JS)
    one = lambda x, y: x and y and x["sl"] is not None and y["sl"] is not None and abs(abs(y["sl"] - x["sl"]) - x["cw"]) <= 2
    check(f"{name}: tap near the next-page edge turns exactly one page; the other edge comes back",
          one(a, b) and c and abs(c["sl"] - a["sl"]) <= 1 and (await pg.evaluate(VISIBLE_TEXT_JS) or "").startswith("فصل اول"),
          f"{a and a['sl']} → {b and b['sl']} → {c and c['sl']} (page {a and a['cw']})")
    h0 = (await pg.locator("#viewer").bounding_box())["height"]
    await tap_at(pg, 0.5)
    imm = not await pg.is_visible(".reader-toolbar") and (await pg.locator("#viewer").bounding_box())["height"] > h0 + 50
    await tap_at(pg, 0.5)
    check(f"{name}: middle tap toggles immersive mode (and back)", imm and await pg.is_visible(".reader-toolbar"))
    # TOC jump (drawer on narrow screens), heading of the chapter inside the viewer, header title
    t0 = await pg.evaluate(VISIBLE_TEXT_JS)
    if await pg.is_visible("#tocToggle"):
        await pg.locator("#tocToggle").tap(); await pg.wait_for_timeout(600)
    await pg.locator("#tocList .toc-link", has_text="فصل دوم").first.tap()
    try:
        await poll(lambda: pg.evaluate(H_IN_VIEW, "فصل دوم"), 8000)
    except TimeoutError:
        pass
    await pg.wait_for_timeout(800)
    check(f"{name}: TOC jump shows the chapter heading inside the viewer and its title in the header",
          await pg.evaluate(H_IN_VIEW, "فصل دوم") and await pg.inner_text("#chapterLabel") == "فصل دوم", await pg.inner_text("#chapterLabel"))
    veil = await pg.evaluate(VEIL_JS)
    check(f"{name}: chapter veil is lifted", not veil["veil"] and veil["vis"] == "visible", str(veil))
    try:
        await poll(lambda: pg.evaluate("!document.getElementById('bookSeek').disabled"), 30000)
        seek = True
    except TimeoutError:
        seek = False
    chip = await pg.is_visible("#backChip")
    if chip:
        await pg.locator("#backChip").tap()
        try:
            await poll(lambda: pg.evaluate(H_IN_VIEW, "فصل اول"), 8000)
        except TimeoutError:
            pass
        await pg.wait_for_timeout(800)
    t2 = await pg.evaluate(VISIBLE_TEXT_JS)
    check(f"{name}: seek bar enabled; back chip appears after the jump and returns to the previous page",
          seek and chip and t0 and t2 == t0 and not await pg.is_visible("#backChip"), f"seek={seek} chip={chip} {t0!r}/{t2!r}")
    check(f"{name}: no page errors, no external requests", not errs and not ext, str(errs[:3] + ext[:3]))
    await ctx.close()

async def desktop(browser, url, tmp):
    errs, ext = [], []
    ctx, pg = await new_ctx(browser, DEVICES["desktop"], errs, ext)
    ok = await import_and_open(pg, url, english_epub(tmp / "english.epub"), "English Weekly")
    lay = await pg.evaluate(LAYOUT_JS) if ok else None
    check("desktop: English book (no declared direction) is laid out LTR inside the viewer",
          ok and bool(lay) and lay["hit"] and lay["frameW"] < 50 * lay["viewerW"] and await pg.evaluate(BODY_DIR) == "ltr", str(lay))
    if ok:
        await pg.locator("#tocList .toc-sub", has_text="Second article").click()
        try:
            await poll(lambda: pg.evaluate(H_IN_VIEW, "Second article"), 8000)
        except TimeoutError:
            pass
        await pg.wait_for_timeout(800)
        check("desktop: nested TOC entry opens its section with the heading inside the viewer",
              await pg.evaluate(H_IN_VIEW, "Second article") and await pg.inner_text("#chapterLabel") == "Second article", await pg.inner_text("#chapterLabel"))
    check("desktop: no page errors, no external requests", not errs and not ext, str(errs[:3] + ext[:3]))
    await ctx.close()

async def run(url, engine):
    tmp = Path(tempfile.mkdtemp(prefix="borg-webkit-"))
    try:
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
            print(f"{engine} {browser.version}", flush=True)
            for name in ("iPad", "iPhone"):
                try:
                    await touch_device(browser, url, name)
                except Exception as e:
                    check(f"{name}: run crashed", False, re.sub(r"\s+", " ", str(e))[:300])
            try:
                await desktop(browser, url, tmp)
            except Exception as e:
                check("desktop: run crashed", False, re.sub(r"\s+", " ", str(e))[:300])
            await browser.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(Path(__file__).resolve().parent.parent / "src"))
    ap.add_argument("--browser", default="webkit", choices=["webkit", "chromium"], help=argparse.SUPPRESS)  # chromium: local logic check
    a = ap.parse_args()
    srv, url = start_server(Path(a.src).resolve(), "/borg/")
    try:
        asyncio.run(run(url, a.browser))
    finally:
        srv.shutdown()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed or not results else 0)

if __name__ == "__main__":
    main()
