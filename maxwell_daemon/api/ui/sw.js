/* Maxwell-Daemon Service Worker — offline-first caching strategy.
 *
 * Strategy:
 *   - Shell assets (HTML, CSS, JS, manifest) → cache-first, update in background
 *   - API calls → network-first, fall back to stale if offline
 *   - WebSocket events → always network (can't cache WS)
 *
 * Cache version: bump CACHE_VERSION on breaking UI changes to force refresh.
 */

const CACHE_VERSION = 'v1';
const SHELL_CACHE = `maxwell-shell-${CACHE_VERSION}`;
const API_CACHE   = `maxwell-api-${CACHE_VERSION}`;

const SHELL_ASSETS = [
  '/ui/',
  '/ui/index.html',
  '/ui/style.css',
  '/ui/app.js',
  '/ui/manifest.json',
];

// ── install ─────────────────────────────────────────────────────────────────

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

// ── activate — delete old caches ────────────────────────────────────────────

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== API_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── fetch ────────────────────────────────────────────────────────────────────

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Never intercept WebSocket upgrades or cross-origin requests.
  if (request.headers.get('upgrade') === 'websocket') return;
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/api/')) {
    // API: network-first, stale fallback for GET requests only.
    if (request.method !== 'GET') return;
    event.respondWith(networkFirstApi(request));
  } else {
    // UI shell: cache-first, background update.
    event.respondWith(cacheFirstShell(request));
  }
});

async function networkFirstApi(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(API_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(
      JSON.stringify({ error: 'offline', cached: false }),
      { status: 503, headers: { 'content-type': 'application/json' } }
    );
  }
}

async function cacheFirstShell(request) {
  const cached = await caches.match(request);
  const fetchPromise = fetch(request).then((response) => {
    if (response.ok) {
      caches.open(SHELL_CACHE).then((cache) => cache.put(request, response.clone()));
    }
    return response;
  });
  return cached || fetchPromise;
}
