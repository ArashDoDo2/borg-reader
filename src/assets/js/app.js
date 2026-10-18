/* Prototype only: local library. Cloud accounts and cross-device sync are not implemented yet. */
(() => {
  "use strict";
  const DB_NAME = "ketabkhane-db";
  const DB_VERSION = 1;
  let db, currentBook = null, rendition = null, bookInstance = null, currentProgressKey = null;
  let fontSize = 100, isReaderDark = false, toastTimer;
  const READER_PREFS_KEY = "ketabkhane-reader-preferences";
  const defaultReaderPrefs = { font: "vazirmatn", lineHeight: "1.9", fontSize: 100 };
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
  function applyReaderStyles() {
    if (!rendition) return;
    const ink = isReaderDark ? "#edf1ed" : "#252923";
    const background = isReaderDark ? "#202825" : "#fffdf7";
    const lineHeight = readerPrefs.lineHeight || "1.9";
    rendition.themes.default({
      body: { "font-family": selectedFontStack(), "line-height": lineHeight, "color": ink, "background": background, "direction": "rtl" },
      p: { "text-align": "justify", "margin": "0 0 1em", "line-height": lineHeight },
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
  function renderLibrary() {
    const query = $("searchInput").value.trim().toLocaleLowerCase();
    getAllBooks().then(books => {
      books.sort((a,b) => (b.addedAt || 0) - (a.addedAt || 0));
      const filtered = books.filter(b => `${b.title} ${b.author}`.toLocaleLowerCase().includes(query));
      $("bookGrid").innerHTML = filtered.map(b => {
        const [bg, fg] = paletteFor(b.title || b.id);
        const cover = b.cover ? `<img alt="" src="${b.cover}" style="width:100%;height:100%;object-fit:cover;position:absolute;inset:0">` : `<div style="position:relative"><div class="cover-symbol">✳</div><div class="cover-name">${esc(b.title)}</div></div>`;
        return `<article class="book-card" tabindex="0" role="button" data-open="${esc(b.id)}" aria-label="باز کردن ${esc(b.title)}">
          <div class="book-cover-thumb" style="background:${bg};color:${fg}">${cover}</div>
          <div class="book-meta"><h3 title="${esc(b.title)}">${esc(b.title)}</h3><p>${esc(b.author || "نویسنده نامشخص")}</p><span class="book-badge">ذخیره‌شده روی دستگاه</span></div>
          <button class="remove-book" type="button" data-remove="${esc(b.id)}" aria-label="حذف ${esc(b.title)}" title="حذف از دستگاه">×</button>
        </article>`;
      }).join("");
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
      const metadata = await extractMetadata(file);
      const record = {
        id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        title: metadata.title, author: metadata.author, cover: metadata.cover,
        file: file, size: file.size, addedAt: Date.now()
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
      showToast("کتاب به کتابخانه همین دستگاه اضافه شد.");
    } catch (err) {
      console.error(err);
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
    $("tocList").querySelectorAll("[data-href]").forEach(btn => btn.addEventListener("click", () => rendition && rendition.display(btn.dataset.href)));
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
    $("reader").classList.add("hidden");
    document.body.style.overflow = "";
    if (bookInstance) { try { bookInstance.destroy(); } catch (_) {} }
    bookInstance = null; rendition = null; currentBook = null;
  }
  function setReaderTheme() {
    isReaderDark = !isReaderDark;
    applyReaderStyles();
  }
  $("themeToggle").addEventListener("click", () => {
    document.body.classList.toggle("dark");
    localStorage.setItem("ketab-theme", document.body.classList.contains("dark") ? "dark" : "light");
  });
  $("readerTheme").addEventListener("click", setReaderTheme);
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
  $("bookGrid").addEventListener("click", async e => {
    const removeBtn = e.target.closest("[data-remove]");
    if (removeBtn) {
      e.stopPropagation();
      const id = removeBtn.dataset.remove;
      if (confirm("این کتاب از حافظه همین دستگاه حذف شود؟")) {
        await remove("books", id); await remove("progress", id); renderLibrary(); showToast("کتاب از این دستگاه حذف شد.");
      }
      return;
    }
    const card = e.target.closest("[data-open]");
    if (card) openReader(card.dataset.open).catch(err => { console.error(err); showToast("بازکردن کتاب ناموفق بود."); });
  });
  $("bookGrid").addEventListener("keydown", e => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-open]")) { e.preventDefault(); openReader(e.target.dataset.open).catch(err => { console.error(err); showToast("بازکردن کتاب ناموفق بود."); }); }
  });
  function network() {
    const online = navigator.onLine;
    $("networkStatus").textContent = online ? "● آنلاین" : "● آفلاین";
    $("networkStatus").style.color = online ? "#4b9874" : "#c67b5c";
  }
  window.addEventListener("online", network); window.addEventListener("offline", network);
  if (localStorage.getItem("ketab-theme") === "dark") document.body.classList.add("dark");
  openDB().then(database => { db = database; renderLibrary(); }).catch(err => { console.error(err); showToast("مرورگر اجازه ذخیره محلی نمی‌دهد."); });
  network();
  if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
    window.addEventListener("load", () => navigator.serviceWorker.register("./service-worker.js").catch(err => console.warn("Service worker registration failed", err)));
  }
})();
