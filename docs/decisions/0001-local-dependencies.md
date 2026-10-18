# 0001 — All runtime dependencies are hosted locally

Status: accepted (2026-10-18, v5)

## Context
v4 loaded epub.js from a CDN and did not load JSZip at all, so no EPUB could open. CDN files were also
never cached by the Service Worker, so the app could not work offline.

## Decision
JSZip 3.10.1, epub.js 0.3.93 and Vazirmatn are committed under `src/assets/vendor` and `src/assets/fonts`
with their licenses. No CDN or third-party request is allowed at runtime; the e2e tests block and fail on any.

## Consequences
Full offline support and a strict CSP become possible. Upgrading a library is a deliberate commit that must
pass the tests.
