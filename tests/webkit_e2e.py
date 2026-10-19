#!/usr/bin/env python3
"""Safari-engine (WebKit) end-to-end checks that gate the deploy.

Covers the bugs that only showed up on iPad/iPhone: books stored in IndexedDB (Blob refusal, 8.0.1), text laid
out inside the viewer (frame width, 8.0.4), edge taps turning exactly one page (8.0.5), TOC jumps landing on the
chapter heading in the book's direction, the load veil, the seek bar and the back chip, and page errors; v8.8: a PDF
opens in pdf.js (canvas + text layer) and edge taps turn its pages.
Contexts: iPad (820x1180 touch), iPhone (390x844 touch) and a desktop one (English LTR book).

Usage:  python3 tests/webkit_e2e.py            (WebKit; CI installs it with `playwright install --with-deps webkit`)
Exit code 1 when any check fails. Failures are printed as `::error title=webkit::…` annotations in GitHub Actions
(`::warning` when WEBKIT_ANNOTATE=warning, used for the first attempt before the flake-guard retry).
"""
import json, argparse, asyncio, os, re, shutil, sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_test import start_server, FIX, poll, VISIBLE_TEXT_JS, GUIDE_SEEN_JS
from playwright.async_api import async_playwright

results = []
LEVEL = "warning" if os.environ.get("WEBKIT_ANNOTATE") == "warning" else "error"

def check(name, ok, info="", gate=True):
    """gate=False: reported (as a warning in CI) but does not fail the run."""
    if gate:
        results.append((name, bool(ok)))
    print(("PASS " if ok else ("FAIL " if gate else "WARN ")) + name + (f"  [{info}]" if info else ""), flush=True)
    if not ok and os.environ.get("GITHUB_ACTIONS"):
        print(f"::{LEVEL if gate else 'warning'} title=webkit::{name} [{str(info)[:600]}]", flush=True)

# Taps used to reach no listener in WebKit: the book iframe was sandboxed without allow-scripts, and WebKit runs no
# event listeners there (real iPhones too). Fixed in 8.6.1 (allowScriptedContent); tap checks gate again.
TAP_GATE = os.environ.get("WEBKIT_TAP_GATE", "1") == "1"
IFRAME_COUNTERS_JS = """() => { const f = document.querySelector('#viewer iframe'); const d = f.contentDocument; if (d.__wk) return;
  d.__wk = {touchstart: 0, touchend: 0, click: 0, pointerdown: 0};
  ['touchstart', 'touchend', 'click', 'pointerdown'].forEach(t => d.addEventListener(t, () => { d.__wk[t]++; }, true)); }"""
TAP_PROBE_JS = """([x, y]) => { const el = document.elementFromPoint(x, y); const f = document.querySelector('#viewer iframe');
  const d = f && f.contentDocument; return { at: el ? (el.tagName + '#' + (el.id || '') + '.' + String(el.className || '').slice(0, 40)) : null,
  inFrame: d && d.__wk ? d.__wk : null }; }"""

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
DIAG_JS = "() => { const d = document.querySelector('#viewer iframe'); return { diag: document.documentElement.dataset.borgDiag || '', frames: document.querySelectorAll('#viewer iframe').length, veils: [...document.querySelectorAll('#viewer iframe')].map(f => { try { const x = f.contentDocument; return [!!x.getElementById('borg-veil'), x.defaultView.getComputedStyle(x.documentElement).visibility, (x.getAnimations ? x.getAnimations().length : -1)]; } catch (e) { return String(e); } }) } }"
# The chapter name shown to the user: the toolbar line on phones (8.6), the page-nav label elsewhere.
CHAPTER_JS = "() => { const v = e => e && e.getBoundingClientRect().height > 0 ? e.textContent.trim() : ''; return v(document.getElementById('readerChapter')) || v(document.getElementById('chapterLabel')) || (document.getElementById('chapterLabel') || {}).textContent || '' }"
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

def simple_pdf(path, titles):
    """A small text PDF (Helvetica, one heading + 25 lines + a frame per page, /Title in the info dictionary).
    Written by hand because page.pdf() exists only in Chromium."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(titles)))}] /Count {len(titles)} >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, t in enumerate(titles):
        ops = [f"BT /F1 28 Tf 60 760 Td ({t}) Tj ET", "0 0 0 RG 4 w 40 40 515 762 re S"]
        ops += [f"BT /F1 14 Tf 60 {710 - 24 * k} Td (Line {k} of {t} in plain English words.) Tj ET" for k in range(25)]
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>")
        objs.append("\n".join(ops).encode())
    objs.append("<< /Title (WebKit PDF) /Author (Borg tests) >>")
    out, offs = b"%PDF-1.4\n", []
    for k, o in enumerate(objs, 1):
        offs.append(len(out))
        body = (f"<< /Length {len(o)} >>\nstream\n".encode() + o + b"\nendstream") if isinstance(o, bytes) else o.encode()
        out += f"{k} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {len(objs)} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(path).write_bytes(out)
    return str(path)

PDF_PAGES_JS = "()=>[...document.querySelectorAll('#viewer .pdf-page')].map(p=>+p.dataset.page)"
PDF_PIX_JS = """()=>{const c=document.querySelector('#viewer .pdf-page canvas');if(!c||!c.dataset.ready)return null;
  const v=document.getElementById('viewer').getBoundingClientRect(),r=c.getBoundingClientRect(),d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
  let dark=0;for(let i=0;i<d.length;i+=32){if(d[i]+d[i+1]+d[i+2]<300)dark++}
  return {dark,w:Math.round(r.width),h:Math.round(r.height),inside:r.width>100&&r.left>=v.left-1&&r.right<=v.right+1&&r.top>=v.top-1}}"""

async def new_ctx(browser, opts, errors, external):
    ctx = await browser.new_context(**opts)
    await ctx.add_init_script(GUIDE_SEEN_JS)
    await ctx.add_init_script("try{localStorage.setItem('borg-debug', JSON.stringify({stats:1}))}catch(e){}")
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
    await pg.evaluate(IFRAME_COUNTERS_JS)
    a = await pg.evaluate(LAYOUT_JS)
    await tap_at(pg, 0.1)
    b = await pg.evaluate(LAYOUT_JS)
    await tap_at(pg, 0.9)
    c = await pg.evaluate(LAYOUT_JS)
    one = lambda x, y: x and y and x["sl"] is not None and y["sl"] is not None and abs(abs(y["sl"] - x["sl"]) - x["cw"]) <= 2
    box = await pg.locator("#viewer").bounding_box()
    tapdiag = {"app": await pg.evaluate(DIAG_JS), "probe": await pg.evaluate(TAP_PROBE_JS, [box["x"] + box["width"] * 0.1, box["y"] + box["height"] * 0.5])}
    check(f"{name}: tap near the next-page edge turns exactly one page; the other edge comes back",
          one(a, b) and c and abs(c["sl"] - a["sl"]) <= 1 and (await pg.evaluate(VISIBLE_TEXT_JS) or "").startswith("فصل اول"),
          f"{a and a['sl']} → {b and b['sl']} → {c and c['sl']} (page {a and a['cw']}) diag={tapdiag}", gate=TAP_GATE)
    h0 = (await pg.locator("#viewer").bounding_box())["height"]
    await tap_at(pg, 0.5)
    imm = not await pg.is_visible(".reader-toolbar") and (await pg.locator("#viewer").bounding_box())["height"] > h0 + 50
    await tap_at(pg, 0.5)
    check(f"{name}: middle tap toggles immersive mode (and back)", imm and await pg.is_visible(".reader-toolbar"), gate=TAP_GATE)
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
          await pg.evaluate(H_IN_VIEW, "فصل دوم") and await pg.evaluate(CHAPTER_JS) == "فصل دوم", await pg.evaluate(CHAPTER_JS))
    try:  # the veil may legitimately stay up to its 1.5 s safety limit
        await poll(lambda: pg.evaluate("()=>{const v=(" + VEIL_JS + ")();return !v.veil && v.vis==='visible'}"), 3000)
    except TimeoutError:
        pass
    veil = await pg.evaluate(VEIL_JS)
    try: vms = json.loads((await pg.evaluate(DIAG_JS))["diag"] or "{}").get("veilMs")
    except Exception: vms = None
    check(f"{name}: chapter veil lifts before its 1.5 s safety limit (fonts detected)", vms is not None and vms < 1400, f"veilMs={vms}", gate=False)
    check(f"{name}: chapter veil is lifted", not veil["veil"] and veil["vis"] == "visible", str(veil) + " diag=" + str(await pg.evaluate(DIAG_JS)))
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
              await pg.evaluate(H_IN_VIEW, "Second article") and await pg.evaluate(CHAPTER_JS) == "Second article", await pg.evaluate(CHAPTER_JS))
    check("desktop: no page errors, no external requests", not errs and not ext, str(errs[:3] + ext[:3]))
    await ctx.close()

async def pdf_phone(browser, url, tmp):
    """v8.8: a text PDF opens in pdf.js (module worker, canvas, text layer) and touch edge taps turn its pages."""
    errs, ext = [], []
    ctx, pg = await new_ctx(browser, DEVICES["iPhone"], errs, ext)
    await pg.goto(url); await pg.wait_for_timeout(800)
    await pg.set_input_files("#epubInput", simple_pdf(tmp / "webkit.pdf", ["Page one", "Page two", "Page three"]))
    card = pg.locator("#bookGrid .book-card", has_text="WebKit PDF")
    try:
        await poll(lambda: card.first.is_visible(), 15000)
    except TimeoutError:
        pass
    ok = await card.count() > 0
    check("iPhone PDF: importing a PDF stores it and shows its card (title from the PDF)", ok)
    if not ok:
        await ctx.close(); return
    await card.first.click()
    try:
        await poll(lambda: pg.evaluate(PDF_PIX_JS), 15000)
    except TimeoutError:
        pass
    px = await pg.evaluate(PDF_PIX_JS)
    layer = await pg.evaluate("()=>(document.querySelector('#viewer .textLayer')||{}).textContent||''")
    check("iPhone PDF: page 1 is drawn inside the viewer (non-blank canvas) with its text layer",
          bool(px) and px["inside"] and px["dark"] > 100 and "Page one" in layer, f"{px} {layer[:60]!r} diag={await pg.evaluate(DIAG_JS)}")
    async def tap_pages(rx, want):
        await tap_at(pg, rx)
        try:
            await poll(lambda: pg.evaluate("(w)=>JSON.stringify((" + PDF_PAGES_JS + ")())===JSON.stringify(w)", want), 4000)
        except TimeoutError:
            pass
        return await pg.evaluate(PDF_PAGES_JS)
    p1 = await tap_pages(0.9, [2])
    p2 = await tap_pages(0.1, [1])
    check("iPhone PDF: touch edge taps turn the page (English PDF: right edge next, left edge back)", p1 == [2] and p2 == [1], f"{p1} {p2}", gate=TAP_GATE)
    check("iPhone PDF: no page errors, no external requests", not errs and not ext, str(errs[:3] + ext[:3]))
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
            try:
                await pdf_phone(browser, url, tmp)
            except Exception as e:
                check("iPhone PDF: run crashed", False, re.sub(r"\s+", " ", str(e))[:300])
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
