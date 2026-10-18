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
  let fontSize = 100, toastTimer;
  const READER_PREFS_KEY = "ketabkhane-reader-preferences" + STORAGE_SUFFIX;
  const defaultReaderPrefs = { font: "vazirmatn", lineHeight: "1.9", fontSize: 100, theme: "light", align: "justify", librarySort: "added", libraryFilter: "all", dailyGoal: 20,
    margin: "m", para: "m", flow: "paginated", spread: "auto", digits: false, taps: true, wake: true, ttsRate: 1, ttsVoice: "" };
  const READER_THEMES = { light: ["#fffdf7", "#252923"], sepia: ["#f4ecd8", "#5b4636"], dark: ["#1f2623", "#e6ece7"], black: ["#000000", "#d4d4d4"] };
  const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function effectiveTheme() {
    if (readerPrefs.theme === "auto") return darkQuery && darkQuery.matches ? "dark" : "light";
    return READER_THEMES[readerPrefs.theme] ? readerPrefs.theme : "light";
  }
  let readerPrefs = loadReaderPrefs();
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
    const align = readerPrefs.align === "right" ? "right" : "justify";
    rendition.themes.default({
      body: { "font-family": selectedFontStack(), "line-height": lineHeight, "color": ink, "background": background, "direction": "rtl" },
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
  function putRaw(store, value) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).put(value);
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
    if (!account || !navigator.onLine) return;
    clearTimeout(metaTimer);
    metaTimer = setTimeout(() => { syncLibraryMeta(null).catch(err => console.warn("Meta sync failed:", err)); }, 2000);
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
    if (!currentBook || document.visibilityState !== "visible" || Date.now() - lastActivity > 120000) return;
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
    let title = file.name.replace(/\.epub$/i, ""), author = "نویسنده نامشخص", cover = null;
    let temp = null;
    try {
      temp = ePub(await file.arrayBuffer());
      // epub.js reports a broken archive through the "openFailed" event instead of rejecting `opened`.
      const failed = new Promise((_, reject) => temp.on("openFailed", err => reject(err || new Error("openFailed"))));
      await withTimeout(Promise.race([temp.opened, failed]), 20000, "خواندن این EPUB بیش از حد طول کشید؛ ممکن است فایل خراب باشد.");
      const metadata = await temp.loaded.metadata;
      if (metadata.title) title = metadata.title;
      if (metadata.creator) author = metadata.creator;
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
      throw new UserError("این فایل EPUB قابل خواندن نیست یا خراب است.");
    } finally {
      if (temp) { try { temp.destroy(); } catch (_) {} }
    }
    return { title, author, cover };
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
        file: file, size: file.size, addedAt: Date.now(), contentHash
      };
      try {
        await put("books", record);
      } catch (err) {
        console.error("Saving book failed:", err);
        throw new UserError(err && err.name === "QuotaExceededError"
          ? "فضای ذخیره‌سازی مرورگر کافی نیست."
          : "ذخیره کتاب در مرورگر انجام نشد.");
      }
      renderLibrary();
      requestPersistentStorage();
      sync();
      showToast("کتاب به کتابخانه همین دستگاه اضافه شد.");
    } catch (err) {
      // Expected outcomes (duplicate, broken file, …) are UserErrors and are not logged as app errors.
      if (!(err instanceof UserError)) console.error(err);
      showToast(err instanceof UserError ? err.message : "افزودن کتاب انجام نشد.");
    } finally {
      resetInputs();
    }
  }
  let openToken = 0;
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
  async function saveProgress(bookId, cfi, pct, bookmark) {
    const old = await getProgress(bookId);
    await put("progress", { bookId, cfi, percentage: pct, updatedAt: Date.now(), bookmark: bookmark !== undefined ? bookmark : (old?.bookmark || null) });
    scheduleProgressPush(bookId);
  }
  async function prepareLocations(record, token) {
    const book = bookInstance;
    try {
      if (record.locations) {
        book.locations.load(record.locations);
      } else {
        await book.locations.generate(1600);
        if (token !== openToken) return;
        const latest = await getBook(record.id);
        if (latest) await put("books", { ...latest, locations: book.locations.save() });
      }
      if (token !== openToken || !rendition) return;
      const loc = rendition.currentLocation();
      if (loc?.start?.cfi) {
        const pct = percentFor(loc.start.cfi);
        showProgress(pct);
        await saveProgress(record.id, loc.start.cfi, pct);
      }
    } catch (err) {
      console.warn("Could not compute reading progress:", err);
    }
  }
  async function openReader(id) {
    const record = await getBook(id);
    if (!record) { showToast("فایل این کتاب روی دستگاه پیدا نشد."); return; }
    if (!librariesReady()) { showToast("کتاب‌خوان کامل بارگذاری نشده است. صفحه را تازه‌سازی کن."); return; }
    const token = ++openToken;
    currentBook = record; currentProgressKey = record.id;
    $("readerTitle").textContent = record.title;
    $("reader").classList.remove("hidden");
    document.body.style.overflow = "hidden";
    $("tocList").innerHTML = '<p class="muted">در حال ساخت فهرست…</p>';
    $("viewer").innerHTML = "";
    $("chapterLabel").textContent = "شروع مطالعه";
    $("prevPage").disabled = true; $("nextPage").disabled = false;
    showProgress(0);
    if (bookInstance) { try { bookInstance.destroy(); } catch (_) {} }
    const bookFile = fileOf(record);
    if (!bookFile) { showToast("فایل این کتاب روی دستگاه پیدا نشد."); return; }
    const epubBytes = await bookFile.arrayBuffer();
    if (token !== openToken) return;
    bookInstance = ePub(epubBytes);
    bookInstance.on("openFailed", err => {
      console.error("EPUB openFailed:", err);
      if (token === openToken) showToast("این فایل EPUB قابل خواندن نیست یا خراب است.");
    });
    rendition = bookInstance.renderTo("viewer", {
      width: "100%", height: "100%", flow: readerPrefs.flow === "scrolled" ? "scrolled-doc" : "paginated",
      spread: readerPrefs.spread === "none" ? "none" : "auto", manager: "default"
    });
    rendition.hooks.content.register(onBookContent);
    // The book is rendered in an iframe; load the bundled font there too.
    rendition.hooks.content.register(contents => contents.addStylesheet(READER_FONTS_URL));
    applyReaderStyles();
    // Input inside the book iframe does not reach the main document; epub.js re-emits it on the rendition.
    rendition.on("keydown", handleReaderKey);
    rendition.on("touchstart", onTouchStart);
    rendition.on("touchend", onTouchEnd);
    rendition.on("click", onReaderClick);
    rendition.on("selected", onTextSelected);
    rendition.on("keydown", markActivity); rendition.on("touchstart", markActivity); rendition.on("click", markActivity);
    lastTurnAt = 0; markActivity(); updateWakeLock();
    if (!record.status || record.status === "want") setBookMeta(record.id, { status: "reading" }).then(r => { if (r && currentBook && currentBook.id === r.id) currentBook = r; });
    renderedHighlights = new Set();
    rendition.on("relocated", async location => {
      if (token !== openToken || !location?.start) return;
      const pct = percentFor(location.start.cfi, location.start.percentage);
      showProgress(pct);
      $("chapterLabel").textContent = location.start.index != null ? `بخش ${Number(location.start.index + 1).toLocaleString("fa-IR")}` : "مطالعه";
      $("prevPage").disabled = !!location.atStart;
      if (!pendingSelection) hideSelectionBar();
      updateBookmarkButton();
      markActivity();
      updateTimeLeft(location);
      onTtsRelocated(location);
      if (location.atEnd && currentBook && currentBook.status !== "finished") {
        setBookMeta(currentBook.id, { status: "finished" }).then(r => { if (r) { currentBook = r; showToast("تبریک! این کتاب را تمام کردی."); } });
      }
      $("nextPage").disabled = !!location.atEnd;
      try { await saveProgress(record.id, location.start.cfi, pct); } catch (err) { console.warn("Saving progress failed:", err); }
    });
    rendition.on("displayError", (err) => {
      console.error("EPUB displayError:", err);
      showToast("نمایش این کتاب ناموفق بود. خطای دقیق در Console ثبت شده است.");
    });
    rendition.on("rendered", () => {
      try {
        rendition.getContents().forEach(c => {
          c.addStylesheetRules({ "body": { "direction": "rtl" } });
        });
      } catch (_) {}
    });
    const saved = await getProgress(record.id);
    let shown = false;
    if (saved?.cfi) {
      try { await rendition.display(saved.cfi); shown = true; } catch (err) { console.warn("Saved location could not be restored:", err); }
    }
    if (!shown) await rendition.display();
    if (token !== openToken) return;
    const nav = await bookInstance.loaded.navigation;
    if (token !== openToken) return;
    const toc = nav.toc || [];
    $("tocList").innerHTML = toc.length ? toc.map((item, i) => `<button class="toc-link" type="button" data-href="${esc(item.href)}">${esc(item.label || `بخش ${i+1}`)}</button>`).join("") : '<p class="muted">فهرست فصل‌ها در این EPUB وجود ندارد.</p>';
    $("tocList").querySelectorAll("[data-href]").forEach(btn => btn.addEventListener("click", () => { closeToc(false); if (rendition) rendition.display(btn.dataset.href); }));
    await renderBookAnnotations();
    updateBookmarkButton();
    prepareLocations(record, token);
  }
  function getProgress(id) {
    return new Promise((resolve, reject) => {
      const req = db.transaction("progress", "readonly").objectStore("progress").get(id);
      req.onsuccess = () => resolve(req.result || null); req.onerror = () => reject(req.error);
    });
  }
  async function closeReader() {
    if (rendition && currentBook) {
      try {
        const loc = rendition.currentLocation();
        if (loc?.start?.cfi) await saveProgress(currentBook.id, loc.start.cfi, percentFor(loc.start.cfi, loc.start.percentage));
      } catch (_) {}
    }
    openToken++;
    $("reader").classList.remove("toc-open", "immersive");
    closeSettings(false);
    closeSearch(false);
    hideSelectionBar(); renderedHighlights = new Set();
    ttsStop(); $("notePopup").hidden = true; $("lookupBox").hidden = true; closeImage();
    $("bookSearchResults").innerHTML = ""; $("bookSearchStatus").textContent = ""; $("bookSearchInput").value = "";
    $("tocToggle").setAttribute("aria-expanded", "false");
    $("reader").classList.add("hidden");
    document.body.style.overflow = "";
    if (bookInstance) { try { bookInstance.destroy(); } catch (_) {} }
    bookInstance = null; rendition = null; currentBook = null;
    updateWakeLock();
    renderLibrary();
  }
  // ---- Reader navigation (v6.1): keyboard, swipe, edge tap zones, TOC drawer, immersive mode ----
  const SWIPE_MIN_PX = 50, EDGE_ZONE = 0.25;
  let touchStart = null, suppressClickUntil = 0;
  function bookIsRtl() {
    try {
      const meta = ((bookInstance && (bookInstance.packaging || bookInstance.package)) || {}).metadata || {};
      if (meta.direction === "rtl") return true;
      if (meta.direction === "ltr") return false;
      return /^(fa|ar|he|ur|ps|ku)/i.test(meta.language || "fa");
    } catch (_) { return true; }
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
    const first = $("tocList").querySelector("button");
    setTimeout(() => (first || $("tocPanel")).focus({ preventScroll: true }), 50);
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
    if (on) showToast("برای نمایش دوبارهٔ نوارها، وسط صفحه را لمس کن.");
    // The viewer changed size without a window resize, so re-paginate explicitly.
    requestAnimationFrame(() => { try { if (rendition) rendition.resize(); } catch (_) {} });
  }
  function handleReaderKey(e) {
    if (!readerOpen() || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const tag = (e.target && e.target.tagName) || "";
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
        if (!$("imageViewer").hidden) closeImage();
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
  function onTouchEnd(e) {
    const t = e.changedTouches && e.changedTouches[0];
    if (!touchStart || !t) return;
    const dx = t.clientX - touchStart.x, dy = t.clientY - touchStart.y, dt = Date.now() - touchStart.time;
    touchStart = null;
    if (Math.abs(dx) >= SWIPE_MIN_PX && Math.abs(dx) > Math.abs(dy) * 1.5 && dt < 800) {
      suppressClickUntil = Date.now() + 500;
      // Finger moving right turns to the next page in an RTL book, like a Persian paper book.
      if (dx > 0) goLeft(); else goRight();
    }
  }
  function onReaderClick(e, contents) {
    if (Date.now() < suppressClickUntil) return;
    if (settingsOpen() || searchOpen()) { closeSettings(false); closeSearch(false); return; }
    if (!$("selectionBar").hidden) { hideSelectionBar(); return; }
    if (!$("notePopup").hidden || !$("lookupBox").hidden) { $("notePopup").hidden = true; $("lookupBox").hidden = true; return; }
    if (e.target && e.target.closest && e.target.closest("a, button, input, select, textarea, audio, video, label")) return;
    try {
      const win = (contents && contents.window) || e.view;
      if (win && String(win.getSelection()).trim()) return;
    } catch (_) {}
    const frame = e.view && e.view.frameElement;
    const box = $("viewer").getBoundingClientRect();
    if (!frame || !box.width) return;
    const ratio = (frame.getBoundingClientRect().left + e.clientX - box.left) / box.width;
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

  // ======================= Reading features (v8) =======================
  function reopenCurrentBook() {
    if (!currentBook || !rendition) return;
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
  $("notePopupGo").addEventListener("click", () => { $("notePopup").hidden = true; if (rendition && noteTarget) rendition.display(noteTarget); });
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
        showFootnote(contents, a).then(ok => { if (!ok && rendition) rendition.display(a.getAttribute("href")); }).catch(() => {});
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
    rendition.display(btn.dataset.cfi);
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
    rec.updatedAt = Date.now(); rec.dirty = true;
    await put("annotations", rec);
    scheduleAnnotationPush();
  }
  $("bookmarkButton").addEventListener("click", async () => {
    if (!currentBook || !rendition) return;
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
        <span class="note-text"><span class="note-quote">${a.type === "bookmark" ? "🔖 " : ""}${esc(a.quote || "")}</span>${a.note ? `<span class="note-note">✎ ${esc(a.note)}</span>` : ""}</span>
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
    if (item && rendition) { closeToc(false); rendition.display(item.dataset.goto); }
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
    if (card) openReader(card.dataset.open).catch(err => { console.error(err); showToast("بازکردن کتاب ناموفق بود."); });
  };
  const onLibraryKey = e => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-open]")) { e.preventDefault(); openReader(e.target.dataset.open).catch(err => { console.error(err); showToast("بازکردن کتاب ناموفق بود."); }); }
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
  function setSyncStatus(text) { $("syncStatus").textContent = text || ""; }

  function setAccount(acc) {
    account = acc;
    const user = acc && acc.user;
    $("accountButton").textContent = user ? user.name : "ورود";
    document.querySelector('[data-view="catalog"]').hidden = !user;
    document.querySelector('[data-view="admin"]').hidden = !(user && user.role === "admin");
    if (!user) { showView("mine"); setSyncStatus(""); }
    $("authForms").hidden = !!user;
    $("accountInfo").hidden = !user;
    if (user) { $("accountName").textContent = user.name; $("accountEmail").textContent = user.email; }
    renderLibrary();
  }

  // ---------- Views ----------
  function showView(view) {
    document.querySelectorAll(".view-tabs [role=tab]").forEach(t => t.setAttribute("aria-selected", String(t.dataset.view === view)));
    $("viewMine").hidden = view !== "mine";
    $("viewCatalog").hidden = view !== "catalog";
    $("viewAdmin").hidden = view !== "admin";
    if (view === "catalog") loadCatalog();
    if (view === "admin") loadAdmin();
  }
  document.querySelectorAll(".view-tabs [role=tab]").forEach(t => t.addEventListener("click", () => showView(t.dataset.view)));

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
      sha256: b.contentHash, size: data.byteLength, title: b.title || "بدون عنوان", author: b.author || "", cover: b.cover || undefined,
    } });
    if (r.exists) return r.book;
    const chunk = r.chunk_bytes, id = r.upload_id;
    for (let off = 0; off < data.byteLength; off += chunk) {
      await api("upload-chunk", { method: "POST", raw: data.slice(off, off + chunk), query: `&upload_id=${id}&offset=${off}` });
    }
    r = await api("upload-complete", { method: "POST", body: { upload_id: id } });
    return r.book;
  }
  async function downloadBook(sb) {
    const res = await api("book-file", { query: `&id=${sb.id}` });
    const blob = await res.blob();
    let cover = null;
    if (sb.has_cover) {
      try { cover = await blobToDataUrl(await (await api("book-cover", { query: `&id=${sb.id}` })).blob()); } catch (_) {}
    }
    const record = {
      id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      title: sb.title, author: sb.author || "نویسنده نامشخص", cover,
      file: new File([blob], `${sb.title}.epub`, { type: "application/epub+zip" }),
      size: blob.size, addedAt: Date.now(), contentHash: sb.sha256, serverId: sb.id, catalogStatus: sb.catalog_status || null,
    };
    await put("books", record);
    return record;
  }
  async function sync() {
    if (!account || !navigator.onLine) return;
    if (syncing) { syncAgain = true; return; }
    syncing = true;
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
      local = await getAllBooks();
      const localSha = new Set();
      let done = 0;
      const toUpload = [];
      for (let b of local) {
        b = await ensureHash(b);
        if (b.contentHash) localSha.add(b.contentHash);
        const sb = b.contentHash && bySha.get(b.contentHash);
        if (sb) {
          const upd = { ...b, serverId: sb.id, catalogStatus: sb.catalog_status || null };
          const serverAuthor = sb.author || "نویسنده نامشخص";
          if (b.metaRepaired && !b.metaPushed) {
            try { await api("book-meta", { method: "POST", body: { book_id: sb.id, title: b.title, author: b.author === "نویسنده نامشخص" ? "" : (b.author || ""), cover: b.cover || undefined } }); } catch (_) {}
            upd.metaPushed = true;
          } else if (!b.metaRepaired && (sb.title !== b.title || serverAuthor !== (b.author || "نویسنده نامشخص"))) {
            upd.title = sb.title; upd.author = serverAuthor;
          }
          if (!b.cover && sb.has_cover) {
            try { upd.cover = await blobToDataUrl(await (await api("book-cover", { query: `&id=${sb.id}` })).blob()); } catch (_) {}
          }
          if (["serverId", "catalogStatus", "title", "author", "cover", "metaPushed"].some(k => upd[k] !== b[k])) await put("books", upd);
        } else if (b.contentHash) toUpload.push(b);
      }
      for (const b of toUpload) {
        setSyncStatus(`در حال آپلود «${b.title}» (${(++done).toLocaleString("fa-IR")} از ${toUpload.length.toLocaleString("fa-IR")})…`);
        try { const sb = await uploadBook(b); await put("books", { ...b, serverId: sb.id }); byId.set(sb.id, sb); }
        catch (err) { console.warn("Upload failed:", b.title, err); }
      }
      const missing = server.filter(sb => !localSha.has(sb.sha256));
      done = 0;
      for (const sb of missing) {
        setSyncStatus(`در حال دریافت «${sb.title}» (${(++done).toLocaleString("fa-IR")} از ${missing.length.toLocaleString("fa-IR")})…`);
        try { await downloadBook(sb); } catch (err) { console.warn("Download failed:", sb.title, err); }
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
      setSyncStatus("همگام شد ✓");
    } catch (err) {
      setSyncStatus(err instanceof ApiFail && err.status === 401 ? "" : "همگام‌سازی انجام نشد");
      console.warn("Sync failed:", err);
    } finally {
      syncing = false;
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
    const localToServer = new Map(books.filter(b => b.serverId).map(b => [b.id, b.serverId]));
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
    if (!account || !navigator.onLine) return;
    clearTimeout(annTimer);
    annTimer = setTimeout(() => { syncAnnotations().catch(err => console.warn("Annotation sync failed:", err)); }, 2500);
  }
  /** Debounced push of one book's progress while reading. */
  function scheduleProgressPush(localBookId) {
    if (!account || !navigator.onLine) return;
    clearTimeout(progressTimers.get(localBookId));
    progressTimers.set(localBookId, setTimeout(async () => {
      progressTimers.delete(localBookId);
      try {
        const b = await getBook(localBookId), p = await getProgress(localBookId);
        if (b && b.serverId && p && p.cfi) await api("progress", { method: "POST", body: { book_id: b.serverId, cfi: p.cfi, percentage: p.percentage || 0, updated_at: p.updatedAt || Date.now() } });
      } catch (_) {}
    }, 4000));
  }

  // ---------- Public catalog ----------
  let catalogBooks = [];
  async function loadCatalog() {
    $("catalogGrid").innerHTML = '<p class="muted">در حال بارگذاری…</p>';
    try {
      catalogBooks = (await api("catalog")).books;
      renderCatalog();
    } catch (err) { $("catalogGrid").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; }
  }
  function renderCatalog() {
    {
      const q = $("catalogSearch").value.trim().toLocaleLowerCase();
      const books = catalogBooks.filter(b => !q || `${b.title} ${b.author} ${b.description || ""} ${b.submitter || ""}`.toLocaleLowerCase().includes(q));
      $("catalogEmpty").hidden = catalogBooks.length > 0;
      $("catalogGrid").innerHTML = books.map(b => {
        const [bg, fg] = paletteFor(b.title || String(b.id));
        const cover = b.has_cover ? `<img alt="" loading="lazy" src="${API}?r=book-cover&id=${b.id}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">`
          : `<div style="position:relative"><div class="cover-symbol">✳</div><div class="cover-name">${esc(b.title)}</div></div>`;
        const action = b.in_library ? '<span class="book-badge">در کتابخانهٔ تو</span>'
          : `<button class="secondary-button" type="button" data-catalog-add="${b.id}">＋ افزودن به کتابخانهٔ من</button>`;
        return `<article class="book-card catalog-card" data-detail="${b.id}" tabindex="0"><div class="book-cover-thumb" style="background:${bg};color:${fg}">${cover}</div>
          <div class="book-meta"><h3 title="${esc(b.title)}">${esc(b.title)}</h3><p>${esc(b.author || "نویسنده نامشخص")}</p>
          ${b.description ? `<p title="${esc(b.description)}">${esc(b.description)}</p>` : ""}<div class="catalog-actions">${action}</div></div></article>`;
      }).join("");
      if (catalogBooks.length && !books.length) $("catalogGrid").innerHTML = '<p class="muted">کتابی با این عبارت پیدا نشد.</p>';
    }
  }
  $("catalogSearch").addEventListener("input", renderCatalog);
  function openBookDialog(id) {
    const b = catalogBooks.find(x => x.id === id);
    if (!b) return;
    const [bg, fg] = paletteFor(b.title || String(b.id));
    $("bookDialogCover").style.background = bg; $("bookDialogCover").style.color = fg;
    $("bookDialogCover").innerHTML = b.has_cover ? `<img alt="" src="${API}?r=book-cover&id=${b.id}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">` : `<div class="cover-symbol">✳</div>`;
    $("bookDialogTitle").textContent = b.title;
    $("bookDialogAuthor").textContent = b.author || "نویسنده نامشخص";
    $("bookDialogMeta").textContent = `${(b.size / 1048576).toLocaleString("fa-IR", { maximumFractionDigits: 1 })} مگابایت` + (b.submitter ? ` · پیشنهاد ${b.submitter}` : "");
    $("bookDialogDesc").textContent = b.description || "";
    $("bookDialogActions").innerHTML = b.in_library ? '<span class="book-badge">در کتابخانهٔ تو</span>' : `<button class="primary-button" type="button" data-catalog-add="${b.id}">＋ افزودن به کتابخانهٔ من</button>`;
    $("bookDialog").showModal();
  }
  $("bookDialogClose").addEventListener("click", () => $("bookDialog").close());
  $("bookDialogActions").addEventListener("click", e => { if (e.target.closest("[data-catalog-add]")) { $("bookDialog").close(); addFromCatalog(Number(e.target.closest("[data-catalog-add]").dataset.catalogAdd)); } });
  async function addFromCatalog(id, btn) {
    if (btn) btn.disabled = true;
    try {
      await api("catalog-add", { method: "POST", body: { book_id: id } });
      showToast("به کتابخانه‌ات اضافه شد؛ در حال دریافت…");
      await sync(); loadCatalog();
    } catch (err) { if (btn) btn.disabled = false; showToast(apiMessage(err)); }
  }
  $("catalogGrid").addEventListener("click", e => {
    const btn = e.target.closest("[data-catalog-add]");
    if (btn) { e.stopPropagation(); addFromCatalog(Number(btn.dataset.catalogAdd), btn); return; }
    const card = e.target.closest("[data-detail]");
    if (card) openBookDialog(Number(card.dataset.detail));
  });
  $("catalogGrid").addEventListener("keydown", e => {
    const card = e.target.closest("[data-detail]");
    if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openBookDialog(Number(card.dataset.detail)); }
  });
  async function shareBook(localId) {
    const b = await getBook(localId);
    if (!b || !b.serverId) { showToast("اول باید همگام‌سازی کامل شود."); return; }
    const description = (prompt("یک توضیح کوتاه برای این کتاب (اختیاری):", "") || "").slice(0, 2000);
    try {
      const r = await api("catalog-submit", { method: "POST", body: { book_id: b.serverId, description } });
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
      ].map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
      $("settingApproval").checked = !!settings.catalog_requires_approval;
      $("catalogAdminList").innerHTML = catalogAll.books.filter(b => b.status !== "pending").map(b => `<div class="admin-item">
          <span><strong>${esc(b.title)}</strong> — ${esc(b.author || "")} <span class="status-chip ${b.status === "approved" ? "" : "off"}">${STATUS_FA[b.status] || b.status}</span>
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
          <span><strong>${esc(b.title)}</strong> — ${esc(b.author || "")} <span class="muted">· ${esc(b.submitter || "")} · ${fmtDate(b.submitted_at)}</span>
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
      const b = (await api("catalog-all")).books.find(x => x.id === id);
      if (!b) return;
      const title = prompt("عنوان:", b.title); if (title === null) return;
      const author = prompt("نویسنده:", b.author || ""); if (author === null) return;
      const description = prompt("توضیحات:", b.description || ""); if (description === null) return;
      return adminAction("catalog-update", { book_id: id, title: title.trim() || b.title, author: author.trim(), description: description.trim() }, "ذخیره شد.");
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
  $("createInvite").addEventListener("click", async () => {
    try {
      const r = await api("invites", { method: "POST", body: { note: $("inviteNote").value.trim(), days: 14 } });
      $("inviteResult").textContent = `کد دعوت: ${r.code}`;
      $("inviteNote").value = "";
      loadAdmin();
    } catch (err) { showToast(apiMessage(err)); }
  });

  // ---------- Start ----------
  // ---- Repair titles/authors/covers of books imported by old versions that could not read metadata ----
  const UNKNOWN_AUTHOR = "نویسنده نامشخص";
  const looksLikeFilename = t => /_|\.epub$/i.test(t || "");
  async function repairMetadata() {
    let changed = false;
    for (const b of await getAllBooks()) {
      if (b.metaChecked || !fileOf(b)) continue;
      const rec = { ...b, metaChecked: true };
      const weak = !b.cover || !b.author || b.author === UNKNOWN_AUTHOR || looksLikeFilename(b.title);
      if (weak) {
        try {
          const raw = fileOf(b);
          const file = raw instanceof File ? raw : new File([raw], "book.epub", { type: "application/epub+zip" });
          const fromName = file.name.replace(/\.epub$/i, "");
          const m = await extractMetadata(file);
          if (m.title && m.title !== fromName && (looksLikeFilename(b.title) || b.title === fromName)) rec.title = m.title;
          if (m.author && m.author !== UNKNOWN_AUTHOR && (!b.author || b.author === UNKNOWN_AUTHOR)) rec.author = m.author;
          if (!b.cover && m.cover) rec.cover = m.cover;
          if (rec.title !== b.title || rec.author !== b.author || rec.cover !== b.cover) { rec.metaRepaired = true; rec.metaPushed = false; changed = true; }
        } catch (_) { /* unreadable file: keep what we have */ }
      }
      await put("books", rec);
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
      if (r.user) { setAccount({ user: r.user, csrf: r.csrf }); sync(); }
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
    }).catch(err => console.warn("Service worker registration failed", err)));
  }
})();
