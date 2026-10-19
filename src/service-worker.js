// Bump this name on every release so old caches are removed.
const CACHE_NAME = "borg-shell-v8.6.4";
// Files shared to the app (Android share sheet) wait here until the page imports them.
const SHARE_CACHE = "borg-share";
const APP_SHELL = [
  "./",
  "./index.html",
  "./offline.html",
  "./manifest.webmanifest",
  "./assets/css/app.css",
  "./assets/css/reader-fonts.css",
  "./assets/js/app.js",
  "./assets/vendor/jszip.min.js",
  "./assets/vendor/epub.min.js",
  "./assets/fonts/Vazirmatn-Variable.woff2",
  "./assets/icon-192.png",
  "./assets/icon-512.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME && key !== SHARE_CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

// Pages: network first, so a new release reaches users; cached copy when offline.
async function handleNavigation(req) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(req);
    if (response && response.ok) cache.put(req, response.clone());
    return response;
  } catch (_) {
    return (await cache.match(req, { ignoreSearch: true }))
      || (await cache.match("./index.html"))
      || (await cache.match("./offline.html"))
      || new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}

// Static files: cache first (they are versioned by CACHE_NAME), network as fallback.
async function handleAsset(req) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(req, { ignoreSearch: true });
  if (cached) return cached;
  try {
    const response = await fetch(req);
    if (response && response.ok) cache.put(req, response.clone());
    return response;
  } catch (_) {
    return new Response("", { status: 504, statusText: "Offline resource unavailable" });
  }
}

async function handleShare(req) {
  try {
    const form = await req.formData();
    const cache = await caches.open(SHARE_CACHE);
    let i = 0;
    for (const f of form.getAll("epub")) {
      if (!f || typeof f !== "object") continue;
      await cache.put(new Request(`shared/${Date.now()}-${i++}`), new Response(f, {
        headers: { "Content-Type": f.type || "application/epub+zip", "X-File-Name": encodeURIComponent(f.name || "book.epub") },
      }));
    }
  } catch (_) {}
  return Response.redirect(new URL("./?shared=1", self.registration.scope).href, 303);
}

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method === "POST" && new URL(req.url).pathname === new URL("share-target", self.registration.scope).pathname) {
    event.respondWith(handleShare(req));
    return;
  }
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Only this app's own files: never other paths on the site, and never the API (always live, per user).
  const scopePath = new URL(self.registration.scope).pathname;
  if (!url.pathname.startsWith(scopePath) || url.pathname.startsWith(scopePath + "api/")) return;
  if (req.mode === "navigate") event.respondWith(handleNavigation(req));
  else event.respondWith(handleAsset(req));
});
