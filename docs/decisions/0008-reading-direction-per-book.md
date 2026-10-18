# 0008 — Reading direction is decided per book

Status: accepted (2026-10-19, v8.1.3)

## Context
Until 8.1.0 the reader forced RTL on every book body while epub.js used the book's declared direction (default
LTR). Real files broke both ways: a Persian book with no `page-progression-direction` and `lang=en` opened TOC
chapters on their last page; an English magazine was laid out right-to-left.

## Decision
`detectBookDirection()` runs once when a book opens: declared spine direction wins; otherwise compare
Arabic/Hebrew-script letters with Latin/Greek/Cyrillic letters in the first ≤8 chapters; if there is almost no
text, use the language tag; otherwise RTL (Persian-first product). The result (`bookDir`) drives epub.js
`defaultDirection`, the body `direction`, and physical navigation (`bookIsRtl()`). UI lists that show book text
(TOC, notes, title) use `dir="auto"`.

## Consequences
Opening a book loads up to 8 chapters once more (small delay on large books). Mixed-script books follow the
majority script. A per-book manual override could be added later if a misdetection shows up.
