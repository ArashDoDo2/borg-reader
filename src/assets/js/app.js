/* Borg (بُرگ) — local EPUB library and reader. Cloud accounts and sync are not implemented yet. */
(() => {
  "use strict";
  // Storage names predate the rename and are kept so existing books survive (ADR 0003).
  // Staging shares the origin with production, so it gets its own storage (ADR 0005).
  const STORAGE_SUFFIX = location.pathname.includes("/borg-test/") ? "-test" : "";
  const DB_NAME = "ketabkhane-db" + STORAGE_SUFFIX;
  const THEME_KEY = "ketab-theme" + STORAGE_SUFFIX;
  const DB_VERSION = 3; // v2 (7.2): annotations; v3 (7.3): shelves
  let db, currentBook = null, rendition = null, bookInstance = null, currentProgressKey = null;
  let bookDir = "rtl"; // reading direction of the open book, decided by detectBookDirection()
  let pdf = null; // v8.8: the open PDF (pdf.js document, pages, zoom, render cache); null while an EPUB (or nothing) is open
  let fontSize = 100, toastTimer;
  const READER_PREFS_KEY = "ketabkhane-reader-preferences" + STORAGE_SUFFIX;

  // ======================= v8.4 error reports to the admin («خطاهای کاربران») =======================
  // Real failures (uncaught errors, unhandled rejections, EPUB/sync/storage failures) are queued in memory and
  // localStorage, deduplicated, and sent in batches to api/?r=client-error. Never book text, query strings or e-mails.
  const ERR_QUEUE_KEY = "borg-error-queue" + STORAGE_SUFFIX;
  const ERR_MAX_QUEUE = 50, ERR_BATCH = 20, ERR_FLUSH_MS = 30000;
  const ERR_API = new URL("api/?r=client-error", document.baseURI).href;
  const APP_VERSION = (document.querySelector('meta[name="app-version"]') || {}).content || "";
  let errQueue = (() => { try { const q = JSON.parse(localStorage.getItem(ERR_QUEUE_KEY) || "[]"); return Array.isArray(q) ? q.slice(-ERR_MAX_QUEUE) : []; } catch (_) { return []; } })();
  let errFlushing = false, errBlockedUntil = 0, errTimer = null;
  function errSave() {
    try { if (errQueue.length) localStorage.setItem(ERR_QUEUE_KEY, JSON.stringify(errQueue)); else localStorage.removeItem(ERR_QUEUE_KEY); } catch (_) {}
  }
  // Same scrubbing as the server: URL query strings/fragments, e-mails and long token-like strings.
  function errScrub(s, max) {
    return String(s == null ? "" : s)
      .replace(/((?:https?|blob|file):\/\/[^\s?#"'<>()]*)[?#][^\s"'<>()]*?(?=:\d+:\d+|:\d+\b|[\s"'<>()]|$)/gi, "$1")
      .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[email]")
      .replace(/\b[A-Fa-f0-9]{32,}\b/g, "[token]")
      .trim().slice(0, max);
  }
  /** Expected outcomes are not reported: offline, aborted, auth/permission/not-found/conflict/rate-limit API answers. */
  function errReportable(err) {
    if (!navigator.onLine) return false;
    if (err && (err.name === "AbortError" || (err.constructor && err.constructor.name === "UserError"))) return false;
    if (err && typeof err.status === "number" && typeof err.code === "string") return ![401, 403, 404, 409, 413, 429].includes(err.status);
    return true;
  }
  function reportError(kind, err, context) {
    try {
      if (!errReportable(err)) return;
      let message = "", stack = "";
      if (err && typeof err === "object") {
        const api = typeof err.status === "number" && typeof err.code === "string" ? `API ${err.status} ${err.code}: ` : "";
        const name = err.name && err.name !== "Error" ? err.name + ": " : "";
        message = api + name + (err.message || (api || name ? "" : (() => { try { return JSON.stringify(err); } catch (_) { return String(err); } })()));
        stack = typeof err.stack === "string" ? err.stack : "";
      } else message = String(err);
      const item = {
        kind, message: errScrub(message, 500) || "(بدون پیام)", source: errScrub(context || "", 300), stack: errScrub(stack, 4000),
        path: location.pathname.slice(0, 300), app_version: APP_VERSION, user_agent: navigator.userAgent.slice(0, 300), count: 1,
      };
      const same = errQueue.find(x => x.kind === item.kind && x.message === item.message && x.source === item.source);
      if (same) same.count = Math.min(100, (same.count || 1) + 1);
      else { errQueue.push(item); if (errQueue.length > ERR_MAX_QUEUE) errQueue.splice(0, errQueue.length - ERR_MAX_QUEUE); }
      errSave();
      if (!errTimer) errTimer = setTimeout(() => { errTimer = null; flushErrors(false); }, ERR_FLUSH_MS);
    } catch (_) { /* reporting must never break the app */ }
  }
  async function flushErrors(keepalive) {
    if (errFlushing || !errQueue.length || !navigator.onLine || Date.now() < errBlockedUntil) return;
    let csrf = null;
    try { csrf = account && account.csrf; } catch (_) { /* account not initialised yet */ }
    const batch = errQueue.slice(0, ERR_BATCH);
    // keepalive requests are limited to 64 KB in total.
    while (keepalive && batch.length > 1 && JSON.stringify({ items: batch }).length > 60000) batch.pop();
    const headers = { "Content-Type": "application/json" };
    if (csrf) headers["X-CSRF-Token"] = csrf;
    errFlushing = true;
    try {
      const res = await fetch(ERR_API, { method: "POST", headers, credentials: "same-origin", cache: "no-store", keepalive: !!keepalive, body: JSON.stringify({ items: batch }) });
      if (res.status === 429) errBlockedUntil = Date.now() + 15 * 60000;
      else if (res.ok || (res.status >= 400 && res.status < 500 && res.status !== 403)) { // 403 = stale CSRF: keep and retry later
        errQueue = errQueue.filter(x => !batch.includes(x)); errSave();
      }
    } catch (_) { /* network: keep the queue */ }
    finally { errFlushing = false; }
    if (errQueue.length && !errTimer && Date.now() >= errBlockedUntil) errTimer = setTimeout(() => { errTimer = null; flushErrors(false); }, ERR_FLUSH_MS);
  }
  window.addEventListener("error", e => {
    if (!e || (!e.error && (!e.message || e.message === "Script error."))) return; // opaque cross-origin errors
    if (e.filename && !e.filename.startsWith(location.origin)) return; // extensions, injected scripts
    let file = "";
    try { file = e.filename ? new URL(e.filename).pathname : ""; } catch (_) {}
    reportError("error", e.error || e.message, file ? `${file}:${e.lineno || 0}:${e.colno || 0}` : "");
  });
  window.addEventListener("unhandledrejection", e => reportError("rejection", e && e.reason, "unhandledrejection"));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushErrors(true); });
  window.addEventListener("pagehide", () => flushErrors(true));
  window.addEventListener("online", () => flushErrors(false));
  if (errQueue.length) errTimer = setTimeout(() => { errTimer = null; flushErrors(false); }, 5000);
  const defaultReaderPrefs = { font: "vazirmatn", lineHeight: "1.9", fontSize: 100, theme: "light", align: "justify", librarySort: "added", libraryFilter: "all", dailyGoal: 20,
    margin: "m", para: "m", flow: "paginated", spread: "auto", digits: false, taps: true, wheel: true, wake: true, ttsRate: 1, ttsVoice: "" };
  const READER_THEMES = { light: ["#fffdf7", "#252923"], sepia: ["#f4ecd8", "#5b4636"], dark: ["#1f2623", "#e6ece7"], black: ["#000000", "#d4d4d4"] };
  const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function effectiveTheme() {
    if (readerPrefs.theme === "auto") return darkQuery && darkQuery.matches ? "dark" : "light";
    return READER_THEMES[readerPrefs.theme] ? readerPrefs.theme : "light";
  }
  let readerPrefs = loadReaderPrefs();
  // Diagnostics only (set by the WebKit probe): {noDir, method, direction}. Empty for real users.
  let DEBUG = {};
  // Alternative content-width measurements for WebKit, where Range#getBoundingClientRect over RTL columns is huge.
  function patchTextWidth(mode) {
    const C = window.ePub && window.ePub.Contents; if (!C || C.prototype.__borgTw) return;
    const orig = C.prototype.textWidth; C.prototype.__borgTw = mode;
    C.prototype.textWidth = function () {
      try {
        const doc = this.document, root = this.content || doc.body, r = doc.createRange();
        if (mode === "rects") {
          r.selectNodeContents(root); let lo = Infinity, hi = -Infinity;
          for (const q of r.getClientRects()) { if (q.width > 0 && q.height > 0) { lo = Math.min(lo, q.left); hi = Math.max(hi, q.right); } }
          if (hi > lo) return Math.round(hi - lo);
        } else if (mode === "elems") {
          let lo = Infinity, hi = -Infinity;
          for (const el of root.querySelectorAll("*")) for (const q of el.getClientRects()) { if (q.width > 0 && q.height > 0) { lo = Math.min(lo, q.left); hi = Math.max(hi, q.right); } }
          if (hi > lo) return Math.round(hi - lo);
        }
      } catch (_) {}
      return orig.call(this);
    };
  }
  try { DEBUG = JSON.parse(localStorage.getItem("borg-debug") || "{}") || {}; } catch (_) {}
  fontSize = Math.min(180, Math.max(70, Number(readerPrefs.fontSize) || 100));

  function loadReaderPrefs() {
    try { return { ...defaultReaderPrefs, ...JSON.parse(localStorage.getItem(READER_PREFS_KEY) || "{}") }; }
    catch (_) { return { ...defaultReaderPrefs }; }
  }
  function saveReaderPrefs() {
    try { localStorage.setItem(READER_PREFS_KEY, JSON.stringify(readerPrefs)); } catch (_) {}
  }
  function selectedFontStack() {
    switch (readerPrefs.font) {
      case "tahoma": return "Tahoma, Arial, sans-serif";
      case "arial": return "Arial, Tahoma, sans-serif";
      case "serif": return "Georgia, 'Times New Roman', serif";
      case "sans-serif": return "sans-serif";
      case "shabnam": return '"Shabnam", "Vazirmatn", Tahoma, sans-serif';
      case "parastoo": return '"Parastoo", "Vazirmatn", Tahoma, serif';
      case "naskh": return '"Noto Naskh Arabic", "Vazirmatn", serif';
      case "amiri": return '"Amiri", "Vazirmatn", serif';
      default: return '"Vazirmatn", Tahoma, Arial, sans-serif';
    }
  }
  function syncSettingsUI() {
    const theme = effectiveTheme();
    const reader = document.getElementById("reader");
    reader.classList.remove("theme-light", "theme-sepia", "theme-dark", "theme-black");
    reader.classList.add("theme-" + theme);
    document.querySelectorAll("[data-theme]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.theme === (readerPrefs.theme || "light"))));
    const pressed = (sel, attr, val) => document.querySelectorAll(sel).forEach(b => b.setAttribute("aria-pressed", String(b.dataset[attr] === val)));
    pressed("[data-margin]", "margin", readerPrefs.margin || "m");
    pressed("[data-para]", "para", readerPrefs.para || "m");
    pressed("[data-flow]", "flow", readerPrefs.flow || "paginated");
    pressed("[data-spread]", "spread", readerPrefs.spread || "auto");
    syncBookDirUI();
    const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    chk("prefDigits", readerPrefs.digits); chk("prefTaps", readerPrefs.taps !== false); chk("prefWheel", readerPrefs.wheel !== false); chk("prefWake", readerPrefs.wake !== false);
    // v8.8 PDF: dark/black themes invert the page images (optional), fit and zoom controls.
    reader.classList.toggle("pdf-invert", (theme === "dark" || theme === "black") && readerPrefs.pdfInvert !== false);
    pressed("[data-pdffit]", "pdffit", pdfFitMode());
    chk("prefPdfInvert", readerPrefs.pdfInvert !== false);
    const zl = document.getElementById("pdfZoomLabel");
    if (zl) zl.textContent = `${Math.round((pdf ? pdf.zoom : 1) * 100).toLocaleString("fa-IR")}٪`;
    const viewer = document.getElementById("viewer");
    if (viewer) viewer.style.setProperty("--viewer-margin", { s: "0px", m: "2.5%", l: "7%" }[readerPrefs.margin] || "2.5%");
    document.querySelectorAll("[data-align]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.align === readerPrefs.align)));
    const label = document.getElementById("fontSizeLabel");
    if (label) label.textContent = `${fontSize.toLocaleString("fa-IR")}٪`;
  }
  function applyReaderStyles() {
    syncSettingsUI();
    if (!rendition) return;
    const [background, ink] = READER_THEMES[effectiveTheme()];
    const paraGap = { s: "0 0 .35em", m: "0 0 1em", l: "0 0 1.7em" }[readerPrefs.para] || "0 0 1em";
    const lineHeight = readerPrefs.lineHeight || "1.9";
    const align = readerPrefs.align === "right" ? "start" : "justify"; // "start" = right in RTL books, left in LTR ones
    rendition.themes.default({
      body: Object.assign({ "font-family": selectedFontStack(), "line-height": lineHeight, "color": ink, "background": background }, DEBUG.noDir ? {} : { "direction": bookDir }),
      p: { "text-align": align, "margin": paraGap, "line-height": lineHeight },
      img: { "cursor": "zoom-in" },
      "img.borg-blockimg": { "display": "block", "margin-left": "auto", "margin-right": "auto" },
      "h1,h2,h3,h4": { "line-height": "1.7", "margin": "0 0 1em" },
      a: { "color": effectiveTheme() === "light" || effectiveTheme() === "sepia" ? "#397a67" : "#8fd1b5" }
    });
    rendition.themes.fontSize(`${fontSize}%`);
  }

  const $ = (id) => document.getElementById(id);
  const inputEls = [$("epubInput"), $("epubInputSmall"), $("emptyEpubInput")];

  function showToast(message) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
  }
  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (event) => {
        const database = req.result;
        const tx = req.transaction;
        if (!database.objectStoreNames.contains("books")) database.createObjectStore("books", { keyPath: "id" });
        if (!database.objectStoreNames.contains("progress")) database.createObjectStore("progress", { keyPath: "bookId" });
        if (!database.objectStoreNames.contains("shelves")) database.createObjectStore("shelves", { keyPath: "uid" });
        if (!database.objectStoreNames.contains("annotations")) {
          const ann = database.createObjectStore("annotations", { keyPath: "uid" });
          ann.createIndex("bookId", "bookId");
          // 1 -> 2: the old single bookmark per book becomes an annotation (kept in progress too, never deleted).
          if (event.oldVersion >= 1) {
            tx.objectStore("progress").openCursor().onsuccess = e => {
              const cur = e.target.result;
              if (!cur) return;
              const p = cur.value;
              if (p && p.bookmark) {
                const now = Date.now();
                ann.put({ uid: `bm-${p.bookId}`.slice(0, 40), bookId: p.bookId, type: "bookmark", cfi: p.bookmark, quote: "", note: "", color: "",
                          createdAt: p.updatedAt || now, updatedAt: now, deleted: false, dirty: true });
              }
              cur.continue();
            };
          }
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  function getAllBooks() {
    return new Promise((resolve, reject) => {
      const req = db.transaction("books", "readonly").objectStore("books").getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }
  function getBook(id) {
    return new Promise((resolve, reject) => {
      const req = db.transaction("books", "readonly").objectStore("books").get(id);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  // v8.5: caches derived from the book file (epub.js locations, detected direction). A write from an older copy of
  // the record (e.g. read by sync before the book was opened) must not drop them while the file is the same.
  const FILE_CACHES = ["locations", "dirCache"];
  // User choices stored on the record that no stale copy may drop, even when the file changed (8.6.5).
  const STICKY_FIELDS = ["dirOverride"];
  function putRaw(store, value) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      if (store === "books" && value && value.id && [...FILE_CACHES, ...STICKY_FIELDS].some(k => value[k] === undefined)) {
        const os = tx.objectStore(store), g = os.get(value.id);
        g.onsuccess = () => {
          const old = g.result;
          const same = old && old.size === value.size && (!old.contentHash || !value.contentHash || old.contentHash === value.contentHash);
          const keep = same ? Object.fromEntries(FILE_CACHES.filter(k => value[k] === undefined && old[k] !== undefined).map(k => [k, old[k]])) : {};
          if (old) STICKY_FIELDS.forEach(k => { if (value[k] === undefined && old[k] !== undefined) keep[k] = old[k]; });
          os.put(Object.keys(keep).length ? { ...value, ...keep } : value);
        };
      } else tx.objectStore(store).put(value);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }
  // Some WebKit/Safari modes cannot store Blobs in IndexedDB; fall back to an ArrayBuffer copy of the book.
  async function put(store, value) {
    try {
      await putRaw(store, value);
    } catch (err) {
      if (store !== "books" || !(value && value.file instanceof Blob)) throw err;
      const buf = await value.file.arrayBuffer();
      await putRaw(store, { ...value, file: null, fileBuffer: buf, fileName: value.file.name || "book.epub" });
    }
  }
  /** The book file as a Blob/File, whichever way it was stored. */
  function fileOf(rec) {
    if (!rec) return null;
    if (rec.file instanceof Blob) return rec.file;
    if (rec.fileBuffer) return isPdfRecord(rec) ? new File([rec.fileBuffer], rec.fileName || "book.pdf", { type: "application/pdf" })
      : new File([rec.fileBuffer], rec.fileName || "book.epub", { type: "application/epub+zip" });
    return null;
  }
  function remove(store, key) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).delete(key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
  }
  function paletteFor(seed) {
    const colors = [["#e5c88d","#173b35"],["#c9d8c6","#28463b"],["#d8a38b","#4b3028"],["#b9cbd6","#243f51"],["#d4c2d9","#4c3453"],["#d9d5c8","#4d493d"]];
    let n = 0; for (const c of seed) n = (n * 31 + c.charCodeAt(0)) >>> 0;
    return colors[n % colors.length];
  }
  function getAllProgress() {
    return new Promise((resolve, reject) => {
      const req = db.transaction("progress", "readonly").objectStore("progress").getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }
  function getAnnotations(bookId) {
    return new Promise((resolve, reject) => {
      const req = db.transaction("annotations", "readonly").objectStore("annotations").index("bookId").getAll(bookId);
      req.onsuccess = () => resolve((req.result || []).filter(a => !a.deleted));
      req.onerror = () => reject(req.error);
    });
  }
  function getAllAnnotations() {
    return new Promise((resolve, reject) => {
      const req = db.transaction("annotations", "readonly").objectStore("annotations").getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }
  function getAnnotation(uid) {
    return new Promise((resolve, reject) => {
      const req = db.transaction("annotations", "readonly").objectStore("annotations").get(uid);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }
  function getAllShelves() {
    return new Promise((resolve, reject) => {
      const req = db.transaction("shelves", "readonly").objectStore("shelves").getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }
  let shelvesCache = [];
  async function refreshShelves() { shelvesCache = (await getAllShelves()).filter(x => !x.deleted).sort((a, b) => (a.position || 0) - (b.position || 0) || a.updatedAt - b.updatedAt); }
  function renderFilters(books) {
    const counts = { reading: 0, want: 0, finished: 0 };
    const f = readerPrefs.libraryFilter || "all";
    const chips = [["all", "همه"], ["status:reading", "در حال خواندن"], ["status:want", "می‌خواهم بخوانم"], ["status:finished", "خوانده‌شده"],
                   ...shelvesCache.map(x => [`shelf:${x.uid}`, `📚 ${x.name}`])];
    $("libraryFilters").innerHTML = books.length ? chips.map(([k, l]) => `<button type="button" data-filter="${esc(k)}" aria-pressed="${k === f}">${esc(l)}</button>`).join("") : "";
  }
  async function setBookMeta(localId, patch) {
    const b = await getBook(localId);
    if (!b) return null;
    const rec = { ...b, ...patch, metaUpdatedAt: Date.now(), metaDirty: true };
    await put("books", rec);
    scheduleMetaPush();
    return rec;
  }
  let metaTimer = null;
  function scheduleMetaPush() {
    refreshSyncChip();
    if (!account || !navigator.onLine) return;
    clearTimeout(metaTimer);
    metaTimer = setTimeout(() => { syncLibraryMeta(null).catch(err => { console.warn("Meta sync failed:", err); reportError("sync", err, "library-meta"); }); }, 2000);
  }
  async function saveShelf(rec) {
    rec.updatedAt = Date.now(); rec.dirty = true;
    await put("shelves", rec);
    await refreshShelves();
    scheduleMetaPush();
  }
  // ---- Reading time and goals ----
  const DAYS_KEY = "borg-reading-days" + STORAGE_SUFFIX;
  const OTHER_KEY = "borg-reading-other" + STORAGE_SUFFIX;
  const PUSHED_KEY = "borg-reading-pushed" + STORAGE_SUFFIX;
  function readJson(key) { try { return JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch (_) { return {}; } }
  function writeJson(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (_) {} }
  function dayKey(d = new Date()) { const z = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`; }
  function deviceId() {
    let id = null;
    try { id = localStorage.getItem("borg-device-id"); } catch (_) {}
    if (!id) { id = newUid(); try { localStorage.setItem("borg-device-id", id); } catch (_) {} }
    return id;
  }
  function secondsOn(day) { return (readJson(DAYS_KEY)[day] || 0) + (readJson(OTHER_KEY)[day] || 0); }
  let lastActivity = 0;
  function markActivity() { lastActivity = Date.now(); }
  setInterval(() => {
    if (!currentBook || currentBook.preview || document.visibilityState !== "visible" || Date.now() - lastActivity > 120000) return;
    const days = readJson(DAYS_KEY), k = dayKey();
    days[k] = Math.min(86400, (days[k] || 0) + 15);
    writeJson(DAYS_KEY, days);
  }, 15000);
  function renderStats(books) {
    if (!books || !books.length) { $("statsCard").hidden = true; return; }
    $("statsCard").hidden = false;
    const fa = n => Number(n).toLocaleString("fa-IR");
    const goal = Math.max(1, Number(readerPrefs.dailyGoal) || 20);
    const today = Math.floor(secondsOn(dayKey()) / 60);
    $("statToday").textContent = fa(today);
    $("goalArc").setAttribute("stroke-dasharray", `${Math.min(100, Math.round(today / goal * 100))} 100`);
    $("goalButton").textContent = `هدف: ${fa(goal)} دقیقه`;
    let streak = 0;
    const d = new Date();
    if (secondsOn(dayKey(d)) < 60) d.setDate(d.getDate() - 1);
    while (secondsOn(dayKey(d)) >= 60 && streak < 3650) { streak++; d.setDate(d.getDate() - 1); }
    let week = 0;
    for (let i = 0; i < 7; i++) { const x = new Date(); x.setDate(x.getDate() - i); week += secondsOn(dayKey(x)); }
    $("statStreak").textContent = fa(streak);
    $("statWeek").textContent = fa(Math.floor(week / 60));
    $("statFinished").textContent = fa(books.filter(b => b.status === "finished").length);
  }
  // ---- Time left in chapter (from this reader's own page speed) ----
  let secPerPage = 60, lastTurnAt = 0;
  try { secPerPage = Number(localStorage.getItem("borg-sec-per-page")) || 60; } catch (_) {}
  /** Learn this reader's seconds per page from the time between page turns. */
  function notePageTurn() {
    const now = Date.now(), dt = (now - lastTurnAt) / 1000;
    if (lastTurnAt && dt > 3 && dt < 300) {
      secPerPage = Math.round(secPerPage * 0.7 + dt * 0.3);
      try { localStorage.setItem("borg-sec-per-page", String(secPerPage)); } catch (_) {}
    }
    lastTurnAt = now;
  }
  function updateTimeLeft(location) {
    notePageTurn();
    const disp = location?.start?.displayed;
    if (!disp || !disp.total) return;
    const pagesLeft = Math.max(0, disp.total - disp.page);
    const min = Math.ceil(pagesLeft * secPerPage / 60);
    const pageInfo = `صفحهٔ ${disp.page.toLocaleString("fa-IR")} از ${disp.total.toLocaleString("fa-IR")}`;
    $("timeLeft").textContent = pagesLeft === 0 ? `${pageInfo} · آخرین صفحهٔ این فصل` : `${pageInfo} · حدود ${min.toLocaleString("fa-IR")} دقیقه تا پایان فصل`;
  }
  // ---- Quote card ----
  function wrapLines(ctx, text, maxWidth) {
    const words = text.replace(/\s+/g, " ").trim().split(" "), lines = [];
    let line = "";
    for (const w of words) {
      const t = line ? line + " " + w : w;
      if (ctx.measureText(t).width > maxWidth && line) { lines.push(line); line = w; } else line = t;
    }
    if (line) lines.push(line);
    return lines;
  }
  async function makeQuoteCard(quote, title, author) {
    try { await document.fonts.load('700 52px "Vazirmatn"'); } catch (_) {}
    const W = 1080, H = 1350, c = document.createElement("canvas");
    c.width = W; c.height = H;
    const ctx = c.getContext("2d");
    const g = ctx.createLinearGradient(0, 0, W, H); g.addColorStop(0, "#173b35"); g.addColorStop(1, "#2a5d50");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "rgba(255,255,255,0.06)"; ctx.beginPath(); ctx.arc(W * 0.85, H * 0.12, 260, 0, Math.PI * 2); ctx.fill();
    ctx.direction = "rtl"; ctx.textAlign = "right"; ctx.fillStyle = "#f7efdc";
    ctx.font = '700 120px "Vazirmatn", Tahoma, sans-serif'; ctx.fillStyle = "#e5c58b"; ctx.fillText("«", W - 110, 260);
    let size = 54, lines;
    for (;;) {
      ctx.font = `500 ${size}px "Vazirmatn", Tahoma, sans-serif`;
      lines = wrapLines(ctx, quote, W - 220);
      if (lines.length * size * 1.75 < H - 620 || size <= 32) break;
      size -= 4;
    }
    const maxLines = Math.floor((H - 620) / (size * 1.75));
    if (lines.length > maxLines) { lines = lines.slice(0, maxLines); lines[maxLines - 1] += " …"; }
    ctx.fillStyle = "#f7efdc";
    lines.forEach((l, i) => ctx.fillText(l, W - 110, 360 + i * size * 1.75));
    ctx.fillStyle = "#e5c58b"; ctx.fillRect(W - 210, H - 300, 100, 4);
    ctx.font = '700 40px "Vazirmatn", Tahoma, sans-serif'; ctx.fillStyle = "#f7efdc"; ctx.fillText(title.slice(0, 60), W - 110, H - 220);
    ctx.font = '400 32px "Vazirmatn", Tahoma, sans-serif'; ctx.fillStyle = "#cfe0d8"; ctx.fillText((author || "").slice(0, 60), W - 110, H - 165);
    ctx.textAlign = "left"; ctx.font = '700 34px "Vazirmatn", Tahoma, sans-serif'; ctx.fillStyle = "#e5c58b"; ctx.fillText("بُرگ", 110, H - 110);
    return new Promise(res => c.toBlob(b => res(b), "image/png"));
  }
  async function shareQuote(quote) {
    if (!currentBook || !quote) return;
    const blob = await makeQuoteCard(quote, currentBook.title || "", currentBook.author && currentBook.author !== "نویسنده نامشخص" ? currentBook.author : "");
    if (!blob) { showToast("ساخت تصویر ممکن نشد."); return; }
    const file = new File([blob], `${(currentBook.title || "quote").slice(0, 40)}.png`, { type: "image/png" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try { await navigator.share({ files: [file], title: currentBook.title }); return; } catch (err) { if (err && err.name === "AbortError") return; }
    }
    const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(file), download: file.name });
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 5000);
    showToast("کارت نقل‌قول ذخیره شد.");
  }
  function newUid() { return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`).slice(0, 40); }

  // v8.5 library search: one spelling for the query and the book fields. normalizeForSearch (ی/ي/ى, ک/ك, RLM half-space)
  // keeps offsets; then half-spaces, spaces, bidi marks, tatweel and harakat are dropped, Persian/Arabic digits become
  // 0-9 and Latin is lower-cased. `map[i]` is the index in the original text of key[i] (used to highlight the match).
  const LIB_SEARCH_KEY = "borg-library-search"; // sessionStorage only: the query is not kept across sessions
  const LIB_DIGITS = "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩";
  const LIB_DROP = /[\s\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF\u0640\u064B-\u065F\u0670]/;
  function libFold(text) {
    const src = normalizeForSearch(String(text ?? ""));
    let key = ""; const map = [];
    for (let i = 0; i < src.length; i++) {
      const c = src[i];
      if (LIB_DROP.test(c)) continue;
      const d = LIB_DIGITS.indexOf(c);
      const f = d >= 0 ? String(d % 10) : c.toLocaleLowerCase();
      for (let k = 0; k < f.length; k++) { key += f[k]; map.push(i); }
    }
    return { key, map };
  }
  function libMarkHtml(text, q) {
    text = String(text ?? "");
    if (!q) return esc(text);
    const { key, map } = libFold(text);
    const at = key.indexOf(q);
    if (at < 0) return esc(text);
    const a = map[at], z = map[at + q.length - 1] + 1;
    return `${esc(text.slice(0, a))}<mark>${esc(text.slice(a, z))}</mark>${esc(text.slice(z))}`;
  }
  function bookCardHtml(b, prog, hl) {
    const [bg, fg] = paletteFor(b.title || b.id);
    const cover = b.cover ? `<img alt="" src="${b.cover}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">` : `<div style="position:relative"><div class="cover-symbol">✳</div><div class="cover-name">${esc(b.title)}</div></div>`;
    const pct = prog && typeof prog.percentage === "number" ? Math.max(0, Math.min(100, prog.percentage)) : null;
    const bar = pct ? `<div class="cover-progress" aria-hidden="true"><span style="width:${pct}%"></span></div>` : "";
    const catalogLabel = b.catalogStatus === "approved" ? " · عمومی" : b.catalogStatus === "pending" ? " · در انتظار تأیید" : "";
    const statusLabel = b.status === "finished" ? "خوانده‌شده" : b.status === "want" ? "می‌خواهم بخوانم" : null;
    const badge = (statusLabel || (pct ? `${pct.toLocaleString("fa-IR")}٪ خوانده‌شده` : (prog ? "شروع‌شده" : "خوانده‌نشده"))) + catalogLabel;
    return `<article class="book-card" tabindex="0" role="button" data-open="${esc(b.id)}" aria-label="باز کردن ${esc(b.title)}">
          <div class="book-cover-thumb" style="background:${bg};color:${fg}">${cover}${bar}${isPdfRecord(b) ? '<span class="format-badge" aria-label="PDF">PDF</span>' : ""}</div>
          <div class="book-meta"><h3 title="${esc(b.title)}">${libMarkHtml(b.title, hl)}</h3><p>${libMarkHtml(b.author || "نویسنده نامشخص", hl)}</p><span class="book-badge">${badge}</span></div>
          <button class="remove-book" type="button" data-remove="${esc(b.id)}" aria-label="حذف ${esc(b.title)}" title="حذف">×</button>
          <button class="book-options" type="button" data-options="${esc(b.id)}" aria-label="تنظیمات ${esc(b.title)}" title="وضعیت و قفسه">⋯</button>
          ${account && b.serverId && !b.catalogStatus ? `<button class="share-book" type="button" data-share="${esc(b.id)}" aria-label="پیشنهاد ${esc(b.title)} برای کتابخانهٔ عمومی" title="پیشنهاد برای کتابخانهٔ عمومی">⇪</button>` : ""}
        </article>`;
  }
  let libRenderSeq = 0;
  function renderLibrary() {
    const seq = ++libRenderSeq;
    const raw = $("searchInput").value.trim();
    $("librarySearchClear").hidden = !raw;
    Promise.all([getAllBooks(), getAllProgress()]).then(([books, progressList]) => {
      if (seq !== libRenderSeq) return; // a newer render (e.g. the next keystroke) is on its way
      const query = libFold(raw).key;
      const shelfName = new Map(shelvesCache.map(x => [x.uid, x.name]));
      const haystack = b => libFold([b.title, b.author, b.publisher, ...(b.shelves || []).map(u => shelfName.get(u))].filter(Boolean).join("\u0000")).key;
      const prog = new Map(progressList.map(p => [p.bookId, p]));
      const lastRead = b => (prog.get(b.id) && prog.get(b.id).updatedAt) || 0;
      const sort = readerPrefs.librarySort;
      if (sort === "title") books.sort((a, b) => String(a.title).localeCompare(String(b.title), "fa"));
      else if (sort === "read") books.sort((a, b) => lastRead(b) - lastRead(a) || (b.addedAt || 0) - (a.addedAt || 0));
      else books.sort((a, b) => (b.addedAt || 0) - (a.addedAt || 0));
      const f = readerPrefs.libraryFilter || "all";
      const filtered = books.filter(b => !query || haystack(b).includes(query))
        .filter(b => f === "all" || (f.startsWith("status:") ? (b.status || (prog.get(b.id)?.cfi ? "reading" : "")) === f.slice(7) : (b.shelves || []).includes(f.slice(6))));
      renderFilters(books);
      renderStats(books);
      $("bookGrid").innerHTML = filtered.map(b => bookCardHtml(b, prog.get(b.id), query)).join("");
      // While searching the «ادامهٔ مطالعه» shelf is hidden: the grid is the single result list the count refers to.
      const reading = query ? [] : books.filter(b => { const p = prog.get(b.id); return p && p.cfi && (p.percentage || 0) < 100; })
        .sort((a, b) => lastRead(b) - lastRead(a)).slice(0, 8);
      $("continueSection").hidden = reading.length === 0;
      $("continueShelf").innerHTML = reading.map(b => bookCardHtml(b, prog.get(b.id))).join("");
      updateStorageInfo();
      $("emptyState").classList.toggle("visible", books.length === 0);
      // v8.6: the welcome hero is a first-run screen; once the library holds books it is hidden (CSS).
      document.body.classList.remove("lib-loading");
      document.body.classList.toggle("lib-has-books", books.length > 0);
      $("bookGrid").style.display = books.length ? "grid" : "none";
      const countEl = $("librarySearchCount");
      countEl.hidden = !query || !books.length || !filtered.length;
      countEl.textContent = query && filtered.length ? `${filtered.length.toLocaleString("fa-IR")} کتاب پیدا شد` : "";
      if (books.length && !filtered.length) {
        $("bookGrid").style.display = "block";
        $("bookGrid").innerHTML = query
          ? '<div class="lib-search-empty" role="status"><p>کتابی با این عبارت پیدا نشد</p><button type="button" class="secondary-button" data-search-clear>× پاک کردن جستجو</button></div>'
          : '<p class="muted">کتابی با این عبارت پیدا نشد.</p>';
      }
    }).catch(() => { document.body.classList.remove("lib-loading"); showToast("خواندن کتابخانه محلی ناموفق بود."); });
  }
  class UserError extends Error {}
  const READER_FONTS_URL = new URL("assets/css/reader-fonts.css", document.baseURI).href;
  function withTimeout(promise, ms, message) {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new UserError(message)), ms); })])
      .finally(() => clearTimeout(timer));
  }
  function librariesReady() {
    return typeof window.JSZip === "function" && typeof window.ePub === "function";
  }
  async function extractMetadata(file) {
    if (!librariesReady()) throw new UserError("کتاب‌خوان کامل بارگذاری نشده است. صفحه را تازه‌سازی کن.");
    let title = file.name.replace(/\.epub$/i, ""), author = "نویسنده نامشخص", cover = null, extra = {};
    let temp = null;
    try {
      temp = ePub(await file.arrayBuffer());
      // epub.js reports a broken archive through the "openFailed" event instead of rejecting `opened`.
      const failed = new Promise((_, reject) => temp.on("openFailed", err => reject(err || new Error("openFailed"))));
      await withTimeout(Promise.race([temp.opened, failed]), 20000, "خواندن این EPUB بیش از حد طول کشید؛ ممکن است فایل خراب باشد.");
      const metadata = await temp.loaded.metadata;
      if (metadata.title) title = metadata.title;
      if (metadata.creator) author = metadata.creator;
      extra = extraMeta(metadata);
      try {
        const coverUrl = await temp.coverUrl();
        if (coverUrl) {
          const response = await fetch(coverUrl);
          cover = await blobToDataUrl(await response.blob());
        }
      } catch (err) { console.warn("Cover extraction skipped:", err); }
      // Let epub.js finish its own background loading before destroying the instance.
      await Promise.allSettled([temp.loaded.navigation, temp.loaded.displayOptions, temp.loaded.cover, temp.loaded.resources]);
    } catch (err) {
      console.error("Metadata extraction failed:", err);
      if (err instanceof UserError) throw err;
      reportError("epub", err, "import:metadata");
      throw new UserError("این فایل EPUB قابل خواندن نیست یا خراب است.");
    } finally {
      if (temp) { try { temp.destroy(); } catch (_) {} }
    }
    return { title, author, cover, ...extra };
  }
  // Optional EPUB metadata kept on the book record and sent to the server (limits match the API).
  const META_LIMITS = { publisher: 300, published: 40, language: 20 };
  function cleanMeta(v, max) { return String(v == null ? "" : v).replace(/\s+/g, " ").trim().slice(0, max); }
  function extraMeta(m) {
    m = m || {};
    let published = cleanMeta(m.pubdate, 40);
    // "2019-05-01T00:00:00Z" -> "2019-05-01"; a plain year stays as it is.
    const iso = /^(\d{4}(?:-\d{2}(?:-\d{2})?)?)T/.exec(published);
    if (iso) published = iso[1];
    return { publisher: cleanMeta(m.publisher, 300), published, language: cleanMeta(m.language, 20) };
  }
  /** The optional fields of a record that have a value, ready for an API body. */
  function metaBody(rec) {
    const out = {};
    for (const [k, max] of Object.entries(META_LIMITS)) { const v = cleanMeta(rec && rec[k], max); if (v) out[k] = v; }
    return out;
  }
  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = reject; r.readAsDataURL(blob);
    });
  }
  async function sha256(buffer) {
    try {
      if (!crypto.subtle) return null;
      const digest = await crypto.subtle.digest("SHA-256", buffer);
      return [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, "0")).join("");
    } catch (_) { return null; }
  }
  // Same size first (cheap), then hash; books imported before v6.2 get their hash filled in lazily.
  async function findDuplicate(size, hash) {
    const books = await getAllBooks();
    for (const b of books) {
      if (b.sourceHash && b.sourceHash === hash) return b; // v8.3: same original file, stored cleaned
      if (b.size !== size) continue;
      let h = b.contentHash;
      if (!h && fileOf(b)) {
        h = await sha256(await fileOf(b).arrayBuffer());
        if (h) { try { await put("books", { ...b, contentHash: h }); } catch (_) {} }
      }
      if (h === hash) return b;
    }
    return null;
  }
  async function requestPersistentStorage() {
    try {
      if (navigator.storage && navigator.storage.persist && !(await navigator.storage.persisted())) await navigator.storage.persist();
    } catch (_) {}
    updateStorageInfo();
  }
  async function updateStorageInfo() {
    const el = $("storageInfo");
    try {
      if (!navigator.storage || !navigator.storage.estimate) { el.textContent = ""; return; }
      const { usage = 0 } = await navigator.storage.estimate();
      const persisted = navigator.storage.persisted ? await navigator.storage.persisted() : false;
      const mb = (usage / 1048576).toLocaleString("fa-IR", { maximumFractionDigits: 1 });
      el.textContent = `فضای استفاده‌شده روی این دستگاه: ${mb} مگابایت` + (persisted ? " · ذخیره‌سازی ماندگار فعال است" : "");
    } catch (_) { el.textContent = ""; }
  }
  function resetInputs() { inputEls.forEach(el => { if (el) el.value = ""; }); }
  // quiet: used by importFiles() for several files — no toasts/sync per file, returns the outcome instead.
  async function importFile(file, quiet) {
    const say = msg => { if (!quiet) showToast(msg); };
    try {
      if (!file) return { ok: false };
      // v8.8: PDFs are recognised by their bytes (%PDF-), whatever the name; EPUBs by extension or type as before.
      const isPdf = await sniffPdf(file);
      if (!isPdf && /\.pdf$/i.test(file.name)) throw new UserError("این فایل PDF قابل خواندن نیست یا خراب است.");
      if (!isPdf && !/\.epub$/i.test(file.name) && file.type !== "application/epub+zip") throw new UserError("لطفاً یک فایل EPUB یا PDF انتخاب کن.");
      if (file.size > 100 * 1024 * 1024) throw new UserError("این نسخه برای فایل‌های حداکثر ۱۰۰ مگابایت تنظیم شده است.");
      if (!db) throw new UserError("ذخیره‌سازی مرورگر آماده نیست.");
      say("در حال بررسی کتاب…");
      const contentHash = await sha256(await file.arrayBuffer());
      if (contentHash && await findDuplicate(file.size, contentHash)) {
        const dup = new UserError("این کتاب قبلاً در کتابخانه هست."); dup.duplicate = true; throw dup;
      }
      const metadata = isPdf ? await extractPdfMetadata(file) : await extractMetadata(file);
      const record = {
        id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        title: metadata.title, author: metadata.author, cover: metadata.cover,
        publisher: metadata.publisher || "", published: metadata.published || "", language: metadata.language || "", metaExt: true,
        file: file, size: file.size, addedAt: Date.now(), contentHash,
        ...(isPdf ? { format: "pdf", pages: metadata.pages || 0 } : {})
      };
      try {
        await put("books", record);
      } catch (err) {
        console.error("Saving book failed:", err);
        if (!(err && err.name === "QuotaExceededError")) reportError("storage", err, "import:save");
        throw new UserError(err && err.name === "QuotaExceededError"
          ? "فضای ذخیره‌سازی مرورگر کافی نیست."
          : "ذخیره کتاب در مرورگر انجام نشد.");
      }
      // v8.3: saved cleanup rules apply to new books before they are uploaded.
      const cleaned = await autoCleanImported(record.id);
      if (!quiet) {
        renderLibrary();
        requestPersistentStorage();
        sync();
      }
      say(cleaned ? `کتاب به کتابخانه اضافه شد؛ ${faNum(cleaned)} مورد حذف شد` : "کتاب به کتابخانه همین دستگاه اضافه شد.");
      return { ok: true, cleaned };
    } catch (err) {
      // Expected outcomes (duplicate, broken file, …) are UserErrors and are not logged as app errors.
      if (!(err instanceof UserError)) { console.error(err); reportError("import", err, "import"); }
      say(err instanceof UserError ? err.message : "افزودن کتاب انجام نشد.");
      return { ok: false, duplicate: !!(err && err.duplicate), message: err instanceof UserError ? err.message : "افزودن کتاب انجام نشد." };
    } finally {
      if (!quiet) resetInputs();
    }
  }
  // Several files at once (multi-select, drag & drop): one after another, progress in a toast, one summary at the end.
  let importingMany = false;
  async function importFiles(list) {
    const files = Array.from(list || []).filter(Boolean);
    if (!files.length) return;
    if (files.length === 1) return importFile(files[0]);
    if (importingMany) { showToast("افزودن کتاب‌های قبلی هنوز تمام نشده است."); return; }
    importingMany = true;
    let added = 0, dups = 0, cleanedSum = 0; const failed = [];
    try {
      for (let i = 0; i < files.length; i++) {
        showToast(`در حال افزودن ${faNum(i + 1)} از ${faNum(files.length)}…`);
        const r = await importFile(files[i], true);
        if (r && r.ok) { added++; cleanedSum += r.cleaned || 0; }
        else if (r && r.duplicate) dups++;
        else failed.push(files[i].name);
        if ((i + 1) % 5 === 0) renderLibrary();
      }
    } finally {
      importingMany = false; resetInputs();
      renderLibrary(); requestPersistentStorage(); sync();
    }
    const parts = [`${faNum(added)} کتاب اضافه شد`];
    if (dups) parts.push(`${faNum(dups)} تکراری بود`);
    if (failed.length) parts.push(`${faNum(failed.length)} ناموفق: ${failed.slice(0, 3).join("، ")}${failed.length > 3 ? "…" : ""}`);
    if (cleanedSum) parts.push(`${faNum(cleanedSum)} مورد پاک‌سازی شد`);
    showToast(parts.join("؛ "));
  }
  let openToken = 0, previewPos = null;
  function percentFor(cfi, fallback) {
    try {
      if (bookInstance && bookInstance.locations && bookInstance.locations.length() > 0 && cfi) {
        return Math.round(bookInstance.locations.percentageFromCfi(cfi) * 100);
      }
    } catch (_) {}
    return typeof fallback === "number" ? Math.round(fallback * 100) : 0;
  }
  function showProgress(pct) {
    const ib = document.getElementById("immersiveBar"); if (ib) ib.style.width = `${pct}%`;
    $("progressBar").style.width = `${pct}%`;
    $("progressText").textContent = `${pct.toLocaleString("fa-IR")}٪`;
  }
  // ---- v8.2 nav: chapter title from the TOC, whole-book seek bar ----
  // tocFlat: the flattened TOC as rendered (same order as the .toc-link buttons).
  let tocFlat = [], seekDragging = false, seekNavigating = false, sectionLocs = new Map();
  const faNum = n => Number(n).toLocaleString("fa-IR");
  function normHref(h) {
    let s = String(h || "").split("#")[0].split("?")[0];
    try { s = decodeURIComponent(s); } catch (_) {}
    const out = [];
    s.replace(/\\/g, "/").split("/").forEach(p => { if (!p || p === ".") return; if (p === "..") out.pop(); else out.push(p); });
    return out.join("/");
  }
  // TOC hrefs may be relative to the nav document, spine hrefs to the package: compare as path suffixes.
  const sameFile = (a, b) => !!a && !!b && (a === b || a.endsWith("/" + b) || b.endsWith("/" + a));
  // v8.4: nav/NCX hrefs are relative to the nav document, but epub.js looks them up as package-relative spine hrefs,
  // so "../text/c1.xhtml" from "nav/nav.xhtml" failed with "No Section Found". Resolve against the nav document's
  // own path to the spine item's href (keeping the #fragment); unknown targets are returned unchanged.
  function resolveTocHref(href) {
    const raw = String(href || ""), hash = raw.indexOf("#");
    const file = hash >= 0 ? raw.slice(0, hash) : raw, frag = hash >= 0 ? raw.slice(hash) : "";
    if (!file || !bookInstance || /^[a-z][a-z0-9+.-]*:/i.test(file)) return raw;
    const items = (bookInstance.spine && bookInstance.spine.spineItems) || [];
    const pk = bookInstance.packaging || {}, nav = String(pk.navPath || pk.ncxPath || "");
    const want = normHref(nav.slice(0, nav.lastIndexOf("/") + 1) + file);
    let item = items.find(s => normHref(s.href) === want);
    if (!item) { try { item = bookInstance.spine.get(file); } catch (_) {} }
    if (!item) item = items.find(s => sameFile(normHref(s.href), want));
    return item ? item.href + frag : raw;
  }
  function setTocEntries(flat) {
    tocFlat = flat.map(({ it, depth, href: resolved }, i) => {
      const href = String(resolved || it.href || ""), hash = href.indexOf("#");
      let frag = hash >= 0 ? href.slice(hash + 1) : "";
      try { frag = decodeURIComponent(frag); } catch (_) {}
      return { i, depth, label: (it.label || "").trim() || `بخش ${i + 1}` /* same text as the TOC button */, file: normHref(href), frag };
    });
  }
  // The TOC entry for a position: same file; among #fragment entries the last one whose target is at or before
  // the position (callers pass the end of the visible page; only when that chapter is loaded), otherwise the deepest
  // whole-file entry, otherwise the first.
  function tocEntryFor(index, cfi) {
    if (!tocFlat.length || index == null || !bookInstance) return null;
    let item = null;
    try { item = bookInstance.spine.get(index); } catch (_) {}
    if (!item) return null;
    const file = normHref(item.href), matches = tocFlat.filter(e => sameFile(e.file, file));
    if (!matches.length) return null;
    const whole = matches.filter(e => !e.frag);
    const fallback = whole.length ? whole.reduce((a, e) => (e.depth > a.depth ? e : a)) : matches[0];
    const frags = matches.filter(e => e.frag);
    if (frags.length && cfi && rendition) {
      try {
        const contents = (rendition.getContents() || []).find(c => c.sectionIndex === index);
        const doc = contents && contents.document;
        if (doc) {
          const cmp = new ePub.CFI();
          let best = null;
          for (const e of frags) {
            const el = doc.getElementById(e.frag);
            if (el && cmp.compare(contents.cfiFromNode(el), cfi) <= 0) best = e;
          }
          if (best) return best;
        }
      } catch (_) {}
    }
    return fallback;
  }
  function chapterTitle(index, cfi) {
    const e = tocEntryFor(index, cfi);
    return e ? e.label : (index != null ? `بخش ${faNum(index + 1)}` : "مطالعه");
  }
  function scrollTocToActive() {
    const act = $("tocList").querySelector(".toc-link.active"), panel = $("tocPanel");
    if (!act || !panel.clientHeight) return;
    const r = act.getBoundingClientRect(), p = panel.getBoundingClientRect();
    if (r.top < p.top || r.bottom > p.bottom) panel.scrollTop += r.top - p.top - panel.clientHeight / 3;
  }
  function updateChapter(location) {
    const st = location && location.start, en = location && location.end;
    // "Position" is the page shown: a #fragment heading visible on this page (e.g. after a TOC jump) already counts.
    const entry = st ? tocEntryFor(st.index, en && en.index === st.index && en.cfi ? en.cfi : st.cfi) : null;
    const label = entry ? entry.label : (st && st.index != null ? `بخش ${faNum(st.index + 1)}` : "مطالعه");
    $("chapterLabel").textContent = label;
    $("chapterLabel").title = label;
    $("readerChapter").textContent = label; // v8.6: the same title as the toolbar's second line on phones
    let changed = false;
    $("tocList").querySelectorAll(".toc-link[data-href]").forEach((b, i) => {
      const on = !!entry && entry.i === i;
      if (b.classList.contains("active") !== on) changed = true;
      b.classList.toggle("active", on);
      if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    });
    // Desktop: the TOC is always visible, keep the current chapter in view.
    if (changed && window.matchMedia("(min-width: 701px)").matches) scrollTocToActive();
  }
  function locationsReady() {
    try { return !!(bookInstance && bookInstance.locations && bookInstance.locations.length() > 0); } catch (_) { return false; }
  }
  function resetSeek() {
    const r = $("bookSeek");
    r.disabled = true; r.value = "0"; r.dir = bookIsRtl() ? "rtl" : "ltr";
    r.setAttribute("aria-valuetext", "در حال محاسبهٔ صفحه‌ها…");
    $("seekInfo").textContent = "در حال محاسبهٔ صفحه‌ها…";
    $("seekPreview").hidden = true;
    seekDragging = false; seekNavigating = false; sectionLocs = new Map();
  }
  function enableSeek() {
    sectionLocs = new Map();
    // Locations per spine item (from the CFI's itemref step), used to turn the chapter's page count into a book estimate.
    (bookInstance.locations._locations || []).forEach(c => {
      const m = /^epubcfi\(\/\d+\/(\d+)/.exec(String(c));
      if (m) { const idx = Number(m[1]) / 2 - 1; sectionLocs.set(idx, (sectionLocs.get(idx) || 0) + 1); }
    });
    $("bookSeek").disabled = false;
  }
  function setSeekValueText(pct) { $("bookSeek").setAttribute("aria-valuetext", `${faNum(pct)} درصد از کتاب`); }
  function updateSeek(location) {
    if (!locationsReady() || !location?.start?.cfi) return;
    const L = bookInstance.locations;
    let p = 0;
    try { p = Math.max(0, Math.min(1, L.percentageFromCfi(location.start.cfi) || 0)); } catch (_) { return; }
    if (!seekDragging && !seekNavigating) { $("bookSeek").value = String(Math.round(p * 1000)); setSeekValueText(Math.round(p * 100)); }
    // Book time left: remaining locations × pages per location (measured in this chapter) × the reader's seconds per page.
    let min = 0;
    try {
      const total = L.length(), cur = Math.max(0, L.locationFromCfi(location.start.cfi) || 0);
      const disp = location.start.displayed, inSection = sectionLocs.get(location.start.index) || 0;
      const ratio = disp && disp.total && inSection ? Math.max(0.1, Math.min(10, disp.total / inSection)) : 1;
      min = Math.ceil(Math.max(0, total - cur - 1) * ratio * secPerPage / 60);
    } catch (_) {}
    const head = `٪${faNum(Math.round(p * 100))} از کتاب`;
    $("seekInfo").textContent = location.atEnd ? `${head} · آخرین صفحهٔ کتاب` : `${head} · حدود ${faNum(Math.max(1, min))} دقیقه تا پایان کتاب`;
  }
  function showSeekPreview() {
    const r = $("bookSeek"), pv = $("seekPreview");
    if (!locationsReady() && !pdf) return;
    const p = Number(r.value) / 1000;
    let title = "";
    if (pdf) {
      const n = pdfPageFromSeek(), e = pdfOutlineFor(pdf, n);
      pv.textContent = `${e ? e.label + " · " : ""}صفحهٔ ${faNum(n)}`;
    } else {
      try { const cfi = bookInstance.locations.cfiFromPercentage(p); title = chapterTitle(new ePub.CFI(cfi).spinePos, cfi); } catch (_) {}
      pv.textContent = `${title ? title + " · " : ""}٪${faNum(Math.round(p * 100))}`;
    }
    setSeekValueText(Math.round(p * 100));
    pv.hidden = false;
    const wrap = r.parentElement.getBoundingClientRect(), rr = r.getBoundingClientRect();
    const frac = r.dir === "rtl" ? 1 - p : p, thumb = 8;
    const x = rr.left - wrap.left + thumb + frac * Math.max(0, rr.width - 2 * thumb), w = pv.offsetWidth;
    pv.style.left = `${Math.max(w / 2, Math.min(wrap.width - w / 2, x))}px`;
  }
  function endSeekDrag() { seekDragging = false; $("seekPreview").hidden = true; }
  $("bookSeek").addEventListener("input", () => { seekDragging = true; showSeekPreview(); });
  $("bookSeek").addEventListener("change", () => {
    endSeekDrag();
    if (pdf) { jumpTo(`pdf:${pdfPageFromSeek()}`); return; } // jumpTo: offers «برگشت به صفحهٔ قبل»
    if (!rendition || !locationsReady()) return;
    let cfi = null;
    try { cfi = bookInstance.locations.cfiFromPercentage(Number($("bookSeek").value) / 1000); } catch (_) {}
    if (!cfi) return;
    const token = openToken;
    seekNavigating = true; // keep the thumb where the reader put it (arrow keys step from there)
    Promise.resolve(jumpTo(cfi)).catch(err => console.warn("Seek failed:", err)) // jumpTo: offers «برگشت به صفحهٔ قبل»
      .then(() => new Promise(r => setTimeout(r, 300))).then(() => {
        if (token !== openToken || !rendition) return;
        seekNavigating = false;
        // Small steps (arrow keys) often stay on the same page: keep the thumb unless the page is clearly elsewhere.
        try {
          const loc = rendition.currentLocation(), want = Number($("bookSeek").value);
          const got = Math.round(bookInstance.locations.percentageFromCfi(loc.start.cfi) * 1000);
          if (Math.abs(got - want) > 20) updateSeek(loc);
        } catch (_) {}
      });
  });
  ["pointerup", "pointercancel", "blur"].forEach(ev => $("bookSeek").addEventListener(ev, endSeekDrag));
  // Online previews of catalog books (ids "preview-N") are never stored: last line of defence for every write path.
  const isPreviewId = id => typeof id === "string" && id.startsWith("preview-");
  async function saveProgress(bookId, cfi, pct, bookmark) {
    if (isPreviewId(bookId)) return;
    const old = await getProgress(bookId);
    await put("progress", { bookId, cfi, percentage: pct, updatedAt: Date.now(), bookmark: bookmark !== undefined ? bookmark : (old?.bookmark || null) });
    scheduleProgressPush(bookId);
  }
  // v8.5 diagnostics: how often the expensive open steps really ran (read by the e2e tests when borg-debug has "stats").
  const openStats = { detect: 0, locGen: 0 };
  // Generic diagnostics (WebKit probe): counters/timestamps in <html data-borg-diag> when borg-debug has "stats".
  const diagData = {}, diagT0 = Date.now();
  function diag(k, v) { if (!DEBUG.stats) return; diagData[k] = v === undefined ? (diagData[k] || 0) + 1 : v; document.documentElement.dataset.borgDiag = JSON.stringify(diagData); }
  function countOpenStat(k) { openStats[k]++; if (DEBUG.stats) document.documentElement.dataset["borg" + k[0].toUpperCase() + k.slice(1)] = String(openStats[k]); }
  async function prepareLocations(record, token) {
    const book = bookInstance;
    try {
      if (record.locations) {
        book.locations.load(record.locations);
      } else {
        countOpenStat("locGen");
        // epub.js waits 100 ms after every spine item (80 chapters = 8 s idle); a short pause still yields to the UI.
        book.locations.pause = 8;
        await book.locations.generate(1600);
        if (token !== openToken) return;
        const latest = record.preview ? null : await getBook(record.id);
        if (latest) await put("books", { ...latest, locations: book.locations.save() });
      }
      if (token !== openToken || !rendition) return;
      enableSeek();
      const loc = rendition.currentLocation();
      if (loc?.start?.cfi) {
        const pct = percentFor(loc.start.cfi);
        showProgress(pct);
        updateSeek(loc);
        if (currentBook && !currentBook.preview) await saveProgress(currentBook.id, loc.start.cfi, pct);
      }
    } catch (err) {
      console.warn("Could not compute reading progress:", err);
    }
  }
  /**
   * Open a stored book by id, or (previewRecord) an ephemeral one that is not in the library:
   * a preview is never written to IndexedDB, and its progress, annotations and reading time are not saved or synced.
   */
  async function openReader(id, previewRecord) {
    const record = previewRecord || await getBook(id);
    if (!record) { showToast("فایل این کتاب روی دستگاه پیدا نشد."); return; }
    // v8.8: a record without `format` (older client, server without the field) is a PDF only if its bytes say so.
    if (!record.format && fileOf(record) && await sniffPdf(fileOf(record))) record.format = "pdf";
    if (!isPdfRecord(record) && !librariesReady()) { showToast("کتاب‌خوان کامل بارگذاری نشده است. صفحه را تازه‌سازی کن."); return; }
    const token = ++openToken;
    closePdf();
    clearJumps();
    currentBook = record; currentProgressKey = record.id;
    $("readerTitle").textContent = record.title;
    $("previewBanner").hidden = !record.preview;
    $("previewAdd").disabled = false;
    $("reader").classList.remove("hidden");
    document.body.style.overflow = "hidden";
    $("tocList").innerHTML = '<p class="muted">در حال ساخت فهرست…</p>';
    $("viewer").innerHTML = "";
    showOpening();
    $("chapterLabel").textContent = "شروع مطالعه";
    $("readerChapter").textContent = "";
    tocFlat = []; resetSeek();
    $("prevPage").disabled = true; $("nextPage").disabled = false;
    showProgress(0);
    if (bookInstance) { try { bookInstance.destroy(); } catch (_) {} }
    const bookFile = fileOf(record);
    if (!bookFile) { hideOpening(); showToast("فایل این کتاب روی دستگاه پیدا نشد."); return; }
    // The saved position is read while the file is (they are independent).
    const savedP = record.preview ? Promise.resolve(previewPos ? { cfi: previewPos } : null) : getProgress(record.id).catch(() => null);
    if (isPdfRecord(record)) return openPdfReader(record, token, savedP);
    const epubBytes = await bookFile.arrayBuffer();
    if (token !== openToken) return;
    bookInstance = ePub(epubBytes);
    bookInstance.on("openFailed", err => {
      console.error("EPUB openFailed:", err);
      reportError("epub", err, "reader:openFailed");
      if (token === openToken) { hideOpening(); showToast("این فایل EPUB قابل خواندن نیست یا خراب است."); }
    });
    bookDir = await resolveBookDir(record, () => detectBookDirection(bookInstance));
    if (token !== openToken) return;
    $("bookSeek").dir = bookIsRtl() ? "rtl" : "ltr"; // RTL books fill the seek bar from the right
    patchTextWidth(DEBUG.tw || "rects"); // Safari/iPad fix: stock measurement inflates RTL column width to ~500k px → blank page
    rendition = bookInstance.renderTo("viewer", {
      width: "100%", height: "100%", flow: readerPrefs.flow === "scrolled" ? "scrolled-doc" : "paginated",
      spread: readerPrefs.spread === "none" ? "none" : "auto", manager: "default",
      ...(DEBUG.method ? { method: DEBUG.method } : {}),
      // Many Persian EPUBs omit page-progression-direction (some even claim lang="en"); without RTL here the
      // chapter start sits off-screen and TOC jumps land on the chapter's last page. A book's own direction still wins.
      defaultDirection: DEBUG.direction || bookDir,
      // Safari runs no event listener inside a sandboxed iframe without allow-scripts, so taps, footnote and image
      // clicks and keys inside the book silently did nothing on iPhone/iPad. Book scripts still cannot run: the
      // srcdoc frame inherits our CSP (script-src 'self') and stripBookScripts() removes them before rendering.
      allowScriptedContent: true,
    });
    // Keep each newly loaded chapter invisible until the reader font and theme are applied, so the text
    // doesn't first paint in the fallback font and then visibly re-flow.
    // Registered after "opened" so it runs after epub.js's own resource-substitution hook (both rewrite section.output).
    // Defence in depth for allowScriptedContent: drop scripts, inline handlers and javascript: URLs from every chapter.
    bookInstance.spine.hooks.content.register(stripBookScripts);
    const veilBook = bookInstance;
    veilBook.opened.then(() => veilBook.spine.hooks.serialize.register((output, section) => {
      const html = typeof section.output === "string" ? section.output : output;
      // The CSS animation lifts the veil by itself after 1.5 s even if no script ever runs (it stuck forever in WebKit).
      if (typeof html === "string") section.output = html.replace(/<head(\s[^>]*)?>/i, m => `${m}<style id="borg-veil">html{visibility:hidden;animation:borg-unveil 0s 1.5s forwards}@keyframes borg-unveil{to{visibility:visible}}</style>`);
    })).catch(() => {});
    rendition.hooks.content.register(onBookContent);
    // The book is rendered in an iframe; load the bundled font there too, then lift the veil.
    // Not returned to epub.js (never block rendering on fonts); every reveal looks the veil up in the live document.
    const liftVeil = c => { try { const d = c && c.document; if (d) d.querySelectorAll("style#borg-veil").forEach(v => v.remove()); } catch (_) {} };
    rendition.hooks.content.register(contents => {
      diag("veilHook"); const veilStart = Date.now();
      const reveal = () => { diag("veilLiftMs", Date.now() - diagT0); diag("veilMs", Date.now() - veilStart); liftVeil(contents); if (token === openToken) hideOpening(); };
      const safety = setTimeout(reveal, 1500); // never leave a page hidden
      // WebKit never fires the <link> load event inside the book iframe, so addStylesheet() can hang: also poll
      // for the stylesheet object itself (capped at 600 ms) instead of waiting for the 1.5 s safety.
      const cssApplied = new Promise(r => {
        const t0 = Date.now();
        (function poll() {
          let ok = false;
          try { const l = contents.document && contents.document.querySelector(`link[href="${READER_FONTS_URL}"]`); ok = !!(l && l.sheet); } catch (_) {}
          if (ok || Date.now() - t0 > 600) r(); else setTimeout(poll, 25);
        })();
      });
      Promise.race([Promise.resolve(contents.addStylesheet(READER_FONTS_URL)), cssApplied])
        .then(() => { diag("fontsCssMs", Date.now() - diagT0); return new Promise(r => { const d = contents.document; if (d && d.body) void d.body.offsetHeight; const w = contents.window; (w && w.requestAnimationFrame ? w.requestAnimationFrame.bind(w) : setTimeout)(r); }); })
        .then(() => { const d = contents.document; return Promise.race([d && d.fonts ? d.fonts.ready : null, new Promise(r => setTimeout(r, 700))]); })
        .catch(() => {})
        .then(() => { diag("fontsReadyMs", Date.now() - diagT0); clearTimeout(safety); reveal(); });
    });
    rendition.on("rendered", () => setTimeout(() => { try { rendition.getContents().forEach(liftVeil); } catch (_) {} }, 1600));
    applyReaderStyles();
    // Input inside the book iframe does not reach the main document; epub.js re-emits it on the rendition.
    rendition.on("keydown", handleReaderKey);
    rendition.on("touchstart", onTouchStart);
    rendition.on("touchend", onTouchEnd);
    rendition.on("click", onReaderClick);
    rendition.on("selected", onTextSelected);
    rendition.on("relocated", onJumpRelocated);
    rendition.on("keydown", markActivity); rendition.on("touchstart", markActivity); rendition.on("click", markActivity);
    lastTurnAt = 0; markActivity(); updateWakeLock();
    if (!record.preview && (!record.status || record.status === "want")) setBookMeta(record.id, { status: "reading" }).then(r => { if (r && currentBook && currentBook.id === r.id) currentBook = r; });
    renderedHighlights = new Set();
    rendition.on("relocated", async location => {
      if (token !== openToken || !location?.start) return;
      const pct = percentFor(location.start.cfi, location.start.percentage);
      showProgress(pct);
      updateChapter(location);
      $("prevPage").disabled = !!location.atStart;
      if (!pendingSelection) hideSelectionBar();
      updateBookmarkButton();
      markActivity();
      updateTimeLeft(location);
      updateSeek(location);
      onTtsRelocated(location);
      if (location.atEnd && currentBook && !currentBook.preview && currentBook.status !== "finished") {
        setBookMeta(currentBook.id, { status: "finished" }).then(r => { if (r) { currentBook = r; showToast("تبریک! این کتاب را تمام کردی."); } });
      }
      $("nextPage").disabled = !!location.atEnd;
      const bk = currentBook;
      if (!bk) return;
      if (bk.preview) { previewPos = location.start.cfi; return; }
      try { await saveProgress(bk.id, location.start.cfi, pct); } catch (err) { console.warn("Saving progress failed:", err); }
    });
    rendition.on("displayError", (err) => {
      console.error("EPUB displayError:", err);
      reportError("epub", err, "reader:displayError");
      showToast("نمایش این کتاب ناموفق بود. خطای دقیق در Console ثبت شده است.");
    });
    rendition.on("rendered", () => {
      try {
        rendition.getContents().forEach(c => {
          if (!DEBUG.noDir) c.addStylesheetRules({ "body": { "direction": bookDir } });
        });
      } catch (_) {}
    });
    const saved = await savedP;
    let shown = false;
    if (saved?.cfi) {
      try { await rendition.display(saved.cfi); shown = true; } catch (err) { console.warn("Saved location could not be restored:", err); }
    }
    // The indicator goes when the first chapter's veil is lifted; this is only its safety net (the veil's is 1.5 s).
    try { if (!shown) await rendition.display(); } finally { setTimeout(() => { if (token === openToken) hideOpening(); }, 1600); }
    if (token !== openToken) return;
    maybeShowGuide();
    const nav = await bookInstance.loaded.navigation;
    if (token !== openToken) return;
    const toc = nav.toc || [];
    // Nested entries (e.g. articles under a section) are listed too, indented by depth (max 3 levels).
    const flat = [];
    (function walk(items, depth) { (items || []).forEach(it => { flat.push({ it, depth, href: resolveTocHref(it.href) }); if (depth < 2) walk(it.subitems, depth + 1); }); })(toc, 0);
    $("tocList").innerHTML = flat.length ? flat.map(({ it, depth, href }, i) => `<button class="toc-link${depth ? " toc-sub toc-d" + depth : ""}" type="button" dir="auto" data-href="${esc(href)}">${esc((it.label || "").trim() || `بخش ${i+1}`)}</button>`).join("") : '<p class="muted">فهرست فصل‌ها در این EPUB وجود ندارد.</p>';
    $("tocList").querySelectorAll("[data-href]").forEach(btn => btn.addEventListener("click", () => { closeToc(false); jumpTo(btn.dataset.href); }));
    setTocEntries(flat);
    try { updateChapter(rendition.currentLocation()); } catch (_) {}
    await renderBookAnnotations();
    updateBookmarkButton();
    prepareLocations(record, token);
  }
  /**
   * Reading direction of a book: «جهت این کتاب» override (8.6.5), else the cached detection (v8.5: per file and
   * detector version), else `detect()` — whose result is cached on the record (not for previews).
   */
  async function resolveBookDir(record, detect) {
    const dc = record.dirCache, dirKey = record.contentHash || `size:${record.size || 0}`;
    if (record.dirOverride === "rtl" || record.dirOverride === "ltr") return record.dirOverride;
    if (dc && dc.v === DIR_DETECT_V && dc.key === dirKey && (dc.dir === "rtl" || dc.dir === "ltr")) return dc.dir;
    countOpenStat("detect");
    const dir = await detect();
    if (!record.preview) {
      const cache = { dir, v: DIR_DETECT_V, key: dirKey };
      // One read-write transaction that only adds the field, so it cannot undo a concurrent write (status etc.).
      try {
        const os = db.transaction("books", "readwrite").objectStore("books"), g = os.get(record.id);
        g.onsuccess = () => { const cur = g.result; if (cur && (cur.contentHash || `size:${cur.size || 0}`) === dirKey) os.put({ ...cur, dirCache: cache }); };
      } catch (err) { console.warn("Caching the book direction failed:", err); }
    }
    return dir;
  }
  // v8.5: «در حال باز کردن کتاب…» over the page from the click until the first page is shown.
  function showOpening() { $("openingIndicator").hidden = false; renderSyncChip(); }
  function hideOpening() { $("openingIndicator").hidden = true; }
  function getProgress(id) {
    return new Promise((resolve, reject) => {
      const req = db.transaction("progress", "readonly").objectStore("progress").get(id);
      req.onsuccess = () => resolve(req.result || null); req.onerror = () => reject(req.error);
    });
  }
  async function closeReader() {
    const wasPreview = !!(currentBook && currentBook.preview);
    if (pdf && currentBook && !currentBook.preview) {
      try { await saveProgress(currentBook.id, `pdf:${pdf.page}`, pdfPercent(pdf)); } catch (_) {}
    }
    closePdf();
    if (rendition && currentBook && !currentBook.preview) {
      try {
        const loc = rendition.currentLocation();
        if (loc?.start?.cfi) await saveProgress(currentBook.id, loc.start.cfi, percentFor(loc.start.cfi, loc.start.percentage));
      } catch (_) {}
    }
    openToken++; hideOpening();
    $("reader").classList.remove("toc-open", "immersive");
    closeSettings(false);
    closeSearch(false);
    clearJumps(); closeGuide(false);
    hideSelectionBar(); renderedHighlights = new Set();
    ttsStop(); $("notePopup").hidden = true; $("lookupBox").hidden = true; closeImage();
    $("bookSearchResults").innerHTML = ""; $("bookSearchStatus").textContent = ""; $("bookSearchInput").value = "";
    $("tocToggle").setAttribute("aria-expanded", "false");
    $("reader").classList.add("hidden");
    renderSyncChip();
    document.body.style.overflow = "";
    if (bookInstance) { try { bookInstance.destroy(); } catch (_) {} }
    bookInstance = null; rendition = null; currentBook = null; previewPos = null;
    $("previewBanner").hidden = true;
    updateWakeLock();
    renderLibrary();
    // A preview was opened from the catalog detail page, which is still underneath: put focus back there.
    if (wasPreview && !$("viewBook").hidden) $("detailRead").focus({ preventScroll: true });
  }
  // ======================= v8.8 PDF reading (text-based magazines in their original layout) =======================
  // pdf.js 4.10.38 (legacy build, vendored under assets/vendor/pdfjs) is imported only when a PDF is imported or opened;
  // the Service Worker's runtime cache keeps it afterwards (it is not in APP_SHELL). PDF scripting is never enabled
  // (no sandbox build is shipped), eval is off (isEvalSupported: false) and CMaps / standard fonts come from the
  // vendored folders, so nothing leaves the site. The page is drawn on a canvas inside #viewer with pdf.js' text layer
  // on top (selection, copy, search marks). Only the visible page(s) and their neighbours keep a canvas.
  const PDFJS_BASE = new URL("assets/vendor/pdfjs/", document.baseURI).href;
  const PDF_MAX_PX = 10e6;            // pixels per canvas (iOS refuses canvases above ~16.7 M and limits their total memory)
  const PDF_ZOOM_MIN = 0.5, PDF_ZOOM_MAX = 5, PDF_ZOOM_STEP = 1.25, PDF_PAD = 10;
  let pdfjsLoading = null;
  function loadPdfJs() {
    if (!pdfjsLoading) {
      pdfjsLoading = import(PDFJS_BASE + "pdf.min.mjs").then(m => { m.GlobalWorkerOptions.workerSrc = PDFJS_BASE + "pdf.worker.min.mjs"; return m; })
        .catch(err => { pdfjsLoading = null; throw err; });
    }
    return pdfjsLoading;
  }
  function pdfOpenOptions(data) {
    return { data, isEvalSupported: false, enableXfa: false, cMapUrl: PDFJS_BASE + "cmaps/", cMapPacked: true,
             standardFontDataUrl: PDFJS_BASE + "standard_fonts/", verbosity: 0 };
  }
  function isPdfRecord(rec) { return !!rec && rec.format === "pdf"; }
  /** A PDF starts with "%PDF-" (the format allows up to 1 KB of junk before it). */
  async function sniffPdf(blob) {
    try {
      const b = new Uint8Array(await blob.slice(0, 1024).arrayBuffer());
      for (let i = 0; i + 4 < b.length; i++) if (b[i] === 0x25 && b[i + 1] === 0x50 && b[i + 2] === 0x44 && b[i + 3] === 0x46 && b[i + 4] === 0x2d) return true;
    } catch (_) {}
    return false;
  }
  function pdfOpenError(err) {
    const n = err && err.name;
    if (n === "PasswordException") return new UserError("این PDF رمز دارد و باز نمی‌شود.");
    if (n === "InvalidPDFException" || n === "MissingPDFException" || n === "UnexpectedResponseException") return new UserError("این فایل PDF قابل خواندن نیست یا خراب است.");
    return null;
  }
  async function loadPdfJsOrExplain(context) {
    try { return await loadPdfJs(); }
    catch (err) {
      reportError("pdf", err, context);
      throw new UserError(navigator.onLine ? "نمایشگر PDF بارگذاری نشد؛ صفحه را تازه‌سازی کن." : "نمایشگر PDF هنوز روی این دستگاه ذخیره نشده است؛ یک بار با اینترنت باز کن.");
    }
  }
  /** Page 1 as a small JPEG data URL, like EPUB covers. */
  async function pdfCoverDataUrl(doc) {
    const page = await doc.getPage(1);
    const vp1 = page.getViewport({ scale: 1 }), vp = page.getViewport({ scale: Math.min(2, 360 / Math.max(1, vp1.width)) });
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.floor(vp.width)); c.height = Math.max(1, Math.floor(vp.height));
    const ctx = c.getContext("2d", { alpha: false });
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
    await page.render({ canvasContext: ctx, viewport: vp }).promise;
    const url = c.toDataURL("image/jpeg", 0.82);
    c.width = c.height = 0;
    return url;
  }
  /** Title/author from the PDF info dictionary (file name / «نویسنده نامشخص» otherwise), cover and page count. */
  async function extractPdfMetadata(file) {
    const lib = await loadPdfJsOrExplain("import:load");
    let task = null;
    try {
      task = lib.getDocument(pdfOpenOptions(new Uint8Array(await file.arrayBuffer())));
      const doc = await withTimeout(task.promise, 30000, "خواندن این PDF بیش از حد طول کشید؛ ممکن است فایل خراب باشد.");
      let info = {};
      try { info = ((await doc.getMetadata()) || {}).info || {}; } catch (_) {}
      let cover = null;
      try { cover = await pdfCoverDataUrl(doc); } catch (err) { console.warn("PDF cover skipped:", err); }
      return { title: cleanMeta(info.Title, 500) || file.name.replace(/\.pdf$/i, "") || "بدون عنوان",
               author: cleanMeta(info.Author, 300) || "نویسنده نامشخص", cover, pages: doc.numPages };
    } catch (err) {
      if (err instanceof UserError) throw err;
      const known = pdfOpenError(err);
      if (known) throw known;
      console.error("PDF metadata failed:", err);
      reportError("pdf", err, "import:metadata");
      throw new UserError("این فایل PDF قابل خواندن نیست یا خراب است.");
    } finally {
      if (task) { try { task.destroy(); } catch (_) {} }
    }
  }
  // ---- Text (direction detection, search) ----
  async function pdfPageText(st, n) {
    if (st.text.has(n)) return st.text.get(n);
    const page = await pdfPageProxy(st, n);
    const tc = await page.getTextContent();
    const text = tc.items.map(it => (it.str || "") + (it.hasEOL ? "\n" : "")).join("");
    st.text.set(n, text);
    return text;
  }
  /** Like detectBookDirection(): Arabic-script vs Latin letters on the first pages; image-only PDFs fall back to RTL. */
  async function detectPdfDirection(st) {
    let rtl = 0, ltr = 0;
    for (let n = 1; n <= Math.min(st.pages, 6) && rtl + ltr <= 5000; n++) {
      try {
        const text = await pdfPageText(st, n);
        rtl += (text.match(/[֐-ࣿיִ-﷿ﹰ-﻿]/g) || []).length;
        ltr += (text.match(/[A-Za-zÀ-ɏͰ-ϿЀ-ӿ]/g) || []).length;
      } catch (_) {}
    }
    return rtl + ltr > 50 ? (rtl >= ltr ? "rtl" : "ltr") : "rtl";
  }
  // Search key: presentation forms → letters (NFKC), ی/ي/ى, ک/ك, ە/ه, digits 0-9, lower case; half-spaces, bidi marks,
  // tatweel and harakat dropped; whitespace collapsed. `map[i]` is the index in the original text of key[i].
  const PDF_DROP = /[​-‏‪-‮⁦-⁩﻿ـً-ٰٟ]/;
  function pdfFold(text) {
    let key = "", space = true; const map = [];
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (/\s/.test(c)) { if (!space) { key += " "; map.push(i); space = true; } continue; }
      for (let f of c.normalize("NFKC")) {
        if (PDF_DROP.test(f)) continue;
        const d = LIB_DIGITS.indexOf(f);
        f = d >= 0 ? String(d % 10) : f === "ي" || f === "ى" ? "ی" : f === "ك" ? "ک" : f === "ە" ? "ه" : f.toLowerCase();
        key += f; map.push(i); space = false;
      }
    }
    return { key, map };
  }
  // Many PDFs (Chromium's own, some Persian publishing tools) store Arabic-script text glyph by glyph in visual order,
  // so a word comes out reversed in the text content; such words are found by also searching the reversed query.
  function pdfQueryVariants(query) {
    const q = pdfFold(String(query || "")).key.trim();
    if (!q) return [];
    const out = [q];
    if (/[֐-ࣿ]/.test(q) && q.length > 1) out.push([...q].reverse().join(""));
    return [...new Set(out)];
  }
  function pdfFindAll(key, variants) {
    const hits = [];
    variants.forEach((v, k) => { for (let i = key.indexOf(v); i >= 0; i = key.indexOf(v, i + 1)) hits.push([i, v.length, k > 0]); });
    return hits.sort((a, b) => a[0] - b[0]);
  }
  async function pdfSearch(st, query) {
    const variants = pdfQueryVariants(query), results = [];
    if (!variants.length) return results;
    for (let n = 1; n <= st.pages && results.length < 100; n++) {
      if (n % 8 === 0) await sleep(0); // keep the page responsive on big magazines
      if (pdf !== st) return [];
      let text;
      try { text = await pdfPageText(st, n); } catch (_) { continue; }
      const { key, map } = pdfFold(text);
      for (const [i, len, reversed] of pdfFindAll(key, variants)) {
        const a = map[i], z = map[i + len - 1] + 1, flat = t => t.replace(/\s+/g, " ");
        let before = flat(text.slice(Math.max(0, a - 40), a)), match = flat(text.slice(a, z)), after = flat(text.slice(z, z + 40));
        // Text stored in visual order: show the excerpt in reading order.
        if (reversed) { const rev = t => [...t].reverse().join(""); [before, match, after] = [rev(after), rev(match), rev(before)]; }
        results.push({ page: n, cfi: `pdf:${n}`, before, match, after });
        if (results.length >= 100) break;
      }
    }
    return results;
  }
  /** Mark the search query in a rendered text layer (the match on the page the search result points to). */
  function pdfApplyHits(st, n) {
    const pe = st.pageEls.get(n), layer = pe && pe.el.querySelector(".textLayer");
    if (!layer) return;
    layer.querySelectorAll(".pdf-hit").forEach(h => h.replaceWith(document.createTextNode(h.textContent)));
    layer.normalize();
    if (!st.hl || st.hl.page !== n || !layer.dataset.ready) return;
    const nodes = [], starts = [];
    let all = "";
    const w = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode()) { nodes.push(t); starts.push(all.length); all += t.nodeValue; }
    const { key, map } = pdfFold(all);
    const segs = [];
    for (const [i, len] of pdfFindAll(key, pdfQueryVariants(st.hl.q))) {
      const a = map[i], z = map[i + len - 1] + 1;
      nodes.forEach((t, k) => { const lo = Math.max(a, starts[k]), hi = Math.min(z, starts[k] + t.nodeValue.length); if (lo < hi) segs.push({ k, lo: lo - starts[k], hi: hi - starts[k] }); });
    }
    segs.sort((x, y) => y.k - x.k || y.lo - x.lo); // from the end, so earlier offsets stay valid
    let lastK = -1, lastLo = Infinity, first = null;
    for (const sg of segs) {
      if (sg.k === lastK && sg.hi > lastLo) continue;
      const t = nodes[sg.k];
      t.splitText(sg.hi);
      const mid = t.splitText(sg.lo), m = document.createElement("span");
      m.className = "pdf-hit"; mid.replaceWith(m); m.append(mid);
      lastK = sg.k; lastLo = sg.lo; first = m;
    }
    // A zoomed or fit-to-width page may hide the match: scroll the stage to it.
    if (first && st.hl.scroll) {
      st.hl.scroll = false;
      const s = st.stage, r = first.getBoundingClientRect(), sr = s.getBoundingClientRect();
      if (r.top < sr.top || r.bottom > sr.bottom) s.scrollTop += r.top - sr.top - s.clientHeight / 3;
      if (r.left < sr.left || r.right > sr.right) s.scrollLeft += r.left - sr.left - s.clientWidth / 3;
    }
  }
  // ---- Layout ----
  function pdfFitMode() {
    const f = readerPrefs.pdfFit;
    if (f === "width" || f === "page") return f;
    return window.matchMedia("(max-width: 700px)").matches ? "width" : "page"; // phones: fit to width, desktop: whole page
  }
  /** Two pages side by side on wide landscape viewers («ستون‌ها: خودکار»); the first page (cover) stays alone. */
  function pdfSpreadOn(st) {
    if (readerPrefs.spread === "none" || st.pages < 3) return false;
    const w = st.stage.clientWidth, h = st.stage.clientHeight;
    return w >= 800 && w > h * 1.05;
  }
  function pdfFirstOf(st, n) {
    n = Math.max(1, Math.min(st.pages, n | 0));
    return pdfSpreadOn(st) && n > 1 ? n - (n % 2 === 1 ? 1 : 0) : n;
  }
  function pdfVisible(st) {
    const a = pdfFirstOf(st, st.page);
    return pdfSpreadOn(st) && a > 1 && a + 1 <= st.pages ? [a, a + 1] : [a];
  }
  function pdfPercent(st) { const v = pdfVisible(st); return Math.round(v[v.length - 1] / st.pages * 100); }
  function pdfTargetPage(target) {
    const m = /^pdf:(\d+)$/.exec(String(target == null ? "" : target).trim());
    const n = m ? Number(m[1]) : (typeof target === "number" ? target : 0);
    return pdf && n >= 1 && n <= pdf.pages ? n : (n >= 1 && !pdf ? n : 0);
  }
  function pdfPageFromSeek() {
    const v = Number($("bookSeek").value) / 1000;
    return pdf ? Math.max(1, Math.min(pdf.pages, 1 + Math.round(v * (pdf.pages - 1)))) : 1;
  }
  function pdfOutlineFor(st, n) {
    let best = null;
    for (const e of st.outline) if (e.page && e.page <= n && (!best || e.page >= best.page)) best = e;
    return best;
  }
  function pdfPageProxy(st, n) {
    let p = st.proxies.get(n);
    if (!p) { p = st.doc.getPage(n); st.proxies.set(n, p); p.catch(() => st.proxies.delete(n)); }
    return p;
  }
  function pdfCanPanX() { return !!pdf && pdf.stage.scrollWidth > pdf.stage.clientWidth + 2; }
  function pdfReleaseCanvas(e) {
    if (e.task) { try { e.task.cancel(); } catch (_) {} e.task = null; }
    e.canvas.remove(); e.canvas.width = e.canvas.height = 0; // frees the bitmap right away (iOS keeps it otherwise)
  }
  /** The page's canvas at this scale: reused when already rendered (or rendering), otherwise a new cancellable render. */
  function pdfEnsureCanvas(st, n, page, scale) {
    const key = `${scale.toFixed(5)}@${window.devicePixelRatio || 1}`;
    let e = st.canvases.get(n);
    if (e && e.key === key) return e;
    if (e) pdfReleaseCanvas(e);
    const vp = page.getViewport({ scale });
    let out = Math.min(window.devicePixelRatio || 1, 3);
    if (vp.width * vp.height * out * out > PDF_MAX_PX) out = Math.sqrt(PDF_MAX_PX / (vp.width * vp.height));
    const canvas = document.createElement("canvas");
    canvas.className = "pdf-canvas";
    canvas.width = Math.max(1, Math.floor(vp.width * out)); canvas.height = Math.max(1, Math.floor(vp.height * out));
    const task = page.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport: vp, transform: out === 1 ? null : [out, 0, 0, out, 0, 0] });
    e = { key, canvas, task };
    e.done = task.promise.then(() => { e.task = null; canvas.dataset.ready = "1"; }, err => {
      e.task = null;
      if (err && err.name === "RenderingCancelledException") return;
      console.warn("PDF page render failed:", err);
      reportError("pdf", err, "reader:render");
    });
    st.canvases.set(n, e);
    return e;
  }
  function pdfBuildTextLayer(st, n, page, pe, scale) {
    if (pe.tl) { try { pe.tl.cancel(); } catch (_) {} pe.tl = null; }
    pe.el.querySelectorAll(".textLayer").forEach(x => x.remove());
    const div = document.createElement("div");
    div.className = "textLayer";
    pe.el.append(div);
    const key = pe.key = scale.toFixed(5);
    page.getTextContent().then(tc => {
      if (pdf !== st || pe.key !== key) return;
      const tl = pe.tl = new st.lib.TextLayer({ textContentSource: tc, container: div, viewport: page.getViewport({ scale }) });
      return tl.render().then(() => { if (pe.tl === tl) { div.dataset.ready = "1"; pdfApplyHits(st, n); } });
    }).catch(err => { if (!(err && (err.name === "AbortException" || /cancel/i.test(err.message || "")))) console.warn("PDF text layer failed:", err); });
  }
  /**
   * Lay out and draw the visible page(s): scale from the fit mode («اندازهٔ صفحه» / «عرض صفحه») × zoom, canvases at
   * devicePixelRatio (capped), text layer on top. Then pre-render the neighbouring pages and release every other canvas.
   * `afterPlace` runs once the pages are in the DOM (before their pixels are ready).
   */
  async function pdfRender(afterPlace) {
    const st = pdf;
    if (!st) return;
    const seq = ++st.seq;
    st.page = pdfFirstOf(st, st.page);
    const list = pdfVisible(st);
    let pages;
    try { pages = await Promise.all(list.map(n => pdfPageProxy(st, n))); }
    catch (err) { if (pdf === st) { hideOpening(); showToast("این صفحهٔ PDF نمایش داده نشد."); reportError("pdf", err, "reader:getPage"); } return; }
    if (pdf !== st || seq !== st.seq) return;
    const fit = pdfFitMode();
    st.stage.classList.toggle("fit-width", fit === "width");
    const pad = fit === "width" ? 0 : PDF_PAD;
    const W = Math.max(50, st.stage.clientWidth - 2 * pad), H = Math.max(50, st.stage.clientHeight - 2 * pad);
    const vps = pages.map(p => p.getViewport({ scale: 1 }));
    const sumW = vps.reduce((a, v) => a + v.width, 0), maxH = Math.max(...vps.map(v => v.height));
    const scale = (fit === "width" ? W / sumW : Math.min(W / sumW, H / maxH)) * st.zoom;
    const els = list.map((n, i) => {
      let pe = st.pageEls.get(n);
      if (!pe) {
        const el = document.createElement("div");
        el.className = "pdf-page"; el.dataset.page = String(n);
        pe = { el, key: "", tl: null }; st.pageEls.set(n, pe);
      }
      const vp = pages[i].getViewport({ scale });
      pe.el.style.width = `${Math.floor(vp.width)}px`; pe.el.style.height = `${Math.floor(vp.height)}px`;
      pe.el.style.setProperty("--scale-factor", String(scale)); // pdf.js text layer sizes itself from it
      const c = pdfEnsureCanvas(st, n, pages[i], scale);
      if (c.canvas.parentNode !== pe.el) pe.el.prepend(c.canvas);
      const fresh = pe.key !== scale.toFixed(5);
      if (fresh) pdfBuildTextLayer(st, n, pages[i], pe, scale);
      return { n, el: pe.el, done: c.done, fresh };
    });
    st.spreadEl.style.flexDirection = bookIsRtl() ? "row-reverse" : "row"; // an RTL magazine's first page is on the right
    st.spreadEl.style.padding = `${pad}px`;
    st.spreadEl.style.transform = "";
    st.spreadEl.replaceChildren(...els.map(x => x.el));
    st.scale = scale;
    for (const [n, pe] of st.pageEls) if (!list.includes(n)) { if (pe.tl) { try { pe.tl.cancel(); } catch (_) {} } st.pageEls.delete(n); }
    pdfRelocated(st);
    if (afterPlace) { try { afterPlace(); } catch (_) {} }
    els.forEach(x => { if (!x.fresh) pdfApplyHits(st, x.n); }); // new text layers mark themselves when rendered
    // Neighbours (next and previous spread) keep / get a canvas while not zoomed in; everything else is released.
    const next = list[list.length - 1] + 1, prevFirst = pdfFirstOf(st, list[0] - 1);
    const around = st.zoom <= 1 ? [next, next + (pdfSpreadOn(st) ? 1 : 0), prevFirst, prevFirst + (pdfSpreadOn(st) && prevFirst > 1 ? 1 : 0)]
      .filter(n => n >= 1 && n <= st.pages && !list.includes(n)) : [];
    const keep = new Set([...list, ...around]);
    for (const [n, e] of st.canvases) if (!keep.has(n)) { pdfReleaseCanvas(e); st.canvases.delete(n); }
    for (const [n, p] of st.proxies) if (!keep.has(n)) p.then(pg => { try { if (!st.canvases.has(n)) pg.cleanup(); } catch (_) {} }).catch(() => {});
    await Promise.all(els.map(x => x.done));
    if (pdf !== st || seq !== st.seq) return;
    if (st.token === openToken) hideOpening();
    for (const n of around) {
      try {
        const page = await pdfPageProxy(st, n);
        if (pdf !== st || seq !== st.seq) return;
        await pdfEnsureCanvas(st, n, page, scale).done;
      } catch (_) {}
      if (pdf !== st || seq !== st.seq) return;
    }
  }
  function pdfScrollToStart(st) {
    st.stage.scrollTop = 0;
    st.stage.scrollLeft = bookIsRtl() ? st.stage.scrollWidth : 0; // the stage is LTR: RTL pages start at the right
  }
  function pdfShow(n) {
    const st = pdf;
    if (!st) return;
    st.page = pdfFirstOf(st, n);
    if (st.hl && st.hl.page && pdfVisible(st).includes(st.hl.page)) st.hl.scroll = true;
    pdfRender(() => pdfScrollToStart(st));
  }
  function pdfStep(dir) {
    const st = pdf;
    if (!st) return;
    const vis = pdfVisible(st);
    if (dir > 0) { if (vis[vis.length - 1] < st.pages) pdfShow(vis[vis.length - 1] + 1); }
    else if (vis[0] > 1) pdfShow(pdfFirstOf(st, vis[0] - 1));
  }
  /** After every render: progress, header, TOC, buttons, seek bar, time left, back-chip bookkeeping, saved position. */
  function pdfRelocated(st) {
    const vis = pdfVisible(st), first = vis[0], last = vis[vis.length - 1], N = st.pages, cfi = `pdf:${first}`;
    const pct = Math.round(last / N * 100);
    showProgress(pct);
    const pageLabel = vis.length > 1 ? `صفحهٔ ${faNum(first)}–${faNum(last)} از ${faNum(N)}` : `صفحهٔ ${faNum(first)} از ${faNum(N)}`;
    const entry = pdfOutlineFor(st, last), label = entry ? entry.label : pageLabel;
    $("chapterLabel").textContent = label; $("chapterLabel").title = label; $("readerChapter").textContent = label;
    $("tocList").querySelectorAll(".toc-link[data-page]").forEach((b, i) => {
      const on = !!entry && st.outline[i] === entry;
      b.classList.toggle("active", on);
      if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    });
    $("prevPage").disabled = first <= 1;
    $("nextPage").disabled = last >= N;
    if (!seekDragging) { $("bookSeek").value = String(N > 1 ? Math.round((last - 1) / (N - 1) * 1000) : 0); setSeekValueText(pct); }
    const changed = st.lastCfi !== cfi;
    st.lastCfi = cfi;
    if (!changed) return;
    if (st.shown) notePageTurn(); else { st.shown = true; lastTurnAt = Date.now(); }
    const min = Math.ceil(Math.max(0, N - last) * secPerPage / 60);
    $("timeLeft").textContent = last >= N ? `${pageLabel} · آخرین صفحه` : pageLabel;
    $("seekInfo").textContent = last >= N ? `٪${faNum(pct)} از کتاب · آخرین صفحهٔ کتاب` : `٪${faNum(pct)} از کتاب · حدود ${faNum(Math.max(1, min))} دقیقه تا پایان کتاب`;
    updateBookmarkButton();
    markActivity();
    onJumpRelocated({ start: { cfi } });
    const bk = currentBook;
    if (!bk || st.token !== openToken) return;
    if (last >= N && !bk.preview && bk.status !== "finished") {
      setBookMeta(bk.id, { status: "finished" }).then(r => { if (r && currentBook && currentBook.id === r.id) { currentBook = r; showToast("تبریک! این کتاب را تمام کردی."); } });
    }
    if (bk.preview) { previewPos = cfi; return; }
    saveProgress(bk.id, cfi, pct).catch(err => console.warn("Saving progress failed:", err));
  }
  // ---- Zoom: buttons, ctrl+wheel / trackpad pinch, touch pinch; double tap on a zoomed page resets ----
  function pdfClampZoom(z) { return Math.min(PDF_ZOOM_MAX, Math.max(PDF_ZOOM_MIN, Math.round(z * 100) / 100)); }
  /** anchor: {ox, oy} point in the (unzoomed) spread and {cx, cy} where it is on screen; it stays there after zooming. */
  function pdfSetZoom(z, anchor) {
    const st = pdf;
    if (!st) return;
    z = pdfClampZoom(z);
    if (z === st.zoom) { st.spreadEl.style.transform = ""; return; }
    if (!anchor) {
      const sr = st.stage.getBoundingClientRect(), r = st.spreadEl.getBoundingClientRect();
      const cx = sr.left + sr.width / 2, cy = sr.top + sr.height / 2;
      anchor = { ox: cx - r.left, oy: cy - r.top, cx, cy };
    }
    const f = z / st.zoom;
    st.zoom = z;
    syncSettingsUI();
    pdfRender(() => {
      const r = st.spreadEl.getBoundingClientRect();
      st.stage.scrollLeft += r.left + anchor.ox * f - anchor.cx;
      st.stage.scrollTop += r.top + anchor.oy * f - anchor.cy;
    });
  }
  function pdfWheelZoom(e) {
    const st = pdf;
    if (e.cancelable) e.preventDefault();
    if (!st || settingsOpen() || searchOpen()) return;
    if (!st.wheelZoom) {
      const r = st.spreadEl.getBoundingClientRect();
      st.wheelZoom = { target: st.zoom, anchor: { ox: e.clientX - r.left, oy: e.clientY - r.top, cx: e.clientX, cy: e.clientY } };
      st.spreadEl.style.transformOrigin = `${st.wheelZoom.anchor.ox}px ${st.wheelZoom.anchor.oy}px`;
    }
    const unit = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1;
    st.wheelZoom.target = pdfClampZoom(st.wheelZoom.target * Math.exp(-e.deltaY * unit * 0.0025));
    st.spreadEl.style.transform = `scale(${st.wheelZoom.target / st.zoom})`; // live preview, re-rendered when the wheel rests
    clearTimeout(st.wheelZoomTimer);
    st.wheelZoomTimer = setTimeout(() => { const w = st.wheelZoom; st.wheelZoom = null; if (pdf === st && w) pdfSetZoom(w.target, w.anchor); }, 180);
  }
  /** A page larger than the viewer scrolls with the wheel; only at its edge does a (new) wheel gesture turn the page. */
  function pdfWheelScrolls(e) {
    const s = pdf.stage, vertical = Math.abs(e.deltaY) >= Math.abs(e.deltaX);
    const canY = s.scrollHeight > s.clientHeight + 1, canX = s.scrollWidth > s.clientWidth + 1;
    const scrolls = vertical ? canY && (e.deltaY > 0 ? s.scrollTop + s.clientHeight < s.scrollHeight - 1 : s.scrollTop > 0) : canX;
    if (!scrolls) return false;
    clearTimeout(wheelState.quietTimer);
    wheelState.locked = true; wheelState.sum = 0; wheelState.sumX = 0;
    wheelState.quietTimer = setTimeout(() => { wheelState.locked = false; wheelState.sum = 0; wheelState.sumX = 0; }, 250);
    return true; // native scrolling
  }
  let pdfPinch = null, pdfPinchEndAt = 0, pdfTapTimer = null, pdfLastMiddle = 0;
  const touchGap = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY) || 1;
  function onPdfTouchStart(e) {
    markActivity();
    if (!pdf) return;
    if (e.touches && e.touches.length >= 2) {
      touchStart = null; // a pinch is never a tap or a swipe
      const r = pdf.spreadEl.getBoundingClientRect();
      const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2, cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      pdfPinch = { d: touchGap(e.touches), zoom: pdf.zoom, s: 1, anchor: { ox: cx - r.left, oy: cy - r.top, cx, cy } };
      pdf.spreadEl.style.transformOrigin = `${pdfPinch.anchor.ox}px ${pdfPinch.anchor.oy}px`;
      return;
    }
    if (Date.now() - pdfPinchEndAt < 350) { touchStart = null; return; }
    onTouchStart(e);
  }
  function onPdfTouchMove(e) {
    if (!pdfPinch || !pdf || !e.touches || e.touches.length < 2) return;
    if (e.cancelable) e.preventDefault();
    const z = pdfClampZoom(pdfPinch.zoom * touchGap(e.touches) / pdfPinch.d);
    pdfPinch.s = z / pdfPinch.zoom;
    pdf.spreadEl.style.transform = `scale(${pdfPinch.s})`;
  }
  function onPdfTouchEnd(e) {
    if (pdfPinch) {
      if (!e.touches || e.touches.length < 2) {
        const p = pdfPinch; pdfPinch = null; pdfPinchEndAt = Date.now(); touchStart = null;
        suppressClickUntil = Date.now() + 500;
        if (pdf) pdfSetZoom(p.zoom * p.s, p.anchor);
      }
      return;
    }
    if (Date.now() - pdfPinchEndAt < 350) return;
    // Second tap of a double tap in the middle of a zoomed page: reset the zoom. (onTouchEnd would swallow it as the
    // synthetic click that follows the first tap.)
    const t = e.changedTouches && e.changedTouches[0], box = $("viewer").getBoundingClientRect();
    if (pdf && pdf.zoom !== 1 && t && touchStart && Date.now() - pdfLastMiddle < 350 && box.width
        && Math.abs(t.clientX - touchStart.x) < 10 && Math.abs(t.clientY - touchStart.y) < 10) {
      const ratio = (t.clientX - box.left) / box.width;
      if (ratio >= EDGE_ZONE && ratio <= 1 - EDGE_ZONE) {
        touchStart = null; clearTimeout(pdfTapTimer); pdfLastMiddle = 0;
        suppressClickUntil = Date.now() + 700;
        pdfSetZoom(1);
        return;
      }
    }
    onTouchEnd(e, null);
  }
  /** Middle tap: immersive mode; on a zoomed page a second tap within 350 ms resets the zoom instead. */
  function pdfMiddleTap() {
    const toggle = () => setImmersive(!$("reader").classList.contains("immersive"));
    if (!pdf || pdf.zoom === 1) { toggle(); return; }
    const now = Date.now();
    if (now - pdfLastMiddle < 350) { clearTimeout(pdfTapTimer); pdfLastMiddle = 0; pdfSetZoom(1); return; }
    pdfLastMiddle = now;
    clearTimeout(pdfTapTimer);
    pdfTapTimer = setTimeout(toggle, 350);
  }
  // ---- Open / close ----
  async function pdfBuildOutline(st) {
    let raw = null;
    try { raw = await st.doc.getOutline(); } catch (_) {}
    const flat = [];
    const pageOf = async dest => {
      try {
        const d = typeof dest === "string" ? await st.doc.getDestination(dest) : dest;
        if (!Array.isArray(d) || !d.length) return 0;
        if (Number.isInteger(d[0])) return d[0] + 1;
        if (d[0] && typeof d[0] === "object") return (await st.doc.getPageIndex(d[0])) + 1;
      } catch (_) {}
      return 0;
    };
    // Nested entries up to 3 levels, like the EPUB TOC; entries without a page in this file (web links) are left out.
    const walk = async (items, depth) => {
      for (const it of items || []) {
        const page = await pageOf(it.dest);
        if (page) flat.push({ label: String(it.title || "").replace(/\s+/g, " ").trim() || `صفحهٔ ${faNum(page)}`, page, depth });
        if (depth < 2) await walk(it.items, depth + 1);
      }
    };
    await walk(raw, 0);
    return flat;
  }
  async function openPdfReader(record, token, savedP) {
    $("reader").classList.add("is-pdf");
    if (bookInstance) bookInstance = null;
    rendition = null;
    ttsStop();
    let lib, doc, task = null;
    try {
      lib = await loadPdfJsOrExplain("reader:load");
      if (token !== openToken) return;
      const data = new Uint8Array(await fileOf(record).arrayBuffer());
      if (token !== openToken) return;
      task = lib.getDocument(pdfOpenOptions(data));
      doc = await task.promise;
    } catch (err) {
      if (task) { try { task.destroy(); } catch (_) {} }
      if (token !== openToken) return;
      hideOpening();
      const known = err instanceof UserError ? err : pdfOpenError(err);
      if (!known) { console.error("PDF open failed:", err); reportError("pdf", err, "reader:open"); }
      showToast(known ? known.message : "این فایل PDF قابل خواندن نیست یا خراب است.");
      return;
    }
    if (token !== openToken) { try { task.destroy(); } catch (_) {} return; }
    $("viewer").innerHTML = '<div class="pdf-stage" dir="ltr"><div class="pdf-spread"></div></div>';
    const st = pdf = { lib, task, doc, token, pages: doc.numPages, page: 1, zoom: 1, seq: 0, scale: 0,
                       proxies: new Map(), canvases: new Map(), pageEls: new Map(), text: new Map(), outline: [], hl: null,
                       stage: $("viewer").querySelector(".pdf-stage"), spreadEl: $("viewer").querySelector(".pdf-spread"), lastCfi: null, shown: false };
    st.stage.addEventListener("touchstart", onPdfTouchStart, { passive: true });
    st.stage.addEventListener("touchmove", onPdfTouchMove, { passive: false });
    st.stage.addEventListener("touchend", onPdfTouchEnd);
    st.stage.addEventListener("click", e => { markActivity(); onReaderClick(e, null); });
    if (window.ResizeObserver) {
      let last = "";
      st.ro = new ResizeObserver(() => {
        const size = `${st.stage.clientWidth}x${st.stage.clientHeight}`;
        if (size === last) return;
        const first = !last; last = size;
        if (first) return;
        clearTimeout(st.resizeTimer);
        st.resizeTimer = setTimeout(() => { if (pdf === st) pdfRender(); }, 120);
      });
      st.ro.observe(st.stage);
    }
    bookDir = await resolveBookDir(record, () => detectPdfDirection(st));
    if (token !== openToken || pdf !== st) return;
    $("bookSeek").dir = bookIsRtl() ? "rtl" : "ltr";
    $("bookSeek").disabled = st.pages < 2;
    syncSettingsUI();
    lastTurnAt = 0; markActivity(); updateWakeLock();
    if (!record.preview && (!record.status || record.status === "want")) setBookMeta(record.id, { status: "reading" }).then(r => { if (r && currentBook && currentBook.id === r.id) currentBook = r; });
    const saved = await savedP;
    if (token !== openToken || pdf !== st) return;
    st.page = pdfTargetPage(saved && saved.cfi) || 1;
    await pdfRender(() => pdfScrollToStart(st));
    setTimeout(() => { if (token === openToken) hideOpening(); }, 1600); // safety net; normally lifted when page 1 is drawn
    if (token !== openToken || pdf !== st) return;
    maybeShowGuide();
    st.outline = await pdfBuildOutline(st);
    if (token !== openToken || pdf !== st) return;
    $("tocList").innerHTML = st.outline.length
      ? st.outline.map(e => `<button class="toc-link${e.depth ? " toc-sub toc-d" + e.depth : ""}" type="button" dir="auto" data-page="${e.page}">${esc(e.label)}</button>`).join("")
      : '<p class="muted">این PDF فهرست ندارد؛ با نوار پایین صفحه به هر صفحه برو.</p>';
    $("tocList").querySelectorAll("[data-page]").forEach(btn => btn.addEventListener("click", () => { closeToc(false); jumpTo(`pdf:${btn.dataset.page}`); }));
    st.lastCfi = null; pdfRelocated(st); // header and active TOC entry from the outline
    renderNotesList(); updateBookmarkButton();
    // Fill what an imported-elsewhere record may lack: page count and a cover (added fields only, one transaction).
    if (!record.preview && (record.pages !== st.pages || !record.cover)) {
      let cover = null;
      if (!record.cover) { try { cover = await pdfCoverDataUrl(doc); } catch (_) {} }
      if (pdf !== st) return;
      try {
        const os = db.transaction("books", "readwrite").objectStore("books"), g = os.get(record.id);
        g.onsuccess = () => { const cur = g.result; if (cur) os.put({ ...cur, pages: st.pages, ...(cover && !cur.cover ? { cover } : {}) }); };
      } catch (_) {}
    }
  }
  function closePdf() {
    const st = pdf;
    if (!st) return;
    pdf = null;
    st.seq++;
    clearTimeout(st.resizeTimer); clearTimeout(st.wheelZoomTimer); clearTimeout(pdfTapTimer);
    if (st.ro) { try { st.ro.disconnect(); } catch (_) {} }
    for (const e of st.canvases.values()) pdfReleaseCanvas(e);
    for (const pe of st.pageEls.values()) { if (pe.tl) { try { pe.tl.cancel(); } catch (_) {} } }
    st.canvases.clear(); st.pageEls.clear(); st.text.clear();
    try { st.task.destroy(); } catch (_) {} // ends the worker and frees the document
    pdfPinch = null;
    $("reader").classList.remove("is-pdf");
    if ($("viewer").querySelector(".pdf-stage")) $("viewer").innerHTML = "";
  }
  $("pdfZoomIn").addEventListener("click", () => pdfSetZoom((pdf ? pdf.zoom : 1) * PDF_ZOOM_STEP));
  $("pdfZoomOut").addEventListener("click", () => pdfSetZoom((pdf ? pdf.zoom : 1) / PDF_ZOOM_STEP));
  document.querySelectorAll("[data-pdffit]").forEach(b => b.addEventListener("click", () => {
    readerPrefs.pdfFit = b.dataset.pdffit; saveReaderPrefs();
    if (pdf) pdf.zoom = 1;
    syncSettingsUI();
    if (pdf) pdfRender(() => pdfScrollToStart(pdf));
  }));
  $("prefPdfInvert").addEventListener("change", e => { readerPrefs.pdfInvert = e.target.checked; saveReaderPrefs(); syncSettingsUI(); });

  // ---- Reader navigation (v6.1): keyboard, swipe, edge tap zones, TOC drawer, immersive mode ----
  const SWIPE_MIN_PX = 50, EDGE_ZONE = 0.25;
  let touchStart = null, suppressClickUntil = 0;
  function stripBookScripts(doc) {
    try {
      if (!doc || !doc.querySelectorAll) return;
      doc.querySelectorAll("script, iframe, object, embed").forEach(el => el.remove());
      doc.querySelectorAll("*").forEach(el => {
        for (const attr of Array.from(el.attributes)) {
          const n = attr.name.toLowerCase();
          if (n.startsWith("on")) el.removeAttribute(attr.name);
          else if ((n === "href" || n === "src" || n === "xlink:href" || n === "action") && /^\s*javascript:/i.test(attr.value)) el.removeAttribute(attr.name);
        }
      });
    } catch (_) {}
  }
  function bookIsRtl() { return bookDir === "rtl"; }
  // A book's declared page-progression-direction wins. Otherwise look at the actual text of the first chapters:
  // the language tag is unreliable (Persian books tagged "en" exist). Image-only books fall back to the tag, then RTL.
  const DIR_DETECT_V = 1; // bump when detectBookDirection() changes, so cached directions are re-detected
  async function detectBookDirection(book) {
    try {
      await book.opened;
      const meta = (book.package && book.package.metadata) || {};
      if (meta.direction === "rtl" || meta.direction === "ltr") return meta.direction;
      let rtl = 0, ltr = 0;
      for (const item of (book.spine.spineItems || []).slice(0, 8)) {
        try {
          const node = await item.load(book.load.bind(book)); // resolves to the section's <html> element
          const text = (node && (node.body || node.documentElement || node).textContent) || "";
          rtl += (text.match(/[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/g) || []).length;
          ltr += (text.match(/[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF]/g) || []).length;
        } catch (_) {} finally { try { item.unload(); } catch (_) {} }
        if (rtl + ltr > 5000) break;
      }
      if (rtl + ltr > 50) return rtl >= ltr ? "rtl" : "ltr";
      return !meta.language || /^(fa|ar|he|ur|ps|ku|ckb|yi|dv|sd|ug)/i.test(meta.language) ? "rtl" : "ltr";
    } catch (_) { return "rtl"; }
  }
  function goForward() { if (pdf) pdfStep(1); else if (rendition) rendition.next(); }
  function goBack() { if (pdf) pdfStep(-1); else if (rendition) rendition.prev(); }
  // Physical directions: in an RTL book the left side of the page is "forward".
  function goLeft() { if (bookIsRtl()) goForward(); else goBack(); }
  function goRight() { if (bookIsRtl()) goBack(); else goForward(); }
  function readerOpen() { return !$("reader").classList.contains("hidden"); }
  function openToc() {
    $("reader").classList.add("toc-open");
    $("tocToggle").setAttribute("aria-expanded", "true");
    const first = $("tocList").querySelector(".toc-link.active") || $("tocList").querySelector("button");
    scrollTocToActive();
    setTimeout(() => { scrollTocToActive(); (first || $("tocPanel")).focus({ preventScroll: true }); }, 50);
  }
  function closeToc(returnFocus) {
    if (!$("reader").classList.contains("toc-open")) return;
    $("reader").classList.remove("toc-open");
    $("tocToggle").setAttribute("aria-expanded", "false");
    if (returnFocus) $("tocToggle").focus({ preventScroll: true });
  }
  function setImmersive(on) {
    const reader = $("reader");
    if (reader.classList.contains("immersive") === on) return;
    reader.classList.toggle("immersive", on);
    placeBackChip();
    if (on) showToast("برای نمایش دوبارهٔ نوارها، وسط صفحه را لمس کن.");
    // The viewer changed size without a window resize, so re-paginate explicitly.
    requestAnimationFrame(() => { try { if (rendition) rendition.resize(); } catch (_) {} if (pdf) pdfRender(); });
  }
  function handleReaderKey(e) {
    if (!readerOpen() || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const tag = (e.target && e.target.tagName) || "";
    // The first-run guide is modal: only Escape (closes it) and Tab (kept on its button) do anything.
    if (guideOpen() && e.key !== "Escape") {
      if (e.key === "Tab") { e.preventDefault(); $("readerGuideOk").focus(); }
      return;
    }
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(tag)) return;
    if (tag === "BUTTON" && (e.key === " " || e.key === "Enter")) return;
    let handled = true;
    switch (e.key) {
      case "ArrowLeft": goLeft(); break;
      case "ArrowRight": goRight(); break;
      case "PageDown": goForward(); break;
      case "PageUp": goBack(); break;
      case " ": if (e.shiftKey) goBack(); else goForward(); break;
      case "Escape":
        // Innermost layer first; this also runs for keys pressed while focus is inside the book iframe.
        if (guideOpen()) closeGuide(true);
        else if (!$("imageViewer").hidden) closeImage();
        else if (!$("notePopup").hidden || !$("lookupBox").hidden) { $("notePopup").hidden = true; $("lookupBox").hidden = true; }
        else if (!$("selectionBar").hidden) hideSelectionBar();
        else if (searchOpen()) closeSearch(true);
        else if (settingsOpen()) closeSettings(true);
        else if ($("reader").classList.contains("toc-open")) closeToc(true);
        else if ($("reader").classList.contains("immersive")) setImmersive(false);
        else closeReader();
        break;
      default: handled = false;
    }
    // Keys from inside the book iframe arrive through epub.js as passive events; preventDefault is only valid here.
    const fromPage = e.target === document || (e.target && e.target.ownerDocument === document);
    if (handled && fromPage && e.cancelable) e.preventDefault();
  }
  function onTouchStart(e) {
    diag("touchstart");
    const t = e.changedTouches && e.changedTouches[0];
    touchStart = t ? { x: t.clientX, y: t.clientY, time: Date.now() } : null;
  }
  function onTouchEnd(e, contents) {
    diag("touchend");
    const t = e.changedTouches && e.changedTouches[0];
    diag("touchendHasPoint", !!t); diag("touchendHadStart", !!touchStart);
    if (!touchStart || !t) return;
    const dx = t.clientX - touchStart.x, dy = t.clientY - touchStart.y, dt = Date.now() - touchStart.time;
    touchStart = null;
    // iOS Safari never fires "click" on plain text inside the book iframe, so edge taps are detected from the touch itself.
    if (Math.abs(dx) < 10 && Math.abs(dy) < 10 && dt < 500) {
      if (Date.now() < suppressClickUntil) return;
      suppressClickUntil = Date.now() + 700; // swallow the synthetic click that other browsers send afterwards
      handleTap(e.target, t.clientX, e.view, contents);
      return;
    }
    if (Math.abs(dx) >= SWIPE_MIN_PX && Math.abs(dx) > Math.abs(dy) * 1.5 && dt < 800) {
      suppressClickUntil = Date.now() + 500;
      if (pdf && pdfCanPanX()) return; // a zoomed PDF page is being panned, not turned
      // Finger moving right turns to the next page in an RTL book, like a Persian paper book.
      if (dx > 0) goLeft(); else goRight();
    }
  }
  // Mouse wheel / trackpad turns pages in paginated mode. Deltas are summed (trackpads send dozens of tiny events)
  // and one gesture turns at most one page: after a turn, further wheel events are ignored until 250 ms of quiet.
  const wheelState = { sum: 0, sumX: 0, lockUntil: 0, quietTimer: null, locked: false };
  function onReaderWheel(e) {
    if (pdf && e.ctrlKey) { pdfWheelZoom(e); return; } // v8.8: ctrl+wheel (and trackpad pinch) zooms the PDF page
    if (readerPrefs.wheel === false || (!pdf && readerPrefs.flow === "scrolled") || (!rendition && !pdf) || e.ctrlKey) return;
    if (settingsOpen() || searchOpen() || $("reader").classList.contains("toc-open")) return;
    if (pdf && pdfWheelScrolls(e)) return; // a zoomed / fit-to-width page scrolls first, then turns at its edge
    if (e.cancelable) e.preventDefault();
    const unit = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1;
    clearTimeout(wheelState.quietTimer);
    wheelState.quietTimer = setTimeout(() => { wheelState.locked = false; wheelState.sum = 0; wheelState.sumX = 0; }, 250);
    if (wheelState.locked) return;
    wheelState.sum += e.deltaY * unit; wheelState.sumX += e.deltaX * unit;
    const ax = Math.abs(wheelState.sumX), ay = Math.abs(wheelState.sum);
    if (Math.max(ax, ay) < 40) return;
    wheelState.locked = true; diag("wheelTurn");
    if (ay >= ax) { if (wheelState.sum > 0) goForward(); else goBack(); }
    else { if (wheelState.sumX > 0) goRight(); else goLeft(); } // two-finger swipe: content follows the fingers
    wheelState.sum = 0; wheelState.sumX = 0;
  }
  function onReaderClick(e, contents) {
    diag("click");
    if (Date.now() < suppressClickUntil) return;
    handleTap(e.target, e.clientX, e.view, contents);
  }
  function handleTap(target, clientX, view, contents) {
    diag("tap"); diag("tapHasFrame", !!((view && view.frameElement) || (contents && contents.window && contents.window.frameElement)));
    if (settingsOpen() || searchOpen()) { closeSettings(false); closeSearch(false); return; }
    if (!$("selectionBar").hidden) { hideSelectionBar(); return; }
    if (!$("notePopup").hidden || !$("lookupBox").hidden) { $("notePopup").hidden = true; $("lookupBox").hidden = true; return; }
    const el = target && target.nodeType === 3 ? target.parentElement : target;
    if (el && el.closest && el.closest("a, button, input, select, textarea, audio, video, label, img")) return;
    try {
      const win = (contents && contents.window) || view;
      if (win && String(win.getSelection()).trim()) return;
    } catch (_) {}
    const frame = (view && view.frameElement) || (contents && contents.window && contents.window.frameElement);
    const box = $("viewer").getBoundingClientRect();
    if ((!frame && !pdf) || !box.width) return;
    // The PDF page is part of the main document (clientX is already a page coordinate); EPUB chapters live in an iframe.
    const ratio = ((pdf || !frame ? 0 : frame.getBoundingClientRect().left) + clientX - box.left) / box.width;
    const edges = readerPrefs.taps !== false && (!!pdf || readerPrefs.flow !== "scrolled");
    const middle = () => { if (pdf) pdfMiddleTap(); else setImmersive(!$("reader").classList.contains("immersive")); };
    if (!edges) middle();
    else if (ratio < EDGE_ZONE) goLeft();
    else if (ratio > 1 - EDGE_ZONE) goRight();
    else middle();
  }
  $("tocToggle").addEventListener("click", () => { if ($("reader").classList.contains("toc-open")) closeToc(true); else openToc(); });
  $("tocBackdrop").addEventListener("click", () => closeToc(true));
  document.addEventListener("keydown", handleReaderKey);
  // Wheel over the margins/bars around the book (inside the book the iframe listener in onBookContent handles it).
  $("viewer").addEventListener("wheel", onReaderWheel, { passive: false });
  document.addEventListener("keydown", () => { if (currentBook) markActivity(); });

  // ---- «برگشت به جای قبلی» (v8.2): a jump (not a page turn) remembers where the reader was ----
  const JUMP_STACK_MAX = 10, JUMP_HIDE_AFTER_TURNS = 5;
  // stack: CFIs to go back to (newest last). pending: a jump whose relocation has not settled yet.
  const jumps = { stack: [], pending: null, timer: 0, turns: 0, lastCfi: null };
  function currentStartCfi() {
    if (pdf) return `pdf:${pdf.page}`;
    try { const loc = rendition && rendition.currentLocation(); return (loc && loc.start && loc.start.cfi) || null; } catch (_) { return null; }
  }
  /** Remember the current page before a jump that epub.js (or jumpTo) is about to make. kind "back" = returning via the chip. */
  function noteJumpOrigin(kind) {
    if (!rendition && !pdf) return;
    clearTimeout(jumps.timer);
    jumps.pending = { from: currentStartCfi(), kind: kind || "jump" };
    jumps.timer = setTimeout(settleJump, 2500); // no relocation at all (link to the same page): settle anyway
  }
  /**
   * jumpTo(target): the one entry point for every non-page-turn navigation — TOC entries, search results,
   * notes/bookmarks list, footnote targets, and future callers such as a whole-book seek bar.
   * `target` is anything rendition.display() accepts (CFI or href). It records the current start CFI first so the
   * «↩ برگشت به صفحهٔ قبل» chip can return there, then returns rendition.display(target)'s promise.
   * Plain page turns keep using rendition.next()/prev(). Nothing is stored, so previews (preview-*) are safe.
   */
  function jumpTo(target) {
    if (!rendition && !pdf) return Promise.resolve();
    noteJumpOrigin("jump");
    return safeDisplay(target, "jumpTo");
  }
  // rendition.display() rejects ("No Section Found") for targets epub.js cannot resolve; never leave that unhandled.
  // The report carries where it came from and the (trimmed) target so the cause can be found.
  function safeDisplay(target, from) {
    if (pdf) { // v8.8: PDF targets are "pdf:<page>" (TOC, search, bookmarks, seek bar, back chip)
      const n = pdfTargetPage(target);
      if (n) pdfShow(n); else showToast("این بخش در کتاب پیدا نشد.");
      return Promise.resolve();
    }
    if (!rendition) return Promise.resolve();
    let p;
    try { p = Promise.resolve(rendition.display(target)); } catch (err) { p = Promise.reject(err); }
    return p.catch(err => {
      const t = String(target == null ? "" : target).slice(0, 160);
      reportError("nav", err, `${from}: ${t} (spine ${bookInstance && bookInstance.spine ? bookInstance.spine.spineItems.length : "?"})`);
      showToast("این بخش در کتاب پیدا نشد.");
    });
  }
  function settleJump() {
    clearTimeout(jumps.timer);
    const p = jumps.pending; jumps.pending = null;
    if (!p || (!rendition && !pdf)) return;
    const here = currentStartCfi();
    if (p.kind === "jump" && p.from && here && p.from !== here && jumps.stack[jumps.stack.length - 1] !== p.from) {
      jumps.stack.push(p.from);
      if (jumps.stack.length > JUMP_STACK_MAX) jumps.stack.shift();
    }
    jumps.turns = 0; jumps.lastCfi = here;
    updateBackChip();
  }
  function onJumpRelocated(location) {
    const cfi = location && location.start && location.start.cfi;
    if (!cfi) return;
    if (jumps.pending) { clearTimeout(jumps.timer); jumps.timer = setTimeout(settleJump, 400); return; } // wait for the jump to settle
    if (!jumps.stack.length || cfi === jumps.lastCfi) return;
    jumps.lastCfi = cfi;
    // After ~5 page turns away from the jump target the way back is no longer interesting.
    if (++jumps.turns >= JUMP_HIDE_AFTER_TURNS) clearJumps();
  }
  function clearJumps() {
    clearTimeout(jumps.timer);
    jumps.stack = []; jumps.pending = null; jumps.turns = 0; jumps.lastCfi = null;
    updateBackChip();
  }
  function updateBackChip() {
    const chip = $("backChip"); if (!chip) return;
    chip.hidden = jumps.stack.length === 0;
    if (!chip.hidden) placeBackChip();
  }
  // Keep the chip just above the bottom bar, whose height changes with width (the seek bar wraps on narrow screens).
  function placeBackChip() {
    const chip = $("backChip"), bar = document.querySelector(".reader-bottom"), host = chip && chip.offsetParent;
    if (!chip || chip.hidden || !host) return;
    const visibleBar = bar && bar.getBoundingClientRect().height > 0 && getComputedStyle(bar).display !== "none";
    const bottom = visibleBar ? host.getBoundingClientRect().bottom - bar.getBoundingClientRect().top + 8 : 16;
    chip.style.bottom = `${Math.max(8, Math.round(bottom))}px`;
  }
  window.addEventListener("resize", placeBackChip);
  $("backChip").addEventListener("click", () => {
    const cfi = jumps.stack.pop();
    updateBackChip();
    if (!cfi || (!rendition && !pdf)) return;
    noteJumpOrigin("back");
    safeDisplay(cfi, "backChip");
  });

  // ---- First-run gesture guide (v8.2): shown once per device, can be reopened from the settings panel ----
  const GUIDE_KEY = "borg-guide-seen" + STORAGE_SUFFIX;
  let guideReturnFocus = null;
  function guideOpen() { return !$("readerGuide").hidden; }
  function maybeShowGuide() {
    let seen = true;
    try { seen = localStorage.getItem(GUIDE_KEY) === "1"; } catch (_) {}
    if (!seen) showGuide(null);
  }
  function showGuide(returnFocus) {
    const rtl = bookIsRtl(), scrolled = !pdf && readerPrefs.flow === "scrolled", edges = readerPrefs.taps !== false && !scrolled;
    const nextSide = rtl ? "چپ" : "راست", prevSide = rtl ? "راست" : "چپ";
    const items = [];
    if (scrolled) {
      items.push(["scroll", "حالت پیمایشی: متن را با کشیدن انگشت یا چرخ ماوس بالا و پایین ببر؛ برای فصل بعد و قبل از دکمه‌های «بعدی» و «قبلی» استفاده کن."]);
      items.push(["middle", "لمس صفحه: حالت تمام‌صفحه (برای برگشتن نوارها دوباره لمس کن)."]);
    } else {
      if (edges) {
        items.push(["next", `لمس لبهٔ ${nextSide} صفحه: صفحهٔ بعد`]);
        items.push(["prev", `لمس لبهٔ ${prevSide} صفحه: صفحهٔ قبل`]);
        items.push(["middle", "لمس وسط صفحه: حالت تمام‌صفحه (برای برگشتن نوارها دوباره لمس کن)."]);
      } else {
        items.push(["edges-off", "ورق زدن با لمس لبه‌ها در تنظیمات خاموش است؛ با کشیدن انگشت یا دکمه‌های «قبلی» و «بعدی» ورق بزن."]);
        items.push(["middle", "لمس صفحه: حالت تمام‌صفحه (برای برگشتن نوارها دوباره لمس کن)."]);
      }
      items.push(["swipe", `روی گوشی و تبلت: کشیدن انگشت به ${prevSide} صفحهٔ بعد را می‌آورد و به ${nextSide} صفحهٔ قبل را.`]);
      items.push(["keys", `صفحه‌کلید: کلید ${rtl ? "←" : "→"} صفحهٔ بعد و ${rtl ? "→" : "←"} صفحهٔ قبل.`]);
    }
    if (pdf) {
      items.push(["zoom", "بزرگ‌نمایی: با دو انگشت (یا Ctrl و چرخ موس)؛ دو بار لمس صفحهٔ بزرگ‌شده، اندازه را برمی‌گرداند."]);
      items.push(["settings", "دکمهٔ «Aa» بالای صفحه: بزرگ‌نمایی، اندازهٔ صفحه یا عرض صفحه، و رنگ صفحه."]);
    } else items.push(["settings", "دکمهٔ «Aa» بالای صفحه: فونت، اندازهٔ متن، رنگ صفحه و نحوهٔ نمایش."]);
    $("readerGuideList").innerHTML = items.map(([k, t]) => `<li data-guide="${k}">${esc(t)}</li>`).join("");
    const left = $("readerGuideLeft"), right = $("readerGuideRight");
    left.hidden = right.hidden = !edges;
    left.textContent = rtl ? "← صفحهٔ بعد" : "← صفحهٔ قبل";
    right.textContent = rtl ? "صفحهٔ قبل →" : "صفحهٔ بعد →";
    guideReturnFocus = returnFocus || null;
    $("readerGuide").hidden = false;
    setTimeout(() => { if (guideOpen()) $("readerGuideOk").focus({ preventScroll: true }); }, 30);
  }
  /** remember=true: the reader dismissed it, so it is not shown automatically again on this device. */
  function closeGuide(remember) {
    if (!guideOpen()) return;
    $("readerGuide").hidden = true;
    if (remember) { try { localStorage.setItem(GUIDE_KEY, "1"); } catch (_) {} }
    const back = guideReturnFocus; guideReturnFocus = null;
    if (back && remember) back.focus({ preventScroll: true });
  }
  $("readerGuideOk").addEventListener("click", () => closeGuide(true));
  $("showGuide").addEventListener("click", () => { closeSettings(false); showGuide($("settingsToggle")); });

  // ======================= Reading features (v8) =======================
  function reopenCurrentBook() {
    if (currentBook && pdf) {
      const rec = currentBook, at = `pdf:${pdf.page}`;
      if (rec.preview) { previewPos = at; openReader(null, rec).catch(err => console.warn("Reopen failed:", err)); return; }
      saveProgress(rec.id, at, pdfPercent(pdf)).then(() => openReader(rec.id)).catch(err => console.warn("Reopen failed:", err));
      return;
    }
    if (!currentBook || !rendition) return;
    if (currentBook.preview) {
      const rec = currentBook, loc = rendition.currentLocation();
      if (loc?.start?.cfi) previewPos = loc.start.cfi;
      openReader(null, rec).catch(err => console.warn("Reopen failed:", err));
      return;
    }
    const id = currentBook.id, loc = rendition.currentLocation();
    const done = loc?.start?.cfi ? saveProgress(id, loc.start.cfi, percentFor(loc.start.cfi, loc.start.percentage)) : Promise.resolve();
    done.then(() => openReader(id)).catch(err => console.warn("Reopen failed:", err));
  }
  // «جهت این کتاب»: per-book override of the detected reading direction ("auto" | "rtl" | "ltr"), then reopen.
  function syncBookDirUI() {
    const v = (currentBook && currentBook.dirOverride) || "auto";
    document.querySelectorAll("[data-bookdir]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.bookdir === v)));
  }
  function setBookDir(value) {
    if (!currentBook || (!rendition && !pdf)) return;
    const v = value === "rtl" || value === "ltr" ? value : "auto";
    if ((currentBook.dirOverride || "auto") === v) return;
    currentBook.dirOverride = v; syncBookDirUI();
    if (currentBook.preview) { reopenCurrentBook(); return; }
    const id = currentBook.id;
    try {
      const tx = db.transaction("books", "readwrite"), os = tx.objectStore("books"), g = os.get(id);
      g.onsuccess = () => { if (g.result) os.put({ ...g.result, dirOverride: v }); };
      tx.oncomplete = () => reopenCurrentBook();
      tx.onerror = () => showToast("ذخیرهٔ جهت کتاب انجام نشد.");
    } catch (err) { console.warn("Saving the book direction failed:", err); }
  }
  document.querySelectorAll("[data-bookdir]").forEach(b => b.addEventListener("click", () => setBookDir(b.dataset.bookdir)));
  function setPref(key, value, how) {
    readerPrefs[key] = value; saveReaderPrefs(); syncSettingsUI();
    if (how === "styles") applyReaderStyles();
    if (how === "resize") requestAnimationFrame(() => { try { rendition && rendition.resize(); } catch (_) {} });
    if (how === "reopen" && pdf && key === "spread") { pdfRender(); return; } // a PDF only re-lays out its pages
    if (how === "reopen") reopenCurrentBook();
  }
  document.querySelectorAll("[data-margin]").forEach(b => b.addEventListener("click", () => setPref("margin", b.dataset.margin, "resize")));
  document.querySelectorAll("[data-para]").forEach(b => b.addEventListener("click", () => setPref("para", b.dataset.para, "styles")));
  document.querySelectorAll("[data-flow]").forEach(b => b.addEventListener("click", () => { if (readerPrefs.flow !== b.dataset.flow) setPref("flow", b.dataset.flow, "reopen"); }));
  document.querySelectorAll("[data-spread]").forEach(b => b.addEventListener("click", () => { if (readerPrefs.spread !== b.dataset.spread) setPref("spread", b.dataset.spread, "reopen"); }));
  $("prefDigits").addEventListener("change", e => setPref("digits", e.target.checked, "reopen"));
  $("prefTaps").addEventListener("change", e => setPref("taps", e.target.checked));
  $("prefWheel").addEventListener("change", e => setPref("wheel", e.target.checked));
  $("prefWake").addEventListener("change", e => { setPref("wake", e.target.checked); updateWakeLock(); });
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener("change", () => { if (readerPrefs.theme === "auto") applyReaderStyles(); });

  // ---- Keep the screen on while reading ----
  let wakeLock = null;
  async function updateWakeLock() {
    const want = readerPrefs.wake !== false && !!currentBook && document.visibilityState === "visible";
    try {
      if (want && !wakeLock && navigator.wakeLock) {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", () => { wakeLock = null; });
      } else if (!want && wakeLock) { const w = wakeLock; wakeLock = null; await w.release(); }
    } catch (_) { wakeLock = null; }
  }
  document.addEventListener("visibilitychange", updateWakeLock);

  // ---- Content hook: Persian digits, footnote popups, image viewer ----
  const OPS_NS = "http://www.idpf.org/2007/ops";
  function epubType(el) { return ((el.getAttributeNS && el.getAttributeNS(OPS_NS, "type")) || el.getAttribute("epub:type") || "") + " " + (el.getAttribute("role") || ""); }
  function isNoteRef(a) {
    if (/noteref/.test(epubType(a))) return true;
    const href = a.getAttribute("href") || "";
    if (!href.includes("#")) return false;
    if (/#(fn|footnote|note|endnote|ftn|en)[-_]?\d/i.test(href)) return true;
    const label = a.textContent.trim();
    return /^[\[(]?[\d۰-۹٠-٩*†]{1,4}[\])]?$/.test(label) && !!(a.closest("sup") || a.querySelector("sup") || /note|fn/i.test(a.className));
  }
  function toPersianDigits(doc) {
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, {
      acceptNode: n => n.parentElement && /^(SCRIPT|STYLE|CODE|PRE|KBD|SAMP)$/.test(n.parentElement.tagName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    const nodes = [];
    while (walker.nextNode()) if (/\d/.test(walker.currentNode.nodeValue)) nodes.push(walker.currentNode);
    for (const n of nodes) n.nodeValue = n.nodeValue.replace(/\d/g, d => "۰۱۲۳۴۵۶۷۸۹"[d]);
  }
  let noteTarget = null;
  async function showFootnote(contents, a) {
    const href = a.getAttribute("href") || "";
    const [path, id] = href.split("#");
    if (!id) return false;
    let doc = contents.document, sectionHref = null;
    if (path) {
      const here = bookInstance.spine.get(contents.sectionIndex);
      const resolved = decodeURIComponent(new URL(path, "https://book.invalid/" + (here ? here.href : "")).pathname.slice(1));
      const target = bookInstance.spine.get(resolved);
      if (!target) return false;
      await target.load(bookInstance.load.bind(bookInstance));
      doc = target.document; sectionHref = target.href;
    }
    let el = doc && (doc.getElementById(id) || doc.querySelector(`[name="${CSS.escape(id)}"]`));
    if (!el) return false;
    if (el.tagName === "A" || el.tagName === "SPAN" || el.tagName === "SUP") el = el.closest("aside, li, p, div, section") || el;
    const text = el.textContent.replace(/[↩↑⮐]/g, "").replace(/\s+/g, " ").trim();
    if (!text) return false;
    $("notePopupText").textContent = text.length > 2000 ? text.slice(0, 2000) + " …" : text;
    noteTarget = (sectionHref || (bookInstance.spine.get(contents.sectionIndex) || {}).href || "") + "#" + id;
    $("lookupBox").hidden = true;
    $("notePopup").hidden = false;
    return true;
  }
  $("notePopupClose").addEventListener("click", () => { $("notePopup").hidden = true; });
  $("notePopupGo").addEventListener("click", () => { $("notePopup").hidden = true; if (rendition && noteTarget) jumpTo(noteTarget); });
  function openImage(src) {
    if (!src) return;
    $("imageViewerImg").src = src;
    $("imageViewerImg").classList.remove("zoomed");
    $("imageViewer").hidden = false;
  }
  function closeImage() { $("imageViewer").hidden = true; $("imageViewerImg").removeAttribute("src"); }
  $("imageViewerClose").addEventListener("click", closeImage);
  $("imageViewer").addEventListener("click", e => {
    if (e.target === $("imageViewerImg")) $("imageViewerImg").classList.toggle("zoomed");
    else if (e.target.closest(".image-viewer-scroll") === null || e.target.classList.contains("image-viewer-scroll")) closeImage();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("imageViewer").hidden) { e.stopImmediatePropagation(); closeImage(); } }, true);
  // Many Persian EPUBs (old keyboards / converters) use RLM (U+200F) where a ZWNJ half-space was meant.
  // RLM does not stop letters from joining, so "صیقل‌یافته" renders as "صیقلیافته". Same length, so CFIs stay valid.
  const AR_LETTER = "[\u0620-\u064A\u066E-\u06D3\u06D5\u06EE\u06EF\u06FA-\u06FC\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFC]";
  const RLM_AS_ZWNJ = new RegExp(`(${AR_LETTER})\u200F(?=${AR_LETTER}|$)`, "g");
  function eachTextNode(doc, fn) {
    const w = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT, {
      acceptNode: n => n.parentElement && /^(SCRIPT|STYLE)$/.test(n.parentElement.tagName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    const nodes = [];
    while (w.nextNode()) nodes.push(w.currentNode);
    for (const n of nodes) { const v = fn(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v; }
  }
  function fixHalfSpaces(doc) { eachTextNode(doc, v => v.indexOf("\u200F") < 0 ? v : v.replace(RLM_AS_ZWNJ, "$1\u200C")); }
  /** For search only: one spelling for ی/ي/ى, ک/ك and half-spaces (all single characters, so offsets are unchanged). */
  function normalizeForSearch(t) { return t.replace(/[يى]/g, "ی").replace(/ك/g, "ک").replace(RLM_AS_ZWNJ, "$1\u200C"); }
  // v8.6: an image that is the only content of its block (cover pages, figures) is centred as a block (theme rule
  // img.borg-blockimg); book CSS like `body{text-indent:2em}` pushed such images off-centre. Images inside running
  // text stay inline. Only a class is added, so CFIs are unchanged.
  const INLINE_WRAP = /^(A|SPAN|B|I|EM|STRONG|FONT|SMALL|BIG|U|SUP|SUB)$/;
  function markBlockImages(doc) {
    doc.querySelectorAll("img").forEach(img => {
      let block = img.parentElement;
      while (block && INLINE_WRAP.test(block.tagName.toUpperCase()) && block.parentElement && block.parentElement !== doc.documentElement) block = block.parentElement;
      if (!block || block.querySelectorAll("img").length !== 1 || block.textContent.replace(/[\s\u200b-\u200f\u00a0]/g, "") !== "") return;
      img.classList.add("borg-blockimg");
    });
  }
  // Resolve a link inside a chapter against that chapter's own path to a spine item; null when not in the book.
  function resolveBookLink(contents, href) {
    const raw = String(href || "").trim(), hash = raw.indexOf("#");
    const file = hash >= 0 ? raw.slice(0, hash) : raw, frag = hash >= 0 ? raw.slice(hash) : "";
    const items = (bookInstance && bookInstance.spine && bookInstance.spine.spineItems) || [];
    let here = null;
    try { here = bookInstance.spine.get(contents.sectionIndex); } catch (_) {}
    if (!file) return here ? here.href + frag : null;
    const base = here ? String(here.href) : "";
    const want = normHref(base.slice(0, base.lastIndexOf("/") + 1) + file);
    let item = items.find(it => normHref(it.href) === want) || items.find(it => sameFile(normHref(it.href), want));
    if (!item) { try { item = bookInstance.spine.get(file); } catch (_) {} }
    return item ? item.href + frag : null;
  }
  function followBookLink(contents, href) {
    const raw = String(href || "").trim();
    if (!raw || /^javascript:/i.test(raw)) return;
    if (/^(https?:|mailto:|tel:)/i.test(raw) || /^\/\//.test(raw)) {
      try { window.open(raw, "_blank", "noopener,noreferrer"); } catch (_) {}
      return;
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) return; // other schemes: ignore
    const target = resolveBookLink(contents, raw);
    if (target) jumpTo(target);
    else showToast("این پیوند به بخشی اشاره می‌کند که در این کتاب پیدا نشد.");
  }
  function onBookContent(contents) {
    const doc = contents.document;
    if (!doc || !doc.body) return;
    try { fixHalfSpaces(doc); } catch (_) {}
    try { markBlockImages(doc); } catch (_) {}
    if (readerPrefs.digits) { try { toPersianDigits(doc); } catch (_) {} }
    doc.addEventListener("wheel", onReaderWheel, { passive: false });
    // Capture phase: runs before epub.js' own link handling, so footnotes open in place instead of jumping.
    doc.addEventListener("click", e => {
      const a = e.target.closest && e.target.closest("a[href]");
      if (a && isNoteRef(a)) {
        e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
        showFootnote(contents, a).then(ok => { if (!ok && rendition) jumpTo(a.getAttribute("href")); }).catch(() => {});
        return;
      }
      // Every other link is handled here, never by the browser: a missed epub.js handler used to let the iframe
      // navigate to e.g. https://parsun.com/feed_4/… (outside the reader). Internal → chapter, external → new tab.
      if (a) {
        e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
        followBookLink(contents, a.getAttribute("href") || "");
        return;
      }
      const img = !a && e.target.closest && e.target.closest("img, image");
      if (img) {
        e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
        openImage(img.currentSrc || img.src || (img.getAttribute && (img.getAttribute("href") || img.getAttributeNS("http://www.w3.org/1999/xlink", "href"))));
      }
    }, true);
  }

  // ---- Word lookup ----
  function showLookup(text) {
    const word = String(text || "").replace(/\s+/g, " ").trim().slice(0, 80);
    if (!word) return;
    const q = encodeURIComponent(word);
    const latin = /[A-Za-z]/.test(word) && !/[؀-ۿ]/.test(word);
    const links = [
      ...(latin ? [["ترجمهٔ گوگل", `https://translate.google.com/?sl=auto&tl=fa&text=${q}`]] : []),
      ["آبادیس", `https://abadis.ir/fatofa/${q}/`],
      ["واژه‌یاب", `https://vajehyab.com/?q=${q}`],
      ["ویکی‌واژه", `https://fa.wiktionary.org/wiki/${q}`],
      ...(latin ? [] : [["ترجمهٔ گوگل", `https://translate.google.com/?sl=fa&tl=en&text=${q}`]]),
    ];
    $("lookupWord").textContent = word;
    $("lookupLinks").innerHTML = links.map(([l, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(l)}</a>`).join("");
    $("notePopup").hidden = true;
    $("lookupBox").hidden = false;
  }
  $("lookupClose").addEventListener("click", () => { $("lookupBox").hidden = true; });

  // ---- Read aloud (Web Speech API) ----
  const tts = { active: false, paused: false, queue: [], idx: 0, contents: null, utter: null, hlCfi: null, selfNav: false, awaitIndex: null };
  const ttsSupported = () => "speechSynthesis" in window && typeof window.SpeechSynthesisUtterance === "function";
  function ttsVoice() {
    const vs = ttsSupported() ? window.speechSynthesis.getVoices() : [];
    return vs.find(v => v.voiceURI === readerPrefs.ttsVoice) || vs.find(v => /^fa/i.test(v.lang)) || null;
  }
  function fillVoices() {
    if (!ttsSupported()) return;
    const vs = window.speechSynthesis.getVoices();
    const cur = ttsVoice();
    const sorted = [...vs].sort((a, b) => (/^fa/i.test(b.lang) - /^fa/i.test(a.lang)) || a.name.localeCompare(b.name));
    $("ttsVoice").innerHTML = sorted.length ? sorted.map(v => `<option value="${esc(v.voiceURI)}" ${cur && cur.voiceURI === v.voiceURI ? "selected" : ""}>${/^fa/i.test(v.lang) ? "🇮🇷 " : ""}${esc(v.name)} (${esc(v.lang)})</option>`).join("")
      : '<option value="">صدای پیش‌فرض</option>';
  }
  if (ttsSupported()) { try { window.speechSynthesis.addEventListener("voiceschanged", fillVoices); } catch (_) {} }
  function textNodesOf(el) {
    const w = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT), out = [];
    while (w.nextNode()) out.push(w.currentNode);
    return out;
  }
  function blockVisible(el, contents) {
    const frame = contents.window.frameElement, box = $("viewer").getBoundingClientRect();
    if (!frame) return true;
    const fr = frame.getBoundingClientRect(), r = el.getBoundingClientRect();
    const x = fr.left + r.left, y = fr.top + r.top;
    return x < box.right - 2 && x + r.width > box.left + 2 && y < box.bottom - 2 && y + r.height > box.top + 2;
  }
  function buildQueue(contents, fromVisible) {
    const doc = contents.document;
    const blocks = [...doc.body.querySelectorAll("h1,h2,h3,h4,h5,h6,p,li,blockquote,dd,dt,figcaption,td")]
      .filter(el => el.textContent.trim() && !el.querySelector("p,li,blockquote"));
    let start = 0;
    if (fromVisible) { const i = blocks.findIndex(el => blockVisible(el, contents)); start = i < 0 ? 0 : i; }
    const queue = [];
    for (const el of blocks.slice(start)) {
      const nodes = textNodesOf(el);
      const full = nodes.map(n => n.nodeValue).join("");
      const re = /[^.!?؟…;؛\n]+[.!?؟…;؛]*/g;
      let m;
      while ((m = re.exec(full))) {
        const text = m[0].trim();
        if (text.replace(/[\s\d۰-۹.,،:]/g, "").length < 1) continue;
        queue.push({ el, nodes, start: m.index, end: m.index + m[0].length, text });
      }
    }
    return queue;
  }
  function rangeFor(item, doc) {
    const range = doc.createRange();
    let pos = 0, startSet = false;
    for (const n of item.nodes) {
      const len = n.nodeValue.length;
      if (!startSet && item.start <= pos + len) { range.setStart(n, Math.max(0, item.start - pos)); startSet = true; }
      if (startSet && item.end <= pos + len) { range.setEnd(n, Math.max(0, item.end - pos)); return range; }
      pos += len;
    }
    const last = item.nodes[item.nodes.length - 1];
    if (last) range.setEnd(last, last.nodeValue.length);
    return range;
  }
  function ttsClearHighlight() {
    if (tts.hlCfi && rendition) { try { rendition.annotations.remove(tts.hlCfi, "highlight"); } catch (_) {} }
    tts.hlCfi = null;
  }
  function ttsUi() {
    $("ttsBar").hidden = !tts.active;
    $("ttsToggle").setAttribute("aria-pressed", String(tts.active));
    $("ttsPlay").textContent = tts.paused ? "▶" : "⏸";
    $("ttsPlay").setAttribute("aria-label", tts.paused ? "ادامه" : "مکث");
  }
  function contentsForIndex(index) {
    const all = rendition ? rendition.getContents() : [];
    return all.find(c => c.sectionIndex === index) || all[0] || null;
  }
  function speakNext() {
    if (!tts.active || tts.paused || !rendition) return;
    if (tts.idx >= tts.queue.length) {
      const cur = tts.contents ? tts.contents.sectionIndex : null;
      const next = cur != null ? bookInstance.spine.get(cur + 1) : null;
      if (!next) { ttsStop(); showToast("خواندن کتاب تمام شد."); return; }
      tts.awaitIndex = next.index; tts.selfNav = true;
      safeDisplay(next.href, "tts-next");
      return;
    }
    const item = tts.queue[tts.idx];
    const doc = tts.contents.document;
    try {
      const range = rangeFor(item, doc);
      const cfi = tts.contents.cfiFromRange(range);
      ttsClearHighlight();
      rendition.annotations.highlight(cfi, {}, null, "tts-hl", { fill: "#7fc8a9", "fill-opacity": "0.35", "mix-blend-mode": "multiply" });
      tts.hlCfi = cfi;
      if (!blockVisible(item.el, tts.contents)) { tts.selfNav = true; safeDisplay(cfi, "tts-follow"); }
    } catch (_) {}
    const u = new window.SpeechSynthesisUtterance(item.text);
    const v = ttsVoice();
    if (v) { u.voice = v; u.lang = v.lang; } else u.lang = "fa-IR";
    u.rate = Number(readerPrefs.ttsRate) || 1;
    u.onend = () => { if (tts.utter !== u) return; tts.idx++; speakNext(); };
    u.onerror = e => { if (tts.utter !== u || (e && (e.error === "interrupted" || e.error === "canceled"))) return; tts.idx++; speakNext(); };
    tts.utter = u;
    markActivity();
    window.speechSynthesis.speak(u);
  }
  function ttsStartFromVisible() {
    const loc = rendition && rendition.currentLocation();
    const contents = contentsForIndex(loc?.start?.index);
    if (!contents) return;
    tts.contents = contents;
    tts.queue = buildQueue(contents, true);
    tts.idx = 0;
    speakNext();
  }
  function ttsStart() {
    if (!ttsSupported()) { showToast("مرورگر این دستگاه خواندن با صدا را پشتیبانی نمی‌کند."); return; }
    if (!rendition) return;
    fillVoices();
    if (!ttsVoice()) showToast("صدای فارسی روی این دستگاه پیدا نشد؛ از فهرست صداها یکی را انتخاب کن.");
    tts.active = true; tts.paused = false;
    ttsUi();
    ttsStartFromVisible();
  }
  function ttsStop() {
    const was = tts.active;
    tts.active = false; tts.paused = false; tts.utter = null; tts.queue = []; tts.awaitIndex = null;
    if (ttsSupported() && was) { try { window.speechSynthesis.cancel(); } catch (_) {} }
    ttsClearHighlight();
    ttsUi();
  }
  function ttsPauseToggle() {
    if (!tts.active) return;
    if (tts.paused) { tts.paused = false; ttsUi(); speakNext(); }
    else { tts.paused = true; tts.utter = null; try { window.speechSynthesis.cancel(); } catch (_) {} ttsUi(); }
  }
  function onTtsRelocated(location) {
    if (!tts.active) return;
    if (tts.awaitIndex != null) {
      if (location?.start?.index !== tts.awaitIndex) return;
      tts.awaitIndex = null; tts.selfNav = false;
      tts.contents = contentsForIndex(location.start.index);
      tts.queue = tts.contents ? buildQueue(tts.contents, false) : [];
      tts.idx = 0;
      setTimeout(speakNext, 50);
      return;
    }
    if (tts.selfNav) { tts.selfNav = false; return; }
    // The reader moved by hand: continue reading from the new page.
    tts.utter = null;
    try { window.speechSynthesis.cancel(); } catch (_) {}
    if (!tts.paused) setTimeout(ttsStartFromVisible, 80);
  }
  $("ttsToggle").addEventListener("click", () => { if (tts.active) ttsStop(); else ttsStart(); });
  $("ttsStop").addEventListener("click", ttsStop);
  $("ttsPlay").addEventListener("click", ttsPauseToggle);
  $("ttsRate").value = String(readerPrefs.ttsRate || 1);
  $("ttsRate").addEventListener("change", e => { readerPrefs.ttsRate = Number(e.target.value) || 1; saveReaderPrefs(); });
  $("ttsVoice").addEventListener("change", e => { readerPrefs.ttsVoice = e.target.value; saveReaderPrefs(); });

  // ---- In-book search (v7.1) ----
  function searchOpen() { return !$("searchPanel").hidden; }
  function openSearch() {
    closeSettings(false);
    $("searchPanel").hidden = false;
    $("searchToggle").setAttribute("aria-expanded", "true");
    setTimeout(() => $("bookSearchInput").focus(), 30);
  }
  function closeSearch(returnFocus) {
    if (!searchOpen()) return;
    $("searchPanel").hidden = true;
    $("searchToggle").setAttribute("aria-expanded", "false");
    if (returnFocus) $("searchToggle").focus({ preventScroll: true });
  }
  // Many Persian EPUBs use Arabic code points for ی and ک; search both spellings.
  function searchVariants(q) {
    const arabic = q.replace(/ی/g, "ي").replace(/ک/g, "ك");
    const persian = q.replace(/ي/g, "ی").replace(/ك/g, "ک");
    return [...new Set([q, arabic, persian])];
  }
  const escapeRegExp = t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  async function searchBook(query) {
    if (pdf) return pdfSearch(pdf, query);
    const book = bookInstance;
    if (!book || !query) return [];
    const results = [], seen = new Set();
    for (const item of book.spine.spineItems) {
      if (results.length >= 100) break;
      try {
        await item.load(book.load.bind(book));
        try { if (item.document && item.document.body) eachTextNode(item.document, normalizeForSearch); } catch (_) {}
        for (const v of searchVariants(normalizeForSearch(query))) {
          for (const r of item.find(v)) { if (!seen.has(r.cfi)) { seen.add(r.cfi); results.push({ ...r, index: item.index }); } }
        }
      } catch (_) {} finally { try { item.unload(); } catch (_) {} }
    }
    return results.slice(0, 100);
  }
  $("searchToggle").addEventListener("click", () => { if (searchOpen()) closeSearch(true); else openSearch(); });
  $("bookSearchForm").addEventListener("submit", async e => {
    e.preventDefault();
    const q = $("bookSearchInput").value.trim();
    if (q.length < 2) { $("bookSearchStatus").textContent = "حداقل دو حرف بنویس."; return; }
    $("bookSearchStatus").textContent = "در حال جست‌وجو…";
    $("bookSearchResults").innerHTML = "";
    const token = openToken;
    const results = await searchBook(q);
    if (token !== openToken) return;
    $("bookSearchStatus").textContent = results.length ? `${results.length.toLocaleString("fa-IR")} نتیجه${results.length >= 100 ? " (۱۰۰ مورد اول)" : ""}` : "چیزی پیدا نشد.";
    $("bookSearchResults").dataset.q = q;
    if (pdf) { // v8.8: PDF results carry the page and the match itself (it may be in visual order, see pdfSearch)
      $("bookSearchResults").innerHTML = results.map(r => `<button type="button" data-cfi="pdf:${r.page}"><small>صفحهٔ ${faNum(r.page)}</small><span dir="auto">${esc(r.before)}<mark>${esc(r.match)}</mark>${esc(r.after)}</span></button>`).join("");
      return;
    }
    const pattern = new RegExp(searchVariants(q).map(v => escapeRegExp(esc(v))).join("|"), "g");
    $("bookSearchResults").innerHTML = results.map(r => `<button type="button" data-cfi="${esc(r.cfi)}"><small>بخش ${(r.index + 1).toLocaleString("fa-IR")}</small>${esc(r.excerpt).replace(pattern, m => `<mark>${m}</mark>`)}</button>`).join("");
  });
  $("bookSearchResults").addEventListener("click", e => {
    const btn = e.target.closest("[data-cfi]");
    if (!btn || (!rendition && !pdf)) return;
    closeSearch(false);
    if (pdf) pdf.hl = { page: pdfTargetPage(btn.dataset.cfi), q: $("bookSearchResults").dataset.q || "" }; // marked in the text layer
    jumpTo(btn.dataset.cfi);
  });
  document.addEventListener("click", e => {
    if (searchOpen() && !$("searchPanel").contains(e.target) && !$("searchToggle").contains(e.target)) closeSearch(false);
  });

  function settingsOpen() { return !$("settingsPanel").hidden; }
  function openSettings() {
    closeSearch(false);
    $("settingsPanel").hidden = false;
    $("settingsToggle").setAttribute("aria-expanded", "true");
    syncSettingsUI();
  }
  function closeSettings(returnFocus) {
    if (!settingsOpen()) return;
    $("settingsPanel").hidden = true;
    $("settingsToggle").setAttribute("aria-expanded", "false");
    if (returnFocus) $("settingsToggle").focus({ preventScroll: true });
  }
  $("settingsToggle").addEventListener("click", () => { if (settingsOpen()) closeSettings(true); else openSettings(); });
  $("settingsClose").addEventListener("click", () => closeSettings(true));
  document.addEventListener("click", e => {
    if (settingsOpen() && !$("settingsPanel").contains(e.target) && !$("settingsToggle").contains(e.target)) closeSettings(false);
  });
  document.querySelectorAll("[data-theme]").forEach(b => b.addEventListener("click", () => { readerPrefs.theme = b.dataset.theme; saveReaderPrefs(); applyReaderStyles(); }));
  document.querySelectorAll("[data-align]").forEach(b => b.addEventListener("click", () => { readerPrefs.align = b.dataset.align; saveReaderPrefs(); applyReaderStyles(); }));
  $("librarySort").value = readerPrefs.librarySort;
  $("librarySort").addEventListener("change", e => { readerPrefs.librarySort = e.target.value; saveReaderPrefs(); renderLibrary(); });
  $("themeToggle").addEventListener("click", () => {
    document.body.classList.toggle("dark");
    localStorage.setItem(THEME_KEY, document.body.classList.contains("dark") ? "dark" : "light");
  });
  $("readerFont").value = readerPrefs.font;
  $("readerLineHeight").value = readerPrefs.lineHeight;
  $("readerFont").addEventListener("change", e => {
    readerPrefs.font = e.target.value;
    saveReaderPrefs();
    applyReaderStyles();
  });
  $("readerLineHeight").addEventListener("change", e => {
    readerPrefs.lineHeight = e.target.value;
    saveReaderPrefs();
    applyReaderStyles();
  });
  function setFontSize(next) {
    fontSize = Math.min(180, Math.max(70, next));
    readerPrefs.fontSize = fontSize;
    saveReaderPrefs();
    if (rendition) rendition.themes.fontSize(`${fontSize}%`);
    syncSettingsUI();
  }
  $("fontUp").addEventListener("click", () => setFontSize(fontSize + 10));
  $("fontDown").addEventListener("click", () => setFontSize(fontSize - 10));
  $("prevPage").addEventListener("click", goBack);
  $("nextPage").addEventListener("click", goForward);
  $("closeReader").addEventListener("click", closeReader);
  // ======================= Bookmarks, highlights, notes (v7.2) =======================
  const HL_COLORS = { yellow: "#ffe066", green: "#9be3b4", pink: "#f7a8c8" };
  let pendingSelection = null, editingUid = null, renderedHighlights = new Set();
  const cfiCompare = (a, b) => { try { return new ePub.CFI().compare(a, b); } catch (_) { return 0; } };
  async function bookmarkOnPage() {
    if (pdf && currentBook) { // v8.8: PDF bookmarks are whole pages ("pdf:<page>")
      const vis = pdfVisible(pdf);
      return (await getAnnotations(currentBook.id)).find(a => a.type === "bookmark" && vis.includes(pdfTargetPage(a.cfi))) || null;
    }
    if (!rendition || !currentBook) return null;
    const loc = rendition.currentLocation();
    if (!loc?.start?.cfi) return null;
    const end = loc.end?.cfi || loc.start.cfi;
    return (await getAnnotations(currentBook.id)).find(a => a.type === "bookmark" && cfiCompare(a.cfi, loc.start.cfi) >= 0 && cfiCompare(a.cfi, end) <= 0) || null;
  }
  async function updateBookmarkButton() {
    const bm = await bookmarkOnPage().catch(() => null);
    // v8.6: star icon (filled through aria-pressed in CSS) + label; phones show only the icon, the label stays for screen readers.
    $("bookmarkLabel").textContent = bm ? "نشانک ذخیره شد" : "نشانک";
    $("bookmarkButton").title = bm ? "نشانک ذخیره شد" : "نشانک";
    $("bookmarkButton").setAttribute("aria-pressed", String(!!bm));
  }
  async function saveAnnotation(rec) {
    if (isPreviewId(rec.bookId)) return;
    rec.updatedAt = Date.now(); rec.dirty = true;
    await put("annotations", rec);
    scheduleAnnotationPush();
  }
  const PREVIEW_NO_SAVE = "در حالت پیش‌نمایش نشانک و یادداشت ذخیره نمی‌شود؛ اول کتاب را به کتابخانه اضافه کن.";
  $("bookmarkButton").addEventListener("click", async () => {
    if (!currentBook || (!rendition && !pdf)) return;
    if (currentBook.preview) { showToast(PREVIEW_NO_SAVE); return; }
    if (pdf) {
      const existing = await bookmarkOnPage();
      if (existing) { await saveAnnotation({ ...existing, deleted: true }); showToast("نشانک برداشته شد."); }
      else {
        const n = pdf.page, e = pdfOutlineFor(pdf, n);
        await saveAnnotation({ uid: newUid(), bookId: currentBook.id, type: "bookmark", cfi: `pdf:${n}`, quote: `صفحهٔ ${faNum(n)}${e ? " · " + e.label : ""}`.slice(0, 140),
                               note: "", color: "", createdAt: Date.now(), deleted: false });
        showToast("نشانک ذخیره شد.");
      }
      updateBookmarkButton(); renderNotesList();
      return;
    }
    const loc = rendition.currentLocation();
    if (!loc?.start?.cfi) return;
    const existing = await bookmarkOnPage();
    if (existing) {
      await saveAnnotation({ ...existing, deleted: true });
      showToast("نشانک برداشته شد.");
    } else {
      let quote = "";
      try { quote = String(await bookInstance.getRange(loc.start.cfi) || "").trim().slice(0, 140); } catch (_) {}
      if (!quote) quote = $("chapterLabel").textContent;
      await saveAnnotation({ uid: newUid(), bookId: currentBook.id, type: "bookmark", cfi: loc.start.cfi, quote, note: "", color: "", createdAt: Date.now(), deleted: false });
      showToast("نشانک ذخیره شد.");
    }
    updateBookmarkButton(); renderNotesList();
  });
  function highlightStyle(color) { return { fill: HL_COLORS[color] || HL_COLORS.yellow, "fill-opacity": "0.4", "mix-blend-mode": "multiply" }; }
  function drawHighlight(a) {
    if (!rendition || renderedHighlights.has(a.uid)) return;
    try {
      rendition.annotations.highlight(a.cfi, { uid: a.uid }, () => openHighlightActions(a.uid), "borg-hl", highlightStyle(a.color));
      renderedHighlights.add(a.uid);
    } catch (err) { console.warn("Highlight could not be drawn:", err); }
  }
  function eraseHighlight(a) {
    try { rendition && rendition.annotations.remove(a.cfi, "highlight"); } catch (_) {}
    renderedHighlights.delete(a.uid);
  }
  async function renderBookAnnotations() {
    if (pdf && currentBook) { renderNotesList(); return; }
    if (!rendition || !currentBook) return;
    const anns = await getAnnotations(currentBook.id);
    const live = new Set(anns.filter(a => a.type === "highlight").map(a => a.uid));
    for (const uid of [...renderedHighlights]) if (!live.has(uid)) { const a = await getAnnotation(uid); if (a) eraseHighlight(a); else renderedHighlights.delete(uid); }
    anns.filter(a => a.type === "highlight").forEach(drawHighlight);
    renderNotesList();
  }
  async function renderNotesList() {
    if (!currentBook) { $("notesList").innerHTML = ""; return; }
    const anns = (await getAnnotations(currentBook.id)).sort((a, b) => pdf ? (pdfTargetPage(a.cfi) || 0) - (pdfTargetPage(b.cfi) || 0) : cfiCompare(a.cfi, b.cfi));
    $("notesList").innerHTML = anns.length ? anns.map(a => `<div class="note-item" data-goto="${esc(a.cfi)}">
        <span class="note-mark" style="background:${a.type === "bookmark" ? "#2f7a62" : (HL_COLORS[a.color] || HL_COLORS.yellow)}"></span>
        <span class="note-text"><span class="note-quote" dir="auto">${a.type === "bookmark" ? "🔖 " : ""}${esc(a.quote || "")}</span>${a.note ? `<span class="note-note" dir="auto">✎ ${esc(a.note)}</span>` : ""}</span>
        <button class="note-del" type="button" data-del="${esc(a.uid)}" aria-label="حذف">×</button></div>`).join("")
      : (pdf ? '<p class="muted">هنوز نشانکی نداری. با دکمهٔ نشانک، صفحه را نشانه بگذار.</p>' : '<p class="muted">هنوز نشانک یا یادداشتی نداری. متن را انتخاب کن تا هایلایت شود.</p>');
  }
  document.querySelectorAll("[data-ptab]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("[data-ptab]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    $("tocList").hidden = b.dataset.ptab !== "toc";
    $("notesView").hidden = b.dataset.ptab !== "notes";
    if (b.dataset.ptab === "notes") renderNotesList();
  }));
  $("notesList").addEventListener("click", async e => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      const a = await getAnnotation(del.dataset.del);
      if (a) { if (a.type === "highlight") eraseHighlight(a); await saveAnnotation({ ...a, deleted: true }); }
      renderNotesList(); updateBookmarkButton();
      return;
    }
    const item = e.target.closest("[data-goto]");
    if (item && (rendition || pdf)) { closeToc(false); jumpTo(item.dataset.goto); }
  });
  $("exportNotes").addEventListener("click", async () => {
    if (!currentBook) return;
    const anns = (await getAnnotations(currentBook.id)).sort((a, b) => cfiCompare(a.cfi, b.cfi));
    const lines = [`# ${currentBook.title}`, currentBook.author ? `*${currentBook.author}*` : "", ""];
    for (const a of anns) {
      lines.push(a.type === "bookmark" ? `- 🔖 ${a.quote || ""}` : `> ${String(a.quote || "").replace(/\n+/g, " ")}`);
      if (a.note) lines.push("", `یادداشت: ${a.note}`);
      lines.push("");
    }
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `${currentBook.title} - یادداشت‌ها.md` });
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 5000);
  });
  function hideSelectionBar() { $("selectionBar").hidden = true; pendingSelection = null; editingUid = null; }
  function showSelectionBar(mode) {
    $("selectionBar").querySelector(".sel-new").hidden = mode !== "new";
    $("selectionBar").querySelector(".sel-edit").hidden = mode !== "edit";
    $("selectionBar").hidden = false;
  }
  function onTextSelected(cfiRange, contents) {
    pendingSelection = { cfiRange, contents }; editingUid = null;
    showSelectionBar("new");
  }
  function openHighlightActions(uid) { pendingSelection = null; editingUid = uid; showSelectionBar("edit"); }
  async function createHighlight(color, note) {
    if (!pendingSelection || !currentBook) return;
    if (currentBook.preview) { hideSelectionBar(); showToast(PREVIEW_NO_SAVE); return; }
    const { cfiRange, contents } = pendingSelection;
    let quote = "";
    try { quote = String(await bookInstance.getRange(cfiRange) || "").trim().slice(0, 2000); } catch (_) {}
    if (!quote) { try { quote = String(contents.window.getSelection()).trim().slice(0, 2000); } catch (_) {} }
    const rec = { uid: newUid(), bookId: currentBook.id, type: "highlight", cfi: cfiRange, quote, note: note || "", color, createdAt: Date.now(), deleted: false };
    await saveAnnotation(rec);
    drawHighlight(rec);
    try { contents.window.getSelection().removeAllRanges(); } catch (_) {}
    hideSelectionBar(); renderNotesList();
  }
  $("selectionBar").addEventListener("click", async e => {
    const sw = e.target.closest("[data-hl]");
    if (sw) { await createHighlight(sw.dataset.hl, ""); return; }
    const act = e.target.closest("[data-sel]")?.dataset.sel;
    if (act === "close") { try { pendingSelection?.contents.window.getSelection().removeAllRanges(); } catch (_) {} hideSelectionBar(); }
    else if ((act === "define" || act === "copy") && pendingSelection) {
      let text = "";
      try { text = String(pendingSelection.contents.window.getSelection()).trim(); } catch (_) {}
      if (!text) { try { text = String(await bookInstance.getRange(pendingSelection.cfiRange) || "").trim(); } catch (_) {} }
      hideSelectionBar();
      if (act === "copy") {
        try { await navigator.clipboard.writeText(text); showToast("کپی شد."); } catch (_) { showToast("کپی ممکن نشد."); }
      } else showLookup(text);
    }
    else if (act === "quote" && pendingSelection) {
      let quote = "";
      try { quote = String(await bookInstance.getRange(pendingSelection.cfiRange) || "").trim(); } catch (_) {}
      if (!quote) { try { quote = String(pendingSelection.contents.window.getSelection()).trim(); } catch (_) {} }
      hideSelectionBar();
      shareQuote(quote.slice(0, 600));
    }
    else if (act === "note" && currentBook && currentBook.preview) { hideSelectionBar(); showToast(PREVIEW_NO_SAVE); }
    else if (act === "note") { const note = prompt("یادداشت تو برای این قسمت:", ""); if (note !== null) await createHighlight("yellow", note.trim()); }
    else if (act === "edit" && editingUid) {
      const a = await getAnnotation(editingUid);
      const note = a ? prompt("یادداشت:", a.note || "") : null;
      if (a && note !== null) await saveAnnotation({ ...a, note: note.trim() });
      hideSelectionBar(); renderNotesList();
    } else if (act === "delete" && editingUid) {
      const a = await getAnnotation(editingUid);
      if (a) { eraseHighlight(a); await saveAnnotation({ ...a, deleted: true }); }
      hideSelectionBar(); renderNotesList();
    }
  });
  // v8.5 library search: debounced live filter, × / Escape clear, "/" focuses it, query kept per tab session only.
  try { $("searchInput").value = sessionStorage.getItem(LIB_SEARCH_KEY) || ""; } catch (_) {}
  let libSearchTimer = null;
  function storeLibSearch() {
    try { const v = $("searchInput").value.trim(); if (v) sessionStorage.setItem(LIB_SEARCH_KEY, v); else sessionStorage.removeItem(LIB_SEARCH_KEY); } catch (_) {}
  }
  function clearLibSearch(focus) {
    clearTimeout(libSearchTimer);
    $("searchInput").value = ""; storeLibSearch(); renderLibrary();
    if (focus) $("searchInput").focus({ preventScroll: true });
  }
  $("searchInput").addEventListener("input", () => {
    $("librarySearchClear").hidden = !$("searchInput").value.trim();
    clearTimeout(libSearchTimer);
    libSearchTimer = setTimeout(() => { storeLibSearch(); renderLibrary(); }, 180);
  });
  $("searchInput").addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    e.preventDefault(); e.stopPropagation();
    if ($("searchInput").value) clearLibSearch(true); else $("searchInput").blur();
  });
  $("librarySearchClear").addEventListener("click", () => clearLibSearch(true));
  $("bookGrid").addEventListener("click", e => { if (e.target.closest("[data-search-clear]")) clearLibSearch(true); });
  document.addEventListener("keydown", e => {
    if (!(e.key === "/" || (e.code === "Slash" && !e.shiftKey)) || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
    if (readerOpen() || $("viewMine").hidden || document.querySelector("dialog[open]")) return;
    const t = e.target;
    if (t && t.closest && (t.closest("input, textarea, select, [contenteditable]") || t.isContentEditable)) return;
    e.preventDefault();
    $("searchInput").focus(); $("searchInput").select();
  });
  inputEls.forEach(el => el?.addEventListener("change", e => importFiles(e.target.files)));
  // Drag & drop EPUB/PDF files onto the library (not while reading).
  document.addEventListener("dragover", e => {
    if (readerOpen() || !e.dataTransfer || ![...(e.dataTransfer.types || [])].includes("Files")) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "copy"; document.body.classList.add("drop-active");
  });
  document.addEventListener("dragleave", e => { if (!e.relatedTarget) document.body.classList.remove("drop-active"); });
  document.addEventListener("drop", e => {
    document.body.classList.remove("drop-active");
    if (readerOpen() || !e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault(); importFiles(e.dataTransfer.files);
  });
  const onLibraryClick = async e => {
    const optBtn = e.target.closest("[data-options]");
    if (optBtn) { e.stopPropagation(); openBookOptions(optBtn.dataset.options); return; }
    const shareBtn = e.target.closest("[data-share]");
    if (shareBtn) { e.stopPropagation(); shareBook(shareBtn.dataset.share); return; }
    const removeBtn = e.target.closest("[data-remove]");
    if (removeBtn) {
      e.stopPropagation();
      const id = removeBtn.dataset.remove;
      const rec = await getBook(id);
      const cloud = account && rec && rec.serverId;
      if (confirm(cloud ? "این کتاب از کتابخانه‌ات روی همهٔ دستگاه‌ها حذف شود؟" : "این کتاب از حافظه همین دستگاه حذف شود؟")) {
        if (cloud) {
          try { await api("library-remove", { method: "POST", body: { book_id: rec.serverId } }); }
          catch (err) { showToast(apiMessage(err)); return; }
        }
        await remove("books", id); await remove("progress", id); renderLibrary(); showToast("کتاب حذف شد.");
      }
      return;
    }
    const card = e.target.closest("[data-open]");
    if (card) openReader(card.dataset.open).catch(err => { console.error(err); reportError("reader", err, "openReader"); showToast("بازکردن کتاب ناموفق بود."); });
  };
  const onLibraryKey = e => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-open]")) { e.preventDefault(); openReader(e.target.dataset.open).catch(err => { console.error(err); reportError("reader", err, "openReader"); showToast("بازکردن کتاب ناموفق بود."); }); }
  };
  ["bookGrid", "continueShelf"].forEach(id => { $(id).addEventListener("click", onLibraryClick); $(id).addEventListener("keydown", onLibraryKey); });
  $("libraryFilters").addEventListener("click", e => {
    const b = e.target.closest("[data-filter]");
    if (!b) return;
    readerPrefs.libraryFilter = b.dataset.filter; saveReaderPrefs(); renderLibrary();
  });
  $("goalButton").addEventListener("click", () => {
    const v = prompt("هدف روزانه (دقیقه):", String(readerPrefs.dailyGoal || 20));
    const n = Math.round(Number(v));
    if (v !== null && n > 0 && n <= 600) { readerPrefs.dailyGoal = n; saveReaderPrefs(); renderLibrary(); }
  });
  let optionsBookId = null;
  async function renderBookOptions() {
    const b = await getBook(optionsBookId);
    if (!b) return;
    $("bookOptionsTitle").textContent = b.title;
    document.querySelectorAll("[data-status]").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.status === (b.status || ""))));
    $("shelfChecks").innerHTML = shelvesCache.length ? shelvesCache.map(sh => `<label><input type="checkbox" data-shelf="${esc(sh.uid)}" ${(b.shelves || []).includes(sh.uid) ? "checked" : ""}> ${esc(sh.name)}
        <button type="button" class="note-del" data-shelf-del="${esc(sh.uid)}" aria-label="حذف قفسه">×</button></label>`).join("") : '<p class="muted small">هنوز قفسه‌ای نساخته‌ای.</p>';
    $("bookOptionsActions").innerHTML = (account && b.serverId && !b.catalogStatus ? `<button class="secondary-button" type="button" data-opt="share">⇪ پیشنهاد برای کتابخانهٔ عمومی</button>` : "")
      + `<button class="secondary-button" type="button" data-opt="remove">حذف کتاب</button>`;
  }
  async function openBookOptions(id) { optionsBookId = id; await refreshShelves(); await renderBookOptions(); $("bookOptions").showModal(); }
  $("bookOptionsClose").addEventListener("click", () => $("bookOptions").close());
  $("bookOptions").addEventListener("close", () => renderLibrary());
  $("bookOptions").addEventListener("click", async e => {
    const st = e.target.closest("[data-status]");
    if (st) {
      const b = await getBook(optionsBookId);
      await setBookMeta(optionsBookId, { status: b && b.status === st.dataset.status ? "" : st.dataset.status });
      return renderBookOptions();
    }
    const del = e.target.closest("[data-shelf-del]");
    if (del) {
      e.preventDefault();
      const sh = shelvesCache.find(x => x.uid === del.dataset.shelfDel);
      if (sh && confirm(`قفسهٔ «${sh.name}» حذف شود؟ (کتاب‌ها حذف نمی‌شوند)`)) {
        await saveShelf({ ...sh, deleted: true });
        if (readerPrefs.libraryFilter === `shelf:${sh.uid}`) { readerPrefs.libraryFilter = "all"; saveReaderPrefs(); }
      }
      return renderBookOptions();
    }
    const opt = e.target.closest("[data-opt]")?.dataset.opt;
    if (opt === "share") { $("bookOptions").close(); return shareBook(optionsBookId); }
    if (opt === "remove") {
      $("bookOptions").close();
      const btn = document.querySelector(`#bookGrid [data-remove="${CSS.escape(optionsBookId)}"]`);
      if (btn) btn.click();
    }
  });
  $("shelfChecks").addEventListener("change", async e => {
    const cb = e.target.closest("[data-shelf]");
    if (!cb) return;
    const b = await getBook(optionsBookId);
    const set = new Set(b.shelves || []);
    if (cb.checked) set.add(cb.dataset.shelf); else set.delete(cb.dataset.shelf);
    await setBookMeta(optionsBookId, { shelves: [...set] });
  });
  $("addShelf").addEventListener("click", async () => {
    const name = $("newShelfName").value.trim();
    if (!name) return;
    const uid = newUid();
    await saveShelf({ uid, name: name.slice(0, 80), position: shelvesCache.length, deleted: false });
    $("newShelfName").value = "";
    const b = await getBook(optionsBookId);
    await setBookMeta(optionsBookId, { shelves: [...new Set([...(b.shelves || []), uid])] });
    renderBookOptions();
  });
  function network() {
    const online = navigator.onLine;
    $("networkStatus").textContent = online ? "● آنلاین" : "● آفلاین";
    $("networkStatus").style.color = online ? "#4b9874" : "#c67b5c";
    $("networkStatus").hidden = online; // v8.6: connectivity is shown only when offline
  }
  window.addEventListener("online", network); window.addEventListener("offline", network);
  if (localStorage.getItem(THEME_KEY) === "dark") document.body.classList.add("dark");
  openDB().then(async database => {
    db = database; renderLibrary();
    await refreshShelves();
    renderLibrary();
    await importSharedFiles();
    await initAccount();
    setTimeout(repairMetadata, 1200);
  }).catch(err => { console.error(err); document.body.classList.remove("lib-loading"); showToast("مرورگر اجازه ذخیره محلی نمی‌دهد."); });
  network();

  // ======================= Account, cloud sync, public catalog, admin (v7) =======================
  const API = new URL("api/", document.baseURI).href;
  let account = null;            // { user, csrf } when logged in
  let syncing = false, syncAgain = false;
  const progressTimers = new Map();
  class ApiFail extends Error { constructor(status, code, message) { super(message); this.status = status; this.code = code; } }

  async function api(route, { method = "GET", body, raw, query = "" } = {}) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (raw !== undefined) headers["Content-Type"] = "application/octet-stream";
    if (method !== "GET" && account) headers["X-CSRF-Token"] = account.csrf;
    const res = await fetch(`${API}?r=${route}${query}`, {
      method, headers, credentials: "same-origin", cache: "no-store",
      body: body !== undefined ? JSON.stringify(body) : raw,
    });
    const type = res.headers.get("Content-Type") || "";
    if (!type.includes("json")) {
      if (!res.ok) throw new ApiFail(res.status, "http", "درخواست ناموفق بود.");
      return res;
    }
    const data = await res.json();
    if (!res.ok) {
      if (res.status === 401 && account && route !== "login") setAccount(null);
      throw new ApiFail(res.status, data.error || "http", data.message || "درخواست ناموفق بود.");
    }
    return data;
  }
  const AUTH_MESSAGES = {
    bad_credentials: "ایمیل یا رمز عبور اشتباه است.", rate_limited: "تلاش‌ها زیاد بود؛ چند دقیقه بعد دوباره امتحان کن.",
    bad_invite: "کد دعوت نامعتبر است یا قبلاً استفاده شده.", email_taken: "این ایمیل قبلاً ثبت شده است.",
    weak_password: "رمز عبور باید حداقل ۸ کاراکتر باشد.", bad_email: "ایمیل معتبر نیست.",
    bad_setup_key: "کلید راه‌اندازی اشتباه است.", already_setup: "راه‌اندازی قبلاً انجام شده است.",
    setup_disabled: "کلید راه‌اندازی روی سرور تنظیم نشده است.", not_configured: "سرور هنوز پیکربندی نشده است.",
  };
  function apiMessage(err) {
    if (err instanceof ApiFail) return AUTH_MESSAGES[err.code] || (err.code.startsWith("missing_") ? "همهٔ فیلدها را پر کن." : err.message);
    return navigator.onLine ? "ارتباط با سرور برقرار نشد." : "اینترنت قطع است.";
  }
  // v8.5 sync chip (library tabs row + reader toolbar): synced (with relative time) / syncing / waiting for a
  // connection (offline with local changes) / failed (tap retries). Derived from sync(), navigator.onLine and pending work.
  const syncChip = { at: 0, failed: false, detail: "", pending: false };
  const unpushedProgress = new Set(); // local book ids whose reading position has not reached the server yet
  function setSyncStatus(text) { syncChip.detail = text || ""; renderSyncChip(); }
  async function hasPendingSync() {
    if (unpushedProgress.size) return true;
    const books = await getAllBooks();
    if (books.some(b => b.cleanPending || b.metaDirty || (!b.serverId && fileOf(b)))) return true;
    const synced = new Set(books.filter(b => b.serverId).map(b => b.id));
    if ((await getAllAnnotations()).some(a => a.dirty && synced.has(a.bookId))) return true;
    return (await getAllShelves()).some(x => x.dirty);
  }
  function syncAgo(ms) {
    const m = Math.floor(Math.max(0, Date.now() - ms) / 60000);
    if (m < 1) return "همین حالا";
    if (m < 60) return `${faNum(m)} دقیقه پیش`;
    const h = Math.floor(m / 60);
    return h < 24 ? `${faNum(h)} ساعت پیش` : `${faNum(Math.floor(h / 24))} روز پیش`;
  }
  function renderSyncChip() {
    let state = "", text = "", tip = "";
    if (account) {
      if (syncing || (!syncChip.at && !syncChip.failed && navigator.onLine)) { state = "syncing"; text = "در حال همگام‌سازی…"; tip = syncChip.detail; }
      else if (!navigator.onLine && syncChip.pending) { state = "offline"; text = "در انتظار اتصال"; tip = "تغییرهای این دستگاه بعد از وصل شدن اینترنت همگام می‌شوند."; }
      else if (syncChip.failed) { state = "error"; text = "خطای همگام‌سازی"; tip = "برای تلاش دوباره بزن."; }
      else { state = "synced"; text = syncChip.at ? `همگام شد · ${syncAgo(syncChip.at)}` : "همگام شد"; tip = "برای همگام‌سازی دوباره بزن."; }
    }
    const inReader = !$("reader").classList.contains("hidden");
    for (const [id, live] of [["syncStatus", !inReader], ["readerSync", inReader]]) {
      const el = $(id);
      el.hidden = !state; el.dataset.state = state;
      if (el.textContent !== text) el.textContent = text;
      el.title = tip || text; el.setAttribute("aria-label", tip && state !== "syncing" ? `${text}. ${tip}` : text);
      el.parentElement.setAttribute("aria-live", live ? "polite" : "off"); // one announcement, from the visible chip
    }
  }
  /** Re-check pending local work (cheap IndexedDB reads) and redraw the chip. */
  let pendingCheck = null;
  function refreshSyncChip() {
    if (!account) { renderSyncChip(); return; }
    clearTimeout(pendingCheck);
    pendingCheck = setTimeout(() => {
      hasPendingSync().then(p => { syncChip.pending = p; }).catch(() => {}).then(renderSyncChip);
    }, 150);
  }
  ["syncStatus", "readerSync"].forEach(id => $(id).addEventListener("click", () => { if (account && navigator.onLine) sync(); else refreshSyncChip(); }));
  window.addEventListener("online", refreshSyncChip); window.addEventListener("offline", refreshSyncChip);
  setInterval(() => { if (account && document.visibilityState === "visible") renderSyncChip(); }, 30000);

  function setAccount(acc) {
    account = acc;
    const user = acc && acc.user;
    $("accountButton").textContent = user ? user.name : "ورود";
    document.querySelector('[data-view="catalog"]').hidden = !user;
    document.querySelector('[data-view="admin"]').hidden = !(user && user.role === "admin");
    if (!user) {
      showView("mine"); syncChip.at = 0; syncChip.failed = false; unpushedProgress.clear(); setSyncStatus("");
      resetCatalog(); previewCache = null;
      if (location.hash.startsWith("#catalog")) setHash("", false);
      if (location.hash.startsWith("#admin")) setHash("", false);
    }
    $("authForms").hidden = !!user;
    $("accountInfo").hidden = !user;
    if (user) { $("accountName").textContent = user.name; $("accountEmail").textContent = user.email; }
    if (!user || user.role !== "admin") { hideNewApiToken(); setCleanSyncState(""); }
    else pullCleanRules();
    renderLibrary();
  }

  // ---------- Views ----------
  // "book" is a catalog book's page; it lives under the catalog tab.
  function showView(view) {
    const tab = view === "book" ? "catalog" : view;
    document.querySelectorAll(".view-tabs [role=tab]").forEach(t => t.setAttribute("aria-selected", String(t.dataset.view === tab)));
    $("viewMine").hidden = view !== "mine";
    $("viewCatalog").hidden = view !== "catalog";
    $("viewBook").hidden = view !== "book";
    $("viewAdmin").hidden = view !== "admin";
    closeAddMenu();
    if (view === "catalog") showCatalog();
    if (view === "book") renderBookDetail();
    if (view === "admin") openAdmin();
  }
  document.querySelectorAll(".view-tabs [role=tab]").forEach(t => t.addEventListener("click", () => {
    if (t.dataset.view === "catalog") { catalogFilter.author = ""; catalogFilter.category = ""; catalogFilter.cat = ""; catalogState.at = 0; }
    setHash(t.dataset.view === "catalog" ? "#catalog" : "", false);
    showView(t.dataset.view);
  }));

  // ---------- Account dialog ----------
  let authMode = "login";
  const AUTH_FIELDS = { login: ["email", "password"], register: ["invite", "name", "email", "password"], setup: ["setup_key", "name", "email", "password"] };
  function setAuthMode(mode) {
    authMode = mode;
    document.querySelectorAll("[data-auth]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.auth === mode)));
    document.querySelectorAll("#authForm [data-field]").forEach(l => { l.hidden = !AUTH_FIELDS[mode].includes(l.dataset.field); });
    $("authForm").password.autocomplete = mode === "login" ? "current-password" : "new-password";
    $("authSubmit").textContent = mode === "login" ? "ورود" : mode === "register" ? "ساخت حساب" : "ساخت حساب مدیر";
    $("authError").textContent = "";
  }
  document.querySelectorAll("[data-auth]").forEach(b => b.addEventListener("click", () => setAuthMode(b.dataset.auth)));
  $("accountButton").addEventListener("click", async () => {
    $("accountDialog").showModal();
    if (!account) {
      try { const h = await api("health"); document.querySelector('[data-auth="setup"]').hidden = !h.needs_setup; if (h.needs_setup) setAuthMode("setup"); }
      catch (_) {}
    }
  });
  $("accountClose").addEventListener("click", () => $("accountDialog").close());
  $("authForm").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, body = {};
    AUTH_FIELDS[authMode].forEach(k => { body[k] = f[k].value.trim(); });
    body.password = f.password.value;
    $("authSubmit").disabled = true; $("authError").textContent = "";
    try {
      const r = await api(authMode, { method: "POST", body });
      f.reset();
      setAccount({ user: r.user, csrf: r.csrf });
      $("accountDialog").close();
      showToast(`خوش آمدی، ${r.user.name}.`);
      sync();
    } catch (err) {
      $("authError").textContent = apiMessage(err);
    } finally { $("authSubmit").disabled = false; }
  });
  $("logoutButton").addEventListener("click", async () => {
    try { await api("logout", { method: "POST" }); } catch (_) {}
    setAccount(null);
    $("accountDialog").close();
    showToast("از حساب خارج شدی. کتاب‌ها روی این دستگاه می‌مانند.");
  });
  $("syncNow").addEventListener("click", () => { $("accountDialog").close(); sync(); });

  // ---------- Sync ----------
  async function ensureHash(b) {
    if (b.contentHash || !fileOf(b)) return b;
    const h = await sha256(await fileOf(b).arrayBuffer());
    if (h) { b = { ...b, contentHash: h }; await put("books", b); }
    return b;
  }
  async function uploadBook(b) {
    const data = await fileOf(b).arrayBuffer();
    let r = await api("upload-init", { method: "POST", body: {
      sha256: b.contentHash, size: data.byteLength, title: b.title || "بدون عنوان", author: b.author || "", cover: b.cover || undefined, ...metaBody(b),
      ...(isPdfRecord(b) ? { format: "pdf" } : {}), // v8.8: hint only; the server detects the format from the bytes
    } });
    if (r.exists) return r.book;
    const chunk = r.chunk_bytes, id = r.upload_id;
    for (let off = 0; off < data.byteLength; off += chunk) {
      await api("upload-chunk", { method: "POST", raw: data.slice(off, off + chunk), query: `&upload_id=${id}&offset=${off}` });
    }
    r = await api("upload-complete", { method: "POST", body: { upload_id: id, ...metaBody(b) } });
    return r.book;
  }
  /** Store a server book locally. `blob` can be passed when the file is already here (e.g. from an online preview). */
  async function downloadBook(sb, blob) {
    // Never keep two local copies of the same file (a sync may run while a catalog book is being added).
    const same = sb.sha256 && (await getAllBooks()).find(b => b.contentHash === sb.sha256);
    if (same) return same;
    if (!blob) blob = await (await api("book-file", { query: `&id=${sb.id}` })).blob();
    // v8.8: `format` comes from the server; without it (older server) the bytes decide, EPUB otherwise.
    const isPdf = sb.format === "pdf" || (sb.format !== "epub" && await sniffPdf(blob));
    let cover = null;
    if (sb.has_cover) {
      try { cover = await blobToDataUrl(await (await api("book-cover", { query: `&id=${sb.id}` })).blob()); } catch (_) {}
    }
    const record = {
      id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      title: sb.title, author: sb.author || "نویسنده نامشخص", cover,
      file: isPdf ? new File([blob], `${sb.title}.pdf`, { type: "application/pdf" }) : new File([blob], `${sb.title}.epub`, { type: "application/epub+zip" }),
      size: blob.size, addedAt: Date.now(), contentHash: sb.sha256, serverId: sb.id, catalogStatus: sb.catalog_status || null,
      publisher: sb.publisher || "", published: sb.published || "", language: sb.language || "", metaExt: true,
      ...(isPdf ? { format: "pdf" } : {}),
    };
    await put("books", record);
    return record;
  }
  async function sync() {
    if (!account || !navigator.onLine) { refreshSyncChip(); return; }
    if (syncing) { syncAgain = true; return; }
    syncing = true;
    const progressSnapshot = [...unpushedProgress];
    let partialFail = false, ok = false;
    try {
      setSyncStatus("در حال همگام‌سازی…");
      const server = (await api("library")).books;
      const bySha = new Map(server.map(b => [b.sha256, b]));
      const byId = new Map(server.map(b => [b.id, b]));
      let local = await getAllBooks();
      // Books removed from the account on another device.
      for (const b of local) {
        if (b.serverId && !byId.has(b.serverId)) { await remove("books", b.id); await remove("progress", b.id); }
      }
      // v8.3: books cleaned while offline (or whose replace failed) — upload the new file, then book-replace.
      const replacedOld = new Set();
      for (const b of await getAllBooks()) {
        if (!b.cleanPending || !b.serverId) continue;
        replacedOld.add(b.serverId); // never re-download the old server copy
        try { const nb = await replaceOnServer(b); if (nb) { bySha.set(nb.sha256, nb); byId.set(nb.id, nb); } }
        catch (err) { partialFail = true; console.warn("Book replace failed:", b.title, err); reportError("sync", err, "sync:book-replace"); }
      }
      local = await getAllBooks();
      const localSha = new Set();
      let done = 0;
      const toUpload = [];
      for (let b of local) {
        if (b.cleanPending) continue; // still waiting for book-replace
        b = await ensureHash(b);
        if (b.contentHash) localSha.add(b.contentHash);
        const sb = b.contentHash && bySha.get(b.contentHash);
        if (sb) {
          const upd = { ...b, serverId: sb.id, catalogStatus: sb.catalog_status || null };
          const serverAuthor = sb.author || "نویسنده نامشخص";
          if (b.metaRepaired && !b.metaPushed) {
            try { await api("book-meta", { method: "POST", body: { book_id: sb.id, title: b.title, author: b.author === "نویسنده نامشخص" ? "" : (b.author || ""), cover: b.cover || undefined, ...metaBody(b) } }); } catch (_) {}
            upd.metaPushed = true; if (b.metaExtPush) upd.metaExtPush = false;
          } else if (!b.metaRepaired && (sb.title !== b.title || serverAuthor !== (b.author || "نویسنده نامشخص"))) {
            upd.title = sb.title; upd.author = serverAuthor;
          }
          // Publisher / date / language read from the EPUB: fill what the server is missing, adopt what it has.
          const ext = metaBody(b), missing = {};
          for (const k of Object.keys(META_LIMITS)) if (ext[k] && !sb[k]) missing[k] = ext[k];
          if (b.metaExtPush) {
            if (Object.keys(missing).length) { try { await api("book-meta", { method: "POST", body: { book_id: sb.id, ...missing } }); } catch (_) {} }
            upd.metaExtPush = false;
          }
          for (const k of Object.keys(META_LIMITS)) if (typeof sb[k] === "string" && sb[k] && sb[k] !== b[k]) upd[k] = sb[k];
          if (!b.cover && sb.has_cover) {
            try { upd.cover = await blobToDataUrl(await (await api("book-cover", { query: `&id=${sb.id}` })).blob()); } catch (_) {}
          }
          if (["serverId", "catalogStatus", "title", "author", "cover", "metaPushed", "metaExtPush", ...Object.keys(META_LIMITS)].some(k => upd[k] !== b[k])) await put("books", upd);
        } else if (b.contentHash) toUpload.push(b);
      }
      for (const b of toUpload) {
        setSyncStatus(`در حال آپلود «${b.title}» (${(++done).toLocaleString("fa-IR")} از ${toUpload.length.toLocaleString("fa-IR")})…`);
        try { const sb = await uploadBook(b); await put("books", { ...b, serverId: sb.id }); byId.set(sb.id, sb); }
        catch (err) { partialFail = true; console.warn("Upload failed:", b.title, err); reportError("sync", err, "sync:upload"); }
      }
      const missing = server.filter(sb => !localSha.has(sb.sha256) && !replacedOld.has(sb.id));
      done = 0;
      for (const sb of missing) {
        setSyncStatus(`در حال دریافت «${sb.title}» (${(++done).toLocaleString("fa-IR")} از ${missing.length.toLocaleString("fa-IR")})…`);
        try { await downloadBook(sb); } catch (err) { partialFail = true; console.warn("Download failed:", sb.title, err); reportError("sync", err, "sync:download"); }
      }
      // Reading progress: newest timestamp wins, in both directions.
      local = await getAllBooks();
      for (const b of local) {
        if (!b.serverId) continue;
        const sb = byId.get(b.serverId);
        const lp = await getProgress(b.id);
        const sp = sb && sb.progress;
        if (sp && (!lp || sp.updated_at > (lp.updatedAt || 0))) {
          await put("progress", { bookId: b.id, cfi: sp.cfi, percentage: sp.percentage, updatedAt: sp.updated_at, bookmark: lp?.bookmark || null });
        } else if (lp && lp.cfi && (!sp || (lp.updatedAt || 0) > sp.updated_at)) {
          try { await api("progress", { method: "POST", body: { book_id: b.serverId, cfi: lp.cfi, percentage: lp.percentage || 0, updated_at: lp.updatedAt || Date.now() } }); } catch (_) {}
        }
      }
      await syncLibraryMeta(byId);
      await syncStats();
      await syncAnnotations();
      ok = true;
      progressSnapshot.forEach(id => unpushedProgress.delete(id)); // pushed (or older than the server's) above
      syncChip.at = Date.now(); syncChip.failed = partialFail;
    } catch (err) {
      syncChip.failed = !(err instanceof ApiFail && err.status === 401);
      console.warn("Sync failed:", err);
      reportError("sync", err, "sync");
    } finally {
      syncing = false;
      if (!ok && !navigator.onLine) syncChip.failed = false; // lost the connection mid-way: «در انتظار اتصال», not an error
      setSyncStatus(""); refreshSyncChip();
      renderLibrary();
      if (syncAgain) { syncAgain = false; sync(); }
    }
  }
  /** Shelves and per-book status/shelves. serverBooks: Map(serverId -> library row) from this sync, or null to fetch. */
  async function syncLibraryMeta(serverBooks) {
    const dirtyShelves = (await getAllShelves()).filter(x => x.dirty);
    if (dirtyShelves.length) {
      await api("shelves", { method: "POST", body: { items: dirtyShelves.map(x => ({ uid: x.uid, name: x.name, position: x.position || 0, updated_at: x.updatedAt, deleted: !!x.deleted })) } });
      for (const x of dirtyShelves) { const cur = (await getAllShelves()).find(y => y.uid === x.uid); if (cur && cur.updatedAt === x.updatedAt) await put("shelves", { ...cur, dirty: false }); }
    }
    for (const r of (await api("shelves")).shelves) {
      const cur = (await getAllShelves()).find(y => y.uid === r.uid);
      if (!cur || (!cur.dirty && r.updated_at > (cur.updatedAt || 0))) await put("shelves", { uid: r.uid, name: r.name, position: r.position, updatedAt: r.updated_at, deleted: r.deleted, dirty: false });
    }
    await refreshShelves();
    if (!serverBooks) serverBooks = new Map((await api("library")).books.map(b => [b.id, b]));
    const local = (await getAllBooks()).filter(b => b.serverId);
    const push = [];
    for (const b of local) {
      const sb = serverBooks.get(b.serverId);
      if (!sb) continue;
      if (b.metaDirty && (b.metaUpdatedAt || 0) > (sb.meta_updated_at || 0)) {
        push.push({ book_id: b.serverId, status: b.status || "", shelves: b.shelves || [], updated_at: b.metaUpdatedAt });
      } else if ((sb.meta_updated_at || 0) > (b.metaUpdatedAt || 0)) {
        await put("books", { ...b, status: sb.status || "", shelves: sb.shelves || [], metaUpdatedAt: sb.meta_updated_at, metaDirty: false });
      }
    }
    if (push.length) {
      await api("library-meta", { method: "POST", body: { items: push } });
      for (const p of push) { const b = local.find(x => x.serverId === p.book_id); const cur = b && await getBook(b.id); if (cur && cur.metaUpdatedAt === p.updated_at) await put("books", { ...cur, metaDirty: false }); }
    }
    if (!serverBooks.size) return;
    renderLibrary();
  }
  async function syncStats() {
    const mine = readJson(DAYS_KEY);
    const cutoff = dayKey(new Date(Date.now() - 400 * 86400000));
    const days = Object.entries(mine).filter(([d]) => d >= cutoff).map(([day, seconds]) => ({ day, seconds }));
    if (days.length) await api("stats", { method: "POST", body: { device: deviceId(), days } });
    const totals = (await api("stats")).days;
    const other = {};
    for (const t of totals) other[t.day] = Math.max(0, t.seconds - (mine[t.day] || 0));
    writeJson(OTHER_KEY, other);
  }
  function annSinceKey() { return `borg-ann-since-${account?.user?.id || 0}${STORAGE_SUFFIX}`; }
  async function syncAnnotations() {
    const books = await getAllBooks();
    // Annotations of a book waiting for book-replace are pushed after it (the server re-stamps moved ones).
    const localToServer = new Map(books.filter(b => b.serverId && !b.cleanPending).map(b => [b.id, b.serverId]));
    const serverToLocal = new Map(books.filter(b => b.serverId).map(b => [b.serverId, b.id]));
    // Push local changes.
    const dirty = (await getAllAnnotations()).filter(a => a.dirty && localToServer.has(a.bookId));
    for (let i = 0; i < dirty.length; i += 200) {
      const batch = dirty.slice(i, i + 200);
      await api("annotations", { method: "POST", body: { items: batch.map(a => ({
        uid: a.uid, book_id: localToServer.get(a.bookId), type: a.type, cfi: a.cfi, quote: a.quote || "", note: a.note || "",
        color: a.color || "", created_at: a.createdAt || a.updatedAt, updated_at: a.updatedAt, deleted: !!a.deleted,
      })) } });
      for (const a of batch) { const cur = await getAnnotation(a.uid); if (cur && cur.updatedAt === a.updatedAt) await put("annotations", { ...cur, dirty: false }); }
    }
    // Pull remote changes (newest timestamp wins).
    let since = 0;
    try { since = Number(localStorage.getItem(annSinceKey())) || 0; } catch (_) {}
    const r = await api("annotations", { query: `&since=${since}` });
    let maxSeen = since, changedCurrent = false;
    for (const a of r.annotations) {
      maxSeen = Math.max(maxSeen, a.updated_at);
      const bookId = serverToLocal.get(a.book_id);
      if (!bookId) continue;
      const cur = await getAnnotation(a.uid);
      if (cur && cur.updatedAt >= a.updated_at) continue;
      await put("annotations", { uid: a.uid, bookId, type: a.type, cfi: a.cfi, quote: a.quote, note: a.note, color: a.color,
                                 createdAt: a.created_at, updatedAt: a.updated_at, deleted: a.deleted, dirty: false });
      if (currentBook && currentBook.id === bookId) changedCurrent = true;
    }
    // Re-read from slightly earlier next time to tolerate clock differences between devices.
    try { localStorage.setItem(annSinceKey(), String(Math.max(0, maxSeen - 10 * 60 * 1000))); } catch (_) {}
    if (changedCurrent) { await renderBookAnnotations(); updateBookmarkButton(); }
  }
  let annTimer = null;
  function scheduleAnnotationPush() {
    refreshSyncChip();
    if (!account || !navigator.onLine) return;
    clearTimeout(annTimer);
    annTimer = setTimeout(() => { syncAnnotations().catch(err => { console.warn("Annotation sync failed:", err); reportError("sync", err, "annotations"); }); }, 2500);
  }
  /** Debounced push of one book's progress while reading. */
  function scheduleProgressPush(localBookId) {
    if (account && !unpushedProgress.has(localBookId)) { unpushedProgress.add(localBookId); refreshSyncChip(); }
    if (!account || !navigator.onLine) return;
    clearTimeout(progressTimers.get(localBookId));
    progressTimers.set(localBookId, setTimeout(async () => {
      progressTimers.delete(localBookId);
      try {
        const b = await getBook(localBookId), p = await getProgress(localBookId);
        if (b && b.serverId && p && p.cfi) {
          await api("progress", { method: "POST", body: { book_id: b.serverId, cfi: p.cfi, percentage: p.percentage || 0, updated_at: p.updatedAt || Date.now() } });
          if (!progressTimers.has(localBookId)) { unpushedProgress.delete(localBookId); refreshSyncChip(); }
        }
      } catch (_) {}
    }, 4000));
  }

  // ---------- Public catalog ----------
  // v8.9: the catalog is paged on the server (`catalog&page=N&per=…` with q/category/author/format/sort → books, total,
  // categories). A server without paging answers with the whole list and no `total`: the client then filters and sorts
  // that list itself, as before. Loaded pages stay in memory, so coming back from a book page restores list and scroll.
  let catalogBooks = [], catalogLoaded = false;
  // v8.10: `cat` is a category id from the admin's two-level list (a top-level id includes its subcategories); `category`
  // is the old free-text category, still reachable from a book page whose book has no categories yet.
  const catalogFilter = { author: "", category: "", cat: "", format: "", sort: "new" };
  const CATALOG_PER = 24, CATALOG_TTL = 5 * 60 * 1000;
  // mode: "" unknown, "paged" server pages, "full" whole list filtered here. seq invalidates answers to older requests.
  const catalogState = { mode: "", key: "", total: 0, page: 0, facets: [], loading: false, failed: false, seq: 0, at: 0, scrollY: null };
  const catalogSeen = new Map();   // id → latest record from any catalog answer (book page, add, read online)
  const relatedCache = new Map();  // id → «کتاب‌های مشابه» of that book
  let catalogFull = null;          // the whole list, when one was fetched (old server, or a deep link to an unloaded book)
  const detailChecked = new Set(); // book ids looked up in the whole list (deep link) and not found
  let detailFetching = 0;
  function resetCatalog() {
    catalogBooks = []; catalogLoaded = false; catalogFull = null;
    Object.assign(catalogState, { mode: "", key: "", total: 0, page: 0, facets: [], loading: false, failed: false, at: 0, scrollY: null });
    catalogState.seq++;
    catalogSeen.clear(); relatedCache.clear(); detailChecked.clear(); detailFetching = 0;
    catalogFilter.cat = ""; setCategories([]); cats.loaded = false; cats.seq++; cats.pending = null;
  }

  // ---------- v8.10 categories: a fixed two-level list (category → subcategory) kept by the admin ----------
  // GET categories → {categories: [{id, name, parent_id, sort, count, count_all (admins)}]}, flat: each top-level category
  // followed by its children; a top-level count includes its children's books. Books carry `categories: [{id, name,
  // parent_id, parent_name}]`. An empty list (or a server without the route) turns every category UI off: the old
  // free-text category flows stay as they were.
  const cats = { list: [], byId: new Map(), tops: [], kids: new Map(), loaded: false, supported: true, at: 0, pending: null, seq: 0 };
  const CATS_TTL = 5 * 60 * 1000;
  const catTreeOn = () => cats.list.length > 0;
  function setCategories(list) {
    const raw = (Array.isArray(list) ? list : []).filter(c => c && c.id != null && String(c.name || "").trim());
    const top = raw.find(c => !Number(c.parent_id));
    cats.list = raw.map(c => ({ ...c, id: Number(c.id), name: String(c.name).trim(), parent_id: Number(c.parent_id) || null, count: Number(c.count) || 0,
                                ...(c.count_all != null ? { count_all: Number(c.count_all) || 0 } : {}) }));
    cats.byId = new Map(cats.list.map(c => [c.id, c]));
    cats.tops = cats.list.filter(c => !c.parent_id);
    cats.kids = new Map(cats.tops.map(t => [t.id, []]));
    for (const c of cats.list) if (c.parent_id && cats.kids.has(c.parent_id)) cats.kids.get(c.parent_id).push(c);
    cats.loaded = true; cats.at = Date.now();
  }
  /** The list, cached for 5 minutes (force: after an admin change). Never throws; a 404 means "no categories here". */
  function loadCategories(force) {
    if (!account) return Promise.resolve(cats);
    if (!force && cats.loaded && Date.now() - cats.at < CATS_TTL) return Promise.resolve(cats);
    if (cats.pending && !force) return cats.pending;
    const seq = ++cats.seq;
    const p = (async () => {
      try {
        const r = await api("categories");
        if (seq === cats.seq) { setCategories(r && r.categories); cats.supported = true; }
      } catch (err) {
        if (seq === cats.seq && err instanceof ApiFail && err.status === 404) { setCategories([]); cats.supported = false; }
        else if (seq === cats.seq) console.warn("Categories failed:", err);
      } finally { if (cats.pending === p) cats.pending = null; }
      if (seq === cats.seq) onCategoriesChanged();
      return cats;
    })();
    cats.pending = p;
    return p;
  }
  /** Everything that draws categories redraws itself (catalog navigator, book page, admin tree/filters/details). */
  function onCategoriesChanged() {
    if (!$("viewCatalog").hidden) renderCatalogFilters();
    if (!$("viewBook").hidden && catalogSeen.has(detailId)) renderBookDetail();
    if (!$("viewAdmin").hidden) admCatsChanged();
  }
  const catName = id => (cats.byId.get(Number(id)) || {}).name || "";
  const bookCats = b => (b && Array.isArray(b.categories) ? b.categories.filter(c => c && c.id != null).map(c => ({ ...c, id: Number(c.id), parent_id: Number(c.parent_id) || null })) : []);
  /** «ادبیات › شعر» for a subcategory, «ادبیات» for a top-level one (object from a book, or an id from the list). */
  function catPathOf(c) {
    if (c == null || c === "") return "";
    if (typeof c !== "object") c = cats.byId.get(Number(c)) || { id: Number(c), name: "" };
    const parent = c.parent_id ? (c.parent_name || catName(c.parent_id)) : "";
    return parent ? `${parent} › ${c.name}` : String(c.name || "");
  }
  const catObj = id => { const c = cats.byId.get(Number(id)); return c ? { id: c.id, name: c.name, parent_id: c.parent_id, parent_name: c.parent_id ? catName(c.parent_id) : null } : null; };
  const bookInCat = (b, id) => { id = Number(id); return bookCats(b).some(c => c.id === id || c.parent_id === id); };
  const catCount = c => (c.count_all != null && isAdmin() ? c.count_all : c.count);

  /** Multi-select (or single) category picker: chips with ×, a search box, options grouped by parent, max entries. */
  function makeCatPicker(root, { max = 10, single = false, label = "دسته‌ها" } = {}) {
    root.classList.add("cat-picker");
    root.innerHTML = `<div class="cp-chips"></div><input type="search" class="admin-input cp-search" placeholder="جستجوی دسته…" aria-label="جستجوی ${esc(label)}" autocomplete="off" enterkeyhint="done">`
      + `<p class="cp-note small" aria-live="polite"></p><div class="cp-list" role="listbox" aria-label="${esc(label)}"${single ? "" : ' aria-multiselectable="true"'}></div>`;
    const chips = root.querySelector(".cp-chips"), search = root.querySelector(".cp-search"), note = root.querySelector(".cp-note"), list = root.querySelector(".cp-list");
    const st = { sel: [], q: "", off: false };
    function render() {
      const focused = document.activeElement && root.contains(document.activeElement) ? document.activeElement.dataset.cp : null;
      chips.innerHTML = st.sel.map(id => `<span class="cp-chip" dir="auto">${esc(catPathOf(id))}<button type="button" data-cp-del="${id}" aria-label="برداشتن «${esc(catPathOf(id))}»"${st.off ? " disabled" : ""}>×</button></span>`).join("")
        || `<span class="cp-none muted small">${single ? "هنوز دسته‌ای انتخاب نشده" : "بدون دسته"}</span>`;
      const q = libFold(st.q).key, hit = c => !q || libFold(c.name).key.includes(q);
      const full = !single && st.sel.length >= max;
      const opt = (c, sub) => {
        const on = st.sel.includes(c.id);
        return `<button type="button" role="option" class="cp-opt ${sub ? "cp-sub" : "cp-top"}" data-cp="${c.id}" aria-selected="${on}"${(!on && full) || st.off ? " disabled" : ""}>`
          + `<span class="cp-check" aria-hidden="true"></span><span class="cp-name" dir="auto">${esc(c.name)}</span></button>`;
      };
      let html = "";
      for (const t of cats.tops) {
        const kids = (cats.kids.get(t.id) || []).filter(k => hit(k) || hit(t));
        if (!hit(t) && !kids.length) continue;
        html += `<div class="cp-group" role="group" aria-label="${esc(t.name)}">${opt(t, false)}${kids.map(k => opt(k, true)).join("")}</div>`;
      }
      list.innerHTML = html || `<p class="cp-empty muted small">${cats.list.length ? "دسته‌ای با این نام نیست." : "هنوز دسته‌ای تعریف نشده است."}</p>`;
      note.textContent = full ? `حداکثر ${faNum(max)} دسته؛ برای افزودن، یکی را بردار.` : "";
      if (focused) { const f = list.querySelector(`[data-cp="${focused}"]`); if (f && !f.disabled) f.focus({ preventScroll: true }); }
    }
    function toggle(id) {
      if (st.off) return;
      if (single) st.sel = [id];
      else if (st.sel.includes(id)) st.sel = st.sel.filter(x => x !== id);
      else if (st.sel.length < max) st.sel = [...st.sel, id];
      render();
    }
    list.addEventListener("click", e => { const b = e.target.closest("[data-cp]"); if (b && !b.disabled) toggle(Number(b.dataset.cp)); });
    chips.addEventListener("click", e => {
      const b = e.target.closest("[data-cp-del]");
      if (!b || st.off) return;
      st.sel = st.sel.filter(x => x !== Number(b.dataset.cpDel)); render(); search.focus({ preventScroll: true });
    });
    search.addEventListener("input", () => { st.q = search.value; render(); });
    search.addEventListener("keydown", e => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const first = list.querySelector('[data-cp][aria-selected="false"]:not([disabled])');
      if (first) { toggle(Number(first.dataset.cp)); search.select(); }
    });
    render();
    return {
      get: () => [...st.sel],
      set(ids) {
        const seen = new Set();
        st.sel = (ids || []).map(Number).filter(id => cats.byId.has(id) && !seen.has(id) && seen.add(id)).slice(0, single ? 1 : max);
        st.q = ""; search.value = ""; render();
      },
      disable(on) { st.off = !!on; search.disabled = !!on; render(); },
      render, focus: () => search.focus(),
    };
  }
  let detailId = null;
  let previewCache = null; // { id, blob }: the last catalog file fetched for online reading, reused when it is added
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const LANG_FA = { fa: "فارسی", per: "فارسی", fas: "فارسی", prs: "دری", en: "انگلیسی", eng: "انگلیسی", ar: "عربی", ara: "عربی", fr: "فرانسوی",
    de: "آلمانی", tr: "ترکی", ku: "کردی", ckb: "کردی", ur: "اردو", ru: "روسی", es: "اسپانیایی", it: "ایتالیایی", tg: "تاجیکی", az: "آذری" };
  function langName(code) {
    const c = String(code || "").trim();
    return c ? (LANG_FA[c.toLowerCase().split(/[-_]/)[0]] || c) : "";
  }
  const faDigits = t => String(t).replace(/\d/g, d => "۰۱۲۳۴۵۶۷۸۹"[d]);
  const sizeMb = n => Number(n || 0) < 1048576 ? `${Math.max(1, Math.round(Number(n || 0) / 1024)).toLocaleString("fa-IR")} کیلوبایت`
    : `${(Number(n) / 1048576).toLocaleString("fa-IR", { maximumFractionDigits: 1 })} مگابایت`;
  const catOf = b => String((b && b.category) || "").trim();
  const authorOf = b => String((b && b.author) || "").trim();
  function catalogCover(b, lazy) {
    return b.has_cover ? `<img alt="" ${lazy ? 'loading="lazy"' : ""} src="${API}?r=book-cover&id=${b.id}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">`
      : `<div style="position:relative"><div class="cover-symbol">✳</div><div class="cover-name">${esc(b.title)}</div></div>`;
  }
  function catalogCardHtml(b, withAction) {
    const [bg, fg] = paletteFor(b.title || String(b.id));
    const action = !withAction ? "" : b.in_library ? '<span class="book-badge">در کتابخانهٔ تو</span>'
      : `<button class="secondary-button" type="button" data-catalog-add="${b.id}">＋ افزودن به کتابخانهٔ من</button>`;
    return `<article class="book-card catalog-card" data-detail="${b.id}" tabindex="0" role="button" aria-label="جزئیات ${esc(b.title)}"><div class="book-cover-thumb" style="background:${bg};color:${fg}">${catalogCover(b, true)}${b.format === "pdf" ? '<span class="format-badge" aria-label="PDF">PDF</span>' : ""}</div>
      <div class="book-meta"><h3 title="${esc(b.title)}">${esc(b.title)}</h3><p>${esc(b.author || "نویسنده نامشخص")}</p>
      ${withAction && b.description ? `<p title="${esc(b.description)}">${esc(b.description)}</p>` : ""}${action ? `<div class="catalog-actions">${action}</div>` : ""}</div></article>`;
  }
  const rememberCatalog = books => { for (const b of books) catalogSeen.set(b.id, b); return books; };
  const catalogSearchText = () => $("catalogSearch").value.trim();
  function catalogQueryKey() {
    const f = catalogFilter;
    return JSON.stringify([catalogSearchText(), f.author, f.category, f.cat, f.format, f.sort]);
  }
  function catalogQuery(page, per = CATALOG_PER, params = null) {
    const f = catalogFilter;
    const p = params || { q: catalogSearchText(), category: f.category, cat: f.cat, author: f.author, format: f.format, sort: f.sort === "new" ? "" : f.sort };
    return `&page=${page}&per=${per}` + Object.entries(p).filter(([, v]) => v).map(([k, v]) => `&${k}=${encodeURIComponent(v)}`).join("");
  }
  const catalogFiltered = () => !!(catalogSearchText() || catalogFilter.author || catalogFilter.category || catalogFilter.cat || catalogFilter.format);
  /** (Re)load the first page for the current search and filters (or the whole list from a server without paging). */
  async function loadCatalog() {
    const seq = ++catalogState.seq, key = catalogQueryKey();
    const paged = catalogState.mode !== "full";
    catalogState.loading = true; catalogState.failed = false; catalogState.scrollY = null;
    if (!catalogBooks.length) $("catalogGrid").innerHTML = '<p class="muted">در حال بارگذاری…</p>';
    $("catalogGrid").classList.add("is-loading"); $("catalogGrid").setAttribute("aria-busy", "true");
    renderCatalogMore();
    try {
      const r = await api("catalog", { query: paged ? catalogQuery(1) : "" });
      if (seq !== catalogState.seq) return;
      const books = rememberCatalog(r.books || []);
      if (paged && typeof r.total === "number") {
        const seen = new Set();
        catalogBooks = books.filter(b => !seen.has(b.id) && seen.add(b.id));
        Object.assign(catalogState, { mode: "paged", total: Math.max(r.total, catalogBooks.length), page: 1,
          facets: Array.isArray(r.categories) ? r.categories : [] });
      } else {
        // No `total`: this server has no paging and sent the whole list (params ignored).
        catalogState.mode = "full"; catalogBooks = books; catalogFull = books;
      }
      catalogState.key = key; catalogState.at = Date.now(); catalogLoaded = true;
      catalogState.loading = false;
      renderCatalog();
      if (!$("viewBook").hidden) renderBookDetail();
    } catch (err) {
      if (seq !== catalogState.seq) return;
      catalogState.loading = false;
      $("catalogGrid").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`;
      if (!$("viewBook").hidden && !catalogSeen.has(detailId)) $("detailTitle").textContent = apiMessage(err);
    } finally {
      if (seq === catalogState.seq) {
        $("catalogGrid").classList.remove("is-loading"); $("catalogGrid").removeAttribute("aria-busy");
        renderCatalogMore(); queueCatalogCheck();
      }
    }
  }
  /** Next page, appended (cards already shown stay in place, so the scroll position does not move). */
  async function loadMoreCatalog() {
    const st = catalogState;
    if (st.mode !== "paged" || st.loading || !catalogLoaded || catalogBooks.length >= st.total) return;
    const seq = st.seq;
    st.loading = true; st.failed = false; renderCatalogMore();
    try {
      const r = await api("catalog", { query: catalogQuery(st.page + 1) });
      if (seq !== st.seq) return;
      const got = r.books || [];
      const have = new Set(catalogBooks.map(b => b.id));
      const fresh = rememberCatalog(got.filter(b => !have.has(b.id) && have.add(b.id)));
      st.page += 1;
      if (typeof r.total === "number") st.total = r.total;
      if (!got.length) st.total = catalogBooks.length; // past the end (books were removed meanwhile): stop here
      catalogBooks.push(...fresh);
      st.total = Math.max(st.total, catalogBooks.length);
      if (fresh.length) {
        if (!$("catalogGrid").querySelector("[data-detail]")) $("catalogGrid").innerHTML = "";
        $("catalogGrid").insertAdjacentHTML("beforeend", fresh.map(b => catalogCardHtml(b, true)).join(""));
      }
      renderCatalogCount();
    } catch (err) {
      if (seq !== st.seq) return;
      st.failed = true; // no automatic retry loop: the «بیشتر» button retries
      showToast(apiMessage(err));
    } finally {
      if (seq === st.seq) { st.loading = false; renderCatalogMore(); queueCatalogCheck(); }
    }
  }
  function catalogHasMore() {
    return catalogState.mode === "paged" && catalogLoaded && catalogBooks.length < catalogState.total;
  }
  function renderCatalogMore() {
    const more = catalogHasMore();
    $("catalogMore").hidden = !more;
    const btn = $("catalogMoreButton");
    const busy = more && catalogState.loading;
    btn.disabled = busy;
    btn.textContent = busy ? "در حال بارگذاری…" : catalogState.failed ? "تلاش دوباره" : "بیشتر";
  }
  // Infinite scroll: the «بیشتر» row is the sentinel; the button stays as a fallback (no IntersectionObserver, keyboard).
  function catalogNearEnd() {
    if ($("viewCatalog").hidden || $("catalogMore").hidden) return false;
    return $("catalogMore").getBoundingClientRect().top < window.innerHeight + 600;
  }
  function queueCatalogCheck() {
    requestAnimationFrame(() => { if (!catalogState.failed && catalogNearEnd()) loadMoreCatalog(); });
  }
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting) && !catalogState.failed) loadMoreCatalog();
    }, { rootMargin: "0px 0px 600px 0px" }).observe($("catalogMore"));
  }
  $("catalogMoreButton").addEventListener("click", () => { catalogState.failed = false; loadMoreCatalog(); });
  /** Show the catalog view: from memory when the same list was loaded recently (back from a book page), else load. */
  function showCatalog() {
    const st = catalogState;
    loadCategories();
    const fresh = catalogLoaded && Date.now() - st.at < CATALOG_TTL && (st.mode === "full" || st.key === catalogQueryKey());
    renderCatalog();
    if (!fresh) { loadCatalog(); return; }
    const y = st.scrollY;
    st.scrollY = null;
    if (y != null) {
      window.scrollTo(0, y);
      requestAnimationFrame(() => { window.scrollTo(0, y); queueCatalogCheck(); });
    } else queueCatalogCheck();
  }
  function catalogCategories() {
    if (catalogState.mode === "paged") {
      return catalogState.facets.map(c => String((c && c.name) || "").trim()).filter(Boolean).sort((a, b) => a.localeCompare(b, "fa"));
    }
    return [...new Set((catalogFull || catalogBooks).map(catOf).filter(Boolean))].sort((a, b) => a.localeCompare(b, "fa"));
  }
  function fillCategoryList(extra) {
    const cats = new Set([...catalogCategories(), ...(extra || [])].filter(Boolean));
    $("categoryList").innerHTML = [...cats].map(c => `<option value="${esc(c)}"></option>`).join("");
  }
  // v8.10: the flat category chips gave way to the category navigator (renderCatNav); the author chip and the old
  // free-text category (from a book page) stay as removable chips.
  function renderCatalogFilters() {
    const chips = [];
    if (catalogFilter.author) {
      chips.push(`<button type="button" class="filter-chip-active" data-clear-author aria-label="برداشتن فیلتر نویسنده ${esc(catalogFilter.author)}">نویسنده: ${esc(catalogFilter.author)} <span aria-hidden="true">✕</span></button>`);
    }
    if (catalogFilter.category) {
      chips.push(`<button type="button" class="filter-chip-active" data-clear-category aria-label="برداشتن فیلتر دستهٔ ${esc(catalogFilter.category)}">دسته: ${esc(catalogFilter.category)} <span aria-hidden="true">✕</span></button>`);
    }
    $("catalogFilters").innerHTML = chips.join("");
    $("catalogFilters").hidden = !chips.length;
    renderCatNav();
  }
  /** Old servers (whole list): filter and sort here. Paged servers already did it. */
  function visibleCatalogBooks() {
    if (catalogState.mode === "paged") return catalogBooks;
    const q = catalogSearchText().toLocaleLowerCase(), f = catalogFilter;
    const books = catalogBooks.filter(b => !q || `${b.title} ${b.author} ${b.description || ""} ${b.submitter || ""} ${b.category || ""} ${b.publisher || ""}`.toLocaleLowerCase().includes(q))
      .filter(b => !f.author || authorOf(b) === f.author)
      .filter(b => !f.category || catOf(b) === f.category)
      .filter(b => !f.cat || bookInCat(b, f.cat))
      .filter(b => !f.format || (b.format === "pdf" ? "pdf" : "epub") === f.format);
    if (f.sort === "title") books.sort((a, b) => String(a.title || "").localeCompare(String(b.title || ""), "fa"));
    if (f.sort === "author") books.sort((a, b) => authorOf(a).localeCompare(authorOf(b), "fa") || String(a.title || "").localeCompare(String(b.title || ""), "fa"));
    return books;
  }
  function renderCatalogCount() {
    const n = catalogState.mode === "paged" ? catalogState.total : visibleCatalogBooks().length;
    $("catalogCount").textContent = catalogLoaded ? `${n.toLocaleString("fa-IR")} کتاب` : "";
  }
  function renderCatalogControls() {
    $("catalogFormat").querySelectorAll("[data-format]").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.format === catalogFilter.format)));
    $("catalogSort").value = catalogFilter.sort;
  }
  function renderCatalog() {
    const books = visibleCatalogBooks();
    renderCatalogFilters();
    renderCatalogControls();
    fillCategoryList();
    renderCatalogCount();
    const nothingAtAll = catalogLoaded && (catalogState.mode === "paged" ? catalogState.total === 0 && !catalogFiltered() : catalogBooks.length === 0);
    $("catalogEmpty").hidden = !nothingAtAll;
    if (catalogLoaded || catalogBooks.length) $("catalogGrid").innerHTML = books.map(b => catalogCardHtml(b, true)).join("");
    if (catalogLoaded && !books.length && !nothingAtAll) {
      $("catalogGrid").innerHTML = '<div class="catalog-noresult"><p class="muted">کتابی با این مشخصات پیدا نشد.</p>'
        + '<button type="button" class="secondary-button" data-catalog-reset>پاک کردن جست‌وجو و فیلترها</button></div>';
    }
    renderCatalogMore();
  }
  /** Search/filters changed: paged servers get a new first page, the whole list is filtered here at once. */
  function applyCatalogFilters() {
    if (catalogState.mode === "full") { renderCatalog(); return; }
    renderCatalogFilters(); renderCatalogControls();
    loadCatalog();
  }
  let catalogSearchTimer = 0;
  $("catalogSearch").addEventListener("input", () => {
    clearTimeout(catalogSearchTimer);
    if (catalogState.mode === "full") { renderCatalog(); return; }
    catalogSearchTimer = setTimeout(applyCatalogFilters, 300);
  });
  $("catalogFilters").addEventListener("click", e => {
    if (e.target.closest("[data-clear-author]")) { catalogFilter.author = ""; applyCatalogFilters(); return; }
    if (e.target.closest("[data-clear-category]")) { catalogFilter.category = ""; applyCatalogFilters(); }
  });
  $("catalogFormat").addEventListener("click", e => {
    const t = e.target.closest("[data-format]");
    if (!t || t.dataset.format === catalogFilter.format) return;
    catalogFilter.format = t.dataset.format;
    applyCatalogFilters();
  });
  $("catalogSort").addEventListener("change", e => { catalogFilter.sort = e.target.value; applyCatalogFilters(); });
  /** The book was added to the user's library: update every copy in memory (no reload, the loaded pages stay). */
  function markCatalogInLibrary(id) {
    const lists = [catalogBooks, catalogFull || [], [...catalogSeen.values()], ...relatedCache.values()];
    for (const list of lists) for (const b of list) if (b.id === id) b.in_library = true;
    const card = $("catalogGrid").querySelector(`[data-catalog-add="${id}"]`);
    if (card) card.outerHTML = '<span class="book-badge">در کتابخانهٔ تو</span>';
  }

  // ---- v8.10 category navigator: a list of top categories with a subcategory panel (≥ 900 px), a drill-down sheet on
  // smaller screens, and a breadcrumb above the grid. The choice lives in the hash (#catalog?cat=ID): back, refresh and
  // shared links keep it. ----
  const catalogHash = () => `#catalog${catalogFilter.cat ? `?cat=${catalogFilter.cat}` : ""}`;
  function catFromHash() {
    const m = /^#catalog(?:\?(.*))?$/.exec(location.hash);
    const v = m ? new URLSearchParams(m[1] || "").get("cat") || "" : "";
    return /^\d{1,9}$/.test(v) ? String(Number(v)) : "";
  }
  let catNavOpenId = 0, catNavTimer = 0, catSheetLevel = 0;
  const finePointer = () => !!(window.matchMedia && matchMedia("(hover: hover) and (pointer: fine)").matches);
  function renderCatNav() {
    const on = catTreeOn(), sel = catalogFilter.cat ? Number(catalogFilter.cat) : 0;
    const cur = sel ? cats.byId.get(sel) : null, curTop = cur ? cur.parent_id || cur.id : 0;
    $("catalogLayout").classList.toggle("has-cat-nav", on);
    $("catNav").hidden = !on;
    $("catSheetOpen").hidden = !on;
    $("catSheetOpenLabel").textContent = cur ? `دسته: ${cur.name}` : "دسته‌ها";
    $("catSheetOpen").classList.toggle("active", !!cur);
    if (on) {
      $("catNavList").innerHTML = `<li><button type="button" class="cat-nav-item cat-nav-all" data-cat-pick=""${sel ? "" : ' aria-current="true"'}><span class="cat-nav-name">همهٔ کتاب‌ها</span></button></li>`
        + cats.tops.map(t => {
          const kids = (cats.kids.get(t.id) || []).length;
          const attrs = kids ? `data-cat-open="${t.id}" aria-haspopup="true" aria-expanded="false" aria-controls="catNavPanel"` : `data-cat-pick="${t.id}"`;
          return `<li data-cat-top="${t.id}"><button type="button" class="cat-nav-item" ${attrs}${curTop === t.id ? ' aria-current="true"' : ""}>`
            + `<span class="cat-nav-name" dir="auto">${esc(t.name)}</span><span class="cat-count">${faNum(t.count)}</span>`
            + `${kids ? '<span class="cat-chev" aria-hidden="true">›</span>' : ""}</button></li>`;
        }).join("");
      if (catNavOpenId && cats.kids.has(catNavOpenId)) openCatPanel(catNavOpenId); else closeCatPanel();
    } else closeCatPanel();
    const crumbs = $("catCrumbs");
    if (sel) {
      const top = cur && cur.parent_id ? cats.byId.get(cur.parent_id) : null;
      const sep = '<span class="crumb-sep" aria-hidden="true">›</span>';
      crumbs.innerHTML = `<button type="button" class="crumb" data-cat-pick="">کتابخانهٔ عمومی</button>${sep}`
        + (top ? `<button type="button" class="crumb" data-cat-pick="${top.id}" dir="auto">${esc(top.name)}</button>${sep}` : "")
        + `<span class="crumb-current" aria-current="page" dir="auto">${esc(cur ? cur.name : cats.loaded ? "دستهٔ نامشخص" : "…")}</span>`
        + `<button type="button" class="cat-clear" data-cat-pick="" aria-label="برداشتن فیلتر دسته"><span aria-hidden="true">✕</span> همهٔ دسته‌ها</button>`;
    } else crumbs.innerHTML = "";
    crumbs.hidden = !sel;
  }
  function openCatPanel(id, focus) {
    const t = cats.byId.get(Number(id));
    const kids = t ? cats.kids.get(t.id) || [] : [];
    if (!t || !kids.length) { closeCatPanel(); return; }
    clearTimeout(catNavTimer);
    catNavOpenId = t.id;
    const sel = catalogFilter.cat;
    const p = $("catNavPanel");
    p.innerHTML = `<button type="button" class="cat-panel-all" data-cat-pick="${t.id}"${sel === String(t.id) ? ' aria-current="true"' : ""}>همهٔ کتاب‌های ${esc(t.name)} <span aria-hidden="true">›</span></button>`
      + `<ul class="cat-panel-list" aria-label="زیردسته‌های ${esc(t.name)}">${kids.map(k => `<li><button type="button" class="cat-panel-item" data-cat-pick="${k.id}"${sel === String(k.id) ? ' aria-current="true"' : ""}>`
      + `<span dir="auto">${esc(k.name)}</span><span class="cat-count">${faNum(k.count)}</span></button></li>`).join("")}</ul>`;
    p.hidden = false;
    $("catNavList").querySelectorAll("[data-cat-open]").forEach(b => {
      const open = Number(b.dataset.catOpen) === t.id;
      b.setAttribute("aria-expanded", String(open)); b.classList.toggle("open", open);
    });
    if (focus) p.querySelector("button").focus({ preventScroll: true });
  }
  function closeCatPanel() {
    clearTimeout(catNavTimer);
    catNavOpenId = 0;
    $("catNavPanel").hidden = true;
    $("catNavList").querySelectorAll("[data-cat-open]").forEach(b => { b.setAttribute("aria-expanded", "false"); b.classList.remove("open"); });
  }
  /** Filter the catalog by a category ("" = all). A new history entry, so back returns to the previous choice. */
  function chooseCatalogCat(id) {
    const v = id ? String(Number(id)) : "";
    closeCatPanel();
    if ($("catSheet").open) $("catSheet").close();
    if (v === catalogFilter.cat && !catalogFilter.category) return;
    catalogFilter.cat = v; catalogFilter.category = "";
    setHash(catalogHash(), true, { borg: "catalog" });
    applyCatalogFilters();
    const top = $("viewCatalog").getBoundingClientRect().top;
    if (top < 0) window.scrollBy({ top: top - 10, behavior: "auto" });
  }
  $("viewCatalog").addEventListener("click", e => {
    const pick = e.target.closest("[data-cat-pick]");
    if (pick) { chooseCatalogCat(pick.dataset.catPick); return; }
    const open = e.target.closest("[data-cat-open]");
    if (open) {
      const id = Number(open.dataset.catOpen);
      if (catNavOpenId === id && !finePointer()) closeCatPanel();
      else openCatPanel(id, e.detail === 0);
    }
  });
  $("catNavList").addEventListener("mouseover", e => {
    if (!finePointer()) return;
    const li = e.target.closest("[data-cat-top]");
    clearTimeout(catNavTimer);
    if (!li) return;
    if (li.querySelector("[data-cat-open]")) openCatPanel(li.dataset.catTop); else closeCatPanel();
  });
  $("catNav").addEventListener("mouseenter", () => clearTimeout(catNavTimer));
  $("catNav").addEventListener("mouseleave", () => {
    if (!finePointer() || !catNavOpenId || $("catNav").contains(document.activeElement)) return;
    catNavTimer = setTimeout(closeCatPanel, 250);
  });
  $("catNav").addEventListener("focusout", e => { if (catNavOpenId && !$("catNav").contains(e.relatedTarget)) catNavTimer = setTimeout(closeCatPanel, 250); });
  document.addEventListener("click", e => { if (catNavOpenId && !$("catNav").contains(e.target)) closeCatPanel(); });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || !catNavOpenId) return;
    const id = catNavOpenId;
    closeCatPanel();
    const b = $("catNavList").querySelector(`[data-cat-open="${id}"]`);
    if (b) b.focus({ preventScroll: true });
  });
  // Smaller screens: «دسته‌ها» opens a sheet with a drill-down (top level → its subcategories, back, «همه» at each level).
  function renderCatSheet() {
    const t = catSheetLevel ? cats.byId.get(catSheetLevel) : null, sel = catalogFilter.cat;
    $("catSheetTitle").textContent = t ? t.name : "دسته‌ها";
    $("catSheetBack").hidden = !t;
    const row = (attr, id, label, count, cls, drill) => `<li><button type="button" class="cat-sheet-item${cls ? " " + cls : ""}" ${attr}="${id}"`
      + `${!drill && sel === String(id) ? ' aria-current="true"' : ""}><span class="cat-nav-name" dir="auto">${esc(label)}</span>`
      + `${count != null ? `<span class="cat-count">${faNum(count)}</span>` : ""}${drill ? '<span class="cat-chev" aria-hidden="true">›</span>' : ""}</button></li>`;
    $("catSheetList").innerHTML = t
      ? row("data-cat-pick", t.id, `همهٔ کتاب‌های ${t.name}`, t.count, "cat-sheet-all") + (cats.kids.get(t.id) || []).map(k => row("data-cat-pick", k.id, k.name, k.count)).join("")
      : row("data-cat-pick", "", "همهٔ کتاب‌ها", null, "cat-sheet-all") + cats.tops.map(x => {
        const drill = (cats.kids.get(x.id) || []).length > 0;
        return row(drill ? "data-cat-drill" : "data-cat-pick", x.id, x.name, x.count, "", drill);
      }).join("");
  }
  function openCatSheet() {
    const cur = catalogFilter.cat ? cats.byId.get(Number(catalogFilter.cat)) : null;
    catSheetLevel = cur ? cur.parent_id || ((cats.kids.get(cur.id) || []).length ? cur.id : 0) : 0;
    renderCatSheet();
    $("catSheet").showModal();
    const f = $("catSheetList").querySelector('[aria-current="true"]') || $("catSheetList").querySelector("button");
    if (f) f.focus({ preventScroll: true });
  }
  $("catSheetOpen").addEventListener("click", openCatSheet);
  $("catSheetClose").addEventListener("click", () => $("catSheet").close());
  $("catSheetBack").addEventListener("click", () => {
    const from = catSheetLevel;
    catSheetLevel = 0; renderCatSheet();
    const b = $("catSheetList").querySelector(`[data-cat-drill="${from}"]`) || $("catSheetList").querySelector("button");
    if (b) b.focus({ preventScroll: true });
  });
  $("catSheetList").addEventListener("click", e => {
    const d = e.target.closest("[data-cat-drill]");
    if (d) { catSheetLevel = Number(d.dataset.catDrill); renderCatSheet(); const f = $("catSheetList").querySelector("button"); if (f) f.focus({ preventScroll: true }); return; }
    const p = e.target.closest("[data-cat-pick]");
    if (p) chooseCatalogCat(p.dataset.catPick);
  });
  $("catSheet").addEventListener("click", e => { if (e.target === $("catSheet")) $("catSheet").close(); }); // backdrop
  matchMedia("(min-width: 900px)").addEventListener("change", e => { if (e.matches && $("catSheet").open) $("catSheet").close(); else if (!e.matches) closeCatPanel(); });

  // ---- Navigation between the catalog and a book's page (deep-linkable: #catalog, #catalog?cat=ID, #catalog/book/ID) ----
  function setHash(hash, push, state) {
    const url = hash ? hash : location.pathname + location.search;
    try { (push ? history.pushState : history.replaceState).call(history, state || null, "", url); } catch (_) {}
  }
  function openCatalogBook(id, push = true) {
    if (!$("viewCatalog").hidden) catalogState.scrollY = window.scrollY; // restored when coming back (showCatalog)
    detailId = id;
    setHash(`#catalog/book/${id}`, push, { borg: "book", id, inApp: push });
    showView("book");
    window.scrollTo({ top: Math.max(0, $("app").querySelector(".library-section").offsetTop - 10), behavior: "auto" });
  }
  function goToCatalog(push) {
    setHash(catalogHash(), push, { borg: "catalog" });
    showView("catalog");
  }
  function filterCatalogBy(kind, value) {
    catalogFilter.author = kind === "author" ? value : "";
    catalogFilter.category = kind === "category" ? value : "";
    catalogFilter.cat = kind === "cat" ? String(value) : "";
    $("catalogSearch").value = "";
    goToCatalog(true);
  }
  function routeFromHash() {
    if (!account) return;
    if (readerOpen() && currentBook && currentBook.preview) closeReader();
    const m = /^#catalog\/book\/(\d+)/.exec(location.hash);
    if (m) { detailId = Number(m[1]); showView("book"); }
    else if (location.hash.startsWith("#catalog")) {
      const c = catFromHash();
      if (c !== catalogFilter.cat) { catalogFilter.cat = c; if (c) catalogFilter.category = ""; }
      showView("catalog");
    }
    else if (location.hash.startsWith("#admin")) { if (isAdmin()) showView("admin"); }
    else if (!$("viewAdmin").hidden) showView("mine");
    else if (!$("viewBook").hidden || !$("viewCatalog").hidden) showView("mine");
  }
  // The app restores the catalog position itself (showCatalog); the browser's own restore fired after it and moved the list.
  try { history.scrollRestoration = "manual"; } catch (_) {}
  window.addEventListener("popstate", routeFromHash);
  $("detailBack").addEventListener("click", () => {
    if (history.state && history.state.borg === "book" && history.state.inApp) history.back();
    else goToCatalog(false);
  });
  $("detailCrumbs").addEventListener("click", e => {
    const t = e.target.closest("[data-crumb]");
    if (!t) return;
    if (t.dataset.crumb === "catalog") { catalogFilter.author = ""; catalogFilter.category = ""; catalogFilter.cat = ""; goToCatalog(true); }
    else if (t.dataset.crumb === "cat") filterCatalogBy("cat", t.dataset.crumbCat);
    else filterCatalogBy("category", t.dataset.crumbCat || "");
  });
  $("detailAuthor").addEventListener("click", () => { const b = catalogSeen.get(detailId); if (b && authorOf(b)) filterCatalogBy("author", authorOf(b)); });
  $("detailMeta").addEventListener("click", e => {
    const c = e.target.closest("[data-meta-catid]");
    if (c) { filterCatalogBy("cat", c.dataset.metaCatid); return; }
    const t = e.target.closest("[data-meta-cat]"); if (t) filterCatalogBy("category", t.dataset.metaCat);
  });

  // ---- Book page ----
  /** What «کتاب‌های مشابه» looks for, in order: the same subcategory (v8.10), the same author, the same top category,
   *  then the old free-text category. */
  function relatedSteps(b) {
    const bc = bookCats(b);
    const subs = bc.filter(c => c.parent_id).map(c => c.id).slice(0, 2);
    const tops = [...new Set(bc.map(c => c.parent_id || c.id))].slice(0, 1);
    return [...subs.map(id => ["cat", id]), ["author", authorOf(b)], ...tops.map(id => ["cat", id]), ["category", catOf(b)]].filter(([, v]) => v);
  }
  function relatedBooks(b, list) {
    const out = [], ids = new Set([b.id]);
    for (const [k, v] of relatedSteps(b)) {
      const match = k === "cat" ? x => bookInCat(x, v) : k === "author" ? x => authorOf(x) === v : x => catOf(x) === v;
      for (const x of list) if (!ids.has(x.id) && match(x)) { ids.add(x.id); out.push(x); }
    }
    return out.slice(0, 6);
  }
  /** «کتاب‌های مشابه» without the whole list: one small page per step of relatedSteps (subcategory, author, …). */
  async function relatedFor(b) {
    if (catalogState.mode === "full" && catalogFull) return relatedBooks(b, catalogFull);
    if (relatedCache.has(b.id)) return relatedCache.get(b.id);
    const out = [], ids = new Set([b.id]);
    for (const [k, v] of relatedSteps(b)) {
      if (!v || out.length >= 6) continue;
      const r = await api("catalog", { query: catalogQuery(1, 6, { [k]: v }) });
      if (typeof r.total !== "number") { // server without paging: this was the whole list
        catalogFull = rememberCatalog(r.books || []);
        return relatedBooks(b, catalogFull);
      }
      for (const x of rememberCatalog(r.books || [])) if (!ids.has(x.id)) { ids.add(x.id); out.push(x); }
    }
    const res = out.slice(0, 6);
    relatedCache.set(b.id, res);
    return res;
  }
  /** Deep link to a book that is not in memory: `catalog?page=1&per=1&id=<id>` (v8.9 server). A server without paging
   *  (no `total` in the answer) returns the whole list, which is used as before. */
  async function fetchCatalogBook(id) {
    if (detailFetching === id) return;
    detailFetching = id;
    try {
      const r = await api("catalog", { query: `&page=1&per=1&id=${encodeURIComponent(id)}` });
      if (typeof r.total === "number") rememberCatalog(r.books || []);
      else catalogFull = rememberCatalog(r.books || []);
      if (!catalogSeen.has(id)) detailChecked.add(id);
    } catch (err) {
      if (detailId === id && !$("viewBook").hidden) $("detailTitle").textContent = apiMessage(err);
      return;
    } finally { if (detailFetching === id) detailFetching = 0; }
    if (detailId === id && !$("viewBook").hidden) renderBookDetail();
  }
  function formatPublished(v) {
    const t = String(v || "").trim();
    return t ? faDigits(t.replace(/T.*$/, "")) : "";
  }
  async function renderBookDetail() {
    closeAddMenu();
    const b = catalogSeen.get(detailId);
    $("detailBody").hidden = !b;
    if (!b) {
      $("detailCrumbs").innerHTML = '<button type="button" class="crumb" data-crumb="catalog">کتابخانهٔ عمومی</button>';
      $("detailTitle").textContent = detailChecked.has(detailId) ? "این کتاب در کتابخانهٔ عمومی پیدا نشد." : "در حال بارگذاری…";
      $("detailTitle").hidden = false;
      if (!detailChecked.has(detailId)) fetchCatalogBook(detailId);
      return;
    }
    const cat = catOf(b), bc = bookCats(b), first = bc[0];
    const sep = '<span class="crumb-sep" aria-hidden="true">/</span>';
    const firstParent = first && first.parent_id ? first.parent_name || catName(first.parent_id) : "";
    $("detailCrumbs").innerHTML = `<button type="button" class="crumb" data-crumb="catalog">کتابخانهٔ عمومی</button>${sep}`
      + (first ? (firstParent ? `<button type="button" class="crumb" data-crumb="cat" data-crumb-cat="${first.parent_id}" dir="auto">${esc(firstParent)}</button>${sep}` : "")
        + `<button type="button" class="crumb" data-crumb="cat" data-crumb-cat="${first.id}" dir="auto">${esc(first.name)}</button>${sep}`
      : cat ? `<button type="button" class="crumb" data-crumb="category" data-crumb-cat="${esc(cat)}">${esc(cat)}</button>${sep}` : "")
      + `<span class="crumb-current" aria-current="page">${esc(b.title)}</span>`;
    const [bg, fg] = paletteFor(b.title || String(b.id));
    const cover = $("detailCover");
    cover.style.background = bg; cover.style.color = fg;
    cover.innerHTML = catalogCover(b, false);
    $("detailTitle").textContent = b.title;
    $("detailAuthor").textContent = b.author || "نویسنده نامشخص";
    $("detailAuthor").disabled = !authorOf(b);
    $("detailAuthor").title = authorOf(b) ? "همهٔ کتاب‌های این نویسنده" : "";
    const desc = String(b.description || "").trim();
    $("detailDescWrap").hidden = !desc;
    $("detailDesc").textContent = desc;
    $("detailDesc").classList.add("clamped");
    $("detailMore").hidden = true;
    $("detailMore").textContent = "بیشتر بخوانید";
    $("detailMore").setAttribute("aria-expanded", "false");
    const rows = [
      bc.length ? ["دسته‌ها", `<span class="detail-cats">${bc.map(c => `<button type="button" class="link-button" data-meta-catid="${c.id}" dir="auto">${esc(catPathOf(c))}</button>`).join('<span class="detail-cat-sep">، </span>')}</span>`]
        : ["دسته‌بندی", cat ? `<button type="button" class="link-button" data-meta-cat="${esc(cat)}">${esc(cat)}</button>` : ""],
      ["زبان", esc(langName(b.language))],
      ["ناشر", esc(String(b.publisher || "").trim())],
      ["تاریخ انتشار", esc(formatPublished(b.published))],
      ["حجم فایل", b.size ? esc(sizeMb(b.size)) : ""],
      ["پیشنهاددهنده", esc(String(b.submitter || "").trim())],
    ].filter(([, v]) => v);
    $("detailMeta").innerHTML = rows.map(([k, v]) => `<tr><th scope="row">${k}</th><td>${v}</td></tr>`).join("");
    $("detailMeta").hidden = !rows.length;
    const local = b.in_library ? (await getAllBooks()).find(x => x.serverId === b.id || (b.sha256 && x.contentHash === b.sha256)) : null;
    if (detailId !== b.id) return;
    $("detailRead").textContent = local ? "📖 ادامهٔ مطالعه" : "📖 مطالعهٔ آنلاین";
    $("detailRead").dataset.local = local ? local.id : "";
    $("detailAddGroup").hidden = !!b.in_library;
    $("detailInLibrary").hidden = !b.in_library;
    $("detailAdd").disabled = false; $("detailAddMore").disabled = false;
    // Only offer "read more" when the description is really cut off (measured once the page is visible).
    requestAnimationFrame(() => {
      const d = $("detailDesc");
      $("detailMore").hidden = !desc || d.scrollHeight <= d.clientHeight + 2;
    });
    if ($("detailRelated").dataset.for !== String(b.id)) { $("detailRelatedSection").hidden = true; $("detailRelated").innerHTML = ""; }
    let related = [];
    try { related = await relatedFor(b); } catch (err) { console.warn("Similar books failed:", err); }
    if (detailId !== b.id) return;
    $("detailRelated").dataset.for = String(b.id);
    $("detailRelatedSection").hidden = !related.length;
    $("detailRelated").innerHTML = related.map(x => catalogCardHtml(x, false)).join("");
  }
  $("detailMore").addEventListener("click", () => {
    const d = $("detailDesc"), open = d.classList.toggle("clamped") === false;
    $("detailMore").textContent = open ? "بستن" : "بیشتر بخوانید";
    $("detailMore").setAttribute("aria-expanded", String(open));
  });
  $("detailRelated").addEventListener("click", e => { const c = e.target.closest("[data-detail]"); if (c) openCatalogBook(Number(c.dataset.detail)); });
  $("detailRelated").addEventListener("keydown", e => {
    const c = e.target.closest("[data-detail]");
    if (c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openCatalogBook(Number(c.dataset.detail)); }
  });

  // ---- Online reading: open the catalog file without adding it to the library ----
  async function catalogFile(id) {
    if (previewCache && previewCache.id === id) return previewCache.blob;
    const blob = await (await api("book-file", { query: `&id=${id}` })).blob();
    previewCache = { id, blob };
    return blob;
  }
  async function readOnline(id) {
    const b = catalogSeen.get(id);
    if (!b) return;
    const btn = $("detailRead");
    if (btn.dataset.local) { openReader(btn.dataset.local).catch(err => { console.error(err); reportError("reader", err, "openReader"); showToast("بازکردن کتاب ناموفق بود."); }); return; }
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "در حال دریافت…";
    try {
      const blob = await catalogFile(id);
      const isPdf = b.format === "pdf" || (b.format !== "epub" && await sniffPdf(blob));
      const file = isPdf ? new File([blob], `${b.title}.pdf`, { type: "application/pdf" }) : new File([blob], `${b.title}.epub`, { type: "application/epub+zip" });
      await openReader(null, { id: `preview-${id}`, preview: true, catalogId: id, title: b.title, author: b.author || "نویسنده نامشخص",
                               file, size: blob.size, language: b.language || "", ...(isPdf ? { format: "pdf" } : {}) });
    } catch (err) {
      console.warn("Online reading failed:", err);
      reportError("api", err, "catalog:read-online");
      showToast(err instanceof ApiFail ? apiMessage(err) : "بازکردن کتاب ناموفق بود.");
    } finally { btn.disabled = false; btn.textContent = label; }
  }
  $("detailRead").addEventListener("click", () => readOnline(detailId));
  $("previewAdd").addEventListener("click", () => {
    if (!currentBook || !currentBook.preview) return;
    $("previewAdd").disabled = true;
    addFromCatalog(currentBook.catalogId).finally(() => { $("previewAdd").disabled = false; });
  });
  /** The book being previewed was just added: keep reading it as the stored copy (progress is saved from now on). */
  async function adoptPreview(id, rec) {
    if (!rec || !currentBook || !currentBook.preview || currentBook.catalogId !== id || (!rendition && !pdf)) return;
    currentBook = rec; currentProgressKey = rec.id;
    $("previewBanner").hidden = true;
    try {
      if (pdf) await saveProgress(rec.id, `pdf:${pdf.page}`, pdfPercent(pdf));
      const loc = pdf ? null : rendition.currentLocation();
      if (loc?.start?.cfi) await saveProgress(rec.id, loc.start.cfi, percentFor(loc.start.cfi, loc.start.percentage));
      const r = await setBookMeta(rec.id, { status: "reading" });
      if (r && currentBook && currentBook.id === r.id) currentBook = r;
    } catch (err) { console.warn("Adopting the previewed book failed:", err); }
  }

  // ---- Add to my library (optionally straight onto shelves) ----
  async function addFromCatalog(id, { btn = null, shelves = [] } = {}) {
    if (btn) btn.disabled = true;
    try {
      while (syncing) await sleep(150); // a running sync must not see the new local copy before the server does
      await api("catalog-add", { method: "POST", body: { book_id: id } });
      showToast("به کتابخانه‌ات اضافه شد؛ در حال دریافت…");
      const cb = catalogSeen.get(id);
      let rec = null;
      if (cb) {
        try { rec = await downloadBook({ ...cb, catalog_status: "approved" }, previewCache && previewCache.id === id ? previewCache.blob : null); }
        catch (err) { console.warn("Download after add failed (sync will retry):", err); }
      }
      await sync();
      while (syncing) await sleep(150);
      if (!rec) rec = (await getAllBooks()).find(b => b.serverId === id) || null;
      if (rec && shelves.length) {
        const cur = await getBook(rec.id);
        rec = (await setBookMeta(rec.id, { shelves: [...new Set([...((cur && cur.shelves) || []), ...shelves])] })) || rec;
      }
      await adoptPreview(id, rec);
      if (shelves.length) showToast(rec ? "به کتابخانه و قفسه‌های انتخاب‌شده اضافه شد." : "به کتابخانه اضافه شد؛ قفسه‌ها بعد از دریافت تنظیم می‌شوند.");
      else if (rec) showToast("به کتابخانه‌ات اضافه شد.");
      renderLibrary();
      markCatalogInLibrary(id);
      // Whole-list servers: reload as before. Paged: the loaded pages (and scroll position) stay; the copies were updated.
      if (catalogState.mode === "full") await loadCatalog();
      else if (!$("viewBook").hidden && detailId === id) renderBookDetail();
    } catch (err) { if (btn) btn.disabled = false; showToast(apiMessage(err)); }
  }
  function addMenuOpen() { return !$("detailAddMenu").hidden; }
  function closeAddMenu() {
    if (!addMenuOpen()) return;
    $("detailAddMenu").hidden = true;
    $("detailAddMore").setAttribute("aria-expanded", "false");
  }
  function renderAddMenuShelves(checked) {
    $("detailShelfChecks").innerHTML = shelvesCache.length
      ? shelvesCache.map(sh => `<label><input type="checkbox" data-add-shelf="${esc(sh.uid)}" ${checked.has(sh.uid) ? "checked" : ""}> ${esc(sh.name)}</label>`).join("")
      : '<p class="muted small">هنوز قفسه‌ای نساخته‌ای؛ یکی بساز.</p>';
  }
  const checkedAddShelves = () => new Set([...$("detailShelfChecks").querySelectorAll("[data-add-shelf]:checked")].map(x => x.dataset.addShelf));
  async function openAddMenu() {
    await refreshShelves();
    renderAddMenuShelves(new Set());
    $("detailAddMenu").hidden = false;
    $("detailAddMore").setAttribute("aria-expanded", "true");
    const first = $("detailAddMenu").querySelector("input");
    if (first) first.focus({ preventScroll: true });
  }
  $("detailAdd").addEventListener("click", () => { closeAddMenu(); addFromCatalog(detailId, { btn: $("detailAdd") }); });
  $("detailAddMore").addEventListener("click", e => { e.stopPropagation(); if (addMenuOpen()) closeAddMenu(); else openAddMenu(); });
  async function addShelfFromMenu() {
    const name = $("detailNewShelf").value.trim();
    if (!name) return;
    const keep = checkedAddShelves(), uid = newUid();
    await saveShelf({ uid, name: name.slice(0, 80), position: shelvesCache.length, deleted: false });
    $("detailNewShelf").value = "";
    keep.add(uid);
    renderAddMenuShelves(keep);
  }
  $("detailNewShelfAdd").addEventListener("click", addShelfFromMenu);
  $("detailNewShelf").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); addShelfFromMenu(); } });
  $("detailAddConfirm").addEventListener("click", () => {
    const shelves = [...checkedAddShelves()];
    closeAddMenu();
    addFromCatalog(detailId, { btn: $("detailAdd"), shelves });
  });
  document.addEventListener("click", e => {
    if (addMenuOpen() && !$("detailAddMenu").contains(e.target) && !$("detailAddMore").contains(e.target)) closeAddMenu();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && addMenuOpen()) { closeAddMenu(); $("detailAddMore").focus({ preventScroll: true }); }
  });

  $("catalogGrid").addEventListener("click", e => {
    if (e.target.closest("[data-catalog-reset]")) {
      $("catalogSearch").value = "";
      const hadCat = !!catalogFilter.cat;
      Object.assign(catalogFilter, { author: "", category: "", cat: "", format: "" });
      if (hadCat) setHash(catalogHash(), true, { borg: "catalog" });
      applyCatalogFilters(); return;
    }
    const btn = e.target.closest("[data-catalog-add]");
    if (btn) { e.stopPropagation(); addFromCatalog(Number(btn.dataset.catalogAdd), { btn }); return; }
    const card = e.target.closest("[data-detail]");
    if (card) openCatalogBook(Number(card.dataset.detail));
  });
  $("catalogGrid").addEventListener("keydown", e => {
    if (e.target.closest("[data-catalog-add]")) return;
    const card = e.target.closest("[data-detail]");
    if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openCatalogBook(Number(card.dataset.detail)); }
  });
  async function shareBook(localId) {
    const b = await getBook(localId);
    if (!b || !b.serverId) { showToast("اول باید همگام‌سازی کامل شود."); return; }
    await loadCategories();
    if (catTreeOn()) { openSubmitDialog(b); return; }
    const description = (prompt("یک توضیح کوتاه برای این کتاب (اختیاری):", "") || "").slice(0, 2000);
    const category = (prompt("دسته‌بندی (اختیاری) — مثلاً رمان، شعر، تاریخ:", "") || "").replace(/\s+/g, " ").trim().slice(0, 80);
    try {
      const r = await api("catalog-submit", { method: "POST", body: { book_id: b.serverId, description, ...(category ? { category } : {}) } });
      await put("books", { ...b, catalogStatus: r.status });
      showToast(r.status === "approved" ? "به کتابخانهٔ عمومی اضافه شد." : "برای تأیید مدیر ارسال شد.");
      renderLibrary();
    } catch (err) { showToast(apiMessage(err)); }
  }

  // v8.10: with a category list, the suggestion is a small dialog: description + category picker (max 10).
  let submitBookId = null;
  const submitPicker = makeCatPicker($("submitCats"), { label: "دسته‌های کتاب" });
  function openSubmitDialog(b) {
    submitBookId = b.id;
    $("submitBook").textContent = `«${b.title}» — ${b.author || "نویسنده نامشخص"}`;
    $("submitForm").elements.description.value = "";
    submitPicker.set([]);
    $("submitOk").disabled = false;
    $("submitDialog").showModal();
    $("submitForm").elements.description.focus();
  }
  $("submitClose").addEventListener("click", () => $("submitDialog").close());
  $("submitForm").addEventListener("submit", async e => {
    e.preventDefault();
    const b = submitBookId && await getBook(submitBookId);
    if (!b || !b.serverId) { $("submitDialog").close(); return; }
    const description = $("submitForm").elements.description.value.replace(/[ \t]+/g, " ").trim().slice(0, 2000);
    $("submitOk").disabled = true;
    try {
      const r = await api("catalog-submit", { method: "POST", body: { book_id: b.serverId, description, categories: submitPicker.get() } });
      await put("books", { ...b, catalogStatus: r.status });
      $("submitDialog").close();
      showToast(r.status === "approved" ? "به کتابخانهٔ عمومی اضافه شد." : "برای تأیید مدیر ارسال شد.");
      renderLibrary();
    } catch (err) { showToast(apiMessage(err)); }
    finally { $("submitOk").disabled = false; }
  });

  // ---------- Admin (v8.9: sub-tabs, paged books table, bulk actions, review queue) ----------
  // Tabs deep-link as #admin/<tab>; the books tab keeps its filters in the hash query (#admin/books?status=pending&page=2).
  // Only the active tab's data is fetched; admin-stats (small) feeds the overview cards and the tab badges.
  const fmtDate = s => new Date(s * 1000).toLocaleDateString("fa-IR");
  const mb = n => (n / 1048576).toLocaleString("fa-IR", { maximumFractionDigits: 1 });
  const STATUS_FA = { approved: "عمومی", pending: "در انتظار", rejected: "ردشده", none: "بیرون از کتابخانهٔ عمومی" };
  const ADMIN_TABS = ["overview", "queue", "books", "cats", "users", "invites", "api", "errors", "settings"];
  const ADM_PER = 50;
  // category = the old free-text category (exact, __none__); cat = a category id from the v8.10 list, or "none".
  const ADM_DEF = { q: "", status: "any", category: "", cat: "", format: "", uploader: "", no_cover: false, sort: "date", dir: "desc", page: 1 };
  const ADM_EDIT_FIELDS = ["title", "author", "description", "category", "publisher", "published", "language"];
  const adm = { tab: "overview", f: { ...ADM_DEF }, data: null, books: [], sel: new Set(), all: false, last: -1, seq: 0, legacy: false, cats: null,
                queue: { list: [], idx: 0, mode: "review", busy: false }, detail: null };
  const admSize = n => { const v = Number(n) || 0; return v < 1048576 ? `${faNum(Math.max(1, Math.round(v / 1024)))} KB` : `${mb(v)} MB`; };
  const admStatus = b => (b && b.catalog_status) || "none";

  function admParams(f, withView) {
    const p = new URLSearchParams();
    if (f.q) p.set("q", f.q);
    if (f.status !== "any") p.set("status", f.status);
    if (f.category) p.set("category", f.category);
    if (f.cat) p.set("cat", f.cat);
    if (f.format) p.set("format", f.format);
    if (f.uploader) p.set("uploader", f.uploader);
    if (f.no_cover) p.set("no_cover", "1");
    if (withView) {
      if (f.sort !== ADM_DEF.sort || f.dir !== ADM_DEF.dir) { p.set("sort", f.sort); p.set("dir", f.dir); }
      if (f.page > 1) p.set("page", String(f.page));
    }
    return p;
  }
  function admFromParams(p) {
    const f = { ...ADM_DEF }, g = k => p.get(k) || "";
    f.q = g("q").slice(0, 200);
    if (["approved", "pending", "rejected", "none"].includes(g("status"))) f.status = g("status");
    f.category = g("category").slice(0, 80);
    if (/^(none|\d{1,9})$/.test(g("cat"))) f.cat = g("cat");
    if (["epub", "pdf"].includes(g("format"))) f.format = g("format");
    f.uploader = g("uploader").slice(0, 120);
    f.no_cover = g("no_cover") === "1";
    if (["date", "title", "size", "status"].includes(g("sort"))) f.sort = g("sort");
    if (["asc", "desc"].includes(g("dir"))) f.dir = g("dir");
    f.page = Math.max(1, parseInt(g("page"), 10) || 1);
    return f;
  }
  const admFilterKey = f => admParams(f, true).toString();
  function admHash() {
    const qs = adm.tab === "books" ? admParams(adm.f, true).toString() : "";
    return `#admin/${adm.tab}${qs ? "?" + qs : ""}`;
  }
  /** Adopt the tab (and the books filters) from location.hash; false when the hash is not an admin one. */
  function admReadHash() {
    const m = /^#admin(?:\/([a-z]+))?(?:\?(.*))?$/.exec(location.hash);
    if (!m) return false;
    adm.tab = ADMIN_TABS.includes(m[1]) ? m[1] : "overview";
    if (adm.tab === "books") {
      const f = admFromParams(new URLSearchParams(m[2] || ""));
      if (admFilterKey(f) !== admFilterKey(adm.f)) { adm.f = f; admClearSel(); }
    }
    return true;
  }
  /** Called by showView("admin"): tab from the hash (refresh, back, deep link) or the last one used. */
  function openAdmin() {
    hideNewApiToken();
    loadCategories(true);
    admReadHash();
    if (location.hash !== admHash()) setHash(admHash(), false);
    loadAdmin();
  }
  /** Refresh the badges/overview and the active tab (also after every admin action). */
  function loadAdmin() {
    renderAdminTabs();
    loadAdminStats();
    return loadAdminTab(adm.tab);
  }
  function renderAdminTabs() {
    document.querySelectorAll("[data-admin-tab]").forEach(t => {
      const on = t.dataset.adminTab === adm.tab;
      t.setAttribute("aria-selected", String(on));
    });
    document.querySelectorAll("[data-admin-panel]").forEach(p => { p.hidden = p.dataset.adminPanel !== adm.tab; });
    if (adm.tab !== "books") closeAdmFilters(false);
    admRenderBulk();
  }
  function setAdminTab(tab, push = true) {
    if (!ADMIN_TABS.includes(tab)) return;
    adm.tab = tab;
    setHash(admHash(), push && location.hash !== admHash());
    renderAdminTabs();
    loadAdminTab(tab);
  }
  function loadAdminTab(tab) {
    if (tab === "queue") return loadQueue();
    if (tab === "books") return loadAdminBooks();
    if (tab === "cats") return loadCatAdmin();
    if (tab === "users") return loadUsers();
    if (tab === "invites") return loadInvites();
    if (tab === "api") return loadApiTokens();
    if (tab === "errors") return loadErrors(true);
    if (tab === "settings") return loadSettings();
    return null;
  }
  $("adminTabs").addEventListener("click", e => { const t = e.target.closest("[data-admin-tab]"); if (t) setAdminTab(t.dataset.adminTab); });

  // ---- Overview: stats cards, badges, shortcuts ----
  async function loadAdminStats() {
    let stats;
    try { stats = await api("admin-stats"); }
    catch (err) { $("adminStats").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; return; }
    const fa = faNum;
    $("adminStats").innerHTML = [
      [fa(stats.active_users) + " / " + fa(stats.users), "کاربر فعال"], [fa(stats.books), "کتاب روی سرور"], [mb(stats.storage_bytes) + " MB", "فضای کتاب‌ها"],
      [fa(stats.catalog_approved), "در کتابخانهٔ عمومی"], [fa(stats.catalog_pending), "در انتظار تأیید"], [fa(stats.open_invites), "دعوت‌نامهٔ باز"],
      [fa(stats.client_errors || 0), "خطای کاربران", stats.client_errors ? " stat-errors" : ""],
    ].map(([v, l, c]) => `<div class="stat${c || ""}"><b>${v}</b><span>${l}</span></div>`).join("");
    setAdmBadge("queue", stats.catalog_pending);
    setAdmBadge("errors", stats.client_errors || 0);
  }
  function setAdmBadge(which, n) {
    n = Number(n) || 0;
    const el = $(which === "queue" ? "admBadgeQueue" : "admBadgeErrors");
    el.hidden = !n; el.textContent = faNum(n);
    el.setAttribute("aria-label", which === "queue" ? `${faNum(n)} کتاب در انتظار` : `${faNum(n)} گروه خطا`);
    const sc = document.querySelector(`#adminShortcuts [data-sc="${which === "queue" ? "pending" : "errors"}"]`);
    if (sc) { sc.hidden = !n; sc.textContent = faNum(n); }
  }
  $("adminShortcuts").addEventListener("click", e => {
    const b = e.target.closest("[data-adm-go]");
    if (!b) return;
    if (b.dataset.admFilter) {
      // «کتاب‌های بدون دسته»: with the v8.10 list that means "no category from the list" (cat=none)
      const filter = b.dataset.admFilter === "category=__none__" && catTreeOn() ? "cat=none" : b.dataset.admFilter;
      adm.f = admFromParams(new URLSearchParams(filter)); admClearSel();
    }
    setAdminTab(b.dataset.admGo);
  });

  // ---- Users, invites, settings ----
  async function loadUsers() {
    let users;
    try { users = await api("users"); } catch (err) { $("userList").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; return; }
    const fa = faNum, me = account?.user?.id;
    $("userList").innerHTML = users.users.map(u => `<div class="admin-item" data-user="${u.id}">
        <span>${esc(u.name)} <span class="muted" dir="ltr">${esc(u.email)}</span><br>
        <span class="muted small">${u.role === "admin" ? "مدیر" : "کاربر"} · ${fa(u.books)} کتاب${u.last_seen ? ` · آخرین ورود ${fmtDate(u.last_seen)}` : ""}</span>
        ${u.disabled ? '<span class="status-chip off">غیرفعال</span>' : ""}</span>
        ${u.id === me ? '<span class="muted small">(خودت)</span>' : `<span class="admin-row">
          <button class="secondary-button" type="button" data-user-act="${u.disabled ? "enable" : "disable"}">${u.disabled ? "فعال کردن" : "غیرفعال کردن"}</button>
          <button class="secondary-button" type="button" data-user-act="${u.role === "admin" ? "demote" : "promote"}">${u.role === "admin" ? "کاربر عادی شود" : "مدیر شود"}</button>
          <button class="secondary-button" type="button" data-user-act="reset">ریست رمز</button>
          <button class="secondary-button" type="button" data-user-act="delete">حذف</button></span>`}</div>`).join("");
  }
  async function loadInvites() {
    let invites;
    try { invites = await api("invites"); } catch (err) { $("inviteList").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; return; }
    $("inviteList").innerHTML = invites.invites.map(i => `<div class="admin-item"><span>${esc(i.note || "بدون یادداشت")}</span>
        <span class="admin-row"><span class="muted">${i.used_by ? `استفاده‌شده توسط ${esc(i.used_by)}` : i.expires_at * 1000 < Date.now() ? "منقضی" : `معتبر تا ${fmtDate(i.expires_at)}`}</span>
        ${!i.used_by && i.expires_at * 1000 > Date.now() ? `<button class="secondary-button" type="button" data-invite-revoke="${esc(i.id)}">باطل کردن</button>` : ""}</span></div>`).join("")
      || '<p class="muted">هنوز دعوت‌نامه‌ای ساخته نشده.</p>';
  }
  async function loadSettings() {
    try { $("settingApproval").checked = !!(await api("settings")).catalog_requires_approval; }
    catch (err) { showToast(apiMessage(err)); }
  }

  // ---- Books tab: server-paged table, filters, selection, bulk actions ----
  async function admFetchBooks(f, per = ADM_PER) {
    if (!adm.legacy) {
      const p = admParams(f, false);
      p.set("sort", f.sort); p.set("dir", f.dir); p.set("page", String(f.page)); p.set("per", String(per));
      try { return await api("admin-books", { query: "&" + p.toString() }); }
      catch (err) { if (!(err instanceof ApiFail && err.status === 404)) throw err; adm.legacy = true; }
    }
    return admLegacyBooks(f, per);
  }
  /** Servers without admin-books (before v8.9): the same result built from books-all + catalog-all. */
  async function admLegacyBooks(f, per) {
    const [all, cat] = await Promise.all([api("books-all"), api("catalog-all")]);
    const meta = new Map((cat.books || []).map(b => [b.id, b]));
    const rows = (all.books || []).map(b => {
      const c = meta.get(b.id) || {};
      return { ...b, catalog_status: b.catalog_status || null, category: catOf(c), description: c.description || "", submitter: c.submitter || "",
               uploader_id: b.uploader || "", uploader_name: b.uploader || "", source: "app", in_libraries: Number(b.readers) || 0 };
    });
    const facets = { status: { approved: 0, pending: 0, rejected: 0, none: 0 }, formats: { epub: 0, pdf: 0 }, categories: [], uploaders: [] };
    const cats = new Map(), ups = new Map();
    for (const b of rows) {
      facets.status[admStatus(b)] = (facets.status[admStatus(b)] || 0) + 1;
      facets.formats[b.format === "pdf" ? "pdf" : "epub"]++;
      if (b.category) cats.set(b.category, (cats.get(b.category) || 0) + 1);
      if (b.uploader_name) ups.set(b.uploader_name, (ups.get(b.uploader_name) || 0) + 1);
    }
    facets.categories = [...cats].map(([name, count]) => ({ name, count })).sort((a, b) => a.name.localeCompare(b.name, "fa"));
    facets.uploaders = [...ups].map(([name, count]) => ({ id: name, name, count }));
    const q = libFold(f.q).key;
    const list = rows.filter(b => (!q || libFold(`${b.title} ${b.author} ${b.publisher || ""} ${b.category} ${b.description}`).key.includes(q))
      && (f.status === "any" || admStatus(b) === f.status)
      && (!f.category || (f.category === "__none__" ? !b.category : b.category === f.category))
      && (!f.format || (b.format || "epub") === f.format)
      && (!f.uploader || (f.uploader !== "api" && b.uploader_name === f.uploader))
      && (!f.no_cover || !b.has_cover));
    const key = { date: b => Number(b.created_at) || 0, title: b => String(b.title || ""), size: b => Number(b.size) || 0, status: admStatus }[f.sort];
    list.sort((a, b) => { const x = key(a), y = key(b), c = typeof x === "string" ? x.localeCompare(y, "fa") : x - y; return f.dir === "asc" ? c : -c; });
    return { books: list.slice((f.page - 1) * per, f.page * per), total: list.length, page: f.page, per, facets };
  }
  async function loadAdminBooks() {
    const seq = ++adm.seq;
    await loadCategories();
    if (seq !== adm.seq) return;
    admSyncControls();
    if (!adm.data) $("admBooks").innerHTML = '<tr class="adm-empty"><td colspan="10">در حال بارگذاری…</td></tr>';
    $("admBooks").setAttribute("aria-busy", "true");
    let r;
    try { r = await admFetchBooks(adm.f); }
    catch (err) {
      if (seq !== adm.seq) return;
      $("admBooks").removeAttribute("aria-busy");
      $("admBooks").innerHTML = `<tr class="adm-empty"><td colspan="10">${esc(apiMessage(err))}</td></tr>`;
      $("admBooksSummary").textContent = "";
      return;
    }
    if (seq !== adm.seq) return;
    $("admBooks").removeAttribute("aria-busy");
    const per = Number(r.per) || ADM_PER, total = Number(r.total) || 0, pages = Math.max(1, Math.ceil(total / per));
    if (adm.f.page > pages) { adm.f.page = pages; setHash(admHash(), false); return loadAdminBooks(); } // e.g. after deleting the last page
    adm.data = { ...r, per, total };
    adm.books = Array.isArray(r.books) ? r.books : [];
    for (const id of [...adm.sel]) if (!adm.books.some(b => b.id === id)) adm.sel.delete(id);
    admRenderFacets(r.facets);
    admRenderBooks();
  }
  function admRenderFacets(fc) {
    if (!fc) return;
    const cats = Array.isArray(fc.categories) ? fc.categories : [], ups = (Array.isArray(fc.uploaders) ? fc.uploaders : []).filter(u => String(u.id) !== "api");
    const st = fc.status || {}, n = v => v == null ? "" : ` (${faNum(v)})`;
    $("admFStatus").querySelectorAll("option").forEach(o => {
      const base = o.value === "any" ? "همهٔ وضعیت‌ها" : STATUS_FA[o.value];
      o.textContent = base + (o.value === "any" ? "" : n(st[o.value]));
    });
    admRenderCatSelect(cats);
    const up = $("admFUploader");
    up.innerHTML = '<option value="">همهٔ بارگذارها</option><option value="api">API</option>'
      + ups.map(u => `<option value="${esc(String(u.id))}">${esc(u.name || String(u.id))}${n(u.count)}</option>`).join("")
      + (adm.f.uploader && adm.f.uploader !== "api" && !ups.some(u => String(u.id) === adm.f.uploader) ? `<option value="${esc(adm.f.uploader)}">${esc(adm.f.uploader)}</option>` : "");
    const fm = fc.formats || {};
    $("admFFormat").querySelectorAll("option").forEach(o => { if (o.value) o.textContent = o.value.toUpperCase() + n(fm[o.value]); });
    adm.cats = cats.map(c => c.name);
    $("admCatList").innerHTML = adm.cats.map(c => `<option value="${esc(c)}"></option>`).join("");
    admSyncControls();
  }
  /** The category filter: the v8.10 tree (parents first, children indented; value = id or "none") when the list has
   *  entries, else the old free-text categories from the facets (value = name or __none__). */
  function admRenderCatSelect(facetCats) {
    const sel = $("admFCategory"), n = v => v == null ? "" : ` (${faNum(v)})`;
    if (catTreeOn()) {
      sel.innerHTML = '<option value="">همهٔ دسته‌ها</option><option value="none">بدون دسته</option>'
        + cats.tops.map(t => `<option value="${t.id}">${esc(t.name)}${n(catCount(t))}</option>`
          + (cats.kids.get(t.id) || []).map(k => `<option value="${k.id}">\u00a0\u00a0\u00a0— ${esc(k.name)}${n(catCount(k))}</option>`).join("")).join("")
        + (adm.f.cat && adm.f.cat !== "none" && !cats.byId.has(Number(adm.f.cat)) ? `<option value="${esc(adm.f.cat)}">دستهٔ ${faNum(adm.f.cat)}</option>` : "");
      return;
    }
    const list = Array.isArray(facetCats) ? facetCats : (adm.cats || []).map(name => ({ name }));
    sel.innerHTML = '<option value="">همهٔ دسته‌ها</option><option value="__none__">بدون دسته</option>'
      + list.map(c => `<option value="${esc(c.name)}">${esc(c.name)}${n(c.count)}</option>`).join("")
      + (adm.f.category && adm.f.category !== "__none__" && !list.some(c => c.name === adm.f.category) ? `<option value="${esc(adm.f.category)}">${esc(adm.f.category)}</option>` : "");
  }
  const admActiveFilters = f => (f.status !== "any") + !!(f.category || f.cat) + !!f.format + !!f.uploader + !!f.no_cover;
  function admSyncControls() {
    const f = adm.f;
    if (document.activeElement !== $("admBookSearch")) $("admBookSearch").value = f.q;
    $("admFStatus").value = f.status;
    $("admFCategory").value = catTreeOn() ? f.cat : f.category;
    $("admFFormat").value = f.format;
    $("admFUploader").value = f.uploader;
    $("admFNoCover").checked = f.no_cover;
    $("admSort").value = `${f.sort}:${f.dir}`;
    const n = admActiveFilters(f);
    $("admFilterCount").hidden = !n; $("admFilterCount").textContent = faNum(n);
    $("admFiltersOpen").setAttribute("aria-label", n ? `فیلترها (${faNum(n)} فیلتر فعال)` : "فیلترها");
    $("admFReset").disabled = !n;
  }
  function admRowHtml(b, i) {
    const st = admStatus(b), title = String(b.title || "بدون عنوان"), author = String(b.author || "").trim(), bc = bookCats(b);
    const cat = bc.length ? bc.map(c => c.name).join("، ") : catOf(b);
    const [bg, fg] = paletteFor(title || String(b.id));
    const thumb = b.has_cover ? `<img alt="" loading="lazy" decoding="async" src="${API}?r=book-cover&id=${b.id}">` : `<span aria-hidden="true">${esc([...title][0] || "؟")}</span>`;
    const fmt = b.format === "pdf" ? "PDF" : "EPUB";
    const up = b.source === "api" ? '<span class="adm-chip api">API</span>' : esc(String(b.uploader_name || b.uploader || "—"));
    const on = adm.all || adm.sel.has(b.id);
    return `<tr class="adm-row${on ? " selected" : ""}" data-id="${b.id}" data-idx="${i}" tabindex="0" aria-label="${esc(title)}">
      <td class="adm-c-sel"><input type="checkbox" class="adm-sel" ${on ? "checked" : ""} aria-label="انتخاب «${esc(title)}»"></td>
      <td class="adm-c-cover"><span class="adm-thumb" style="background:${bg};color:${fg}">${thumb}</span></td>
      <td class="adm-c-title"><b dir="auto">${esc(title)}</b><span class="adm-author" dir="auto">${esc(author || "نویسنده نامشخص")}</span>
        <span class="adm-m"><span class="adm-chip fmt">${fmt}</span><span class="adm-st st-${st}">${STATUS_FA[st]}</span>${cat ? `<span class="adm-chip cat" dir="auto">${esc(cat)}</span>` : ""}${b.source === "api" ? '<span class="adm-chip api">API</span>' : ""}<span>${b.created_at ? fmtDate(b.created_at) : ""}</span><span>${admSize(b.size)}</span></span></td>
      <td class="adm-c-fmt"><span class="adm-chip fmt">${fmt}</span></td>
      <td class="adm-c-cat" dir="auto"${bc.length ? ` title="${esc(bc.map(catPathOf).join("، "))}"` : ""}>${cat ? esc(cat) : '<span class="muted">—</span>'}</td>
      <td class="adm-c-st"><span class="adm-st st-${st}">${STATUS_FA[st]}</span></td>
      <td class="adm-c-up" dir="auto">${up}</td>
      <td class="adm-c-date">${b.created_at ? fmtDate(b.created_at) : "—"}</td>
      <td class="adm-c-size">${admSize(b.size)}</td>
      <td class="adm-c-libs">${faNum(Number(b.in_libraries ?? b.readers) || 0)}</td></tr>`;
  }
  function admRenderBooks() {
    const r = adm.data, f = adm.f;
    if (!r) return;
    const pages = Math.max(1, Math.ceil(r.total / r.per));
    const label = `صفحهٔ ${faNum(f.page)} از ${faNum(pages)} · ${faNum(r.total)} کتاب`;
    $("admBooksSummary").textContent = r.total ? label : "";
    $("admPageInfo").textContent = label;
    $("admPrev").disabled = f.page <= 1; $("admNext").disabled = f.page >= pages;
    $("admPrev").closest(".adm-pager").hidden = r.total <= r.per && f.page <= 1;
    const filtered = !!f.q || admActiveFilters(f) > 0;
    $("admBooks").innerHTML = adm.books.map(admRowHtml).join("")
      || `<tr class="adm-empty"><td colspan="10">${filtered ? 'کتابی با این جستجو و فیلترها پیدا نشد. <button type="button" class="link-button" data-adm-reset>پاک کردن جستجو و فیلترها</button>' : "کتابی روی سرور نیست."}</td></tr>`;
    adm.last = -1;
    admRenderSel();
  }
  function admClearSel() { adm.sel.clear(); adm.all = false; adm.last = -1; }
  const admSelCount = () => adm.all ? (adm.data ? adm.data.total : 0) : adm.sel.size;
  function admRenderSel() {
    $("admBooks").querySelectorAll("tr[data-id]").forEach(tr => {
      const on = adm.all || adm.sel.has(Number(tr.dataset.id));
      tr.classList.toggle("selected", on);
      const cb = tr.querySelector(".adm-sel"); if (cb) cb.checked = on;
    });
    const n = adm.books.length, k = adm.books.filter(b => adm.sel.has(b.id)).length, total = adm.data ? adm.data.total : 0;
    const sp = $("admSelPage");
    sp.disabled = !n; sp.checked = n > 0 && (adm.all || k === n); sp.indeterminate = !adm.all && k > 0 && k < n;
    const info = $("admSelectInfo");
    if (adm.all) {
      info.innerHTML = `همهٔ ${faNum(total)} نتیجهٔ این جستجو انتخاب شد. <button type="button" class="link-button" data-adm-selall="0">لغو انتخاب</button>`;
      info.hidden = false;
    } else if (n && k === n && total > n) {
      info.innerHTML = `${faNum(k)} کتاب این صفحه انتخاب شد. <button id="admSelectAll" type="button" class="link-button" data-adm-selall="1">انتخاب همهٔ ${faNum(total)} نتیجه</button>`;
      info.hidden = false;
    } else info.hidden = true;
    admRenderBulk();
  }
  function admRenderBulk() {
    const n = adm.tab === "books" ? admSelCount() : 0;
    $("admBulkBar").hidden = !n;
    const tree = catTreeOn(); // v8.10: add/remove one category of the list; the old free-text actions only without a list
    $("admBulkBar").querySelectorAll('[data-bulk="set_category"],[data-bulk="clear_category"]').forEach(x => { x.hidden = tree; });
    $("admBulkBar").querySelectorAll('[data-bulk="add_category"],[data-bulk="remove_category"]').forEach(x => { x.hidden = !tree; });
    $("admBulkCount").textContent = adm.all ? `همهٔ ${faNum(n)} نتیجه انتخاب شد` : `${faNum(n)} کتاب انتخاب شد`;
  }
  function admToggle(idx, on, range) {
    if (adm.all) { adm.all = false; adm.books.forEach(b => adm.sel.add(b.id)); }
    const from = range && adm.last >= 0 ? Math.min(adm.last, idx) : idx, to = range && adm.last >= 0 ? Math.max(adm.last, idx) : idx;
    for (let i = from; i <= to; i++) { const b = adm.books[i]; if (b) { if (on) adm.sel.add(b.id); else adm.sel.delete(b.id); } }
    adm.last = idx;
    admRenderSel();
  }
  $("admBooks").addEventListener("mousedown", e => { if (e.shiftKey && e.target.closest(".adm-c-sel")) e.preventDefault(); }); // no text selection on shift-click
  $("admBooks").addEventListener("click", e => {
    if (e.target.closest("[data-adm-reset]")) { admResetFilters(true); return; }
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const idx = Number(tr.dataset.idx);
    if (e.target.closest(".adm-c-sel")) {
      const cb = tr.querySelector(".adm-sel");
      if (e.target !== cb) cb.checked = !cb.checked;
      admToggle(idx, cb.checked, e.shiftKey);
      return;
    }
    admOpenDetail(Number(tr.dataset.id));
  });
  $("admBooks").addEventListener("keydown", e => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || e.target !== tr) return;
    if (e.key === "Enter") { e.preventDefault(); admOpenDetail(Number(tr.dataset.id)); }
    else if (e.key === " ") { e.preventDefault(); const cb = tr.querySelector(".adm-sel"); admToggle(Number(tr.dataset.idx), !cb.checked, e.shiftKey); }
  });
  $("admSelPage").addEventListener("change", e => {
    adm.all = false; adm.last = -1;
    if (e.target.checked) adm.books.forEach(b => adm.sel.add(b.id)); else adm.sel.clear();
    admRenderSel();
  });
  $("admSelectInfo").addEventListener("click", e => {
    const b = e.target.closest("[data-adm-selall]");
    if (!b) return;
    if (b.dataset.admSelall === "1") adm.all = true; else admClearSel();
    admRenderSel();
  });
  $("admBulkClear").addEventListener("click", () => { admClearSel(); admRenderSel(); });

  function admSetFilter(patch) {
    Object.assign(adm.f, patch, { page: 1 });
    admClearSel();
    setHash(admHash(), false);
    loadAdminBooks();
  }
  function admResetFilters(withSearch) {
    admSetFilter({ status: "any", category: "", cat: "", format: "", uploader: "", no_cover: false, ...(withSearch ? { q: "" } : {}) });
    if (withSearch) $("admBookSearch").value = "";
  }
  let admSearchTimer = 0;
  $("admBookSearch").addEventListener("input", () => {
    clearTimeout(admSearchTimer);
    admSearchTimer = setTimeout(() => {
      const q = $("admBookSearch").value.replace(/\s+/g, " ").trim().slice(0, 200);
      if (q !== adm.f.q) admSetFilter({ q });
    }, 300);
  });
  $("admFStatus").addEventListener("change", e => admSetFilter({ status: e.target.value }));
  $("admFCategory").addEventListener("change", e => admSetFilter(catTreeOn() ? { cat: e.target.value, category: "" } : { category: e.target.value, cat: "" }));
  $("admFFormat").addEventListener("change", e => admSetFilter({ format: e.target.value }));
  $("admFUploader").addEventListener("change", e => admSetFilter({ uploader: e.target.value }));
  $("admFNoCover").addEventListener("change", e => admSetFilter({ no_cover: e.target.checked }));
  $("admSort").addEventListener("change", e => { const [sort, dir] = e.target.value.split(":"); admSetFilter({ sort, dir }); });
  $("admFReset").addEventListener("click", () => admResetFilters(false));
  function admGoPage(p) {
    adm.f.page = Math.max(1, p);
    admClearSel();
    setHash(admHash(), false);
    loadAdminBooks().then(() => {
      const top = $("admPanel-books").getBoundingClientRect().top;
      if (top < 0) window.scrollBy({ top: top - 60, behavior: "auto" });
    });
  }
  $("admPrev").addEventListener("click", () => admGoPage(adm.f.page - 1));
  $("admNext").addEventListener("click", () => admGoPage(adm.f.page + 1));
  // Phones: the filters are a bottom sheet opened from «فیلترها» (with the number of active filters).
  const admFiltersOpen = () => $("admFilters").classList.contains("open");
  function openAdmFilters() {
    $("admFilters").classList.add("open");
    $("admFilters").setAttribute("role", "dialog"); $("admFilters").setAttribute("aria-modal", "true");
    $("admFiltersBackdrop").hidden = false;
    $("admFiltersOpen").setAttribute("aria-expanded", "true");
    document.body.classList.add("adm-sheet-open");
    $("admFStatus").focus({ preventScroll: true });
  }
  function closeAdmFilters(focus = true) {
    if (!admFiltersOpen()) return;
    $("admFilters").classList.remove("open");
    $("admFilters").setAttribute("role", "group"); $("admFilters").removeAttribute("aria-modal");
    $("admFiltersBackdrop").hidden = true;
    $("admFiltersOpen").setAttribute("aria-expanded", "false");
    document.body.classList.remove("adm-sheet-open");
    if (focus) $("admFiltersOpen").focus({ preventScroll: true });
  }
  $("admFiltersOpen").addEventListener("click", () => (admFiltersOpen() ? closeAdmFilters() : openAdmFilters()));
  $("admFiltersClose").addEventListener("click", () => closeAdmFilters());
  $("admFApply").addEventListener("click", () => closeAdmFilters());
  $("admFiltersBackdrop").addEventListener("click", () => closeAdmFilters());
  document.addEventListener("keydown", e => { if (e.key === "Escape" && admFiltersOpen()) { e.preventDefault(); closeAdmFilters(); } });
  matchMedia("(max-width: 700px)").addEventListener("change", e => { if (!e.matches) closeAdmFilters(false); });

  // Bulk actions: ids of the selected rows, or the current filter for «همهٔ N نتیجه».
  const BULK_FA = { approve: "تأیید", reject: "رد", set_category: "تعیین دسته", clear_category: "حذف دسته", add_category: "افزودن دسته", remove_category: "برداشتن دسته",
                    add_catalog: "افزودن به کتابخانهٔ عمومی", remove_catalog: "برداشتن از کتابخانهٔ عمومی", delete: "حذف" };
  function admBulkMessage(r) {
    const a = Number(r && r.affected) || 0, s = Number(r && r.skipped) || 0;
    return `انجام شد: ${faNum(a)} کتاب` + (s ? ` · ${faNum(s)} مورد نادیده گرفته شد` : "");
  }
  $("admBulkBar").addEventListener("click", async e => {
    const btn = e.target.closest("[data-bulk]");
    if (!btn) return;
    const action = btn.dataset.bulk, n = admSelCount();
    if (!n || !BULK_FA[action]) return;
    const body = { action };
    if (adm.all) {
      const p = admParams(adm.f, false), filter = Object.fromEntries(p);
      if (filter.no_cover) filter.no_cover = 1;
      body.filter = filter;
    } else body.ids = [...adm.sel];
    const cnt = faNum(n);
    if (action === "set_category") {
      const c = await askCategory(`تعیین دسته برای ${cnt} کتاب`);
      if (c == null) return;
      body.category = c;
    }
    if (action === "add_category" || action === "remove_category") {
      const add = action === "add_category";
      const id = await askCategories({ single: true, title: add ? `افزودن یک دسته به ${cnt} کتاب` : `برداشتن یک دسته از ${cnt} کتاب`, ok: add ? "افزودن دسته" : "برداشتن دسته" });
      if (id == null) return;
      body.category_id = id;
    }
    const ask = {
      remove_category: `دستهٔ «${catPathOf(body.category_id)}» از ${cnt} کتاب برداشته شود؟`,
      delete: `${cnt} کتاب برای همه حذف شود؟ فایل، محل مطالعه و یادداشت‌های همهٔ کاربران پاک می‌شود و این کار برگشت‌پذیر نیست.`,
      remove_catalog: `${cnt} کتاب از کتابخانهٔ عمومی برداشته شود؟ (از کتابخانهٔ شخصی کسانی که اضافه‌اش کرده‌اند حذف نمی‌شود)`,
      reject: `${cnt} کتاب رد شود؟`,
      clear_category: `دستهٔ ${cnt} کتاب حذف شود؟`,
    }[action] || (adm.all ? `«${BULK_FA[action]}» برای همهٔ ${cnt} نتیجه انجام شود؟` : "");
    if (ask && !confirm(ask)) return;
    const buttons = [...$("admBulkBar").querySelectorAll("button")];
    buttons.forEach(b => { b.disabled = true; });
    try {
      const r = await api("admin-bulk", { method: "POST", body });
      showToast(admBulkMessage(r));
      admClearSel(); catalogLoaded = false;
      loadAdminStats();
      await loadAdminBooks();
    } catch (err) {
      showToast(err instanceof ApiFail && err.status === 404 ? "کار گروهی هنوز روی سرور فعال نیست." : apiMessage(err));
    } finally { buttons.forEach(b => { b.disabled = false; }); }
  });

  // Category prompt (bulk «تعیین دسته», review queue «C»): a small dialog with the known categories as suggestions.
  let admCatResolve = null;
  async function admEnsureCats() {
    if (adm.cats) return;
    try { const r = await admFetchBooks({ ...ADM_DEF }, 1); if (!adm.cats && r && r.facets) { adm.cats = (r.facets.categories || []).map(c => c.name); $("admCatList").innerHTML = adm.cats.map(c => `<option value="${esc(c)}"></option>`).join(""); } }
    catch (_) {}
  }
  function askCategory(title, value = "") {
    admEnsureCats();
    return new Promise(res => {
      admCatResolve = res;
      $("admCatTitle").textContent = title;
      $("admCatInput").value = value;
      $("admCatDialog").showModal();
      $("admCatInput").focus(); $("admCatInput").select();
    });
  }
  $("admCatForm").addEventListener("submit", e => {
    e.preventDefault();
    const v = $("admCatInput").value.replace(/\s+/g, " ").trim().slice(0, 80);
    if (!v) { $("admCatInput").focus(); return; }
    const r = admCatResolve; admCatResolve = null;
    $("admCatDialog").close();
    if (r) r(v);
  });
  $("admCatClose").addEventListener("click", () => $("admCatDialog").close());
  $("admCatDialog").addEventListener("close", () => { if (admCatResolve) { const r = admCatResolve; admCatResolve = null; r(null); } });

  // ---- Book details: side panel (desktop) / bottom sheet (phones) ----
  function admFindBook(id) { return adm.books.find(x => x.id === id) || adm.queue.list.find(x => x.id === id) || null; }
  function admOpenDetail(id) {
    const b = admFindBook(id);
    if (!b) return;
    adm.detail = b;
    admRenderDetail();
    if (!$("admDetail").open) $("admDetail").showModal();
    $("admDetail").scrollTop = 0;
  }
  function admRenderDetail() {
    const b = adm.detail;
    if (!b) return;
    const st = admStatus(b), title = String(b.title || "بدون عنوان"), cat = catOf(b), bc = bookCats(b);
    const [bg, fg] = paletteFor(title || String(b.id));
    const cover = $("admDetailCover");
    cover.style.background = bg; cover.style.color = fg;
    cover.innerHTML = b.has_cover ? `<img alt="" src="${API}?r=book-cover&id=${b.id}">` : `<div class="cover-name">${esc(title)}</div>`;
    $("admDetailTitle").textContent = title;
    $("admDetailChips").innerHTML = `<span class="adm-chip fmt">${b.format === "pdf" ? "PDF" : "EPUB"}</span><span class="adm-st st-${st}">${STATUS_FA[st]}</span>`
      + (b.source === "api" ? '<span class="adm-chip api">API</span>' : "")
      + (bc.length ? bc.map(c => `<span class="adm-chip cat" dir="auto">${esc(catPathOf(c))}</span>`).join("") : cat ? `<span class="adm-chip cat" dir="auto">${esc(cat)}</span>` : "");
    const info = [
      ["بارگذار", b.source === "api" ? "API" : String(b.uploader_name || b.uploader || "")],
      ["پیشنهاددهنده", String(b.submitter || "")],
      ["تاریخ", b.created_at ? fmtDate(b.created_at) : b.submitted_at ? fmtDate(b.submitted_at) : ""],
      ["حجم", b.size ? admSize(b.size) : ""],
      ["در کتابخانه‌ها", b.in_libraries != null || b.readers != null ? `${faNum(Number(b.in_libraries ?? b.readers) || 0)} کاربر` : ""],
    ].filter(([, v]) => v);
    $("admDetailInfo").innerHTML = info.map(([k, v]) => `<div><dt>${k}</dt><dd dir="auto">${esc(v)}</dd></div>`).join("");
    const acts = {
      pending: [["approve", "تأیید و انتشار", "primary-button"], ["reject", "رد", "secondary-button"]],
      approved: [["remove_catalog", "برداشتن از کتابخانهٔ عمومی", "secondary-button"]],
      rejected: [["approve", "تأیید و انتشار", "primary-button"], ["remove_catalog", "برداشتن از کتابخانهٔ عمومی", "secondary-button"]],
      none: [["add_catalog", "افزودن به کتابخانهٔ عمومی", "primary-button"]],
    }[st];
    $("admDetailStatus").innerHTML = acts.map(([a, l, c]) => `<button type="button" class="${c}" data-dst="${a}">${l}</button>`).join("");
    const f = $("admDetailForm"), inCatalog = st !== "none";
    for (const k of ADM_EDIT_FIELDS) f.elements[k].value = b[k] == null ? "" : String(b[k]);
    f.elements.category.disabled = !inCatalog; f.elements.description.disabled = !inCatalog;
    // v8.10: with a category list the free-text field gives way to the picker (several categories, sent as ids)
    const tree = catTreeOn();
    $("admDetailCatLabel").hidden = tree;
    $("admDetailCatsWrap").hidden = !tree;
    if (tree) {
      admDetailPicker.set(bc.map(c => c.id));
      admDetailPicker.disable(!inCatalog);
      $("admDetailLegacyCat").hidden = !cat;
      $("admDetailLegacyCat").textContent = cat ? `دستهٔ قدیمی (متنی): ${cat}` : "";
    }
    $("admDetailCatalogNote").hidden = inCatalog;
    $("admDetailDownload").href = `${API}?r=book-file&id=${b.id}`;
  }
  $("admDetailClose").addEventListener("click", () => $("admDetail").close());
  $("admDetail").addEventListener("close", () => { adm.detail = null; });
  function admAfterChange() {
    catalogLoaded = false;
    loadAdminStats();
    if (adm.tab === "books") loadAdminBooks(); else if (adm.tab === "queue") loadQueue();
  }
  $("admDetailStatus").addEventListener("click", async e => {
    const btn = e.target.closest("[data-dst]"), b = adm.detail;
    if (!btn || !b) return;
    const act = btn.dataset.dst, st = admStatus(b);
    if (act === "remove_catalog" && !confirm("این کتاب از کتابخانهٔ عمومی برداشته شود؟ (از کتابخانهٔ شخصی کسانی که اضافه‌اش کرده‌اند حذف نمی‌شود)")) return;
    btn.disabled = true;
    try {
      if ((act === "approve" || act === "reject") && st === "pending") await api("catalog-review", { method: "POST", body: { book_id: b.id, approve: act === "approve" } });
      else if (act === "remove_catalog") await api("catalog-remove", { method: "POST", body: { book_id: b.id } });
      else await api("admin-bulk", { method: "POST", body: { action: act, ids: [b.id] } });
      b.catalog_status = act === "approve" || act === "add_catalog" ? "approved" : act === "reject" ? "rejected" : null;
      showToast({ approve: "تأیید شد و در کتابخانهٔ عمومی قرار گرفت.", reject: "رد شد.", remove_catalog: "از کتابخانهٔ عمومی برداشته شد.", add_catalog: "به کتابخانهٔ عمومی اضافه شد." }[act]);
      admRenderDetail();
      admAfterChange();
    } catch (err) { showToast(err instanceof ApiFail && err.status === 404 && act === "add_catalog" ? "این کار هنوز روی سرور فعال نیست." : apiMessage(err)); btn.disabled = false; }
  });
  $("admDetailForm").addEventListener("submit", async e => {
    e.preventDefault();
    const b = adm.detail, f = e.target;
    if (!b) return;
    const v = k => f.elements[k].value.replace(k === "description" ? /[ \t]+/g : /\s+/g, " ").trim();
    if (!v("title")) { f.elements.title.focus(); return; }
    const inCatalog = admStatus(b) !== "none";
    const tree = catTreeOn() && !$("admDetailCatsWrap").hidden;
    const fields = inCatalog ? ADM_EDIT_FIELDS.filter(k => !(tree && k === "category")) : ["title", "author", "publisher", "published", "language"];
    const body = { book_id: b.id };
    for (const k of fields) body[k] = v(k);
    if (inCatalog && tree) body.categories = admDetailPicker.get();
    $("admDetailSave").disabled = true;
    try {
      await api(inCatalog ? "catalog-update" : "book-meta", { method: "POST", body });
      for (const k of fields) b[k] = body[k];
      if (body.categories) b.categories = body.categories.map(catObj).filter(Boolean);
      showToast("ذخیره شد.");
      $("admDetail").close();
      if (adm.tab === "books") admRenderBooks(); else if (adm.tab === "queue") renderQueue();
      admAfterChange();
    } catch (err) { showToast(apiMessage(err)); }
    finally { $("admDetailSave").disabled = false; }
  });
  $("admDetailDelete").addEventListener("click", async () => {
    const b = adm.detail;
    if (!b || !confirm(`«${b.title}» برای همه حذف شود؟ این کار برگشت‌پذیر نیست.`)) return;
    try {
      await api("book-delete", { method: "POST", body: { book_id: b.id } });
      showToast("کتاب حذف شد.");
      $("admDetail").close();
      adm.queue.list = adm.queue.list.filter(x => x.id !== b.id);
      admAfterChange();
    } catch (err) { showToast(apiMessage(err)); }
  });
  $("admDetailRead").addEventListener("click", () => { if (adm.detail) adminPreview(adm.detail, $("admDetailRead")); });
  /** «مطالعهٔ آنلاین» from the admin: the same ephemeral preview as the catalog page (nothing is stored). */
  async function adminPreview(b, btn) {
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "در حال دریافت…"; }
    try {
      const blob = await (await api("book-file", { query: `&id=${b.id}` })).blob();
      const isPdf = b.format === "pdf" || (b.format !== "epub" && await sniffPdf(blob));
      const file = new File([blob], `${b.title}.${isPdf ? "pdf" : "epub"}`, { type: isPdf ? "application/pdf" : "application/epub+zip" });
      if ($("admDetail").open) $("admDetail").close();
      await openReader(null, { id: `preview-${b.id}`, preview: true, catalogId: b.id, title: b.title, author: b.author || "نویسنده نامشخص",
                               file, size: blob.size, language: b.language || "", ...(isPdf ? { format: "pdf" } : {}) });
    } catch (err) {
      console.warn("Admin preview failed:", err);
      reportError("api", err, "admin:read-online");
      showToast(err instanceof ApiFail ? apiMessage(err) : "بازکردن کتاب ناموفق بود.");
    } finally { if (btn) { btn.disabled = false; btn.textContent = label; } }
  }

  // ---- Review queue: one pending book at a time (A/R/C, ←/J next, →/K previous) or the compact list ----
  async function loadQueue() {
    let r;
    try { r = await api("catalog-pending"); }
    catch (err) { $("admReview").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; $("pendingList").innerHTML = ""; return; }
    const q = adm.queue, cur = q.list[q.idx];
    q.list = (r.books || []).map(b => ({ ...b, catalog_status: "pending" }));
    const keep = cur ? q.list.findIndex(b => b.id === cur.id) : -1;
    q.idx = keep >= 0 ? keep : Math.min(q.idx, Math.max(0, q.list.length - 1));
    renderQueue();
  }
  function renderQueue() {
    const q = adm.queue, n = q.list.length, review = q.mode === "review";
    $("admQueueCount").textContent = faNum(n);
    setAdmBadge("queue", n);
    document.querySelectorAll("[data-queue-mode]").forEach(x => x.setAttribute("aria-pressed", String(x.dataset.queueMode === q.mode)));
    $("admReview").hidden = !review;
    $("admReviewKeys").hidden = !review || !n;
    $("pendingList").closest(".adm-pending-block").hidden = review;
    $("pendingList").innerHTML = n ? q.list.map(b => `<div class="admin-item">
        <span><strong dir="auto">${esc(b.title)}</strong> — <span dir="auto">${esc(b.author || "")}</span> <span class="muted">· ${esc(b.submitter || "")} · ${fmtDate(b.submitted_at)}</span>${catOf(b) ? ` <span class="status-chip cat-chip">${esc(catOf(b))}</span>` : ""}
        ${b.description ? `<br><span class="muted">${esc(b.description)}</span>` : ""}</span>
        <span class="admin-row"><button class="secondary-button" type="button" data-pending-open="${b.id}">جزئیات</button>
        <button class="secondary-button" type="button" data-review="${b.id}" data-approve="1">تأیید</button>
        <button class="secondary-button" type="button" data-review="${b.id}" data-approve="0">رد</button></span></div>`).join("")
      : '<p class="muted">موردی در انتظار نیست.</p>';
    if (!n) { $("admReview").innerHTML = '<div class="adm-rv-empty"><p>موردی در انتظار نیست. 🎉</p></div>'; return; }
    const b = q.list[q.idx], title = String(b.title || "بدون عنوان"), cat = catOf(b), bc = bookCats(b);
    const catChips = bc.length ? bc.map(c => `<span class="adm-chip cat" dir="auto">${esc(catPathOf(c))}</span>`).join("")
      : cat ? `<span class="adm-chip cat" dir="auto">${esc(cat)}</span>` : '<span class="adm-chip">بدون دسته</span>';
    const [bg, fg] = paletteFor(title || String(b.id));
    const rows = [
      ["پیشنهاددهنده", esc(String(b.submitter || "").trim())],
      ["تاریخ ارسال", b.submitted_at ? fmtDate(b.submitted_at) : ""],
      ["ناشر", esc(String(b.publisher || "").trim())],
      ["تاریخ انتشار", esc(faDigits(String(b.published || "").trim()))],
      ["زبان", esc(langName(b.language))],
      ["حجم", b.size ? admSize(b.size) : ""],
    ].filter(([, v]) => v);
    $("admReview").innerHTML = `<article class="adm-rv" data-rv-id="${b.id}">
      <div class="book-cover-thumb adm-rv-cover" style="background:${bg};color:${fg}">${b.has_cover ? `<img alt="" src="${API}?r=book-cover&id=${b.id}">` : `<div class="cover-name">${esc(title)}</div>`}</div>
      <div class="adm-rv-body">
        <p class="adm-rv-pos muted small">کتاب ${faNum(q.idx + 1)} از ${faNum(n)}</p>
        <h3 class="adm-rv-title" dir="auto">${esc(title)}</h3>
        <p class="adm-rv-author" dir="auto">${esc(b.author || "نویسنده نامشخص")}</p>
        <p class="adm-detail-chips"><span class="adm-chip fmt">${b.format === "pdf" ? "PDF" : "EPUB"}</span>${catChips}</p>
        ${rows.length ? `<table class="meta-table adm-rv-meta">${rows.map(([k, v]) => `<tr><th scope="row">${k}</th><td dir="auto">${v}</td></tr>`).join("")}</table>` : ""}
        ${b.description ? `<p class="adm-rv-desc" dir="auto">${esc(b.description)}</p>` : '<p class="muted small">بدون توضیح.</p>'}
        <div class="adm-rv-actions">
          <button type="button" class="primary-button" data-rv="approve" aria-keyshortcuts="A">تأیید <kbd>A</kbd></button>
          <button type="button" class="secondary-button" data-rv="reject" aria-keyshortcuts="R">رد <kbd>R</kbd></button>
          <button type="button" class="secondary-button" data-rv="category" aria-keyshortcuts="C">تعیین دسته <kbd>C</kbd></button>
          <span class="adm-rv-nav"><button type="button" class="secondary-button" data-rv="prev" aria-keyshortcuts="K ArrowRight" ${q.idx ? "" : "disabled"}>→ قبلی</button>
          <button type="button" class="secondary-button" data-rv="next" aria-keyshortcuts="J ArrowLeft" ${q.idx < n - 1 ? "" : "disabled"}>بعدی ←</button></span>
        </div>
        <div class="adm-rv-more"><button type="button" class="text-button" data-rv="preview">📖 پیش‌نمایش متن</button><button type="button" class="text-button" data-rv="details">ویرایش جزئیات</button><a class="text-button" href="${API}?r=book-file&id=${b.id}" download>دانلود</a></div>
      </div></article>`;
  }
  async function queueAct(act, btn) {
    const q = adm.queue, b = q.list[q.idx];
    if (!b || q.busy) return;
    if (act === "next") { if (q.idx < q.list.length - 1) { q.idx++; renderQueue(); } else showToast("این آخرین مورد صف است."); return; }
    if (act === "prev") { if (q.idx > 0) { q.idx--; renderQueue(); } return; }
    if (act === "preview") { adminPreview(b, btn); return; }
    if (act === "details") { admOpenDetail(b.id); return; }
    q.busy = true;
    try {
      if (act === "approve" || act === "reject") {
        await api("catalog-review", { method: "POST", body: { book_id: b.id, approve: act === "approve" } });
        q.list = q.list.filter(x => x.id !== b.id);
        if (q.idx >= q.list.length) q.idx = Math.max(0, q.list.length - 1);
        catalogLoaded = false;
        showToast(act === "approve" ? `«${b.title}» تأیید شد.` : `«${b.title}» رد شد.`);
        renderQueue();
        loadAdminStats();
      } else if (act === "category" && (await loadCategories(), catTreeOn())) {
        const ids = await askCategories({ title: `دسته‌های «${b.title}»`, ids: bookCats(b).map(c => c.id) });
        if (ids == null) return;
        await api("catalog-update", { method: "POST", body: { book_id: b.id, categories: ids } });
        b.categories = ids.map(catObj).filter(Boolean); catalogLoaded = false;
        showToast("دسته‌ها ثبت شد.");
        renderQueue();
      } else if (act === "category") {
        const c = await askCategory(`دسته برای «${b.title}»`, catOf(b));
        if (c == null) return;
        await api("catalog-update", { method: "POST", body: { book_id: b.id, category: c } });
        b.category = c; catalogLoaded = false;
        showToast("دسته ثبت شد.");
        renderQueue();
      }
    } catch (err) { showToast(apiMessage(err)); }
    finally { q.busy = false; }
  }
  $("admReview").addEventListener("click", e => { const b = e.target.closest("[data-rv]"); if (b && !b.disabled) queueAct(b.dataset.rv, b); });
  document.querySelector(".adm-queue-mode").addEventListener("click", e => {
    const b = e.target.closest("[data-queue-mode]");
    if (!b) return;
    adm.queue.mode = b.dataset.queueMode;
    renderQueue();
  });
  const QUEUE_KEYS = { KeyA: "approve", KeyR: "reject", KeyC: "category", KeyJ: "next", ArrowLeft: "next", KeyK: "prev", ArrowRight: "prev" };
  document.addEventListener("keydown", e => {
    if (adm.tab !== "queue" || adm.queue.mode !== "review" || $("viewAdmin").hidden || readerOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (document.querySelector("dialog[open]")) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    const act = QUEUE_KEYS[e.code];
    if (!act) return;
    e.preventDefault();
    queueAct(act, act === "preview" ? null : document.querySelector(`#admReview [data-rv="${act}"]`));
  });
  $("pendingList").addEventListener("click", e => { const o = e.target.closest("[data-pending-open]"); if (o) admOpenDetail(Number(o.dataset.pendingOpen)); });

  // ---- v8.10 «دسته‌ها» tab: two-level tree with counts; add, rename inline, move a subcategory, reorder, delete ----
  const CAT_ERRORS = {
    duplicate: "دسته‌ای با همین نام همین‌جا هست؛ نام دیگری بنویس.",
    too_deep: "فقط دو سطح ممکن است: دسته و زیردسته. زیردسته نمی‌تواند زیردستهٔ دیگری داشته باشد.",
    has_children: "این دسته زیردسته دارد. اول زیردسته‌هایش را حذف کن یا به دستهٔ دیگری منتقل کن.",
    has_children_move: "دسته‌ای که زیردسته دارد نمی‌تواند زیردستهٔ دستهٔ دیگری شود.",
    missing_name: "نام دسته را بنویس.", empty_name: "نام دسته را بنویس.", too_long_name: "نام دسته حداکثر ۸۰ نویسه است.",
    not_found: "این دسته دیگر وجود ندارد؛ فهرست تازه شد.", bad_parent: "دستهٔ والد پیدا نشد.", bad_move_to: "دستهٔ مقصد معتبر نیست.",
  };
  function catErrorMessage(err, where) {
    if (!(err instanceof ApiFail)) return apiMessage(err);
    if (err.status === 404 && err.code !== "not_found") return "مدیریت دسته‌ها هنوز روی سرور فعال نیست.";
    if (err.code === "has_children" && where === "save") return CAT_ERRORS.has_children_move;
    return CAT_ERRORS[err.code] || (err.status === 409 ? CAT_ERRORS.duplicate : apiMessage(err));
  }
  const catAdm = { edit: 0, addSub: 0, busy: false };
  async function loadCatAdmin() {
    await loadCategories();
    renderCatTree();
  }
  function catRowHtml(c, i, n) {
    const name = esc(c.name), editing = catAdm.edit === c.id;
    const moveTo = c.parent_id ? cats.tops.filter(t => t.id !== c.parent_id) : [];
    return `<div class="cat-row">`
      + (editing ? `<form class="cat-rename" data-cat-rename="${c.id}" novalidate><input class="admin-input" value="${name}" maxlength="80" aria-label="نام تازهٔ «${name}»" dir="auto" autocomplete="off">`
          + `<button class="secondary-button" type="submit">ذخیره</button><button class="text-button" type="button" data-cat-act="cancel">انصراف</button></form>`
        : `<span class="cat-row-name" dir="auto">${name}</span>`)
      + `<span class="adm-chip cat-row-count" title="کتاب‌های این دسته${c.parent_id ? "" : " و زیردسته‌هایش"}">${faNum(catCount(c))} کتاب</span>`
      + `<span class="cat-row-acts">`
      + `<button class="icon-button cat-mv" type="button" data-cat-act="up" aria-label="بالاتر بردن «${name}»" title="بالاتر"${i ? "" : " disabled"}>↑</button>`
      + `<button class="icon-button cat-mv" type="button" data-cat-act="down" aria-label="پایین‌تر بردن «${name}»" title="پایین‌تر"${i < n - 1 ? "" : " disabled"}>↓</button>`
      + (editing ? "" : `<button class="secondary-button" type="button" data-cat-act="rename">تغییر نام</button>`)
      + (moveTo.length ? `<select class="admin-input cat-move" data-cat-move aria-label="انتقال «${name}» به دستهٔ دیگر"><option value="">انتقال به…</option>${moveTo.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>` : "")
      + `<button class="secondary-button adm-danger" type="button" data-cat-act="delete">حذف</button></span></div>`;
  }
  function renderCatTree() {
    const tree = $("catTree");
    $("catAdmTotal").textContent = faNum(cats.list.length);
    if (!cats.supported) { tree.innerHTML = '<li class="muted">مدیریت دسته‌ها هنوز روی سرور فعال نیست.</li>'; $("catAddTop").hidden = true; return; }
    $("catAddTop").hidden = false;
    tree.innerHTML = cats.tops.map((t, i) => {
      const kids = cats.kids.get(t.id) || [];
      const addSub = catAdm.addSub === t.id
        ? `<form class="admin-row cat-add-sub" data-cat-addsub="${t.id}" novalidate><input class="admin-input" maxlength="80" placeholder="نام زیردسته" aria-label="نام زیردستهٔ تازه برای «${esc(t.name)}»" dir="auto" autocomplete="off"><button class="secondary-button" type="submit">افزودن</button><button class="text-button" type="button" data-cat-act="cancel">انصراف</button></form>`
        : `<button class="text-button cat-add-sub-open" type="button" data-cat-act="addsub">+ زیردسته برای «${esc(t.name)}»</button>`;
      return `<li class="cat-node cat-top" data-cat-id="${t.id}">${catRowHtml(t, i, cats.tops.length)}`
        + `<ul class="cat-kids">${kids.map((k, j) => `<li class="cat-node cat-kid" data-cat-id="${k.id}">${catRowHtml(k, j, kids.length)}</li>`).join("")}`
        + `<li class="cat-addsub-row" data-cat-id="${t.id}">${addSub}</li></ul></li>`;
    }).join("") || '<li class="muted cat-empty">هنوز دسته‌ای نساخته‌ای. با فرم بالا اولین دستهٔ اصلی را بساز.</li>';
    const focus = tree.querySelector(".cat-rename input, .cat-add-sub input");
    if (focus) { focus.focus({ preventScroll: true }); if (focus.closest(".cat-rename")) focus.select(); }
  }
  /** Run one category change; on success reload the list (tree, filters, catalog) and say so. */
  async function catChange(route, body, ok, where = "save") {
    if (catAdm.busy) return null;
    catAdm.busy = true; $("catAdmError").textContent = "";
    try {
      const r = await api(route, { method: "POST", body });
      catAdm.edit = 0; catAdm.addSub = 0;
      catalogLoaded = false; catalogState.at = 0; relatedCache.clear();
      await loadCategories(true);
      renderCatTree();
      if (ok) showToast(typeof ok === "function" ? ok(r) : ok);
      return r || {};
    } catch (err) {
      const m = catErrorMessage(err, where);
      $("catAdmError").textContent = m; showToast(m);
      if (err instanceof ApiFail && err.code === "not_found") { await loadCategories(true); renderCatTree(); }
      return null;
    } finally { catAdm.busy = false; }
  }
  const cleanCatName = v => String(v || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const topParent = () => 0; // the server's "no parent" (contract: parent_id 0 = top level; null is rejected as bad_parent_id)
  $("catAddTop").addEventListener("submit", async e => {
    e.preventDefault();
    const name = cleanCatName($("catAddTopName").value);
    if (!name) { $("catAdmError").textContent = CAT_ERRORS.missing_name; $("catAddTopName").focus(); return; }
    if (await catChange("category-save", { name, parent_id: topParent() }, `دستهٔ «${name}» ساخته شد.`)) { $("catAddTopName").value = ""; $("catAddTopName").focus(); }
  });
  $("catTree").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, input = f.querySelector("input"), name = cleanCatName(input && input.value);
    if (!name) { $("catAdmError").textContent = CAT_ERRORS.missing_name; if (input) input.focus(); return; }
    if (f.dataset.catRename) {
      const c = cats.byId.get(Number(f.dataset.catRename));
      if (!c) return;
      if (name === c.name) { catAdm.edit = 0; renderCatTree(); return; }
      await catChange("category-save", { id: c.id, name, parent_id: c.parent_id || topParent() }, `نام دسته «${name}» شد.`);
    } else if (f.dataset.catAddsub) {
      await catChange("category-save", { name, parent_id: Number(f.dataset.catAddsub) }, `زیردستهٔ «${name}» ساخته شد.`);
    }
  });
  $("catTree").addEventListener("keydown", e => {
    if (e.key === "Escape" && e.target.closest(".cat-rename, .cat-add-sub")) { e.preventDefault(); catAdm.edit = 0; catAdm.addSub = 0; renderCatTree(); }
  });
  $("catTree").addEventListener("change", async e => {
    const sel = e.target.closest("[data-cat-move]");
    if (!sel || !sel.value) return;
    const c = cats.byId.get(Number(sel.closest("[data-cat-id]").dataset.catId)), to = cats.byId.get(Number(sel.value));
    if (!c || !to) return;
    if (!await catChange("category-save", { id: c.id, name: c.name, parent_id: to.id }, `«${c.name}» به «${to.name}» منتقل شد.`)) sel.value = "";
  });
  $("catTree").addEventListener("click", async e => {
    const btn = e.target.closest("[data-cat-act]");
    if (!btn || btn.disabled) return;
    const act = btn.dataset.catAct, li = btn.closest("[data-cat-id]"), c = li ? cats.byId.get(Number(li.dataset.catId)) : null;
    if (act === "cancel") { catAdm.edit = 0; catAdm.addSub = 0; renderCatTree(); return; }
    if (!c) return;
    if (act === "rename") { catAdm.edit = c.id; catAdm.addSub = 0; renderCatTree(); return; }
    if (act === "addsub") { catAdm.addSub = c.id; catAdm.edit = 0; renderCatTree(); return; }
    if (act === "up" || act === "down") {
      const sibs = (c.parent_id ? cats.kids.get(c.parent_id) : cats.tops).map(x => x.id);
      const i = sibs.indexOf(c.id), j = act === "up" ? i - 1 : i + 1;
      if (i < 0 || j < 0 || j >= sibs.length) return;
      [sibs[i], sibs[j]] = [sibs[j], sibs[i]];
      const r = await catChange("category-order", { parent_id: c.parent_id || topParent(), ids: sibs }, null, "order");
      if (r) { const b = $("catTree").querySelector(`[data-cat-id="${c.id}"] [data-cat-act="${act}"]`); (b && !b.disabled ? b : $("catTree").querySelector(`[data-cat-id="${c.id}"] .cat-mv:not([disabled])`))?.focus({ preventScroll: true }); }
      return;
    }
    if (act === "delete") openCatDelete(c);
  });
  // Delete: move the books to another category, or just unlink them. has_children from the server is explained.
  let catDelId = 0;
  function openCatDelete(c) {
    catDelId = c.id;
    const kids = (cats.kids.get(c.id) || []).length, n = catCount(c);
    $("catDelTitle").textContent = `حذف «${c.name}»`;
    $("catDelInfo").textContent = `${n ? `${faNum(n)} کتاب در این دسته است.` : "کتابی در این دسته نیست."}${kids ? ` این دسته ${faNum(kids)} زیردسته دارد.` : ""} با کتاب‌هایش چه شود؟`;
    const opts = cats.list.filter(x => x.id !== c.id && x.parent_id !== c.id);
    $("catDelMoveTo").innerHTML = '<option value="">انتخاب دستهٔ مقصد…</option>' + opts.map(x => `<option value="${x.id}">${esc(catPathOf(x))}</option>`).join("");
    $("catDelForm").elements.mode.value = "unlink";
    $("catDelMoveTo").disabled = !opts.length;
    $("catDelError").textContent = "";
    $("catDelDialog").showModal();
  }
  $("catDelMoveTo").addEventListener("change", () => { if ($("catDelMoveTo").value) $("catDelForm").elements.mode.value = "move"; });
  $("catDelClose").addEventListener("click", () => $("catDelDialog").close());
  $("catDelForm").addEventListener("submit", async e => {
    e.preventDefault();
    const c = cats.byId.get(catDelId);
    if (!c) { $("catDelDialog").close(); return; }
    const move = $("catDelForm").elements.mode.value === "move", to = Number($("catDelMoveTo").value);
    if (move && !to) { $("catDelError").textContent = "دستهٔ مقصد را انتخاب کن."; $("catDelMoveTo").focus(); return; }
    $("catDelOk").disabled = true; $("catDelError").textContent = "";
    try {
      const r = await api("category-delete", { method: "POST", body: move ? { id: c.id, move_to: to } : { id: c.id } });
      $("catDelDialog").close();
      const moved = Number(r && r.moved) || 0, unlinked = Number(r && r.unlinked) || 0;
      showToast(`دستهٔ «${c.name}» حذف شد` + (moved ? ` · ${faNum(moved)} کتاب به «${catPathOf(to)}» منتقل شد` : "") + (unlinked ? ` · از ${faNum(unlinked)} کتاب برداشته شد` : "") + ".");
      catalogLoaded = false; catalogState.at = 0; relatedCache.clear();
      if (catalogFilter.cat === String(c.id)) catalogFilter.cat = "";
      await loadCategories(true);
      renderCatTree();
    } catch (err) {
      $("catDelError").textContent = catErrorMessage(err, "delete");
    } finally { $("catDelOk").disabled = false; }
  });
  /** Admin views that show categories, after the list changed (or arrived). */
  function admCatsChanged() {
    if (adm.tab === "cats") renderCatTree();
    if (adm.tab === "books") { admRenderCatSelect(adm.data && adm.data.facets ? adm.data.facets.categories : null); admSyncControls(); admRenderBulk(); }
    if (adm.detail && $("admDetail").open) admRenderDetail();
  }

  // Pick categories in a dialog: several (review queue «C») or one (bulk «افزودن/برداشتن دسته…»).
  let catPickResolve = null, catPickSingle = false, catPicker = null;
  async function askCategories({ title, ids = [], single = false, ok = "ثبت" }) {
    await loadCategories();
    catPickSingle = single;
    catPicker = makeCatPicker($("catPickBox"), { single, label: title });
    catPicker.set(ids);
    $("catPickTitle").textContent = title;
    $("catPickOk").textContent = ok;
    return new Promise(res => {
      catPickResolve = res;
      $("catPickDialog").showModal();
      catPicker.focus();
    });
  }
  $("catPickForm").addEventListener("submit", e => {
    e.preventDefault();
    const ids = catPicker ? catPicker.get() : [];
    if (catPickSingle && !ids.length) { catPicker.focus(); return; }
    const r = catPickResolve; catPickResolve = null;
    $("catPickDialog").close();
    if (r) r(catPickSingle ? ids[0] : ids);
  });
  $("catPickClose").addEventListener("click", () => $("catPickDialog").close());
  $("catPickDialog").addEventListener("close", () => { if (catPickResolve) { const r = catPickResolve; catPickResolve = null; r(null); } });
  const admDetailPicker = makeCatPicker($("admDetailCats"), { label: "دسته‌های کتاب" });

  // v8.4: client error reports, grouped by fingerprint (newest first), filter by app version.
  let errPage = 1;
  const fmtDateTime = s => new Date(s * 1000).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" });
  async function loadErrors(reset) {
    if (reset) errPage = 1;
    const version = $("errorsVersion").value;
    let r;
    try { r = await api("errors", { query: `&page=${errPage}&per=30${version ? `&version=${encodeURIComponent(version)}` : ""}` }); }
    catch (err) { $("errorsList").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; return; }
    const fa = n => Number(n).toLocaleString("fa-IR");
    const sel = $("errorsVersion"), keep = sel.value;
    sel.innerHTML = '<option value="">همهٔ نسخه‌ها</option>' + r.versions.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    sel.value = r.versions.includes(keep) ? keep : "";
    $("errorsCount").textContent = fa(r.total);
    $("errorsCount").classList.toggle("off", r.total > 0);
    if (!version) setAdmBadge("errors", r.total);
    const html = r.errors.map(e => `<div class="admin-item err-item" data-err-fp="${esc(e.fingerprint)}">
        <details class="err-details"><summary><strong class="err-msg" dir="auto">${esc(e.message)}</strong>
          <span class="status-chip">${esc(e.kind)}</span>${e.app_version ? ` <span class="status-chip cat-chip" dir="ltr">${esc(e.app_version)}</span>` : ""}
          <span class="muted small err-count">× ${fa(e.count)} · آخرین بار ${fmtDateTime(e.last_seen)}</span></summary>
          <div class="err-meta muted small">
            <div>اولین بار ${fmtDateTime(e.first_seen)} · ${e.users ? `${fa(e.users)} کاربر` : ""}${e.users && e.anonymous ? " + " : ""}${e.anonymous ? "بازدیدکنندهٔ ناشناس" : ""}</div>
            ${e.source ? `<div>منبع: <code dir="ltr">${esc(e.source)}</code></div>` : ""}
            ${e.url_path ? `<div>صفحه: <code dir="ltr">${esc(e.url_path)}</code></div>` : ""}
            ${e.user_agent ? `<div>مرورگر: <code dir="ltr">${esc(e.user_agent)}</code></div>` : ""}
            ${e.stack ? `<pre class="err-stack" dir="ltr">${esc(e.stack)}</pre>` : ""}
          </div></details>
        <button class="secondary-button" type="button" data-err-clear="${esc(e.fingerprint)}">پاک کردن</button></div>`).join("");
    if (errPage === 1) $("errorsList").innerHTML = html || '<p class="muted">خطایی ثبت نشده است.</p>';
    else $("errorsList").insertAdjacentHTML("beforeend", html);
    $("errorsMore").hidden = errPage * r.per >= r.total;
  }
  $("errorsVersion").addEventListener("change", () => loadErrors(true));
  $("errorsRefresh").addEventListener("click", () => loadErrors(true));
  $("errorsMore").addEventListener("click", () => { errPage++; loadErrors(false); });
  $("errorsClearAll").addEventListener("click", () => {
    if (!confirm("همهٔ خطاهای ثبت‌شده پاک شوند؟")) return;
    adminAction("errors-clear", { all: true }, "خطاها پاک شد.");
  });
  $("errorsList").addEventListener("click", e => {
    const btn = e.target.closest("[data-err-clear]");
    if (btn) adminAction("errors-clear", { fingerprint: btn.dataset.errClear }, "خطا پاک شد.");
  });
  $("pendingList").addEventListener("click", async e => {
    const btn = e.target.closest("[data-review]");
    if (!btn) return;
    try {
      await api("catalog-review", { method: "POST", body: { book_id: Number(btn.dataset.review), approve: btn.dataset.approve === "1" } });
      showToast(btn.dataset.approve === "1" ? "تأیید شد و در کتابخانهٔ عمومی قرار گرفت." : "رد شد.");
      catalogLoaded = false;
      loadAdmin();
    } catch (err) { showToast(apiMessage(err)); }
  });
  async function adminAction(route, body, okMessage) {
    try { const r = await api(route, { method: "POST", body }); if (okMessage) showToast(okMessage); loadAdmin(); return r; }
    catch (err) { showToast(apiMessage(err)); return null; }
  }
  $("settingApproval").addEventListener("change", e => adminAction("settings", { catalog_requires_approval: e.target.checked }, "تنظیمات ذخیره شد."));
  $("viewAdmin").addEventListener("click", async e => {
    const t = e.target;
    if (t.closest("[data-invite-revoke]")) return adminAction("invite-revoke", { id: t.closest("[data-invite-revoke]").dataset.inviteRevoke }, "دعوت‌نامه باطل شد.");
    const act = t.closest("[data-user-act]")?.dataset.userAct;
    if (act) {
      const userId = Number(t.closest("[data-user]").dataset.user);
      if (act === "enable" || act === "disable") return adminAction("user-update", { user_id: userId, disabled: act === "disable" }, act === "disable" ? "کاربر غیرفعال شد." : "کاربر فعال شد.");
      if (act === "promote" || act === "demote") return adminAction("user-update", { user_id: userId, role: act === "promote" ? "admin" : "user" }, "نقش کاربر تغییر کرد.");
      if (act === "reset") {
        if (!confirm("رمز این کاربر ریست شود؟ از همهٔ دستگاه‌ها خارج می‌شود.")) return;
        const r = await adminAction("user-reset-password", { user_id: userId });
        if (r) prompt("رمز جدید (برای کاربر بفرست):", r.password);
        return;
      }
      if (act === "delete") {
        if (!confirm("این کاربر و کتابخانه‌اش حذف شود؟ این کار برگشت‌پذیر نیست.")) return;
        return adminAction("user-delete", { user_id: userId }, "کاربر حذف شد.");
      }
    }
  });
  $("createInvite").addEventListener("click", async () => {
    try {
      const r = await api("invites", { method: "POST", body: { note: $("inviteNote").value.trim(), days: 14 } });
      $("inviteResult").textContent = `کد دعوت: ${r.code}`;
      $("inviteNote").value = "";
      loadAdmin();
    } catch (err) { showToast(apiMessage(err)); }
  });

  // ---------- v8.7 API keys: other programs send books straight to the public catalog (route api-book) ----------
  // The plaintext key is shown once after creation; the server stores only its hash. Timestamps are Unix seconds.
  const tsSec = v => { const n = Number(v) || 0; return n > 1e12 ? n / 1000 : n; };
  function hideNewApiToken() { $("apiTokenNew").hidden = true; $("apiTokenValue").value = ""; }
  async function loadApiTokens() {
    let r;
    try { r = await api("api-tokens"); }
    catch (err) {
      $("apiTokenList").innerHTML = `<p class="muted">${esc(err instanceof ApiFail && err.status === 404 ? "این بخش هنوز روی سرور فعال نیست." : apiMessage(err))}</p>`;
      return;
    }
    const tokens = Array.isArray(r && r.tokens) ? r.tokens : [];
    $("apiTokenList").innerHTML = tokens.map(t => {
      const name = String(t.name || "بدون نام"), last = tsSec(t.last_used_at), created = tsSec(t.created_at);
      return `<div class="admin-item api-item${t.revoked ? " revoked" : ""}" data-token="${esc(t.id)}">
        <span><strong dir="auto">${esc(name)}</strong> <span class="status-chip${t.revoked ? " off" : ""}">${t.revoked ? "لغوشده" : "فعال"}</span>
          <span class="status-chip cat-chip">${t.require_review ? "منتظر تأیید" : "انتشار مستقیم"}</span><br>
          <span class="muted small">ساخته‌شده ${created ? fmtDate(created) : "—"} · آخرین استفاده ${last ? fmtDateTime(last) : "هرگز"} · ${faNum(Number(t.uses) || 0)} بار استفاده</span></span>
        ${t.revoked ? "" : `<button class="secondary-button" type="button" data-token-revoke="${esc(t.id)}" data-name="${esc(name)}">لغو</button>`}</div>`;
    }).join("") || '<p class="muted">هنوز کلیدی ساخته نشده.</p>';
  }
  $("apiTokenForm").addEventListener("submit", async e => {
    e.preventDefault();
    const name = $("apiTokenName").value.replace(/\s+/g, " ").trim();
    if (!name) { $("apiTokenError").textContent = "برای کلید یک نام بنویس (مثلاً نام برنامه)."; $("apiTokenName").focus(); return; }
    $("apiTokenError").textContent = ""; $("apiTokenCreate").disabled = true;
    try {
      const r = await api("api-token-create", { method: "POST", body: { name, require_review: $("apiTokenReview").checked } });
      if (!r || typeof r.token !== "string" || !r.token) throw new ApiFail(500, "bad_response", "پاسخ سرور کلید نداشت.");
      $("apiTokenValue").value = r.token; $("apiTokenNew").hidden = false;
      $("apiTokenName").value = ""; $("apiTokenReview").checked = false;
      $("apiTokenValue").focus(); $("apiTokenValue").select();
      loadApiTokens();
    } catch (err) {
      $("apiTokenError").textContent = apiMessage(err);
      if (!(err instanceof ApiFail) || err.status >= 500) reportError("api", err, "api-token-create");
    } finally { $("apiTokenCreate").disabled = false; }
  });
  $("apiTokenCopy").addEventListener("click", async () => {
    const field = $("apiTokenValue");
    if (!field.value) return;
    field.focus(); field.select();
    let ok = false;
    try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(field.value); ok = true; } } catch (_) {}
    if (!ok) { try { ok = document.execCommand("copy"); } catch (_) {} }
    showToast(ok ? "کلید کپی شد." : "کلید انتخاب شد؛ آن را کپی کن.");
  });
  $("apiTokenList").addEventListener("click", async e => {
    const btn = e.target.closest("[data-token-revoke]");
    if (!btn) return;
    if (!confirm(`کلید «${btn.dataset.name}» لغو شود؟ برنامه‌ای که از آن استفاده می‌کند دیگر نمی‌تواند کتاب بفرستد.`)) return;
    btn.disabled = true;
    try {
      const id = /^\d+$/.test(btn.dataset.tokenRevoke) ? Number(btn.dataset.tokenRevoke) : btn.dataset.tokenRevoke;
      await api("api-token-revoke", { method: "POST", body: { id } });
      showToast("کلید لغو شد.");
    } catch (err) { showToast(apiMessage(err)); btn.disabled = false; }
    loadApiTokens();
  });
  $("apiCurl").textContent = `curl -H "Authorization: Bearer <کلید>" \\\n  -F "file=@book.epub" \\\n  "${API}?r=api-book"`;

  // ======================= v8.3 text cleanup («پاک‌سازی متن») =======================
  // Rules remove only the matched phrase from the EPUB file itself (no original is kept). They apply to every
  // book: on demand (search → preview → apply) and automatically to newly imported books.
  const CLEAN_KEY = "borg-clean-rules" + STORAGE_SUFFIX;
  const CLEAN_MIN_LEN = 4, CLEAN_MAX_HITS = 300, CLEAN_MAX_SHARE = 0.02, CLEAN_SNIPPETS = 5;
  const TG_LABEL = "لینک‌های تلگرام";
  // t.me / telegram.me / telegram.dog URLs (with or without scheme) and @handles of 5–32 [A-Za-z0-9_] characters.
  const TG_SOURCE = String.raw`(?:(?<![A-Za-z0-9_.\-@])https?:\/\/(?:www\.)?|(?<![A-Za-z0-9_.\-\/@:])(?:www\.)?)(?:t\.me|telegram\.me|telegram\.dog)\/[A-Za-z0-9_\-+\/?=&%.#~]*[A-Za-z0-9_\-\/]|(?<![A-Za-z0-9_.@\/])@[A-Za-z0-9_]{5,32}(?![A-Za-z0-9_])`;
  function loadCleanRules() {
    try { const r = JSON.parse(localStorage.getItem(CLEAN_KEY) || "[]"); return Array.isArray(r) ? r.filter(x => x && (x.type === "telegram" || ((x.type === "phrase" || x.type === "line") && typeof x.text === "string"))) : []; }
    catch (_) { return []; }
  }
  /** Every local change is mirrored to the server for an admin (v8.7); `fromServer` skips that when adopting server rules. */
  function saveCleanRules(rules, fromServer) {
    try { localStorage.setItem(CLEAN_KEY, JSON.stringify(rules)); } catch (_) {}
    if (!fromServer) scheduleCleanPush();
  }
  const ruleLabel = r => r.type === "telegram" ? TG_LABEL : r.type === "line" ? `کل خطِ شامل «${r.text}»` : `«${r.text}»`;
  // "line" rules remove the smallest block (paragraph, list item, heading, div…) whose text contains the fixed part,
  // whatever else (e.g. a changing URL) is in it; blocks much longer than the fixed part are left alone.
  const CLEAN_LINE_EXTRA = 400;
  const LINE_BLOCKS = new Set(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "th", "dd", "dt", "figcaption", "pre", "caption", "aside", "footer", "header", "section", "article"]);
  const squash = t => String(t || "").replace(/[\s\u00a0]+/g, " ").trim();
  function cleanLines(root, rules, stats) {
    let changed = false;
    for (const r of rules) {
      if (r.type !== "line") continue;
      const needle = squash(r.text), s = stats[r.id];
      if (!needle) continue;
      const hits = [...root.querySelectorAll("*")].filter(el => LINE_BLOCKS.has((el.localName || "").toLowerCase()) && squash(el.textContent).includes(needle));
      const deepest = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
      for (const el of deepest) {
        const text = squash(el.textContent);
        if (text.length > needle.length + CLEAN_LINE_EXTRA) { s.tooLong = (s.tooLong || 0) + 1; continue; }
        s.count++; s.chars += text.length;
        if (s.snippets.length < CLEAN_SNIPPETS) s.snippets.push({ before: "", match: text.length > 160 ? text.slice(0, 157) + "…" : text, after: "" });
        el.parentNode && el.parentNode.removeChild(el); changed = true;
      }
    }
    return changed;
  }
  function ruleRegex(r) {
    return r.type === "telegram" ? new RegExp(TG_SOURCE, "g") : new RegExp(r.text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g");
  }
  const XHTML_TYPES = ["application/xhtml+xml", "text/html"];
  function zipResolve(base, href) {
    const parts = (base + href.split("#")[0]).split("/"), out = [];
    for (const p of parts) { if (p === "..") out.pop(); else if (p && p !== ".") out.push(p); }
    return out.join("/");
  }
  /** Content documents of an EPUB as [{path, type}] from the OPF manifest (fallback: every .xhtml/.html file). */
  async function epubContentFiles(zip) {
    try {
      const container = new DOMParser().parseFromString(await zip.file("META-INF/container.xml").async("string"), "application/xml");
      const opfPath = container.getElementsByTagNameNS("*", "rootfile")[0].getAttribute("full-path");
      const opf = new DOMParser().parseFromString(await zip.file(opfPath).async("string"), "application/xml");
      const base = opfPath.includes("/") ? opfPath.slice(0, opfPath.lastIndexOf("/") + 1) : "";
      const out = [];
      for (const it of opf.getElementsByTagNameNS("*", "item")) {
        const type = it.getAttribute("media-type") || "", href = it.getAttribute("href");
        if (!href || !XHTML_TYPES.includes(type)) continue;
        let path; try { path = zipResolve(base, decodeURIComponent(href)); } catch (_) { path = zipResolve(base, href); }
        if (zip.file(path)) out.push({ path, type });
      }
      if (out.length) return out;
    } catch (_) { /* no usable OPF: fall through */ }
    return Object.keys(zip.files).filter(n => /\.x?html?$/i.test(n) && !zip.files[n].dir).map(n => ({ path: n, type: /\.xhtml$/i.test(n) ? "application/xhtml+xml" : "text/html" }));
  }
  /** Remove rule matches from one content document. Updates stats; returns {chars, output|null}. */
  function cleanDocument(src, type, rules, stats) {
    const xml = type === "application/xhtml+xml";
    let doc = new DOMParser().parseFromString(src, xml ? "application/xhtml+xml" : "text/html"), viaHtml = false;
    if (xml && doc.getElementsByTagName("parsererror").length) { doc = new DOMParser().parseFromString(src, "text/html"); viaHtml = true; }
    const root = doc.body || doc.documentElement;
    if (!root) return { chars: 0, output: null };
    const allChars = squash(root.textContent).length;
    let changed = cleanLines(root, rules, stats);
    const textRules = rules.filter(r => r.type !== "line");
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const touched = new Set(); let chars = 0;
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const parentName = (n.parentNode && n.parentNode.localName || "").toLowerCase();
      if (parentName === "script" || parentName === "style") continue;
      let v = n.nodeValue; chars += v.length;
      const orig = v;
      for (const r of textRules) {
        const s = stats[r.id];
        v = v.replace(ruleRegex(r), (m, ...rest) => {
          const off = rest[rest.length - 2], str = rest[rest.length - 1];
          s.count++; s.chars += m.length;
          if (s.snippets.length < CLEAN_SNIPPETS) s.snippets.push({ before: str.slice(Math.max(0, off - 32), off), match: m, after: str.slice(off + m.length, off + m.length + 32) });
          return "";
        });
      }
      if (v !== orig) {
        n.nodeValue = v; changed = true;
        for (let p = n.parentNode; p && p !== root; p = p.parentNode) if ((p.localName || "").toLowerCase() === "a") { touched.add(p); break; }
      }
    }
    chars = Math.max(chars, allChars);
    if (!changed) return { chars, output: null };
    // A link whose text became empty (e.g. <a href="https://t.me/x">https://t.me/x</a>) is removed with it.
    for (const a of touched) {
      if (!a.textContent.trim() && !a.querySelector("img,svg,image,video,audio,object")) a.parentNode && a.parentNode.removeChild(a);
    }
    let output;
    if (xml) {
      const decl = (/^\s*<\?xml[^?]*\?>/.exec(src) || [""])[0];
      const doctype = viaHtml ? ((/<!DOCTYPE[^>]*>/i.exec(src) || [""])[0]) : "";
      // Some engines emit their own XML declaration; keep exactly one (the original's).
      const body = new XMLSerializer().serializeToString(viaHtml ? doc.documentElement : doc).replace(/^\s*<\?xml[^?]*\?>\s*/, "");
      output = (decl ? decl + "\n" : "") + (doctype ? doctype + "\n" : "") + body;
    } else {
      output = (doc.doctype ? "<!DOCTYPE html>\n" : "") + doc.documentElement.outerHTML;
    }
    return { chars, output };
  }
  /**
   * Run rules over every content document of an EPUB (ArrayBuffer). Stats per rule id: {count, chars, snippets}.
   * With write: also returns the rebuilt EPUB as a Blob (mimetype first and STORED, the rest DEFLATE).
   */
  async function cleanEpub(buf, rules, write) {
    const zip = await JSZip.loadAsync(buf);
    const stats = {}; rules.forEach(r => { stats[r.id] = { count: 0, chars: 0, snippets: [] }; });
    let totalChars = 0; const changed = new Map();
    for (const { path, type } of await epubContentFiles(zip)) {
      let src; try { src = await zip.file(path).async("string"); } catch (_) { continue; }
      const res = cleanDocument(src, type, rules, stats);
      totalChars += res.chars;
      if (res.output !== null) changed.set(path, res.output);
    }
    const total = Object.values(stats).reduce((n, s) => n + s.count, 0);
    if (!write || !changed.size) return { stats, totalChars, total, blob: null };
    const out = new JSZip(), mt = zip.file("mimetype");
    out.file("mimetype", mt ? (await mt.async("string")).trim() || "application/epub+zip" : "application/epub+zip", { compression: "STORE" });
    for (const name of Object.keys(zip.files)) {
      const e = zip.files[name];
      if (e.dir || name === "mimetype") continue;
      out.file(name, changed.has(name) ? changed.get(name) : await e.async("uint8array"), { date: e.date });
    }
    const blob = await out.generateAsync({ type: "blob", mimeType: "application/epub+zip", compression: "DEFLATE", compressionOptions: { level: 6 } });
    return { stats, totalChars, total, blob };
  }
  // Whole-line rules remove whole lines by design (e.g. one footer line per article ≈ 2% of a magazine), so their
  // share limit is 10%; phrases keep the 2% limit. Both are capped at CLEAN_MAX_HITS matches per book.
  const isTooBroad = (s, totalChars, rule) => s.count > CLEAN_MAX_HITS || (totalChars > 0 && s.chars > totalChars * (rule && rule.type === "line" ? 0.1 : CLEAN_MAX_SHARE));

  // ---- Re-anchoring annotations by their quote ----
  // Display-time changes (RLM→ZWNJ, Persian digits, ي/ك) are folded char-for-char so offsets stay aligned.
  const FOLD = { "‏": "‌", "ي": "ی", "ى": "ی", "ك": "ک" };
  function foldChar(c) {
    if (FOLD[c]) return FOLD[c];
    const d = c.charCodeAt(0);
    if (d >= 48 && d <= 57) return String.fromCharCode(0x06F0 + d - 48);
    if (d >= 0x0660 && d <= 0x0669) return String.fromCharCode(0x06F0 + d - 0x0660);
    return c;
  }
  function docTextNodes(doc) {
    const root = doc.body || doc.getElementsByTagName("body")[0] || doc.documentElement, nodes = [];
    const w = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) nodes.push(n);
    return nodes;
  }
  function textOffsetOf(doc, node, offset) {
    let pos = 0;
    for (const n of docTextNodes(doc)) { if (n === node) return pos + offset; pos += n.nodeValue.length; }
    return -1;
  }
  /** A Range over `quote` in doc (whitespace-insensitive); the occurrence nearest to `hint` (raw text offset) wins. */
  function findQuoteRange(doc, quote, hint) {
    const nodes = docTextNodes(doc), map = [], starts = [];
    let norm = "", pos = 0;
    nodes.forEach((n, k) => {
      starts.push(pos); const v = n.nodeValue;
      for (let j = 0; j < v.length; j++) {
        if (/\s/.test(v[j])) { if (norm && norm[norm.length - 1] !== " ") { norm += " "; map.push([k, j]); } }
        else { norm += foldChar(v[j]); map.push([k, j]); }
      }
      pos += v.length;
    });
    const q = [...String(quote).replace(/\s+/g, " ").trim()].map(foldChar).join("");
    if (!q) return null;
    let best = -1, bestDist = Infinity;
    for (let i = norm.indexOf(q); i >= 0; i = norm.indexOf(q, i + 1)) {
      const [k, j] = map[i], dist = hint >= 0 ? Math.abs(starts[k] + j - hint) : 0;
      if (dist < bestDist) { best = i; bestDist = dist; }
      if (hint < 0) break;
    }
    if (best < 0) return null;
    const [k1, j1] = map[best], [k2, j2] = map[best + q.length - 1];
    const range = doc.createRange();
    range.setStart(nodes[k1], j1); range.setEnd(nodes[k2], j2 + 1);
    return range;
  }
  /** New CFIs for the book's annotations, found by quote in the same spine item of the cleaned file. */
  async function reanchorAnnotations(bookId, oldBuf, newBuf) {
    const anns = (await getAnnotations(bookId)).filter(a => a.cfi && a.quote && String(a.quote).trim());
    if (!anns.length) return [];
    const oldB = ePub(oldBuf), newB = ePub(newBuf), updated = [], loaded = [];
    try {
      await Promise.all([oldB.opened, newB.opened]);
      const load = async (book, sec) => { if (!sec) return null; if (!sec.document) { await sec.load(book.load.bind(book)); loaded.push(sec); } return sec.document; };
      for (const a of anns) {
        try {
          const sec = newB.spine.get(a.cfi);
          const doc = await load(newB, sec);
          if (!doc) continue;
          let hint = -1;
          try {
            const od = await load(oldB, oldB.spine.get(a.cfi));
            const r = od && new ePub.CFI(a.cfi).toRange(od);
            if (r) hint = textOffsetOf(od, r.startContainer, r.startOffset);
          } catch (_) {}
          const range = findQuoteRange(doc, a.quote, hint);
          if (!range) continue; // quote not found (e.g. it contained the removed phrase): keep the old CFI
          if (a.type === "bookmark") range.collapse(true);
          const cfi = sec.cfiFromRange(range);
          if (cfi && cfi !== a.cfi) updated.push({ ...a, cfi });
        } catch (err) { console.warn("Re-anchoring failed:", err); }
      }
    } finally {
      loaded.forEach(s => { try { s.unload(); } catch (_) {} });
      try { oldB.destroy(); } catch (_) {}
      try { newB.destroy(); } catch (_) {}
    }
    return updated;
  }

  // ---- Applying to a stored book ----
  /** Clean one stored book with `rules`. Returns the number of removed matches (0 = file untouched). */
  async function applyCleanToBook(bookId, rules) {
    const rec = await getBook(bookId);
    const file = fileOf(rec);
    if (!rec || !file || !rules.length || isPdfRecord(rec)) return 0; // v8.8: PDFs are never cleaned
    const oldBuf = await file.arrayBuffer();
    const res = await cleanEpub(oldBuf, rules, true);
    if (!res.blob || !res.total) return 0;
    const newBuf = await res.blob.arrayBuffer();
    const hash = await sha256(newBuf);
    const anns = await reanchorAnnotations(bookId, oldBuf, newBuf);
    const latest = (await getBook(bookId)) || rec;
    const name = (file.name || latest.fileName || "book.epub");
    const upd = { ...latest, file: new File([res.blob], name, { type: "application/epub+zip" }), size: res.blob.size, contentHash: hash,
                  sourceHash: latest.sourceHash || latest.contentHash || null, cleanedAt: Date.now() };
    delete upd.fileBuffer; delete upd.fileName; delete upd.locations; delete upd.dirCache;
    if (upd.serverId) { upd.cleanPending = true; upd.cleanAnns = [...new Set([...(latest.cleanAnns || []), ...anns.map(a => a.uid)])]; }
    await put("books", upd);
    for (const a of anns) await saveAnnotation(a);
    if (currentBook && currentBook.id === bookId && rendition) reopenCurrentBook();
    if (upd.serverId && account && navigator.onLine) {
      try { await replaceOnServer(await getBook(bookId)); } catch (err) { console.warn("Book replace failed (will retry on sync):", err); }
    }
    return res.total;
  }
  /**
   * Synced book whose file was cleaned: upload the new file, then `book-replace` moves progress, annotations,
   * shelves (and, for an admin, the catalog entry) to it. Annotations re-anchored locally are pushed afterwards
   * with fresh timestamps (the server bumps the moved ones to "now").
   */
  async function replaceOnServer(b) {
    if (!b || !b.cleanPending || !b.serverId || !account) return null;
    const sb = await uploadBook(b);
    let book = sb;
    if (sb.id !== b.serverId) {
      const r = await api("book-replace", { method: "POST", body: { old_book_id: b.serverId, new_book_id: sb.id } });
      book = (r && r.book) || sb;
    }
    const cur = (await getBook(b.id)) || b;
    const upd = { ...cur, serverId: book.id, cleanPending: false, cleanAnns: [] };
    if (book.catalog_status !== undefined) upd.catalogStatus = book.catalog_status || null;
    await put("books", upd);
    for (const uid of cur.cleanAnns || []) { const a = await getAnnotation(uid); if (a && !a.deleted) await saveAnnotation({ ...a }); }
    scheduleAnnotationPush();
    return book;
  }
  /** After an import: apply the saved rules, skipping rules that look too broad for this book. */
  async function autoCleanImported(bookId) {
    const rules = loadCleanRules();
    if (!rules.length) return 0;
    try {
      const rec = await getBook(bookId), file = fileOf(rec);
      if (!file || isPdfRecord(rec)) return 0;
      const dry = await cleanEpub(await file.arrayBuffer(), rules, false);
      const ok = rules.filter(r => dry.stats[r.id].count && !isTooBroad(dry.stats[r.id], dry.totalChars, r));
      return ok.length ? await applyCleanToBook(bookId, ok) : 0;
    } catch (err) { console.warn("Auto cleanup failed:", err); return 0; }
  }

  // ---- Dialog ----
  let cleanScan = null, cleanBusy = false; // cleanScan: { rules, books: [{id, title, stats, totalChars}], broad: Set(ruleId) }
  function renderCleanRules() {
    const rules = loadCleanRules();
    $("cleanRules").innerHTML = rules.length
      ? rules.map(r => `<li><span dir="auto">${esc(ruleLabel(r))}</span><button type="button" class="text-button" data-clean-del="${esc(r.id)}" aria-label="حذف قاعدهٔ ${esc(ruleLabel(r))}">حذف</button></li>`).join("")
      : '<li class="muted">هنوز قاعده‌ای نیست.</li>';
    $("cleanPresetTelegram").disabled = rules.some(r => r.type === "telegram");
    $("cleanSearch").disabled = !rules.length || cleanBusy;
    renderCleanSyncNote();
  }
  function resetCleanResults(status) {
    cleanScan = null; $("cleanResults").innerHTML = ""; $("cleanApplyRow").hidden = true;
    $("cleanStatus").textContent = status || "";
  }
  function setCleanProgress(done, total, text) {
    $("cleanProgressWrap").hidden = total === null;
    if (total === null) return;
    $("cleanProgress").max = Math.max(1, total); $("cleanProgress").value = done;
    $("cleanProgressText").textContent = text;
  }
  function cleanIncluded(ruleId) {
    if (!cleanScan || !cleanScan.broad.has(ruleId)) return true;
    const box = $("cleanResults").querySelector(`[data-clean-broad="${CSS.escape(ruleId)}"]`);
    return !!(box && box.checked);
  }
  function renderCleanResults() {
    if (!cleanScan) return;
    const { rules, books, broad } = cleanScan;
    const warn = rules.filter(r => broad.has(r.id)).map(r => {
      const top = books.reduce((m, b) => (b.stats[r.id].count > (m ? m.stats[r.id].count : -1) ? b : m), null);
      return `<label class="clean-broad"><input type="checkbox" data-clean-broad="${esc(r.id)}"> <span><b dir="auto">${esc(ruleLabel(r))}</b> احتمالاً بیش از حد کلی است`
        + (top ? ` (${faNum(top.stats[r.id].count)} مورد در «${esc(top.title)}»)` : "") + `؛ با این حال حذف شود</span></label>`;
    }).join("");
    const list = books.map(b => {
      const live = rules.filter(r => !broad.has(r.id) || cleanIncluded(r.id));
      const count = live.reduce((n, r) => n + b.stats[r.id].count, 0);
      if (!count && !live.some(r => b.stats[r.id].tooLong)) return "";
      const snips = live.flatMap(r => b.stats[r.id].snippets).slice(0, CLEAN_SNIPPETS)
        .map(s => `<li dir="auto">…${esc(s.before)}<mark>${esc(s.match)}</mark>${esc(s.after)}…</li>`).join("");
      const long = live.reduce((n, r) => n + (b.stats[r.id].tooLong || 0), 0);
      return `<div class="clean-book" data-clean-id="${esc(b.id)}"><label class="clean-book-head"><input type="checkbox" data-clean-book="${esc(b.id)}" checked>`
        + ` <b dir="auto">${esc(b.title)}</b> <span class="clean-count">${faNum(count)} مورد</span></label><ul class="clean-snips">${snips}</ul>`
        + (long ? `<p class="muted small">${faNum(long)} خط خیلی بلند است و حذف نمی‌شود.</p>` : "") + `</div>`;
    }).filter(Boolean);
    const keep = new Map([...$("cleanResults").querySelectorAll("[data-clean-broad]")].map(x => [x.dataset.cleanBroad, x.checked]));
    const keepBooks = new Map([...$("cleanResults").querySelectorAll("[data-clean-book]")].map(x => [x.dataset.cleanBook, x.checked]));
    $("cleanResults").innerHTML = warn + (list.length ? list.join("") : '<p class="muted">موردی پیدا نشد.</p>');
    $("cleanResults").querySelectorAll("[data-clean-broad]").forEach(x => { if (keep.has(x.dataset.cleanBroad)) x.checked = keep.get(x.dataset.cleanBroad); });
    $("cleanResults").querySelectorAll("[data-clean-book]").forEach(x => { if (keepBooks.has(x.dataset.cleanBook)) x.checked = keepBooks.get(x.dataset.cleanBook); });
    $("cleanApplyRow").hidden = !list.length;
  }
  async function runCleanSearch() {
    const rules = loadCleanRules();
    if (!rules.length || cleanBusy || !db) return;
    if (!librariesReady()) { showToast("کتاب‌خوان کامل بارگذاری نشده است. صفحه را تازه‌سازی کن."); return; }
    cleanBusy = true; renderCleanRules(); resetCleanResults("");
    try {
      const all = (await getAllBooks()).filter(b => fileOf(b) && !isPdfRecord(b)); // v8.8: cleanup is for EPUB text only
      const books = [], broad = new Set();
      for (let i = 0; i < all.length; i++) {
        setCleanProgress(i, all.length, `در حال بررسی ${faNum(i + 1)} از ${faNum(all.length)}: «${all[i].title}»`);
        await sleep(0); // keep the page responsive between books
        try {
          const r = await cleanEpub(await fileOf(all[i]).arrayBuffer(), rules, false);
          rules.forEach(rule => { if (isTooBroad(r.stats[rule.id], r.totalChars, rule)) broad.add(rule.id); });
          if (r.total) books.push({ id: all[i].id, title: all[i].title || "بدون عنوان", stats: r.stats, totalChars: r.totalChars });
        } catch (err) { console.warn("Cleanup scan failed:", all[i].title, err); }
      }
      cleanScan = { rules, books, broad };
      renderCleanResults();
      $("cleanStatus").textContent = all.length ? `${faNum(all.length)} کتاب بررسی شد.` : "کتابی در کتابخانه نیست.";
    } finally { cleanBusy = false; setCleanProgress(0, null); renderCleanRules(); }
  }
  async function runCleanApply() {
    if (!cleanScan || cleanBusy) return;
    const rules = cleanScan.rules.filter(r => cleanIncluded(r.id));
    const ids = [...$("cleanResults").querySelectorAll("[data-clean-book]:checked")].map(x => x.dataset.cleanBook);
    if (!rules.length || !ids.length) { showToast("کتابی انتخاب نشده است."); return; }
    cleanBusy = true; renderCleanRules(); $("cleanApply").disabled = true;
    let removed = 0, booksDone = 0;
    try {
      for (let i = 0; i < ids.length; i++) {
        const b = cleanScan.books.find(x => x.id === ids[i]);
        setCleanProgress(i, ids.length, `در حال پاک‌سازی ${faNum(i + 1)} از ${faNum(ids.length)}: «${b ? b.title : ""}»`);
        await sleep(0);
        try { const n = await applyCleanToBook(ids[i], rules); removed += n; if (n) booksDone++; }
        catch (err) { console.error("Cleanup failed:", err); if (err && err.name !== "QuotaExceededError") reportError("cleanup", err, "cleanup:apply"); showToast(err && err.name === "QuotaExceededError" ? "فضای ذخیره‌سازی مرورگر کافی نیست." : "پاک‌سازی یکی از کتاب‌ها انجام نشد."); }
      }
    } finally { cleanBusy = false; $("cleanApply").disabled = false; setCleanProgress(0, null); renderCleanRules(); }
    resetCleanResults(`${faNum(removed)} مورد از ${faNum(booksDone)} کتاب حذف شد.`);
    showToast(`${faNum(removed)} مورد حذف شد`);
    renderLibrary();
  }
  $("cleanOpen").addEventListener("click", () => { if (cleanSync.dirty && isAdmin()) pushCleanRules(); renderCleanRules(); $("cleanError").textContent = ""; $("cleanDialog").showModal(); $("cleanPhrase").focus(); });
  $("cleanClose").addEventListener("click", () => $("cleanDialog").close());
  $("cleanForm").addEventListener("submit", e => {
    e.preventDefault();
    const text = $("cleanPhrase").value.trim(), rules = loadCleanRules(), mode = $("cleanMode").value === "line" ? "line" : "phrase";
    if ([...text].length < CLEAN_MIN_LEN) { $("cleanError").textContent = `عبارت باید دست‌کم ${faNum(CLEAN_MIN_LEN)} نویسه باشد؛ عبارت‌های کوتاه بخش‌های درست متن را هم پاک می‌کنند.`; return; }
    if (rules.some(r => r.type === mode && r.text === text)) { $("cleanError").textContent = "این قاعده قبلاً اضافه شده است."; return; }
    rules.push({ id: newUid(), type: mode, text: text.slice(0, 200) });
    saveCleanRules(rules); $("cleanPhrase").value = ""; $("cleanError").textContent = "";
    resetCleanResults(""); renderCleanRules();
  });
  $("cleanPresetTelegram").addEventListener("click", () => {
    const rules = loadCleanRules();
    if (!rules.some(r => r.type === "telegram")) { rules.push({ id: newUid(), type: "telegram" }); saveCleanRules(rules); }
    $("cleanError").textContent = ""; resetCleanResults(""); renderCleanRules();
  });
  $("cleanRules").addEventListener("click", e => {
    const id = e.target.closest("[data-clean-del]")?.dataset.cleanDel;
    if (!id || cleanBusy) return;
    saveCleanRules(loadCleanRules().filter(r => r.id !== id)); resetCleanResults(""); renderCleanRules();
  });
  $("cleanResults").addEventListener("change", e => { if (e.target.matches("[data-clean-broad]")) renderCleanResults(); });
  $("cleanSearch").addEventListener("click", () => { runCleanSearch().catch(err => { console.error(err); showToast("جستجو انجام نشد."); }); });
  $("cleanApply").addEventListener("click", () => { runCleanApply().catch(err => { console.error(err); showToast("پاک‌سازی انجام نشد."); }); });

  // ---- v8.7: an admin's cleanup rules are mirrored to the server, which applies them to books sent with an API key ----
  // Every local change is pushed (debounced) as the full list. On login/start the server list is read: if the local
  // list is empty it is adopted, otherwise local wins and is pushed when the two differ. Offline: retried on reconnect.
  const CLEAN_PUSH_MS = 1200;
  const cleanSync = { state: "", timer: null, dirty: false };
  const isAdmin = () => !!(account && account.user && account.user.role === "admin");
  const CLEAN_SYNC_FA = { pending: "در انتظار ذخیره روی سرور", saving: "در حال ذخیره روی سرور…", ok: "روی سرور ذخیره شد",
    offline: "آفلاین؛ بعداً ذخیره می‌شود", failed: "ذخیره روی سرور انجام نشد", off: "سرور هنوز پشتیبانی نمی‌کند" };
  const serverRules = rules => rules.map(r => r.type === "telegram" ? { id: String(r.id), type: "telegram" } : { id: String(r.id), type: r.type, text: r.text });
  function setCleanSyncState(state) { cleanSync.state = state; renderCleanSyncNote(); }
  function renderCleanSyncNote() {
    const admin = isAdmin();
    $("cleanServerNote").hidden = !admin;
    const chip = $("cleanSyncState");
    chip.textContent = admin && CLEAN_SYNC_FA[cleanSync.state] ? CLEAN_SYNC_FA[cleanSync.state] : "";
    chip.hidden = !chip.textContent;
    chip.dataset.state = cleanSync.state;
    chip.classList.toggle("off", cleanSync.state === "failed" || cleanSync.state === "off");
  }
  function scheduleCleanPush() {
    if (!isAdmin()) return;
    cleanSync.dirty = true;
    clearTimeout(cleanSync.timer);
    setCleanSyncState(navigator.onLine ? "pending" : "offline");
    cleanSync.timer = setTimeout(pushCleanRules, CLEAN_PUSH_MS);
  }
  async function pushCleanRules() {
    clearTimeout(cleanSync.timer); cleanSync.timer = null;
    if (!isAdmin()) return;
    if (!navigator.onLine) { cleanSync.dirty = true; setCleanSyncState("offline"); return; }
    cleanSync.dirty = false; setCleanSyncState("saving");
    try {
      await api("clean-rules", { method: "POST", body: { rules: serverRules(loadCleanRules()) } });
      if (cleanSync.state === "saving") setCleanSyncState("ok");
    } catch (err) {
      cleanSync.dirty = true;
      if (!navigator.onLine) { setCleanSyncState("offline"); return; }
      setCleanSyncState(err instanceof ApiFail && err.status === 404 ? "off" : "failed");
      reportError("api", err, "clean-rules:push");
    }
  }
  async function pullCleanRules() {
    if (!isAdmin() || !navigator.onLine) return;
    let r;
    try { r = await api("clean-rules"); }
    catch (err) {
      if (!navigator.onLine) return;
      setCleanSyncState(err instanceof ApiFail && err.status === 404 ? "off" : "failed");
      reportError("api", err, "clean-rules:get");
      return;
    }
    if (!isAdmin()) return;
    const server = (Array.isArray(r && r.rules) ? r.rules : [])
      .filter(x => x && (x.type === "telegram" || ((x.type === "phrase" || x.type === "line") && typeof x.text === "string" && x.text.trim())))
      .map(x => x.type === "telegram" ? { id: String(x.id || newUid()), type: "telegram" } : { id: String(x.id || newUid()), type: x.type, text: x.text.slice(0, 200) });
    const local = loadCleanRules();
    if (!local.length && server.length) {
      saveCleanRules(server, true); cleanSync.dirty = false; setCleanSyncState("ok");
      if ($("cleanDialog").open && !cleanBusy) { resetCleanResults(""); renderCleanRules(); }
    } else if (cleanSync.dirty || JSON.stringify(serverRules(local)) !== JSON.stringify(serverRules(server))) pushCleanRules();
    else setCleanSyncState("ok");
  }
  window.addEventListener("online", () => { if (cleanSync.dirty && isAdmin()) pushCleanRules(); });

  // ---------- Start ----------
  // ---- Repair titles/authors/covers of books imported by old versions that could not read metadata ----
  const UNKNOWN_AUTHOR = "نویسنده نامشخص";
  const looksLikeFilename = t => /_|\.epub$/i.test(t || "");
  async function repairMetadata() {
    let changed = false;
    for (const b of await getAllBooks()) {
      if ((b.metaChecked && b.metaExt) || !fileOf(b) || isPdfRecord(b)) continue; // PDF metadata is read on import
      const rec = { ...b, metaChecked: true, metaExt: true };
      const weak = !b.metaChecked && (!b.cover || !b.author || b.author === UNKNOWN_AUTHOR || looksLikeFilename(b.title));
      if (!b.metaExt && !weak) {
        // Books imported before publisher/date/language were kept: read just those (once).
        try {
          const raw = fileOf(b);
          const m = await extractMetadata(raw instanceof File ? raw : new File([raw], "book.epub", { type: "application/epub+zip" }));
          for (const k of Object.keys(META_LIMITS)) if (m[k] && !b[k]) { rec[k] = m[k]; rec.metaExtPush = true; }
        } catch (_) { /* unreadable file: keep what we have */ }
      }
      if (weak) {
        try {
          const raw = fileOf(b);
          const file = raw instanceof File ? raw : new File([raw], "book.epub", { type: "application/epub+zip" });
          const fromName = file.name.replace(/\.epub$/i, "");
          const m = await extractMetadata(file);
          if (m.title && m.title !== fromName && (looksLikeFilename(b.title) || b.title === fromName)) rec.title = m.title;
          if (m.author && m.author !== UNKNOWN_AUTHOR && (!b.author || b.author === UNKNOWN_AUTHOR)) rec.author = m.author;
          if (!b.cover && m.cover) rec.cover = m.cover;
          for (const k of Object.keys(META_LIMITS)) if (m[k] && !b[k]) { rec[k] = m[k]; rec.metaExtPush = true; }
          if (rec.title !== b.title || rec.author !== b.author || rec.cover !== b.cover) { rec.metaRepaired = true; rec.metaPushed = false; changed = true; }
        } catch (_) { /* unreadable file: keep what we have */ }
      }
      // Merge into the latest copy: the book may have been opened (status, locations) while its file was read.
      const latest = await getBook(b.id);
      if (latest) await put("books", { ...latest, ...Object.fromEntries(Object.entries(rec).filter(([k, v]) => v !== b[k])) });
    }
    if (changed) { renderLibrary(); if (account) sync(); }
  }
  // ---- Files shared from other apps (Android share sheet) or opened with the installed app (desktop) ----
  async function importSharedFiles() {
    if (!new URLSearchParams(location.search).has("shared") || !("caches" in window)) return;
    history.replaceState(null, "", location.pathname);
    try {
      const cache = await caches.open("borg-share");
      for (const req of await cache.keys()) {
        const res = await cache.match(req);
        await cache.delete(req);
        if (!res) continue;
        const name = decodeURIComponent(res.headers.get("X-File-Name") || "book.epub");
        const blob = await res.blob();
        await importFile(new File([blob], name, { type: blob.type || "application/epub+zip" }));
      }
    } catch (err) { console.warn("Shared file import failed:", err); }
  }
  if ("launchQueue" in window) {
    window.launchQueue.setConsumer(async params => {
      for (const handle of (params && params.files) || []) {
        try { await importFile(await handle.getFile()); } catch (err) { console.warn("Open-with import failed:", err); }
      }
    });
  }

  async function initAccount() {
    if (!navigator.onLine) return; // offline: local-only until the connection returns
    try {
      const r = await api("me");
      if (r.user) { setAccount({ user: r.user, csrf: r.csrf }); routeFromHash(); sync(); }
    } catch (_) { /* offline or API not configured: local-only mode */ }
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && account && navigator.onLine) sync(); });
  window.addEventListener("online", () => { if (account) sync(); else initAccount(); });
  syncSettingsUI();
  if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
    // The SW activates immediately (skipWaiting); when it replaces an existing one, offer a reload.
    const hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener("controllerchange", () => { if (hadController) $("updateBanner").hidden = false; });
    $("updateReload").addEventListener("click", () => location.reload());
    window.addEventListener("load", () => navigator.serviceWorker.register("./service-worker.js").then(reg => {
      document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") reg.update().catch(() => {}); });
    }).catch(err => { console.warn("Service worker registration failed", err); reportError("sw", err, "service-worker"); }));
  }
})();
