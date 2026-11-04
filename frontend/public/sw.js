// SPDX-License-Identifier: AGPL-3.0-or-later
/*
 * PerFin service worker. It exists so the app can be installed and its
 * static files load fast; it is NOT an offline data layer.
 *
 * Rules (SECURITY.md: nothing a person's finances touch is stored by the
 * browser outside the normal, no-store HTTP semantics):
 *   - It handles only same-origin GET requests for fingerprinted bundles
 *     under /static/ and the icon files. Those are public, content-hashed
 *     and identical for everyone, so caching them leaks nothing.
 *   - Everything else (/api/*, the HTML shell, documents, any other path,
 *     every non-GET) is not intercepted at all: the browser handles it
 *     exactly as without a service worker, so no response containing user
 *     data is ever read into, or written to, Cache Storage.
 *   - Only successful (200), basic (same-origin) responses are stored.
 *   - Old caches are deleted when a new version activates.
 * No inline script and no importScripts: it runs under the app's CSP.
 */
const CACHE = "perfin-static-v1";
const ICONS = new Set(["/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/apple-touch-icon.png", "/favicon-32.png"]);

/** True only for the public static files this worker may cache. */
function isCacheable(request, scopeOrigin) {
  if (request.method !== "GET") {
    return false;
  }
  const url = new URL(request.url);
  if (url.origin !== scopeOrigin || url.search !== "") {
    return false;
  }
  return url.pathname.startsWith("/static/") || ICONS.has(url.pathname);
}

self.addEventListener("install", () => {
  void self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (!isCacheable(event.request, self.location.origin)) {
    return;
  }
  event.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(event.request).then(
        (hit) =>
          hit ??
          fetch(event.request).then((response) => {
            if (response.status === 200 && response.type === "basic") {
              void cache.put(event.request, response.clone());
            }
            return response;
          }),
      ),
    ),
  );
});
