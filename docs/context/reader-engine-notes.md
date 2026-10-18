# Reader engine notes — pitfalls and the fixes in place

The reader is epub.js 0.3.93 (vendored, unmodified) rendering each spine item in a sandboxed iframe, paginated
with CSS columns. Most bugs so far came from three sources: Safari/WebKit, epub.js RTL handling, and malformed
Persian EPUBs. Each entry: symptom → cause → fix (where in `src/assets/js/app.js`) → test that guards it.

## Safari / WebKit
- **Nothing inside the book reacts on iPhone/iPad (taps, footnote and image clicks, keys)** → the epub.js iframe was
  sandboxed `allow-same-origin` only; WebKit runs no event listeners in a sandboxed frame without `allow-scripts`
  (console: "Blocked script execution in 'about:srcdoc'"). Chromium does, so every Chromium test passed. → 8.6.1:
  `allowScriptedContent: true`. Book scripts stay dead: the srcdoc frame inherits the page CSP (`script-src 'self'`,
  no inline, no blob:) and `stripBookScripts()` (spine content hook) removes `<script>`, `on*` attributes,
  `javascript:` URLs, iframes/objects/embeds. → e2e section 22, WebKit tap checks (gating).
- **Blank book page on iPad/Safari (TOC visible)** → WebKit's `Range#getBoundingClientRect()` over RTL multi-column
  content made epub.js size the iframe ~500,000 px wide, which Safari cannot paint. → `patchTextWidth("rects")`
  overrides `ePub.Contents.prototype.textWidth` with the union of `getClientRects()`; identical results in Chromium.
  → `tests/webkit_e2e.py` (CI job `webkit`, gates the deploy) checks frame width and that text intersects the viewer;
  `tests/webkit_probe.py` adds per-variant diagnostics when it fails.
- **Edge taps do nothing on iPhone/iPad** → iOS Safari does not fire `click` on plain text inside the iframe. →
  `onTouchEnd` recognises a tap (<10 px, <500 ms) and calls `handleTap`; the follow-up synthetic click is swallowed
  for 700 ms (no double turn). → e2e edge-tap checks; verified with mobile Chromium touch emulation.
- **"Saving book failed" in some Safari modes** → IndexedDB refuses Blobs. → `put()` falls back to storing an
  ArrayBuffer (`fileBuffer`); `fileOf()` rebuilds a File.
- The about:srcdoc "Blocked script execution" console message is expected (scripts in books are disabled).
- WebKit cannot be run in the agent's container; check `tests/webkit_e2e.py` locally with `--browser chromium`, then read
  the CI `webkit` job's annotations (`::error title=webkit`).

## epub.js
- **Direction**: if the spine has no `page-progression-direction`, epub.js uses `defaultDirection` (stock "ltr").
  An RTL body inside an LTR container puts the chapter start off-screen: TOC jumps land on the chapter's *last*
  page and prev/next go the wrong way. → `detectBookDirection()` decides `bookDir` once per open: declared
  direction → Arabic-script vs Latin letter count over the first ≤8 spine items (language tags lie: a Persian book
  tagged `en` exists) → language tag → RTL. `bookDir` feeds `defaultDirection`, the theme's body `direction`, the
  `rendered` hook, and `bookIsRtl()` (edge taps, swipes, arrow keys). → e2e section 14 ("no-direction book",
  "English book is laid out LTR"). Note `section.load()` resolves to the `<html>` element, not a Document.
- **Text "jumps" when a chapter loads** → first paint used the fallback font, then the reader font stylesheet and
  theme re-flowed it. → a `spine.hooks.serialize` hook injects `<style id="borg-veil">html{visibility:hidden}</style>`;
  a content hook removes it after `reader-fonts.css` loads and `document.fonts.ready` (700 ms cap, 1.5 s safety).
  The hook must be registered **after `book.opened`**, otherwise epub.js's own resource-substitution hook
  overwrites `section.output`. → e2e "chapter veil is lifted".
  **WebKit (8.4.1):** the veil was never removed in WebKit (cause not pinned down: reveal looked it up in the
  document captured when the hook ran). Now the veil style carries `animation: borg-unveil 0s 1.5s forwards` so CSS
  alone lifts it, the hook no longer returns its promise to epub.js, and `liftVeil()` queries the live
  `contents.document` (also on every `rendered`). Guarded by `tests/webkit_e2e.py` ("chapter veil is lifted"). Diagnostics (8.5.0, `diag()` with borg-debug
  `{"stats":1}`) showed the real cause of the delay: in WebKit `contents.addStylesheet()` never resolves (no
  `load` event for the link in the srcdoc iframe), so every chapter waited for the 1.5 s safety. Since 8.5.1 the hook
  also polls for `link.sheet` (cap 600 ms).
- **Playwright WebKit taps**: `page.touchscreen.tap` produced no touch/click events at all inside the book iframe
  (CI, iPad/iPhone emulation), while Chromium emulation works. The WebKit tap checks are non-gating
  (`WEBKIT_TAP_GATE=1` makes them gate) until confirmed on a real device.
- Input inside the iframe does not bubble: use `rendition.on("keydown"|"touchstart"|"touchend"|"click")`. Those
  events are passive: `preventDefault()` only for events from the main document.
- Escape is layered (selection bar → popups → search → settings → TOC drawer → immersive → close reader).
- Hooks: `rendition.hooks.content` runs per loaded chapter (fonts, half-space fix, digits, footnote/image
  handlers). `openToken` guards every async step against a reader that was closed or replaced.

## Persian EPUB content
- **Half-spaces shown as joined words** → some books use U+200F (RLM) where ZWNJ (U+200C) belongs. → `fixHalfSpaces`
  replaces RLM-between-letters with ZWNJ at display time; `normalizeForSearch` also unifies ی/ي/ى and ک/ك.
- Fonts: Vazirmatn (default), Shabnam, Parastoo, Noto Naskh Arabic, Amiri (all OFL, local). Sahel/Samim were
  removed because they look like Vazir. Fonts are injected into each iframe via `reader-fonts.css`.
- Footnotes: EPUB3 `epub:type="noteref"` and classic `#fn` links open a popup instead of navigating.
- **TOC entry does nothing ("No Section Found")** → nav/NCX hrefs are relative to the nav document (`../text/c1.xhtml`
  from `nav/nav.xhtml`) but `spine.get()` looks them up as package-relative hrefs. → `resolveTocHref()` resolves each
  href against `packaging.navPath`/`ncxPath` to the spine item's href (fragment kept) when the TOC is built; buttons,
  `jumpTo` and header matching use it. → e2e section 18.
- TOC: nested entries are listed up to 3 levels; labels use `dir="auto"` + `text-align:start`, so English aligns left and Persian right.
- Metadata is often wrong (language, missing direction, missing publisher): never trust a single field.

## Testing tips
- Real books for manual checks: the owner's Persian book (RLM half-spaces, no direction, `lang=en`) and an
  English Economist EPUB (calibre, nested NCX). Neither is committed (copyright); fixtures are generated in tests.
- Measure, don't eyeball: frame width, `elementFromPoint` inside the viewer box, heading position, computed
  `direction`. A frame-by-frame `requestAnimationFrame` recorder was used to prove the veil fix.
