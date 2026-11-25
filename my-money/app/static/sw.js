/* MY MONEY service worker.
   Privacy first: pages (HTML), group data, uploaded files and every POST always go straight to the network and are NEVER stored.
   Only the app's own CSS/JS/images and the icon/font libraries are cached, so the app opens fast and shows a friendly page offline.
   Bump VERSION whenever this file changes. */
const VERSION = "mm-v4";
const STATIC_CACHE = "mm-static-" + VERSION;
const OFFLINE_URL = "/static/offline.html";
const PRECACHE = [
  OFFLINE_URL,
  "/static/img/icon-192.png",
  "/static/img/logo.png",
  "/static/img/favicon.png"
];
// Icons and charts are now served from this site (static/vendor); only the web font still comes from a CDN.
const CDN_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

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
    if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
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
  if (fresh && (fresh.status === 200 || fresh.type === "opaque")) cache.put(request, fresh.clone());
  return fresh;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                       // never touch forms or API writes
  // Video/audio use Range requests (needed by Safari/iPhone). Let the browser fetch them directly, never cache them.
  if (req.headers.has("range") || req.destination === "video" || req.destination === "audio") return;
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
      // Pages ask for /static files with ?v=<file-timestamp>: a changed file has a new URL, so a cached copy is
      // always correct and can be used instantly. Un-versioned code files still check the network first.
      const versioned = url.searchParams.has("v");
      const isCode = /\.(css|js)$/.test(url.pathname);
      event.respondWith(isCode && !versioned ? networkFirst(req) : cacheFirst(req));
    }
    return;                                                // everything else (data, files, pages) is not cached
  }

  if (CDN_HOSTS.includes(url.hostname)) event.respondWith(cacheFirst(req)); // icon library, charts, fonts
});


/* ----- pop-up notifications (Web Push) ----- */
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { title: "MY MONEY", body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(d.title || "MY MONEY", {
    body: d.body || "",
    icon: "/static/img/icon-192.png",
    badge: "/static/img/favicon.png",
    tag: d.tag || undefined,
    renotify: !!d.tag,
    data: { url: d.url || "/notifications/" }
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/notifications/", self.location.origin).href;
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin && "focus" in c) {
        try { await c.focus(); if ("navigate" in c) await c.navigate(target); return; } catch (e) { /* open a new window instead */ }
      }
    }
    await self.clients.openWindow(target);
  })());
});
