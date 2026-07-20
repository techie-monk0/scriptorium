/* Service worker — offline launch + reading for the device-local PWA.
 *
 *  • Shell (incl. shelf.css/js) + reader libs: precache, served stale-while-revalidate →
 *    instant offline launch AND an edited asset appears on the next load (no bump needed).
 *  • /api/v1/replica: network-first, fall back to cache.
 *  • /api/v1/health + /api/v1/capture: network-only (probe / append — never cached).
 *  • /edition/<id>/{cover.jpg,spine.svg}: network-FIRST (revalidates via the cover's ETag —
 *    a 304 when unchanged), fall back to cache offline → a re-pinned/backfilled/regenerated
 *    cover shows on the very next load, yet art still launches offline once fetched.
 *  • Book files (/holding/<id>/file): NOT handled here — the app caches those explicitly
 *    via the FileStore Cache API on first open (on-demand, P8), so the user controls them.
 *
 * Bump CACHE to force an immediate shell purge; otherwise stale-while-revalidate keeps the
 * shell fresh within one extra load. Covers are network-first, so they need no bump to
 * refresh — bump COVERS only to drop offline-cached art that older clients still hold.
 */
const CACHE = 'lib-device-v16';   // bump → purge + re-precache (message-driven refresh for app-version)
const COVERS = 'lib-covers-v3';   // bump → purge stale art cached by older clients (now network-first)
const SHELL = ['/app', '/static/pwa/app.js', '/static/pwa/reader.js', '/static/pwa/pwa.css',
               '/static/css/tokens.css', '/static/css/shelf.css', '/static/js/shelf.js',
               // shared frontend layer (Tiers 2–3) — the same files the web loads
               '/static/js/typeahead.js', '/static/js/library-core.js', '/static/js/library-ui-dom.js',
               '/static/pwa/content-index.js',
               '/manifest.webmanifest', '/static/pwa/icon.svg'];
// Reader libs + the offline content-search SQLite engine (FTS5) — best-effort so a path
// miss never fails install. The engine loads only when offline content search is enabled.
const VENDOR = ['/static/vendor/pdf.min.js', '/static/vendor/pdf.worker.min.js',
                '/static/vendor/sqlite3.mjs', '/static/vendor/sqlite3.wasm',
                '/static/vendor/epub.min.js', '/static/vendor/jszip.min.js',
                // the SHARED reader engine the PWA reader now drives (N5 repoint)
                '/static/reader/reader-core.js', '/static/reader/overlay.js',
                '/static/reader/vendor.js', '/static/reader/reader.css',
                '/static/reader/reader-themes.css', '/static/reader/reader-sessions.js'];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await c.addAll(SHELL);                          // must-have shell (atomic)
    await Promise.all(VENDOR.map(u => c.add(u).catch(() => {})));   // best-effort
    self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE && k !== COVERS && k !== 'lib-files-v1').map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

// Serve the cached copy instantly while fetching a fresh one in the background to update
// the cache — so an edited asset / regenerated spine appears on the NEXT load. Falls back
// to whatever's available when offline.
async function staleWhileRevalidate(cacheName, req) {
  const c = await caches.open(cacheName);
  const hit = await c.match(req);
  const net = fetch(req).then(r => { if (r.ok) c.put(req, r.clone()); return r; }).catch(() => null);
  return hit || (await net) || caches.match(req);
}

// Go to the network first (cheap when the server revalidates: a 304 carries no body),
// refresh the cache, and fall back to the cached copy only when offline. For covers/spines
// this means a changed cover is reflected immediately — never a stale-cache load behind.
async function networkFirst(cacheName, req) {
  const c = await caches.open(cacheName);
  try {
    const r = await fetch(req);
    if (r.ok) c.put(req, r.clone());
    return r;
  } catch (e) {
    return (await c.match(req)) || Response.error();
  }
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                 // never intercept POST (capture)
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const p = url.pathname;

  // Page navigations: network-FIRST so an auth check reaches the browser — a cached shell would
  // mask it and the PWA's fetch()es would then fail forever ("offline"). Cookie auth: an expired
  // session 302-redirects to /login; hand the browser a CLEAN redirect (a "redirected" response
  // can't be returned to a navigation) and NEVER cache /login as the app shell. Falls back to the
  // cached shell only when the server is truly unreachable (so a valid cookie still launches
  // offline). A 401 is passed straight through (covers the Basic-auth provider too).
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const r = await fetch(req);
        if (r.redirected) return Response.redirect(r.url, 302); // → the login form
        if (r.status === 401) return r;
        if (!r.ok) return (await caches.match('/app')) || r;    // server/tunnel down → offline shell
        if (new URL(r.url).pathname === '/app') (await caches.open(CACHE)).put('/app', r.clone());
        return r;
      } catch (e) { return (await caches.match('/app')) || Response.error(); }
    })());
    return;
  }

  if (p === '/api/v1/health') return;               // always network

  if (p === '/api/v1/replica') {                    // network-first, cache fallback
    e.respondWith(fetch(req).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put('/api/v1/replica', copy)); }
      return r;
    }).catch(() => caches.match('/api/v1/replica')));
    return;
  }

  if (/^\/edition\/\d+\/(cover\.jpg|spine\.svg)$/.test(p)) {       // art: network-first (revalidate)
    e.respondWith(networkFirst(COVERS, req));
    return;
  }

  if (p === '/app' || p.startsWith('/static/pwa/') || p.startsWith('/static/vendor/')
      || p.startsWith('/static/reader/') || p.startsWith('/static/css/') || p.startsWith('/static/js/')) {
    e.respondWith(staleWhileRevalidate(CACHE, req));              // shell: stale-while-revalidate
    return;
  }

  e.respondWith(fetch(req).catch(() => caches.match(req)));        // default: network-first
});

// App-version handshake support (see static/js/app-version.js). Stale-while-revalidate means a plain
// reload still serves the PREVIOUSLY-cached (stale) shell/assets — the fresh copy only lands one load
// later. So when the client detects a new server build it asks us to RE-FETCH the shell + reader libs
// from the network NOW (bypassing the HTTP cache) into the active cache, then tells the page to reload
// into genuinely fresh assets. `SKIP_WAITING` lets a newly-installed sw.js take over immediately.
self.addEventListener('message', e => {
  const type = e.data && e.data.type;
  if (type === 'SKIP_WAITING') { self.skipWaiting(); return; }
  if (type === 'REFRESH_ASSETS') {
    e.waitUntil((async () => {
      const c = await caches.open(CACHE);
      await Promise.all([...SHELL, ...VENDOR].map(async u => {
        try { const r = await fetch(u, { cache: 'reload' }); if (r.ok) await c.put(u, r.clone()); }
        catch (err) {}                                 // best-effort: a miss just leaves the old copy
      }));
      const clients = await self.clients.matchAll({ includeUncontrolled: true });
      clients.forEach(cl => cl.postMessage({ type: 'ASSETS_REFRESHED' }));
    })());
  }
});
