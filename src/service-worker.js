// Bump this name on every release so old caches are removed.
const CACHE_NAME = "borg-shell-v7.0.0";
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
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
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

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Only this app's own files: never other paths on the site, and never the API (always live, per user).
  const scopePath = new URL(self.registration.scope).pathname;
  if (!url.pathname.startsWith(scopePath) || url.pathname.startsWith(scopePath + "api/")) return;
  if (req.mode === "navigate") event.respondWith(handleNavigation(req));
  else event.respondWith(handleAsset(req));
});
