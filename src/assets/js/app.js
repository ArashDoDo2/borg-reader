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
    margin: "m", para: "m", flow: "paginated", spread: "auto", digits: false, taps: true, wake: true, ttsRate: 1, ttsVoice: "" };
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
    const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    chk("prefDigits", readerPrefs.digits); chk("prefTaps", readerPrefs.taps !== false); chk("prefWake", readerPrefs.wake !== false);
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
  function putRaw(store, value) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      if (store === "books" && value && value.id && FILE_CACHES.some(k => value[k] === undefined)) {
        const os = tx.objectStore(store), g = os.get(value.id);
        g.onsuccess = () => {
          const old = g.result;
          const same = old && old.size === value.size && (!old.contentHash || !value.contentHash || old.contentHash === value.contentHash);
          const keep = same ? Object.fromEntries(FILE_CACHES.filter(k => value[k] === undefined && old[k] !== undefined).map(k => [k, old[k]])) : {};
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
    if (rec.fileBuffer) return new File([rec.fileBuffer], rec.fileName || "book.epub", { type: "application/epub+zip" });
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
  function updateTimeLeft(location) {
    const now = Date.now(), dt = (now - lastTurnAt) / 1000;
    if (lastTurnAt && dt > 3 && dt < 300) {
      secPerPage = Math.round(secPerPage * 0.7 + dt * 0.3);
      try { localStorage.setItem("borg-sec-per-page", String(secPerPage)); } catch (_) {}
    }
    lastTurnAt = now;
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

  function bookCardHtml(b, prog) {
    const [bg, fg] = paletteFor(b.title || b.id);
    const cover = b.cover ? `<img alt="" src="${b.cover}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">` : `<div style="position:relative"><div class="cover-symbol">✳</div><div class="cover-name">${esc(b.title)}</div></div>`;
    const pct = prog && typeof prog.percentage === "number" ? Math.max(0, Math.min(100, prog.percentage)) : null;
    const bar = pct ? `<div class="cover-progress" aria-hidden="true"><span style="width:${pct}%"></span></div>` : "";
    const catalogLabel = b.catalogStatus === "approved" ? " · عمومی" : b.catalogStatus === "pending" ? " · در انتظار تأیید" : "";
    const statusLabel = b.status === "finished" ? "خوانده‌شده" : b.status === "want" ? "می‌خواهم بخوانم" : null;
    const badge = (statusLabel || (pct ? `${pct.toLocaleString("fa-IR")}٪ خوانده‌شده` : (prog ? "شروع‌شده" : "خوانده‌نشده"))) + catalogLabel;
    return `<article class="book-card" tabindex="0" role="button" data-open="${esc(b.id)}" aria-label="باز کردن ${esc(b.title)}">
          <div class="book-cover-thumb" style="background:${bg};color:${fg}">${cover}${bar}</div>
          <div class="book-meta"><h3 title="${esc(b.title)}">${esc(b.title)}</h3><p>${esc(b.author || "نویسنده نامشخص")}</p><span class="book-badge">${badge}</span></div>
          <button class="remove-book" type="button" data-remove="${esc(b.id)}" aria-label="حذف ${esc(b.title)}" title="حذف">×</button>
          <button class="book-options" type="button" data-options="${esc(b.id)}" aria-label="تنظیمات ${esc(b.title)}" title="وضعیت و قفسه">⋯</button>
          ${account && b.serverId && !b.catalogStatus ? `<button class="share-book" type="button" data-share="${esc(b.id)}" aria-label="پیشنهاد ${esc(b.title)} برای کتابخانهٔ عمومی" title="پیشنهاد برای کتابخانهٔ عمومی">⇪</button>` : ""}
        </article>`;
  }
  function renderLibrary() {
    const query = $("searchInput").value.trim().toLocaleLowerCase();
    Promise.all([getAllBooks(), getAllProgress()]).then(([books, progressList]) => {
      const prog = new Map(progressList.map(p => [p.bookId, p]));
      const lastRead = b => (prog.get(b.id) && prog.get(b.id).updatedAt) || 0;
      const sort = readerPrefs.librarySort;
      if (sort === "title") books.sort((a, b) => String(a.title).localeCompare(String(b.title), "fa"));
      else if (sort === "read") books.sort((a, b) => lastRead(b) - lastRead(a) || (b.addedAt || 0) - (a.addedAt || 0));
      else books.sort((a, b) => (b.addedAt || 0) - (a.addedAt || 0));
      const f = readerPrefs.libraryFilter || "all";
      const filtered = books.filter(b => `${b.title} ${b.author}`.toLocaleLowerCase().includes(query))
        .filter(b => f === "all" || (f.startsWith("status:") ? (b.status || (prog.get(b.id)?.cfi ? "reading" : "")) === f.slice(7) : (b.shelves || []).includes(f.slice(6))));
      renderFilters(books);
      renderStats(books);
      $("bookGrid").innerHTML = filtered.map(b => bookCardHtml(b, prog.get(b.id))).join("");
      const reading = query ? [] : books.filter(b => { const p = prog.get(b.id); return p && p.cfi && (p.percentage || 0) < 100; })
        .sort((a, b) => lastRead(b) - lastRead(a)).slice(0, 8);
      $("continueSection").hidden = reading.length === 0;
      $("continueShelf").innerHTML = reading.map(b => bookCardHtml(b, prog.get(b.id))).join("");
      updateStorageInfo();
      $("emptyState").classList.toggle("visible", books.length === 0);
      $("bookGrid").style.display = books.length ? "grid" : "none";
      if (books.length && !filtered.length) {
        $("bookGrid").style.display = "block";
        $("bookGrid").innerHTML = '<p class="muted">کتابی با این عبارت پیدا نشد.</p>';
      }
    }).catch(() => showToast("خواندن کتابخانه محلی ناموفق بود."));
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
  async function importFile(file) {
    try {
      if (!file) return;
      if (!/\.epub$/i.test(file.name) && file.type !== "application/epub+zip") {
        showToast("لطفاً یک فایل EPUB انتخاب کن."); return;
      }
      if (file.size > 100 * 1024 * 1024) {
        showToast("این نسخه برای فایل‌های حداکثر ۱۰۰ مگابایت تنظیم شده است."); return;
      }
      if (!db) { showToast("ذخیره‌سازی مرورگر آماده نیست."); return; }
      showToast("در حال بررسی کتاب…");
      const contentHash = await sha256(await file.arrayBuffer());
      if (contentHash && await findDuplicate(file.size, contentHash)) {
        throw new UserError("این کتاب قبلاً در کتابخانه هست.");
      }
      const metadata = await extractMetadata(file);
      const record = {
        id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        title: metadata.title, author: metadata.author, cover: metadata.cover,
        publisher: metadata.publisher || "", published: metadata.published || "", language: metadata.language || "", metaExt: true,
        file: file, size: file.size, addedAt: Date.now(), contentHash
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
      renderLibrary();
      requestPersistentStorage();
      sync();
      showToast(cleaned ? `کتاب به کتابخانه اضافه شد؛ ${faNum(cleaned)} مورد حذف شد` : "کتاب به کتابخانه همین دستگاه اضافه شد.");
    } catch (err) {
      // Expected outcomes (duplicate, broken file, …) are UserErrors and are not logged as app errors.
      if (!(err instanceof UserError)) { console.error(err); reportError("import", err, "import"); }
      showToast(err instanceof UserError ? err.message : "افزودن کتاب انجام نشد.");
    } finally {
      resetInputs();
    }
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
    if (!locationsReady()) return;
    const p = Number(r.value) / 1000;
    let title = "";
    try { const cfi = bookInstance.locations.cfiFromPercentage(p); title = chapterTitle(new ePub.CFI(cfi).spinePos, cfi); } catch (_) {}
    pv.textContent = `${title ? title + " · " : ""}٪${faNum(Math.round(p * 100))}`;
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
    if (!librariesReady()) { showToast("کتاب‌خوان کامل بارگذاری نشده است. صفحه را تازه‌سازی کن."); return; }
    const token = ++openToken;
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
    tocFlat = []; resetSeek();
    $("prevPage").disabled = true; $("nextPage").disabled = false;
    showProgress(0);
    if (bookInstance) { try { bookInstance.destroy(); } catch (_) {} }
    const bookFile = fileOf(record);
    if (!bookFile) { hideOpening(); showToast("فایل این کتاب روی دستگاه پیدا نشد."); return; }
    // The saved position is read while the file is (they are independent).
    const savedP = record.preview ? Promise.resolve(previewPos ? { cfi: previewPos } : null) : getProgress(record.id).catch(() => null);
    const epubBytes = await bookFile.arrayBuffer();
    if (token !== openToken) return;
    bookInstance = ePub(epubBytes);
    bookInstance.on("openFailed", err => {
      console.error("EPUB openFailed:", err);
      reportError("epub", err, "reader:openFailed");
      if (token === openToken) { hideOpening(); showToast("این فایل EPUB قابل خواندن نیست یا خراب است."); }
    });
    // v8.5: the detected direction is cached on the book record (per file and detector version).
    const dc = record.dirCache, dirKey = record.contentHash || `size:${record.size || 0}`;
    if (dc && dc.v === DIR_DETECT_V && dc.key === dirKey && (dc.dir === "rtl" || dc.dir === "ltr")) {
      bookDir = dc.dir;
    } else {
      countOpenStat("detect");
      bookDir = await detectBookDirection(bookInstance);
      if (!record.preview) {
        const cache = { dir: bookDir, v: DIR_DETECT_V, key: dirKey };
        // One read-write transaction that only adds the field, so it cannot undo a concurrent write (status etc.).
        try {
          const os = db.transaction("books", "readwrite").objectStore("books"), g = os.get(record.id);
          g.onsuccess = () => { const cur = g.result; if (cur && (cur.contentHash || `size:${cur.size || 0}`) === dirKey) os.put({ ...cur, dirCache: cache }); };
        } catch (err) { console.warn("Caching the book direction failed:", err); }
      }
    }
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
    });
    // Keep each newly loaded chapter invisible until the reader font and theme are applied, so the text
    // doesn't first paint in the fallback font and then visibly re-flow.
    // Registered after "opened" so it runs after epub.js's own resource-substitution hook (both rewrite section.output).
    const veilBook = bookInstance;
    veilBook.opened.then(() => veilBook.spine.hooks.serialize.register((output, section) => {
      const html = typeof section.output === "string" ? section.output : output;
      if (typeof html === "string") section.output = html.replace(/<head(\s[^>]*)?>/i, m => `${m}<style id="borg-veil">html{visibility:hidden}</style>`);
    })).catch(() => {});
    rendition.hooks.content.register(onBookContent);
    // The book is rendered in an iframe; load the bundled font there too, then lift the veil.
    rendition.hooks.content.register(contents => {
      const doc = contents.document, reveal = () => { const v = doc && doc.getElementById("borg-veil"); if (v) v.remove(); if (token === openToken) hideOpening(); };
      const safety = setTimeout(reveal, 1500); // never leave a page hidden
      return contents.addStylesheet(READER_FONTS_URL)
        .then(() => new Promise(r => { void doc.body.offsetHeight; (contents.window.requestAnimationFrame || setTimeout)(r); }))
        .then(() => Promise.race([doc.fonts ? doc.fonts.ready : null, new Promise(r => setTimeout(r, 700))]))
        .catch(() => {})
        .then(() => { clearTimeout(safety); reveal(); });
    });
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
  // ---- Reader navigation (v6.1): keyboard, swipe, edge tap zones, TOC drawer, immersive mode ----
  const SWIPE_MIN_PX = 50, EDGE_ZONE = 0.25;
  let touchStart = null, suppressClickUntil = 0;
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
  function goForward() { if (rendition) rendition.next(); }
  function goBack() { if (rendition) rendition.prev(); }
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
    requestAnimationFrame(() => { try { if (rendition) rendition.resize(); } catch (_) {} });
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
    const t = e.changedTouches && e.changedTouches[0];
    touchStart = t ? { x: t.clientX, y: t.clientY, time: Date.now() } : null;
  }
  function onTouchEnd(e, contents) {
    const t = e.changedTouches && e.changedTouches[0];
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
      // Finger moving right turns to the next page in an RTL book, like a Persian paper book.
      if (dx > 0) goLeft(); else goRight();
    }
  }
  function onReaderClick(e, contents) {
    if (Date.now() < suppressClickUntil) return;
    handleTap(e.target, e.clientX, e.view, contents);
  }
  function handleTap(target, clientX, view, contents) {
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
    if (!frame || !box.width) return;
    const ratio = (frame.getBoundingClientRect().left + clientX - box.left) / box.width;
    const edges = readerPrefs.taps !== false && readerPrefs.flow !== "scrolled";
    if (!edges) setImmersive(!$("reader").classList.contains("immersive"));
    else if (ratio < EDGE_ZONE) goLeft();
    else if (ratio > 1 - EDGE_ZONE) goRight();
    else setImmersive(!$("reader").classList.contains("immersive"));
  }
  $("tocToggle").addEventListener("click", () => { if ($("reader").classList.contains("toc-open")) closeToc(true); else openToc(); });
  $("tocBackdrop").addEventListener("click", () => closeToc(true));
  document.addEventListener("keydown", handleReaderKey);
  document.addEventListener("keydown", () => { if (currentBook) markActivity(); });

  // ---- «برگشت به جای قبلی» (v8.2): a jump (not a page turn) remembers where the reader was ----
  const JUMP_STACK_MAX = 10, JUMP_HIDE_AFTER_TURNS = 5;
  // stack: CFIs to go back to (newest last). pending: a jump whose relocation has not settled yet.
  const jumps = { stack: [], pending: null, timer: 0, turns: 0, lastCfi: null };
  function currentStartCfi() {
    try { const loc = rendition && rendition.currentLocation(); return (loc && loc.start && loc.start.cfi) || null; } catch (_) { return null; }
  }
  /** Remember the current page before a jump that epub.js (or jumpTo) is about to make. kind "back" = returning via the chip. */
  function noteJumpOrigin(kind) {
    if (!rendition) return;
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
    if (!rendition) return Promise.resolve();
    noteJumpOrigin("jump");
    return rendition.display(target);
  }
  function settleJump() {
    clearTimeout(jumps.timer);
    const p = jumps.pending; jumps.pending = null;
    if (!p || !rendition) return;
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
    if (!cfi || !rendition) return;
    noteJumpOrigin("back");
    rendition.display(cfi);
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
    const rtl = bookIsRtl(), scrolled = readerPrefs.flow === "scrolled", edges = readerPrefs.taps !== false && !scrolled;
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
    items.push(["settings", "دکمهٔ «Aa» بالای صفحه: فونت، اندازهٔ متن، رنگ صفحه و نحوهٔ نمایش."]);
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
  function setPref(key, value, how) {
    readerPrefs[key] = value; saveReaderPrefs(); syncSettingsUI();
    if (how === "styles") applyReaderStyles();
    if (how === "resize") requestAnimationFrame(() => { try { rendition && rendition.resize(); } catch (_) {} });
    if (how === "reopen") reopenCurrentBook();
  }
  document.querySelectorAll("[data-margin]").forEach(b => b.addEventListener("click", () => setPref("margin", b.dataset.margin, "resize")));
  document.querySelectorAll("[data-para]").forEach(b => b.addEventListener("click", () => setPref("para", b.dataset.para, "styles")));
  document.querySelectorAll("[data-flow]").forEach(b => b.addEventListener("click", () => { if (readerPrefs.flow !== b.dataset.flow) setPref("flow", b.dataset.flow, "reopen"); }));
  document.querySelectorAll("[data-spread]").forEach(b => b.addEventListener("click", () => { if (readerPrefs.spread !== b.dataset.spread) setPref("spread", b.dataset.spread, "reopen"); }));
  $("prefDigits").addEventListener("change", e => setPref("digits", e.target.checked, "reopen"));
  $("prefTaps").addEventListener("change", e => setPref("taps", e.target.checked));
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
  function onBookContent(contents) {
    const doc = contents.document;
    if (!doc || !doc.body) return;
    try { fixHalfSpaces(doc); } catch (_) {}
    if (readerPrefs.digits) { try { toPersianDigits(doc); } catch (_) {} }
    // Capture phase: runs before epub.js' own link handling, so footnotes open in place instead of jumping.
    doc.addEventListener("click", e => {
      const a = e.target.closest && e.target.closest("a[href]");
      if (a && isNoteRef(a)) {
        e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
        showFootnote(contents, a).then(ok => { if (!ok && rendition) jumpTo(a.getAttribute("href")); }).catch(() => {});
        return;
      }
      // Other internal links are followed by epub.js itself (same rules as its replaceLinks): remember the page first.
      if (a) {
        const href = a.getAttribute("href") || "";
        if (href.indexOf("mailto:") !== 0 && href.indexOf("://") < 0) noteJumpOrigin("jump");
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
      rendition.display(next.href);
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
      if (!blockVisible(item.el, tts.contents)) { tts.selfNav = true; rendition.display(cfi); }
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
    const pattern = new RegExp(searchVariants(q).map(v => escapeRegExp(esc(v))).join("|"), "g");
    $("bookSearchResults").innerHTML = results.map(r => `<button type="button" data-cfi="${esc(r.cfi)}"><small>بخش ${(r.index + 1).toLocaleString("fa-IR")}</small>${esc(r.excerpt).replace(pattern, m => `<mark>${m}</mark>`)}</button>`).join("");
  });
  $("bookSearchResults").addEventListener("click", e => {
    const btn = e.target.closest("[data-cfi]");
    if (!btn || !rendition) return;
    closeSearch(false);
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
  $("prevPage").addEventListener("click", () => rendition?.prev());
  $("nextPage").addEventListener("click", () => rendition?.next());
  $("closeReader").addEventListener("click", closeReader);
  // ======================= Bookmarks, highlights, notes (v7.2) =======================
  const HL_COLORS = { yellow: "#ffe066", green: "#9be3b4", pink: "#f7a8c8" };
  let pendingSelection = null, editingUid = null, renderedHighlights = new Set();
  const cfiCompare = (a, b) => { try { return new ePub.CFI().compare(a, b); } catch (_) { return 0; } };
  async function bookmarkOnPage() {
    if (!rendition || !currentBook) return null;
    const loc = rendition.currentLocation();
    if (!loc?.start?.cfi) return null;
    const end = loc.end?.cfi || loc.start.cfi;
    return (await getAnnotations(currentBook.id)).find(a => a.type === "bookmark" && cfiCompare(a.cfi, loc.start.cfi) >= 0 && cfiCompare(a.cfi, end) <= 0) || null;
  }
  async function updateBookmarkButton() {
    const bm = await bookmarkOnPage().catch(() => null);
    $("bookmarkButton").textContent = bm ? "★ نشانک ذخیره شد" : "☆ نشانک";
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
    if (!currentBook || !rendition) return;
    if (currentBook.preview) { showToast(PREVIEW_NO_SAVE); return; }
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
    if (!rendition || !currentBook) return;
    const anns = await getAnnotations(currentBook.id);
    const live = new Set(anns.filter(a => a.type === "highlight").map(a => a.uid));
    for (const uid of [...renderedHighlights]) if (!live.has(uid)) { const a = await getAnnotation(uid); if (a) eraseHighlight(a); else renderedHighlights.delete(uid); }
    anns.filter(a => a.type === "highlight").forEach(drawHighlight);
    renderNotesList();
  }
  async function renderNotesList() {
    if (!currentBook) { $("notesList").innerHTML = ""; return; }
    const anns = (await getAnnotations(currentBook.id)).sort((a, b) => cfiCompare(a.cfi, b.cfi));
    $("notesList").innerHTML = anns.length ? anns.map(a => `<div class="note-item" data-goto="${esc(a.cfi)}">
        <span class="note-mark" style="background:${a.type === "bookmark" ? "#2f7a62" : (HL_COLORS[a.color] || HL_COLORS.yellow)}"></span>
        <span class="note-text"><span class="note-quote" dir="auto">${a.type === "bookmark" ? "🔖 " : ""}${esc(a.quote || "")}</span>${a.note ? `<span class="note-note" dir="auto">✎ ${esc(a.note)}</span>` : ""}</span>
        <button class="note-del" type="button" data-del="${esc(a.uid)}" aria-label="حذف">×</button></div>`).join("")
      : '<p class="muted">هنوز نشانک یا یادداشتی نداری. متن را انتخاب کن تا هایلایت شود.</p>';
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
    if (item && rendition) { closeToc(false); jumpTo(item.dataset.goto); }
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
  $("searchInput").addEventListener("input", renderLibrary);
  inputEls.forEach(el => el?.addEventListener("change", e => importFile(e.target.files?.[0])));
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
  }).catch(err => { console.error(err); showToast("مرورگر اجازه ذخیره محلی نمی‌دهد."); });
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
      catalogBooks = []; catalogLoaded = false; previewCache = null;
      if (location.hash.startsWith("#catalog")) setHash("", false);
    }
    $("authForms").hidden = !!user;
    $("accountInfo").hidden = !user;
    if (user) { $("accountName").textContent = user.name; $("accountEmail").textContent = user.email; }
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
    if (view === "catalog") { renderCatalog(); loadCatalog(); }
    if (view === "book") { renderBookDetail(); if (!catalogLoaded) loadCatalog(); }
    if (view === "admin") loadAdmin();
  }
  document.querySelectorAll(".view-tabs [role=tab]").forEach(t => t.addEventListener("click", () => {
    if (t.dataset.view === "catalog") { catalogFilter.author = ""; catalogFilter.category = ""; }
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
    let cover = null;
    if (sb.has_cover) {
      try { cover = await blobToDataUrl(await (await api("book-cover", { query: `&id=${sb.id}` })).blob()); } catch (_) {}
    }
    const record = {
      id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      title: sb.title, author: sb.author || "نویسنده نامشخص", cover,
      file: new File([blob], `${sb.title}.epub`, { type: "application/epub+zip" }),
      size: blob.size, addedAt: Date.now(), contentHash: sb.sha256, serverId: sb.id, catalogStatus: sb.catalog_status || null,
      publisher: sb.publisher || "", published: sb.published || "", language: sb.language || "", metaExt: true,
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
  let catalogBooks = [], catalogLoaded = false;
  const catalogFilter = { author: "", category: "" };
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
    return `<article class="book-card catalog-card" data-detail="${b.id}" tabindex="0" role="button" aria-label="جزئیات ${esc(b.title)}"><div class="book-cover-thumb" style="background:${bg};color:${fg}">${catalogCover(b, true)}</div>
      <div class="book-meta"><h3 title="${esc(b.title)}">${esc(b.title)}</h3><p>${esc(b.author || "نویسنده نامشخص")}</p>
      ${withAction && b.description ? `<p title="${esc(b.description)}">${esc(b.description)}</p>` : ""}${action ? `<div class="catalog-actions">${action}</div>` : ""}</div></article>`;
  }
  async function loadCatalog() {
    if (!catalogBooks.length) $("catalogGrid").innerHTML = '<p class="muted">در حال بارگذاری…</p>';
    try {
      catalogBooks = (await api("catalog")).books || [];
      catalogLoaded = true;
      renderCatalog();
      if (!$("viewBook").hidden) renderBookDetail();
    } catch (err) {
      $("catalogGrid").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`;
      if (!$("viewBook").hidden && !catalogBooks.length) $("detailTitle").textContent = apiMessage(err);
    }
  }
  function catalogCategories() {
    return [...new Set(catalogBooks.map(catOf).filter(Boolean))].sort((a, b) => a.localeCompare(b, "fa"));
  }
  function fillCategoryList(extra) {
    const cats = new Set([...catalogCategories(), ...(extra || [])].filter(Boolean));
    $("categoryList").innerHTML = [...cats].map(c => `<option value="${esc(c)}"></option>`).join("");
  }
  function renderCatalogFilters() {
    const cats = catalogCategories();
    const chips = [];
    if (catalogFilter.author) {
      chips.push(`<button type="button" class="filter-chip-active" data-clear-author aria-label="برداشتن فیلتر نویسنده ${esc(catalogFilter.author)}">نویسنده: ${esc(catalogFilter.author)} <span aria-hidden="true">✕</span></button>`);
    }
    if (cats.length || catalogFilter.category) {
      const list = cats.includes(catalogFilter.category) || !catalogFilter.category ? cats : [...cats, catalogFilter.category];
      chips.push(`<button type="button" data-cat-filter="" aria-pressed="${!catalogFilter.category}">همهٔ دسته‌ها</button>`);
      chips.push(...list.map(c => `<button type="button" data-cat-filter="${esc(c)}" aria-pressed="${c === catalogFilter.category}">${esc(c)}</button>`));
    }
    $("catalogFilters").innerHTML = chips.join("");
    $("catalogFilters").hidden = !chips.length;
  }
  function renderCatalog() {
    const q = $("catalogSearch").value.trim().toLocaleLowerCase();
    const books = catalogBooks.filter(b => !q || `${b.title} ${b.author} ${b.description || ""} ${b.submitter || ""} ${b.category || ""} ${b.publisher || ""}`.toLocaleLowerCase().includes(q))
      .filter(b => !catalogFilter.author || authorOf(b) === catalogFilter.author)
      .filter(b => !catalogFilter.category || catOf(b) === catalogFilter.category);
    renderCatalogFilters();
    fillCategoryList();
    $("catalogEmpty").hidden = !catalogLoaded || catalogBooks.length > 0;
    $("catalogGrid").innerHTML = books.map(b => catalogCardHtml(b, true)).join("");
    if (catalogBooks.length && !books.length) $("catalogGrid").innerHTML = '<p class="muted">کتابی با این مشخصات پیدا نشد.</p>';
  }
  $("catalogSearch").addEventListener("input", renderCatalog);
  $("catalogFilters").addEventListener("click", e => {
    if (e.target.closest("[data-clear-author]")) { catalogFilter.author = ""; renderCatalog(); return; }
    const c = e.target.closest("[data-cat-filter]");
    if (c) { catalogFilter.category = c.dataset.catFilter; renderCatalog(); }
  });

  // ---- Navigation between the catalog and a book's page (deep-linkable: #catalog, #catalog/book/ID) ----
  function setHash(hash, push, state) {
    const url = hash ? hash : location.pathname + location.search;
    try { (push ? history.pushState : history.replaceState).call(history, state || null, "", url); } catch (_) {}
  }
  function openCatalogBook(id, push = true) {
    detailId = id;
    setHash(`#catalog/book/${id}`, push, { borg: "book", id, inApp: push });
    showView("book");
    window.scrollTo({ top: Math.max(0, $("app").querySelector(".library-section").offsetTop - 10), behavior: "auto" });
  }
  function goToCatalog(push) {
    setHash("#catalog", push, { borg: "catalog" });
    showView("catalog");
  }
  function filterCatalogBy(kind, value) {
    catalogFilter.author = kind === "author" ? value : "";
    catalogFilter.category = kind === "category" ? value : "";
    $("catalogSearch").value = "";
    goToCatalog(true);
  }
  function routeFromHash() {
    if (!account) return;
    if (readerOpen() && currentBook && currentBook.preview) closeReader();
    const m = /^#catalog\/book\/(\d+)/.exec(location.hash);
    if (m) { detailId = Number(m[1]); showView("book"); }
    else if (location.hash.startsWith("#catalog")) showView("catalog");
    else if (!$("viewBook").hidden || !$("viewCatalog").hidden) showView("mine");
  }
  window.addEventListener("popstate", routeFromHash);
  $("detailBack").addEventListener("click", () => {
    if (history.state && history.state.borg === "book" && history.state.inApp) history.back();
    else goToCatalog(false);
  });
  $("detailCrumbs").addEventListener("click", e => {
    const t = e.target.closest("[data-crumb]");
    if (!t) return;
    if (t.dataset.crumb === "catalog") { catalogFilter.author = ""; catalogFilter.category = ""; goToCatalog(true); }
    else filterCatalogBy("category", t.dataset.crumbCat || "");
  });
  $("detailAuthor").addEventListener("click", () => { const b = catalogBooks.find(x => x.id === detailId); if (b && authorOf(b)) filterCatalogBy("author", authorOf(b)); });
  $("detailMeta").addEventListener("click", e => { const t = e.target.closest("[data-meta-cat]"); if (t) filterCatalogBy("category", t.dataset.metaCat); });

  // ---- Book page ----
  function relatedBooks(b) {
    const others = catalogBooks.filter(x => x.id !== b.id);
    const sameAuthor = authorOf(b) ? others.filter(x => authorOf(x) === authorOf(b)) : [];
    const sameCat = catOf(b) ? others.filter(x => catOf(x) === catOf(b) && !sameAuthor.includes(x)) : [];
    return [...sameAuthor, ...sameCat].slice(0, 6);
  }
  function formatPublished(v) {
    const t = String(v || "").trim();
    return t ? faDigits(t.replace(/T.*$/, "")) : "";
  }
  async function renderBookDetail() {
    closeAddMenu();
    const b = catalogBooks.find(x => x.id === detailId);
    $("detailBody").hidden = !b;
    if (!b) {
      $("detailCrumbs").innerHTML = '<button type="button" class="crumb" data-crumb="catalog">کتابخانهٔ عمومی</button>';
      $("detailTitle").textContent = catalogLoaded ? "این کتاب در کتابخانهٔ عمومی پیدا نشد." : "در حال بارگذاری…";
      $("detailTitle").hidden = false;
      return;
    }
    const cat = catOf(b);
    $("detailCrumbs").innerHTML = `<button type="button" class="crumb" data-crumb="catalog">کتابخانهٔ عمومی</button><span class="crumb-sep" aria-hidden="true">/</span>`
      + (cat ? `<button type="button" class="crumb" data-crumb="category" data-crumb-cat="${esc(cat)}">${esc(cat)}</button><span class="crumb-sep" aria-hidden="true">/</span>` : "")
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
      ["دسته‌بندی", cat ? `<button type="button" class="link-button" data-meta-cat="${esc(cat)}">${esc(cat)}</button>` : ""],
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
    const related = relatedBooks(b);
    $("detailRelatedSection").hidden = !related.length;
    $("detailRelated").innerHTML = related.map(x => catalogCardHtml(x, false)).join("");
    // Only offer "read more" when the description is really cut off (measured once the page is visible).
    requestAnimationFrame(() => {
      const d = $("detailDesc");
      $("detailMore").hidden = !desc || d.scrollHeight <= d.clientHeight + 2;
    });
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
    const b = catalogBooks.find(x => x.id === id);
    if (!b) return;
    const btn = $("detailRead");
    if (btn.dataset.local) { openReader(btn.dataset.local).catch(err => { console.error(err); reportError("reader", err, "openReader"); showToast("بازکردن کتاب ناموفق بود."); }); return; }
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "در حال دریافت…";
    try {
      const blob = await catalogFile(id);
      const file = new File([blob], `${b.title}.epub`, { type: "application/epub+zip" });
      await openReader(null, { id: `preview-${id}`, preview: true, catalogId: id, title: b.title, author: b.author || "نویسنده نامشخص",
                               file, size: blob.size, language: b.language || "" });
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
    if (!rec || !currentBook || !currentBook.preview || currentBook.catalogId !== id || !rendition) return;
    currentBook = rec; currentProgressKey = rec.id;
    $("previewBanner").hidden = true;
    try {
      const loc = rendition.currentLocation();
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
      const cb = catalogBooks.find(x => x.id === id);
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
      await loadCatalog();
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
    const description = (prompt("یک توضیح کوتاه برای این کتاب (اختیاری):", "") || "").slice(0, 2000);
    const category = (prompt("دسته‌بندی (اختیاری) — مثلاً رمان، شعر، تاریخ:", "") || "").replace(/\s+/g, " ").trim().slice(0, 80);
    try {
      const r = await api("catalog-submit", { method: "POST", body: { book_id: b.serverId, description, ...(category ? { category } : {}) } });
      await put("books", { ...b, catalogStatus: r.status });
      showToast(r.status === "approved" ? "به کتابخانهٔ عمومی اضافه شد." : "برای تأیید مدیر ارسال شد.");
      renderLibrary();
    } catch (err) { showToast(apiMessage(err)); }
  }

  // ---------- Admin ----------
  const fmtDate = s => new Date(s * 1000).toLocaleDateString("fa-IR");
  const mb = n => (n / 1048576).toLocaleString("fa-IR", { maximumFractionDigits: 1 });
  const STATUS_FA = { approved: "عمومی", pending: "در انتظار", rejected: "ردشده" };
  async function loadAdmin() {
    try {
      const [pending, invites, users, stats, settings, catalogAll, booksAll] = await Promise.all([
        api("catalog-pending"), api("invites"), api("users"), api("admin-stats"), api("settings"), api("catalog-all"), api("books-all")]);
      const fa = n => Number(n).toLocaleString("fa-IR");
      $("adminStats").innerHTML = [
        [fa(stats.active_users) + " / " + fa(stats.users), "کاربر فعال"], [fa(stats.books), "کتاب روی سرور"], [mb(stats.storage_bytes) + " MB", "فضای کتاب‌ها"],
        [fa(stats.catalog_approved), "در کتابخانهٔ عمومی"], [fa(stats.catalog_pending), "در انتظار تأیید"], [fa(stats.open_invites), "دعوت‌نامهٔ باز"],
        [fa(stats.client_errors || 0), "خطای کاربران", stats.client_errors ? " stat-errors" : ""],
      ].map(([v, l, c]) => `<div class="stat${c || ""}"><b>${v}</b><span>${l}</span></div>`).join("");
      loadErrors(true);
      $("settingApproval").checked = !!settings.catalog_requires_approval;
      $("catalogAdminList").innerHTML = catalogAll.books.filter(b => b.status !== "pending").map(b => `<div class="admin-item">
          <span><strong>${esc(b.title)}</strong> — ${esc(b.author || "")} <span class="status-chip ${b.status === "approved" ? "" : "off"}">${STATUS_FA[b.status] || b.status}</span>${catOf(b) ? ` <span class="status-chip cat-chip">${esc(catOf(b))}</span>` : ""}
          ${b.description ? `<br><span class="muted">${esc(b.description)}</span>` : ""}</span>
          <span class="admin-row"><button class="secondary-button" type="button" data-cat-edit="${b.id}">ویرایش</button>
          <button class="secondary-button" type="button" data-cat-remove="${b.id}">برداشتن از کتابخانهٔ عمومی</button></span></div>`).join("")
        || '<p class="muted">کتابخانهٔ عمومی خالی است.</p>';
      $("booksAdminList").innerHTML = booksAll.books.map(b => `<div class="admin-item">
          <span><strong>${esc(b.title)}</strong> <span class="muted">· ${mb(b.size)} MB · ${esc(b.uploader || "؟")} · ${fa(b.readers)} خواننده</span>
          ${b.catalog_status ? `<span class="status-chip">${STATUS_FA[b.catalog_status] || b.catalog_status}</span>` : ""}</span>
          <span class="admin-row"><a class="secondary-button" href="${API}?r=book-file&id=${b.id}">دانلود</a>
          <button class="secondary-button" type="button" data-book-delete="${b.id}" data-title="${esc(b.title)}">حذف کامل</button></span></div>`).join("")
        || '<p class="muted">کتابی روی سرور نیست.</p>';
      $("pendingList").innerHTML = pending.books.length ? pending.books.map(b => `<div class="admin-item">
          <span><strong>${esc(b.title)}</strong> — ${esc(b.author || "")} <span class="muted">· ${esc(b.submitter || "")} · ${fmtDate(b.submitted_at)}</span>${catOf(b) ? ` <span class="status-chip cat-chip">${esc(catOf(b))}</span>` : ""}
          ${b.description ? `<br><span class="muted">${esc(b.description)}</span>` : ""}</span>
          <span class="admin-row"><a class="secondary-button" href="${API}?r=book-file&id=${b.id}">دانلود</a>
          <button class="secondary-button" type="button" data-review="${b.id}" data-approve="1">تأیید</button>
          <button class="secondary-button" type="button" data-review="${b.id}" data-approve="0">رد</button></span></div>`).join("")
        : '<p class="muted">موردی در انتظار نیست.</p>';
      $("inviteList").innerHTML = invites.invites.map(i => `<div class="admin-item"><span>${esc(i.note || "بدون یادداشت")}</span>
          <span class="admin-row"><span class="muted">${i.used_by ? `استفاده‌شده توسط ${esc(i.used_by)}` : i.expires_at * 1000 < Date.now() ? "منقضی" : `معتبر تا ${fmtDate(i.expires_at)}`}</span>
          ${!i.used_by && i.expires_at * 1000 > Date.now() ? `<button class="secondary-button" type="button" data-invite-revoke="${esc(i.id)}">باطل کردن</button>` : ""}</span></div>`).join("")
        || '<p class="muted">هنوز دعوت‌نامه‌ای ساخته نشده.</p>';
      const me = account?.user?.id;
      $("userList").innerHTML = users.users.map(u => `<div class="admin-item" data-user="${u.id}">
          <span>${esc(u.name)} <span class="muted" dir="ltr">${esc(u.email)}</span><br>
          <span class="muted small">${u.role === "admin" ? "مدیر" : "کاربر"} · ${fa(u.books)} کتاب${u.last_seen ? ` · آخرین ورود ${fmtDate(u.last_seen)}` : ""}</span>
          ${u.disabled ? '<span class="status-chip off">غیرفعال</span>' : ""}</span>
          ${u.id === me ? '<span class="muted small">(خودت)</span>' : `<span class="admin-row">
            <button class="secondary-button" type="button" data-user-act="${u.disabled ? "enable" : "disable"}">${u.disabled ? "فعال کردن" : "غیرفعال کردن"}</button>
            <button class="secondary-button" type="button" data-user-act="${u.role === "admin" ? "demote" : "promote"}">${u.role === "admin" ? "کاربر عادی شود" : "مدیر شود"}</button>
            <button class="secondary-button" type="button" data-user-act="reset">ریست رمز</button>
            <button class="secondary-button" type="button" data-user-act="delete">حذف</button></span>`}</div>`).join("");
    } catch (err) { $("pendingList").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; }
  }
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
    if (t.closest("[data-cat-remove]")) {
      if (!confirm("این کتاب از کتابخانهٔ عمومی برداشته شود؟ (از کتابخانهٔ شخصی کسانی که اضافه‌اش کرده‌اند حذف نمی‌شود)")) return;
      return adminAction("catalog-remove", { book_id: Number(t.closest("[data-cat-remove]").dataset.catRemove) }, "از کتابخانهٔ عمومی برداشته شد.");
    }
    if (t.closest("[data-cat-edit]")) {
      const id = Number(t.closest("[data-cat-edit]").dataset.catEdit);
      let all;
      try { all = (await api("catalog-all")).books; } catch (err) { showToast(apiMessage(err)); return; }
      const b = all.find(x => x.id === id);
      if (!b) return;
      fillCategoryList(all.map(catOf));
      const f = $("catalogEditForm");
      f.dataset.bookId = String(id);
      for (const k of CATALOG_EDIT_FIELDS) f.elements[k].value = b[k] == null ? "" : String(b[k]);
      $("catalogEditDialog").showModal();
      return;
    }
    if (t.closest("[data-book-delete]")) {
      const el = t.closest("[data-book-delete]");
      if (!confirm(`«${el.dataset.title}» برای همه حذف شود؟ این کار برگشت‌پذیر نیست.`)) return;
      return adminAction("book-delete", { book_id: Number(el.dataset.bookDelete) }, "کتاب حذف شد.");
    }
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
  const CATALOG_EDIT_FIELDS = ["title", "author", "description", "category", "publisher", "published", "language"];
  $("catalogEditClose").addEventListener("click", () => $("catalogEditDialog").close());
  $("catalogEditForm").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, body = { book_id: Number(f.dataset.bookId) };
    for (const k of CATALOG_EDIT_FIELDS) body[k] = f.elements[k].value.replace(k === "description" ? /[ \t]+/g : /\s+/g, " ").trim();
    if (!body.title) { f.elements.title.focus(); return; }
    $("catalogEditSave").disabled = true;
    const r = await adminAction("catalog-update", body, "ذخیره شد.");
    $("catalogEditSave").disabled = false;
    if (r) { $("catalogEditDialog").close(); catalogLoaded = false; }
  });
  $("createInvite").addEventListener("click", async () => {
    try {
      const r = await api("invites", { method: "POST", body: { note: $("inviteNote").value.trim(), days: 14 } });
      $("inviteResult").textContent = `کد دعوت: ${r.code}`;
      $("inviteNote").value = "";
      loadAdmin();
    } catch (err) { showToast(apiMessage(err)); }
  });

  // ======================= v8.3 text cleanup («پاک‌سازی متن») =======================
  // Rules remove only the matched phrase from the EPUB file itself (no original is kept). They apply to every
  // book: on demand (search → preview → apply) and automatically to newly imported books.
  const CLEAN_KEY = "borg-clean-rules" + STORAGE_SUFFIX;
  const CLEAN_MIN_LEN = 4, CLEAN_MAX_HITS = 300, CLEAN_MAX_SHARE = 0.02, CLEAN_SNIPPETS = 5;
  const TG_LABEL = "لینک‌های تلگرام";
  // t.me / telegram.me / telegram.dog URLs (with or without scheme) and @handles of 5–32 [A-Za-z0-9_] characters.
  const TG_SOURCE = String.raw`(?:(?<![A-Za-z0-9_.\-@])https?:\/\/(?:www\.)?|(?<![A-Za-z0-9_.\-\/@:])(?:www\.)?)(?:t\.me|telegram\.me|telegram\.dog)\/[A-Za-z0-9_\-+\/?=&%.#~]*[A-Za-z0-9_\-\/]|(?<![A-Za-z0-9_.@\/])@[A-Za-z0-9_]{5,32}(?![A-Za-z0-9_])`;
  function loadCleanRules() {
    try { const r = JSON.parse(localStorage.getItem(CLEAN_KEY) || "[]"); return Array.isArray(r) ? r.filter(x => x && (x.type === "telegram" || (x.type === "phrase" && typeof x.text === "string"))) : []; }
    catch (_) { return []; }
  }
  function saveCleanRules(rules) { try { localStorage.setItem(CLEAN_KEY, JSON.stringify(rules)); } catch (_) {} }
  const ruleLabel = r => r.type === "telegram" ? TG_LABEL : `«${r.text}»`;
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
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const touched = new Set(); let chars = 0, changed = false;
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const parentName = (n.parentNode && n.parentNode.localName || "").toLowerCase();
      if (parentName === "script" || parentName === "style") continue;
      let v = n.nodeValue; chars += v.length;
      const orig = v;
      for (const r of rules) {
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
  const isTooBroad = (s, totalChars) => s.count > CLEAN_MAX_HITS || (totalChars > 0 && s.chars > totalChars * CLEAN_MAX_SHARE);

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
    if (!rec || !file || !rules.length) return 0;
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
      if (!file) return 0;
      const dry = await cleanEpub(await file.arrayBuffer(), rules, false);
      const ok = rules.filter(r => dry.stats[r.id].count && !isTooBroad(dry.stats[r.id], dry.totalChars));
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
      if (!count) return "";
      const snips = live.flatMap(r => b.stats[r.id].snippets).slice(0, CLEAN_SNIPPETS)
        .map(s => `<li dir="auto">…${esc(s.before)}<mark>${esc(s.match)}</mark>${esc(s.after)}…</li>`).join("");
      return `<div class="clean-book" data-clean-id="${esc(b.id)}"><label class="clean-book-head"><input type="checkbox" data-clean-book="${esc(b.id)}" checked>`
        + ` <b dir="auto">${esc(b.title)}</b> <span class="clean-count">${faNum(count)} مورد</span></label><ul class="clean-snips">${snips}</ul></div>`;
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
      const all = (await getAllBooks()).filter(b => fileOf(b));
      const books = [], broad = new Set();
      for (let i = 0; i < all.length; i++) {
        setCleanProgress(i, all.length, `در حال بررسی ${faNum(i + 1)} از ${faNum(all.length)}: «${all[i].title}»`);
        await sleep(0); // keep the page responsive between books
        try {
          const r = await cleanEpub(await fileOf(all[i]).arrayBuffer(), rules, false);
          rules.forEach(rule => { if (isTooBroad(r.stats[rule.id], r.totalChars)) broad.add(rule.id); });
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
  $("cleanOpen").addEventListener("click", () => { renderCleanRules(); $("cleanError").textContent = ""; $("cleanDialog").showModal(); $("cleanPhrase").focus(); });
  $("cleanClose").addEventListener("click", () => $("cleanDialog").close());
  $("cleanForm").addEventListener("submit", e => {
    e.preventDefault();
    const text = $("cleanPhrase").value.trim(), rules = loadCleanRules();
    if ([...text].length < CLEAN_MIN_LEN) { $("cleanError").textContent = `عبارت باید دست‌کم ${faNum(CLEAN_MIN_LEN)} نویسه باشد؛ عبارت‌های کوتاه بخش‌های درست متن را هم پاک می‌کنند.`; return; }
    if (rules.some(r => r.type === "phrase" && r.text === text)) { $("cleanError").textContent = "این عبارت قبلاً اضافه شده است."; return; }
    rules.push({ id: newUid(), type: "phrase", text: text.slice(0, 200) });
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

  // ---------- Start ----------
  // ---- Repair titles/authors/covers of books imported by old versions that could not read metadata ----
  const UNKNOWN_AUTHOR = "نویسنده نامشخص";
  const looksLikeFilename = t => /_|\.epub$/i.test(t || "");
  async function repairMetadata() {
    let changed = false;
    for (const b of await getAllBooks()) {
      if ((b.metaChecked && b.metaExt) || !fileOf(b)) continue;
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
