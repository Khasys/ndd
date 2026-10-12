// Service worker de domain-watch. Les données (/api) ne sont jamais mises en cache :
// l'état d'un domaine affiché doit venir du NAS, pas d'une copie périmée.
const CACHE = "domain-watch-{{VERSION}}";
const SHELL = [
  "/offline",
  "/static/style.css?v={{VERSION}}",
  "/static/app.js?v={{VERSION}}",
  "/static/icons/icon-192.png?v={{VERSION}}",
  "https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js",
];

self.addEventListener("install", (event) => {
  // Un fichier injoignable (CDN bloqué) ne doit pas empêcher l'installation.
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.all(SHELL.map((url) => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function cacheable(url) {
  if (url.origin === self.location.origin) return url.pathname.startsWith("/static/");
  return url.hostname === "cdn.jsdelivr.net" || url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com";
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/offline")));
    return;
  }
  if (!cacheable(url)) return;

  // Fichiers versionnés ou externes figés : le cache suffit, le réseau complète.
  event.respondWith(
    caches.match(request).then((hit) => hit || fetch(request).then((response) => {
      if (response.ok || response.type === "opaque") {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    })),
  );
});
