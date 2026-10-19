# 0009 — PDF magazines are read in their original layout with pdf.js

Status: accepted (2026-10-19, v8.8)

## Context
The owner reads text-based PDF magazines (English and Persian) and wants them in Borg next to the EPUBs. Converting
PDF to EPUB loses the layout (columns, pictures, typography) and is unreliable for Persian. The owner decided: open
the PDF directly, show the original pages, no conversion.

## Decision
- Render with **pdf.js 4.10.38, legacy build** (wide Safari/iOS support), vendored unmodified under
  `src/assets/vendor/pdfjs/` (`pdf.min.mjs`, `pdf.worker.min.mjs`, `cmaps/`, `standard_fonts/`, Apache-2.0 `LICENSE`;
  ADR 0001: no CDN). It is loaded with `import()` only when a PDF is imported or opened, and is **not** in the Service
  Worker's `APP_SHELL`; the runtime cache keeps it after the first use (≈4 MB that EPUB-only readers never download).
- CSP stays strict: module scripts and the module worker come from `'self'`; `isEvalSupported: false`; no PDF
  scripting (the sandbox build is not shipped); CMaps and standard fonts point at the vendored folders.
- A PDF is a normal `books` record with the new optional field `format: "pdf"` (absent = EPUB; no field renamed,
  no `DB_VERSION` change). It is detected by its bytes (`%PDF-`). Progress uses the existing `cfi` field as
  `pdf:<page>`; page bookmarks are annotations with `cfi: "pdf:<page>"`; both sync unchanged.
- The PDF view lives inside the existing reader shell (toolbar, TOC panel, bottom bar, seek bar, back chip) and reuses
  its inputs and their direction logic (`bookDir`, ADR 0008: detected from the text of the first pages, per-book override).
  Page canvases are drawn at `devicePixelRatio` (capped at 10 M pixels per canvas) with pdf.js' text layer on top;
  only the visible page(s) and their neighbours keep a canvas.
- Not in v1: highlights/notes on PDF text, read-aloud, text cleanup (PDFs are skipped), Persian digits, links inside
  the PDF (annotation layer), forms.

## Consequences
- First PDF use needs a connection once (pdf.js is fetched then cached); offline afterwards.
- Some PDFs (including Chromium's own and some Persian tools) store Arabic-script text glyph by glyph in visual order;
  search then also matches the reversed query, but copied text from such files reads reversed (a property of the file).
- Scanned (image-only) PDFs open but have no text layer, search or direction detection (they fall back to RTL).
- Very large magazines (100+ pages): one page is drawn at a time; memory on old iPhones is the main risk and still
  needs confirmation on a real device.
