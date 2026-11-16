/* MY MONEY service worker.
   Privacy first: pages (HTML), group data, uploaded files and every POST always go straight to the network and are NEVER stored.
   Only the app's own CSS/JS/images and the icon/font libraries are cached, so the app opens fast and shows a friendly page offline.
   Bump VERSION whenever this file changes. */
const VERSION = "mm-v1";
const STATIC_CACHE = "mm-static-" + VERSION;
const OFFLINE_URL = "/static/offline.html";
const PRECACHE = [
  OFFLINE_URL,
  "/static/img/icon-192.png",
  "/static/img/logo.png",
  "/static/img/favicon.png",
  "/static/css/app.css",
  "/static/css/responsive.css",
  "/static/css/icons.css",
  "/static/css/pwa.css",
  "/static/js/app.js",
  "/static/js/responsive.js",
  "/static/js/pwa.js"
];
const CDN_HOSTS = ["unpkg.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(STATIC_CACHE);
    // add one by one so a single missing file can never block the install
    await Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => null)));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith("mm-") && k !== STATIC_CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

async function networkFirst(request) {
  const cache = await caches.open(STATIC_CACHE);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(STATIC_CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const fresh = await fetch(request);
  if (fresh && (fresh.ok || fresh.type === "opaque")) cache.put(request, fresh.clone());
  return fresh;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                       // never touch forms or API writes
  const url = new URL(req.url);

  // Page navigations: always live; if the phone is offline show the offline page.
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(async () => {
      const cache = await caches.open(STATIC_CACHE);
      return (await cache.match(OFFLINE_URL)) || Response.error();
    }));
    return;
  }

  if (url.origin === self.location.origin) {
    if (url.pathname.startsWith("/static/")) {
      const isCode = /\.(css|js)$/.test(url.pathname);
      event.respondWith(isCode ? networkFirst(req) : cacheFirst(req)); // fresh code after a deploy, instant images
    }
    return;                                                // everything else (data, files, pages) is not cached
  }

  if (CDN_HOSTS.includes(url.hostname)) event.respondWith(cacheFirst(req)); // icon library, charts, fonts
});
