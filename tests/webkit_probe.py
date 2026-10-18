#!/usr/bin/env python3
"""Safari/WebKit probe: opens a book in WebKit under several rendering variants and reports which ones show text.

Results go to stdout and to GitHub annotations (::warning) so they can be read without downloading logs.
"""
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_test import start_server, FIX
from playwright.async_api import async_playwright

def report(msg):
    print(msg, flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=webkit probe::{msg[:900]}", flush=True)

PROBE_JS = """() => {
  const f = document.querySelector('#viewer iframe'); if (!f) return { error: 'no iframe' };
  const v = document.getElementById('viewer').getBoundingClientRect(), fr = f.getBoundingClientRect();
  const d = f.contentDocument;
  const pts = [[0.5, 0.3], [0.5, 0.5], [0.25, 0.5], [0.75, 0.5]].map(([x, y]) => {
    const el = d.elementFromPoint(v.left + v.width * x - fr.left, v.top + v.height * y - fr.top);
    return el ? (el.textContent || '').trim().slice(0, 18) : null; });
  return { frameW: Math.round(fr.width), frameLeft: Math.round(fr.left), scrollW: d.documentElement.scrollWidth,
           container: (() => { const c = document.querySelector('#viewer .epub-container'); return c ? [c.scrollLeft, c.scrollWidth, c.clientWidth] : null; })(),
           visible: pts };
}"""

DIAG_JS = """() => {
  const f = document.querySelector('#viewer iframe'); const d = f.contentDocument, b = d.body, r = d.createRange();
  r.selectNodeContents(b); const br = r.getBoundingClientRect(); let lo = 1e9, hi = -1e9, n = 0, odd = [];
  for (const q of r.getClientRects()) { n++; if (q.width > 0 && q.height > 0) { lo = Math.min(lo, q.left); hi = Math.max(hi, q.right); } else if (odd.length < 3) odd.push([Math.round(q.left), Math.round(q.width)]); }
  let elo = 1e9, ehi = -1e9; for (const el of b.querySelectorAll('*')) for (const q of el.getClientRects()) if (q.width > 0 && q.height > 0) { elo = Math.min(elo, q.left); ehi = Math.max(ehi, q.right); }
  const cs = getComputedStyle(d.documentElement);
  return { bound: [Math.round(br.left), Math.round(br.right)], rects: [n, Math.round(lo), Math.round(hi)], odd, elems: [Math.round(elo), Math.round(ehi)],
           html: [cs.width, cs.height, cs.columnWidth, cs.columnGap, cs.direction], bodyDir: getComputedStyle(b).direction };
}"""

VARIANTS = [
    ("base", {}, {}),
    ("scrolled", {}, {"flow": "scrolled"}),
    ("spreadNone", {}, {"spread": "none"}),
]

async def probe(p, url, name, debug, prefs):
    b = await p.webkit.launch()
    ctx = await b.new_context(viewport={"width": 820, "height": 1180}, has_touch=True)
    await ctx.add_init_script(f"localStorage.setItem('borg-debug', {json.dumps(json.dumps(debug))});"
                              f"localStorage.setItem('ketabkhane-reader-preferences', {json.dumps(json.dumps(prefs))});")
    pg = await ctx.new_page()
    logs = []
    pg.on("pageerror", lambda e: logs.append(str(e)[:150]))
    await pg.goto(url); await pg.wait_for_timeout(1200)
    await pg.set_input_files("#epubInput", str(FIX / "sample-fa.epub")); await pg.wait_for_timeout(2500)
    await pg.click("#bookGrid .book-card"); await pg.wait_for_timeout(4500)
    first = await pg.evaluate(PROBE_JS)
    await pg.click("#nextPage"); await pg.wait_for_timeout(1500)
    second = await pg.evaluate(PROBE_JS)
    if name == "base":
        report(f"[base diag] {await pg.evaluate(DIAG_JS)}")
    report(f"[{name}] open={first} | afterNext.visible={second.get('visible')} | errors={logs[:3]}")
    await b.close()

async def main():
    srv, url = start_server(Path(__file__).resolve().parent.parent / "src", "/borg/")
    try:
        async with async_playwright() as p:
            for name, debug, prefs in VARIANTS:
                try:
                    await probe(p, url, name, debug, prefs)
                except Exception as e:
                    report(f"[{name}] probe crashed: {str(e)[:300]}")
    finally:
        srv.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
