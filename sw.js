/* AI Ready Mobile service worker — network-first with cache fallback.
 * Bump CACHE on every deploy (kept in lockstep with static/js/version.js). */
const CACHE = "ai-ready-mobile-v1.1.0";

const SHELL = [
  "index.html",
  "about.html",
  "manifest.webmanifest",
  "static/js/version.js",
  "static/js/feedback.js",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "icons/apple-touch-icon.png",
  "py/mobile.py",
  "py/assembly.py",
  "py/masking/__init__.py",
  "py/masking/masker.py",
  "py/converters/__init__.py",
  "py/converters/pdf_converter.py",
  "py/converters/content_detector.py",
  "py/converters/medical_index.py",
  "py/converters/bates.py",
  "py/converters/legal_index.py",
  "py/converters/integrity.py",
  "py/converters/grid_forms.py",
  "py/converters/docx_converter.py",
  "py/converters/pptx_converter.py",
  "py/converters/text_converter.py",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Network-first so updates show when online; cache fallback offline. The big
// Pyodide/CDN assets are cached too (opaque responses are fine to replay).
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return resp;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
