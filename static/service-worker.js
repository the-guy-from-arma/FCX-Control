const CACHE = "fcx-control-v3-security-directory";
const CORE = ["/", "/control.css", "/control.js?v=6b970a2-security-directory", "/manifest.webmanifest"];
self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(CORE)).then(() => self.skipWaiting())));
self.addEventListener("activate", event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET" || new URL(event.request.url).pathname.startsWith("/api/")) return;
  event.respondWith(fetch(event.request).then(response => {
    const clone = response.clone(); caches.open(CACHE).then(cache => cache.put(event.request, clone)); return response;
  }).catch(() => caches.match(event.request)));
});
