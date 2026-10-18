#!/usr/bin/env python3
"""Safari/WebKit probe: opens a book in WebKit and reports whether text renders, with and without the page CSP.

Results are printed and emitted as GitHub annotations (::warning) so they can be read without downloading logs.
Usage: python3 tests/webkit_probe.py
"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_test import start_server, FIX
from playwright.async_api import async_playwright

def report(msg):
    print(msg, flush=True)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=webkit probe::{msg[:900]}", flush=True)

PROBE_JS = """() => {
  const frames = [...document.querySelectorAll('#viewer iframe')];
  const out = frames.map(f => { let len = -1, ps = -1, w = 0, h = 0;
    try { const d = f.contentDocument; len = d && d.body ? d.body.innerText.length : -2; ps = d ? d.querySelectorAll('p').length : -2; } catch (e) { len = 'err:' + e.message; }
    const r = f.getBoundingClientRect(); w = Math.round(r.width); h = Math.round(r.height);
    return { len, ps, w, h, sandbox: f.getAttribute('sandbox'), srcdoc: f.hasAttribute('srcdoc') }; });
  const v = document.getElementById('viewer').getBoundingClientRect();
  return { frames: out, viewer: [Math.round(v.width), Math.round(v.height)], label: document.getElementById('chapterLabel').textContent };
}"""

async def probe(p, url, bypass_csp, mobile):
    b = await p.webkit.launch()
    kw = dict(viewport={"width": 820, "height": 1180}, is_mobile=False, has_touch=True) if mobile else dict(viewport={"width": 1280, "height": 800})
    ctx = await b.new_context(bypass_csp=bypass_csp, **kw)
    pg = await ctx.new_page()
    logs = []
    pg.on("console", lambda m: m.type in ("error", "warning") and logs.append(f"{m.type}: {m.text[:200]}"))
    pg.on("pageerror", lambda e: logs.append(f"pageerror: {str(e)[:200]}"))
    await pg.goto(url); await pg.wait_for_timeout(1500)
    await pg.set_input_files("#epubInput", str(FIX / "sample-fa.epub")); await pg.wait_for_timeout(3000)
    cards = await pg.locator("#bookGrid .book-card").count()
    if cards:
        await pg.click("#bookGrid .book-card"); await pg.wait_for_timeout(5000)
        info = await pg.evaluate(PROBE_JS)
    else:
        info = {"note": "import failed", "toast": await pg.inner_text("#toast")}
    tag = f"[{'iPad' if mobile else 'desktop'} csp={'off' if bypass_csp else 'on'}]"
    report(f"{tag} cards={cards} info={info}")
    report(f"{tag} console={logs[:8]}")
    await b.close()

async def main():
    srv, url = start_server(Path(__file__).resolve().parent.parent / "src", "/borg/")
    try:
        async with async_playwright() as p:
            for mobile in (True, False):
                for bypass in (False, True):
                    try:
                        await probe(p, url, bypass, mobile)
                    except Exception as e:
                        report(f"[{'iPad' if mobile else 'desktop'} csp={'off' if bypass else 'on'}] probe crashed: {e}")
    finally:
        srv.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
