const CACHE = "fcx-control-v6-all-open-positions";
const CORE = ["/", "/control.css", "/control.js?v=all-open-positions-v6", "/manifest.webmanifest"];
self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(CORE)).then(() => self.skipWaiting())));
self.addEventListener("activate", event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET" || new URL(event.request.url).pathname.startsWith("/api/")) return;
  event.respondWith(fetch(event.request).then(response => {
    const clone = response.clone(); caches.open(CACHE).then(cache => cache.put(event.request, clone)); return response;
  }).catch(() => caches.match(event.request)));
});
