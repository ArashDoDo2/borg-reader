/* Borg (بُرگ) — local EPUB library and reader. Cloud accounts and sync are not implemented yet. */
(() => {
  "use strict";
  // Storage names predate the rename and are kept so existing books survive (ADR 0003).
  // Staging shares the origin with production, so it gets its own storage (ADR 0005).
  const STORAGE_SUFFIX = location.pathname.includes("/borg-test/") ? "-test" : "";
  const DB_NAME = "ketabkhane-db" + STORAGE_SUFFIX;
  const THEME_KEY = "ketab-theme" + STORAGE_SUFFIX;
  const DB_VERSION = 1;
  let db, currentBook = null, rendition = null, bookInstance = null, currentProgressKey = null;
  let fontSize = 100, toastTimer;
  const READER_PREFS_KEY = "ketabkhane-reader-preferences" + STORAGE_SUFFIX;
  const defaultReaderPrefs = { font: "vazirmatn", lineHeight: "1.9", fontSize: 100, theme: "light", align: "justify", librarySort: "added" };
  const READER_THEMES = { light: ["#fffdf7", "#252923"], sepia: ["#f4ecd8", "#5b4636"], dark: ["#1f2623", "#e6ece7"] };
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
      default: return '"Vazirmatn", Tahoma, Arial, sans-serif';
    }
  }
  function syncSettingsUI() {
    const theme = READER_THEMES[readerPrefs.theme] ? readerPrefs.theme : "light";
    const reader = document.getElementById("reader");
    reader.classList.remove("theme-light", "theme-sepia", "theme-dark");
    reader.classList.add("theme-" + theme);
    document.querySelectorAll("[data-theme]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.theme === theme)));
    document.querySelectorAll("[data-align]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.align === readerPrefs.align)));
    const label = document.getElementById("fontSizeLabel");
    if (label) label.textContent = `${fontSize.toLocaleString("fa-IR")}٪`;
  }
  function applyReaderStyles() {
    syncSettingsUI();
    if (!rendition) return;
    const [background, ink] = READER_THEMES[readerPrefs.theme] || READER_THEMES.light;
    const lineHeight = readerPrefs.lineHeight || "1.9";
    const align = readerPrefs.align === "right" ? "right" : "justify";
    rendition.themes.default({
      body: { "font-family": selectedFontStack(), "line-height": lineHeight, "color": ink, "background": background, "direction": "rtl" },
      p: { "text-align": align, "margin": "0 0 1em", "line-height": lineHeight },
      "h1,h2,h3,h4": { "line-height": "1.7", "margin": "0 0 1em" },
      a: { "color": "#397a67" }
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
      req.onupgradeneeded = () => {
        const database = req.result;
        if (!database.objectStoreNames.contains("books")) database.createObjectStore("books", { keyPath: "id" });
        if (!database.objectStoreNames.contains("progress")) database.createObjectStore("progress", { keyPath: "bookId" });
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
  function put(store, value) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, "readwrite");
      tx.objectStore(store).put(value);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
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
  function bookCardHtml(b, prog) {
    const [bg, fg] = paletteFor(b.title || b.id);
    const cover = b.cover ? `<img alt="" src="${b.cover}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">` : `<div style="position:relative"><div class="cover-symbol">✳</div><div class="cover-name">${esc(b.title)}</div></div>`;
    const pct = prog && typeof prog.percentage === "number" ? Math.max(0, Math.min(100, prog.percentage)) : null;
    const bar = pct ? `<div class="cover-progress" aria-hidden="true"><span style="width:${pct}%"></span></div>` : "";
    const catalogLabel = b.catalogStatus === "approved" ? " · عمومی" : b.catalogStatus === "pending" ? " · در انتظار تأیید" : "";
    const badge = (pct ? `${pct.toLocaleString("fa-IR")}٪ خوانده‌شده` : (prog ? "شروع‌شده" : "خوانده‌نشده")) + catalogLabel;
    return `<article class="book-card" tabindex="0" role="button" data-open="${esc(b.id)}" aria-label="باز کردن ${esc(b.title)}">
          <div class="book-cover-thumb" style="background:${bg};color:${fg}">${cover}${bar}</div>
          <div class="book-meta"><h3 title="${esc(b.title)}">${esc(b.title)}</h3><p>${esc(b.author || "نویسنده نامشخص")}</p><span class="book-badge">${badge}</span></div>
          <button class="remove-book" type="button" data-remove="${esc(b.id)}" aria-label="حذف ${esc(b.title)}" title="حذف">×</button>
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
      const filtered = books.filter(b => `${b.title} ${b.author}`.toLocaleLowerCase().includes(query));
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
      if (!h && b.file) {
        h = await sha256(await b.file.arrayBuffer());
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
    const epubBytes = await record.file.arrayBuffer();
    if (token !== openToken) return;
    bookInstance = ePub(epubBytes);
    bookInstance.on("openFailed", err => {
      console.error("EPUB openFailed:", err);
      if (token === openToken) showToast("این فایل EPUB قابل خواندن نیست یا خراب است.");
    });
    rendition = bookInstance.renderTo("viewer", {
      width: "100%", height: "100%", flow: "paginated",
      spread: "auto", manager: "default"
    });
    // The book is rendered in an iframe; load the bundled font there too.
    rendition.hooks.content.register(contents => contents.addStylesheet(READER_FONTS_URL));
    applyReaderStyles();
    // Input inside the book iframe does not reach the main document; epub.js re-emits it on the rendition.
    rendition.on("keydown", handleReaderKey);
    rendition.on("touchstart", onTouchStart);
    rendition.on("touchend", onTouchEnd);
    rendition.on("click", onReaderClick);
    rendition.on("relocated", async location => {
      if (token !== openToken || !location?.start) return;
      const pct = percentFor(location.start.cfi, location.start.percentage);
      showProgress(pct);
      $("chapterLabel").textContent = location.start.index != null ? `بخش ${Number(location.start.index + 1).toLocaleString("fa-IR")}` : "مطالعه";
      $("prevPage").disabled = !!location.atStart;
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
    $("bookmarkButton").textContent = saved?.bookmark ? "★ نشانک ذخیره‌شده" : "☆ نشانک";
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
    $("tocToggle").setAttribute("aria-expanded", "false");
    $("reader").classList.add("hidden");
    document.body.style.overflow = "";
    if (bookInstance) { try { bookInstance.destroy(); } catch (_) {} }
    bookInstance = null; rendition = null; currentBook = null;
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
        if (settingsOpen()) closeSettings(true);
        else if ($("reader").classList.contains("toc-open")) closeToc(true);
        else if ($("reader").classList.contains("immersive")) setImmersive(false);
        else closeReader();
        break;
      default: handled = false;
    }
    if (handled) e.preventDefault();
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
    if (settingsOpen()) { closeSettings(false); return; }
    if (e.target && e.target.closest && e.target.closest("a, button, input, select, textarea, audio, video, label")) return;
    try {
      const win = (contents && contents.window) || e.view;
      if (win && String(win.getSelection()).trim()) return;
    } catch (_) {}
    const frame = e.view && e.view.frameElement;
    const box = $("viewer").getBoundingClientRect();
    if (!frame || !box.width) return;
    const ratio = (frame.getBoundingClientRect().left + e.clientX - box.left) / box.width;
    if (ratio < EDGE_ZONE) goLeft();
    else if (ratio > 1 - EDGE_ZONE) goRight();
    else setImmersive(!$("reader").classList.contains("immersive"));
  }
  $("tocToggle").addEventListener("click", () => { if ($("reader").classList.contains("toc-open")) closeToc(true); else openToc(); });
  $("tocBackdrop").addEventListener("click", () => closeToc(true));
  document.addEventListener("keydown", handleReaderKey);

  function settingsOpen() { return !$("settingsPanel").hidden; }
  function openSettings() {
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
  $("bookmarkButton").addEventListener("click", async () => {
    if (!currentBook || !rendition) return;
    const loc = rendition.currentLocation();
    if (!loc?.start?.cfi) return;
    await saveProgress(currentBook.id, loc.start.cfi, percentFor(loc.start.cfi, loc.start.percentage), loc.start.cfi);
    $("bookmarkButton").textContent = "★ نشانک ذخیره شد";
    showToast("نشانک در همین دستگاه ذخیره شد.");
  });
  $("searchInput").addEventListener("input", renderLibrary);
  inputEls.forEach(el => el?.addEventListener("change", e => importFile(e.target.files?.[0])));
  const onLibraryClick = async e => {
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
  function network() {
    const online = navigator.onLine;
    $("networkStatus").textContent = online ? "● آنلاین" : "● آفلاین";
    $("networkStatus").style.color = online ? "#4b9874" : "#c67b5c";
  }
  window.addEventListener("online", network); window.addEventListener("offline", network);
  if (localStorage.getItem(THEME_KEY) === "dark") document.body.classList.add("dark");
  openDB().then(database => { db = database; renderLibrary(); initAccount(); }).catch(err => { console.error(err); showToast("مرورگر اجازه ذخیره محلی نمی‌دهد."); });
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
    if (b.contentHash || !b.file) return b;
    const h = await sha256(await b.file.arrayBuffer());
    if (h) { b = { ...b, contentHash: h }; await put("books", b); }
    return b;
  }
  async function uploadBook(b) {
    const data = await b.file.arrayBuffer();
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
          if (b.serverId !== sb.id || b.catalogStatus !== (sb.catalog_status || null)) await put("books", { ...b, serverId: sb.id, catalogStatus: sb.catalog_status || null });
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
  async function loadCatalog() {
    $("catalogGrid").innerHTML = '<p class="muted">در حال بارگذاری…</p>';
    try {
      const books = (await api("catalog")).books;
      $("catalogEmpty").hidden = books.length > 0;
      $("catalogGrid").innerHTML = books.map(b => {
        const [bg, fg] = paletteFor(b.title || String(b.id));
        const cover = b.has_cover ? `<img alt="" loading="lazy" src="${API}?r=book-cover&id=${b.id}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">`
          : `<div style="position:relative"><div class="cover-symbol">✳</div><div class="cover-name">${esc(b.title)}</div></div>`;
        const action = b.in_library ? '<span class="book-badge">در کتابخانهٔ تو</span>'
          : `<button class="secondary-button" type="button" data-catalog-add="${b.id}">＋ افزودن به کتابخانهٔ من</button>`;
        return `<article class="book-card catalog-card"><div class="book-cover-thumb" style="background:${bg};color:${fg}">${cover}</div>
          <div class="book-meta"><h3 title="${esc(b.title)}">${esc(b.title)}</h3><p>${esc(b.author || "نویسنده نامشخص")}</p>
          ${b.description ? `<p title="${esc(b.description)}">${esc(b.description)}</p>` : ""}<div class="catalog-actions">${action}</div></div></article>`;
      }).join("");
    } catch (err) { $("catalogGrid").innerHTML = `<p class="muted">${esc(apiMessage(err))}</p>`; }
  }
  $("catalogGrid").addEventListener("click", async e => {
    const btn = e.target.closest("[data-catalog-add]");
    if (!btn) return;
    btn.disabled = true;
    try {
      await api("catalog-add", { method: "POST", body: { book_id: Number(btn.dataset.catalogAdd) } });
      showToast("به کتابخانه‌ات اضافه شد؛ در حال دریافت…");
      await sync(); loadCatalog();
    } catch (err) { btn.disabled = false; showToast(apiMessage(err)); }
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
  async function loadAdmin() {
    try {
      const [pending, invites, users] = await Promise.all([api("catalog-pending"), api("invites"), api("users")]);
      $("pendingList").innerHTML = pending.books.length ? pending.books.map(b => `<div class="admin-item">
          <span><strong>${esc(b.title)}</strong> — ${esc(b.author || "")} <span class="muted">· ${esc(b.submitter || "")} · ${fmtDate(b.submitted_at)}</span>
          ${b.description ? `<br><span class="muted">${esc(b.description)}</span>` : ""}</span>
          <span class="admin-row"><a class="secondary-button" href="${API}?r=book-file&id=${b.id}">دانلود</a>
          <button class="secondary-button" type="button" data-review="${b.id}" data-approve="1">تأیید</button>
          <button class="secondary-button" type="button" data-review="${b.id}" data-approve="0">رد</button></span></div>`).join("")
        : '<p class="muted">موردی در انتظار نیست.</p>';
      $("inviteList").innerHTML = invites.invites.map(i => `<div class="admin-item"><span>${esc(i.note || "بدون یادداشت")}</span>
          <span class="muted">${i.used_by ? `استفاده‌شده توسط ${esc(i.used_by)}` : i.expires_at * 1000 < Date.now() ? "منقضی" : `معتبر تا ${fmtDate(i.expires_at)}`}</span></div>`).join("")
        || '<p class="muted">هنوز دعوت‌نامه‌ای ساخته نشده.</p>';
      $("userList").innerHTML = users.users.map(u => `<div class="admin-item"><span>${esc(u.name)} <span class="muted" dir="ltr">${esc(u.email)}</span></span>
          <span class="muted">${u.role === "admin" ? "مدیر" : "کاربر"} · ${u.books.toLocaleString("fa-IR")} کتاب</span></div>`).join("");
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
  $("createInvite").addEventListener("click", async () => {
    try {
      const r = await api("invites", { method: "POST", body: { note: $("inviteNote").value.trim(), days: 14 } });
      $("inviteResult").textContent = `کد دعوت: ${r.code}`;
      $("inviteNote").value = "";
      loadAdmin();
    } catch (err) { showToast(apiMessage(err)); }
  });

  // ---------- Start ----------
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
