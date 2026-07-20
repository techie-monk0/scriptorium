/* Device-local library PWA — client of the /api/v1/* contract.
 *
 * Web-parity shell (P6): Home shelves · Search/Browse · Book detail, rendered from the
 * IndexedDB-cached replica in the web app's theme. Works offline once the replica is cached.
 *
 * CONVERTIBILITY (see device_local_plan.md §12): all platform-specific behavior is behind
 * four thin adapters — Net, FileStore, ReadingStore, Opener. A Capacitor wrap or a native
 * rewrite swaps ONLY these; the rest of the app (and the whole backend) is reused. No
 * business logic lives here — search-folding, covers, reader handles all come from the
 * server in the replica.
 */
'use strict';

// ════════════════════════ IndexedDB (kv · outbox · reading) ════════════════════════
const DB_NAME = 'library-device', DB_VER = 2;
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open(DB_NAME, DB_VER);
    r.onupgradeneeded = () => {
      const db = r.result;
      if (!db.objectStoreNames.contains('kv')) db.createObjectStore('kv');
      if (!db.objectStoreNames.contains('outbox')) db.createObjectStore('outbox', { keyPath: 'uuid' });
      if (!db.objectStoreNames.contains('reading')) db.createObjectStore('reading', { keyPath: 'edition_id' });
    };
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
function tx(store, mode, fn) {
  return idb().then(db => new Promise((res, rej) => {
    const t = db.transaction(store, mode), rq = fn(t.objectStore(store));
    t.oncomplete = () => res(rq ? rq.result : undefined);
    t.onerror = () => rej(t.error);
  }));
}
const kvGet = k => tx('kv', 'readonly', s => s.get(k));
const kvPut = (k, v) => tx('kv', 'readwrite', s => s.put(v, k));

// ════════════════════════ Adapters (the ONLY native-swap seams) ════════════════════
// Net — network. (WKWebView wrap: unchanged; native rewrite: URLSession.)
// On a 401 (session expired / never logged in) the server wants a login — send the user to the
// sign-in form instead of letting the app conclude it's "offline". This is robust even when a
// stale service worker is still serving a cached shell: the first gated request bounces to /login.
let _redirectingToLogin = false;
const Net = {
  async fetch(u, o) {
    const r = await fetch(u, o);
    if (r.status === 401 && !_redirectingToLogin && !location.pathname.startsWith('/login')) {
      _redirectingToLogin = true;
      location.href = '/login?next=' + encodeURIComponent('/app' + (location.hash || ''));
    }
    return r;
  },
};

// FileStore — cache/read book-file bytes for offline reading. Web = Cache API.
// (Wrap: swap to the native filesystem plugin → escapes the iOS storage cap. §12 rule 2.)
const FileStore = {
  CACHE: 'lib-files-v1',
  // Cache API needs a secure context (HTTPS/localhost). Over plain LAN-HTTP it's absent —
  // the reader then renders from an in-memory copy (online only); no offline caching.
  _ok: typeof caches !== 'undefined',
  _u: key => '/__file/' + encodeURIComponent(key),
  async has(key) { if (!this._ok) return false; const c = await caches.open(this.CACHE); return !!(await c.match(this._u(key))); },
  async get(key) { if (!this._ok) return null; const c = await caches.open(this.CACHE); const r = await c.match(this._u(key)); return r ? r.blob() : null; },
  async put(key, response) { if (!this._ok) return; const c = await caches.open(this.CACHE); await c.put(this._u(key), response); },
  async remove(key) { if (!this._ok) return; const c = await caches.open(this.CACHE); return c.delete(this._u(key)); },
  // Set of holding keys (as strings) currently cached for offline reading — one cache scan,
  // so the shelf can flag downloaded books without an await per tile.
  async cachedKeys() {
    if (!this._ok) return new Set();
    const c = await caches.open(this.CACHE), out = new Set();
    for (const req of await c.keys()) {
      const m = new URL(req.url).pathname.match(/^\/__file\/(.+)$/);
      if (m) out.add(decodeURIComponent(m[1]));
    }
    return out;
  },
};

// ReadingStore — per-device reading state (position/bookmarks/recently-opened). Web =
// IndexedDB. Schema + merge rules match §11 so a later kDrive per-device sync drops in.
const ReadingStore = {
  async state(eid) {
    return (await tx('reading', 'readonly', s => s.get(eid)))
      || { edition_id: eid, location: null, bookmarks: [], opened_at: null, updated_at: null };
  },
  async _save(st) { st.updated_at = new Date().toISOString(); await tx('reading', 'readwrite', s => s.put(st)); },
  async recordOpen(eid) { const st = await this.state(eid); st.opened_at = new Date().toISOString(); await this._save(st); },
  async setLocation(eid, loc) { const st = await this.state(eid); st.location = loc; await this._save(st); },   // P9
  async recent(n) {
    const all = await tx('reading', 'readonly', s => s.getAll());
    return (all || []).filter(x => x.opened_at)
      .sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1)).slice(0, n).map(x => x.edition_id);
  },
};

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement('script'); s.src = src;
    s.onload = res; s.onerror = () => rej(new Error('load ' + src));
    document.head.appendChild(s);
  });
}

// Opener — how a holding opens. P8: in-app reader (pdf.js/epub.js) from FileStore-cached
// bytes; falls back to external open if the reader can't load.
const Opener = {
  async open(edition, holding) {
    await ReadingStore.recordOpen(edition.edition_id);
    try {
      if (!window.Reader) await loadScript('/static/pwa/reader.js');
      await window.Reader.open(edition, holding, { FileStore, ReadingStore, Net, canDownload: CAN_DOWNLOAD });
    } catch (err) {
      const url = (holding.storage && holding.storage.open_url)
        || (holding.holding_id != null ? `/holding/${holding.holding_id}/file` : null);
      if (url) window.open(url, '_blank', 'noopener');
    }
  },
};

// ════════════════════════ data + state ════════════════════════
let EDITIONS = [], BY_ID = new Map(), EXPORTED_AT = null, ONLINE = false, REPLICA = { editions: [] };
// Capability advertised by /api/v1/health (refreshed on every probe). A read-only
// "viewer" guest gets can_download:false → we render books inline but never persist
// their bytes to the offline FileStore. Defaults true (editor / open access).
let CAN_DOWNLOAD = true;
const $ = id => document.getElementById(id);

function setReplica(doc) {
  REPLICA = doc || { editions: [] };
  EDITIONS = REPLICA.editions || []; EXPORTED_AT = REPLICA.exported_at;
  BY_ID = new Map(EDITIONS.map(e => [e.edition_id, e]));
}

async function loadCached() {
  const doc = await kvGet('replica');
  if (doc) setReplica(doc);
  const starred = await kvGet('starred');
  if (Array.isArray(starred)) { STARRED_IDS = starred; if (window.Shelf) Shelf.setStarred(STARRED_IDS); }
}

// The starred set (sibling payload, like the replica) — feeds the Starred rail + cover highlights.
let STARRED_IDS = [];
async function refreshStarred() {
  // Route the list GET through the shared mapper too — no surface hardcodes the endpoint (tiers rule).
  let r; try { r = await Net.fetch(LibraryCore.starredRequest('list').path, { headers: { 'Accept': 'application/json' } }); }
  catch { return false; }
  if (!r.ok) return false;
  STARRED_IDS = (await r.json()).editions || [];
  await kvPut('starred', STARRED_IDS);
  if (window.Shelf) Shelf.setStarred(STARRED_IDS);     // seed every tile's highlight, any page
  return true;
}
async function refresh() {
  const etag = (await kvGet('etag')) || '';
  let r; try { r = await Net.fetch('/api/v1/replica', { headers: etag ? { 'If-None-Match': etag } : {} }); }
  catch { return false; }
  if (r.status === 304) return false;   // unchanged — nothing to re-render
  if (!r.ok) return false;
  const doc = await r.json();
  await kvPut('replica', doc); await kvPut('etag', r.headers.get('ETag') || '');
  setReplica(doc);
  return true;                          // changed — new data landed
}

// Eager art prefetch (P7): warm the SW cache with the ACTIVE art mode's images (cover OR
// spine per <html data-shelf>) so the shelves look complete offline — never the other mode
// (don't download spines in cover mode, or vice versa). Background, throttled; failures ignored.
async function prefetchCovers() {
  const spine = document.documentElement.getAttribute('data-shelf') === 'spine';
  const urls = EDITIONS.map(e => spine ? e.spine_url : e.cover_url).filter(Boolean);
  let i = 0;
  const worker = async () => { while (i < urls.length) { const u = urls[i++]; try { await Net.fetch(u); } catch {} } };
  await Promise.all([0, 0, 0, 0].map(worker));
}

// ════════════════════════ reachability ════════════════════════
async function probe() {
  try {
    const r = await Net.fetch('/api/v1/health', { cache: 'no-store', signal: AbortSignal.timeout(3000) });
    if (r.ok) { try {
      const h = await r.json();
      CAN_DOWNLOAD = h.can_download !== false;
      // Version handshake rides on the probe every reachable pass already makes — banner a reload
      // when the server build changed under us or the server is running stale code.
      if (window.AppVersion) AppVersion.apply(h);
    } catch {} }
    return r.ok;
  }
  catch { return false; }
}
let SYNCING = false;
// The freshness chip is now the SHARED spec: `LibraryCore.syncVM` decides the wording (Live / Syncing… /
// Offline · <date>) so the PWA, web, and iOS chips read identically. The dot colour keeps the existing
// online/offline CSS.
function setStatus(online) {
  ONLINE = online;
  const vm = LibraryCore.syncVM({ online: online, syncing: SYNCING, exportedAt: EXPORTED_AT });
  const el = $('status'); if (!el) return;
  el.className = online ? 'online' : 'offline';
  $('statusText').textContent = vm.label;
}

/// One refresh pass, surfaced through the shared chip: flips `Syncing…` on, revalidates the replica +
/// starred + flushes the outbox, and re-renders only if new data actually landed. Used by the manual
/// pull-to-refresh and the visibility/focus triggers (the boot + `online` paths keep their own sequence).
async function syncNow() {
  if (SYNCING) return;
  SYNCING = true; setStatus(ONLINE);
  const online = await probe();
  if (!online) { SYNCING = false; setStatus(false); return; }
  const changed = await refresh();
  await refreshStarred(); await flushOutbox();
  SYNCING = false; setStatus(true);
  if (changed) route();
}

// ════════════════════════ offline-available badge ════════════════════════
// Flag tiles whose book bytes are already in FileStore, so you can tell at a glance which
// books open offline before you tap. PWA-only decoration of the shared tile markup (the web
// has no FileStore) — runs after any render via a #view observer, so home + search both get it.
async function markOfflineTiles(root) {
  if (!FileStore._ok) return;
  const cached = await FileStore.cachedKeys();
  root.querySelectorAll('a.tile').forEach(a => {
    const m = (a.getAttribute('href') || '').match(/#\/book\/(\d+)/);
    const e = m && BY_ID.get(Number(m[1]));
    const has = !!e && (e.holdings || []).some(h => cached.has(String(h.holding_id)));
    a.classList.toggle('cached', has);
  });
}
function watchOfflineTiles() {
  const v = $('view'); if (!v) return;
  let t = null;
  const kick = () => { clearTimeout(t); t = setTimeout(() => markOfflineTiles(v), 120); };
  new MutationObserver(kick).observe(v, { childList: true, subtree: true });
  kick();
}

// ════════════════════════ rendering ════════════════════════

async function renderHome() {
  const v = $('view'); v.innerHTML = '';
  if (!EDITIONS.length) { v.innerHTML = '<div class="empty">No offline copy yet — open this once while the Mac is reachable.</div>'; return; }

  const href = eid => `#/book/${eid}`;   // PWA tile → in-app detail (web uses /edition/<id>/read)
  // Rails composed by the SHARED Tier-2 presenter (recent/added/subject-rollup/series, all ordering
  // inside homeVM) over the cached replica + local reading history — identical to web + native.
  const recentIds = (await ReadingStore.recent(24)).map(id => +id).filter(id => BY_ID.has(id));
  Shelf.setStarred(STARRED_IDS);                       // seed the shared cover-highlight set
  const vm = LibraryCore.homeVM(REPLICA, recentIds, STARRED_IDS, {});
  vm.rails.forEach(rail => {
    if (rail.kind === 'series') {
      v.appendChild(Shelf.renderSeriesRail({
        series: rail.sets.map(s => ({ name: s.name, count: s.count, books: s.cards })), hrefFor: href }));
    } else {
      v.appendChild(Shelf.render({
        title: rail.title, count: rail.count, books: rail.cards, hrefFor: href, kind: rail.kind,
        moreUrl: rail.kind === 'subject' ? '#/subject/' + encodeURIComponent(rail.title) : undefined }));
    }
  });

  Shelf.enhance(v);    // shared magnification + expand + arrows + art-mode
}

// Name-keyed subject browse page (offline, prefix-inclusive): every book under `name`
// (itself + any `name/…` descendant) plus the immediate child subjects to drill into.
// The subject page — composed by the SHARED Tier-2 presenter `LibraryCore.subjectVM` (crumbs,
// child sub-subjects, books), mirroring the server's /subject. The PWA only PAINTS it.
function renderSubject(name) {
  const v = $('view'); v.innerHTML = '';
  const href = eid => `#/book/${eid}`;
  const vm = LibraryCore.subjectVM(REPLICA, name);

  const head = document.createElement('div'); head.className = 'subj-page';
  head.innerHTML = '<p class="crumbs"><a href="#/">Home</a> / ' +
    vm.crumbs.map((c, i, a) => i < a.length - 1
      ? '<a href="#/subject/' + encodeURIComponent(c.name) + '">' + c.label + '</a>'
      : '<span>' + c.label + '</span>').join(' / ') + '</p>' +
    '<h2>' + vm.leaf + ' <small>' + vm.count + ' book' + (vm.count === 1 ? '' : 's') + '</small></h2>';
  v.appendChild(head);

  const addShelf = (title, books, count, moreUrl) => {
    if (books.length) v.appendChild(Shelf.render({ title, count, books: books.slice(0, 40), hrefFor: href, moreUrl }));
  };
  if (vm.children.length) {
    vm.children.forEach(ch => addShelf(ch.leaf, ch.books, ch.books.length, '#/subject/' + encodeURIComponent(ch.name)));
    addShelf(vm.leaf, vm.leftover, vm.leftover.length);
  } else {
    addShelf(vm.leaf, vm.books, vm.count);
  }
  Shelf.enhance(v);
}

// ════════════════════════ shared-layer platform adapter ════════════════════
// The PWA implementation of the LibraryCore adapter protocol (the ONLY device-specific
// code for the shared Search/Browse/Content/Settings features). Search + Browse are served
// OFFLINE from the cached replica; Content search is live (the in-book text isn't in the
// replica) — task 8 adds the downloaded-index offline path.

// Metadata Search + Browse + Suggest are served by the SHARED matcher (LibraryCore.searchReplica /
// browseReplica / suggestReplica) over the cached replica — see the Platform.data adapter below. No
// PWA-local matching/folding remains, so the PWA, native app and goldens agree exactly.

const Platform = {
  data: {
    search: async (q) => LibraryCore.searchReplica(REPLICA, q),
    browse: async (q, only) => LibraryCore.browseReplica(REPLICA, q, only),
    content: async (q) => {
      // Prefer the downloaded offline index (works with no connection); otherwise hit the
      // live endpoint. If offline AND no local index, the fetch fails → the shared contentVM
      // shows the "enable offline content search" state.
      if (window.ContentIndex && ContentIndex.available()) return ContentIndex.search(q);
      const r = await Net.fetch('/api/v1/content?q=' + encodeURIComponent(q || ''));
      return await r.json();
    },
    detail: async (eid) => BY_ID.get(eid) || null,   // read straight from the cached replica
    suggest: async (q) => LibraryCore.suggestReplica(REPLICA, q),
  },
  nav: {
    hrefFor: (ref) => {
      if (!ref) return null;
      if (ref.kind === 'edition') return `#/book/${ref.id}`;
      if (ref.kind === 'url') return ref.url;
      // Subject → the name-keyed offline subject page (replica has names, not ids).
      if (ref.kind === 'subject' && ref.q != null) return '#/subject/' + encodeURIComponent(ref.q);
      return null;                          // works/people: no standalone PWA page yet
    },
  },
  prefs: {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
    remove: (k) => { try { localStorage.removeItem(k); } catch {} },
  },
  isOffline: () => !ONLINE,
  // In-app reader open — the PWA-specific seam the shared detail renderer calls per holding.
  openBook: (eid, h) => { const e = BY_ID.get(eid); if (e) Opener.open(e, h); },
};

function renderCapture(v) {
  const d = document.createElement('details'); d.className = 'capture';
  d.innerHTML = `<summary>＋ Capture a book (ISBN)</summary>
    <input type="text" id="capIsbn" placeholder="Scan or type ISBN-13" inputmode="numeric" enterkeyhint="done" autocomplete="off">
    <div style="margin-top:8px;display:flex;gap:10px;align-items:center">
      <button class="btn" id="capBtn">Add to queue</button><span id="capPending" class="pending"></span></div>
    <div class="note">Queued offline; sent to the catalogue when the Mac is reachable.</div>`;
  v.appendChild(d);
  $('capBtn').onclick = () => queueCapture($('capIsbn').value);
  $('capIsbn').addEventListener('keydown', e => { if (e.key === 'Enter') queueCapture($('capIsbn').value); });
  showPending();
}

// ════════════════════════ capture outbox ════════════════════════
const uuid = () => (crypto.randomUUID && crypto.randomUUID()) || 'c-' + Date.now() + '-' + Math.random().toString(16).slice(2);
async function queueCapture(isbn) {
  const rec = { uuid: uuid(), isbn: (isbn || '').replace(/[^0-9Xx]/g, ''), source: 'pwa', scanned_at: new Date().toISOString() };
  if (rec.isbn.length < 10) { alert('Enter a valid ISBN.'); return; }
  await tx('outbox', 'readwrite', s => s.put(rec));
  if ($('capIsbn')) $('capIsbn').value = '';
  await flushOutbox(); await showPending();
}
async function flushOutbox() {
  const items = (await tx('outbox', 'readonly', s => s.getAll())) || [];
  for (const it of items) {
    // One outbox, two destinations: a wishlist add (path from the SHARED mapper) vs an ISBN capture.
    const url = it.kind === 'wishlist' ? LibraryCore.wishlistRequest('add').path : '/api/v1/capture';
    const { uuid: _u, kind: _k, ...body } = it;
    try {
      const r = await Net.fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      // 201 ok; 422/403 are terminal client errors (bad input / read-only) — drop, don't retry forever.
      if (r.status === 201 || r.status === 422 || r.status === 403) await tx('outbox', 'readwrite', s => s.delete(it.uuid));
    } catch { break; }
  }
}
async function showPending() {
  const n = ((await tx('outbox', 'readonly', s => s.getAll())) || []).length;
  if ($('capPending')) $('capPending').textContent = n ? `${n} queued` : '';
}

// ════════════════════════ wishlist ════════════════════════
// Offline-first like the rest: the list is cached in kv ('wishlist') and rendered via the SHARED
// LibraryCore.wishlistVM; every backend call goes through LibraryCore.wishlistRequest (the shared
// intent→request mapper — no endpoint hardcoded here) and add messages via wishlistAddMessage, so
// PWA/web/iOS stay in lockstep. Adds queue into the outbox (kind:'wishlist') and flush when online.
let _wlRaw = {};   // raw items by id (for a suspected card's candidate editions)

// Execute a shared wishlist descriptor over Net (the PWA's transport adapter).
function wlExec(action, opts) {
  const req = LibraryCore.wishlistRequest(action, opts);
  const o = { method: req.method, headers: { 'Accept': 'application/json' } };
  if (req.body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(req.body); }
  return Net.fetch(req.path, o);
}

function renderWishlist(v) {
  v.innerHTML = '';
  const form = document.createElement('form');
  form.className = 'capture';
  form.innerHTML = `<h2 style="margin:.2rem 0">Wishlist</h2>
    <p class="note" style="margin:.2rem 0 .6rem">Books you want but don't own yet.</p>
    <input type="text" id="wlTitle" placeholder="Title (or leave blank and use ISBN)" autocomplete="off">
    <input type="text" id="wlAuthor" placeholder="Author (optional)" autocomplete="off" style="margin-top:6px">
    <input type="text" id="wlIsbn" placeholder="ISBN-13 (optional)" inputmode="numeric" autocomplete="off" style="margin-top:6px">
    <div style="margin-top:8px;display:flex;gap:10px;align-items:center">
      <button class="btn" id="wlAdd" type="submit">Add to wishlist</button>
      <span id="wlMsg" class="pending"></span></div>`;
  v.appendChild(form);
  const host = document.createElement('div'); host.id = 'wl-host'; v.appendChild(host);

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const isbn = $('wlIsbn').value.trim(), title = $('wlTitle').value.trim(), author = $('wlAuthor').value.trim();
    const body = { source: 'pwa' };
    if (isbn) body.isbn = isbn; else if (title) { body.title = title; if (author) body.author = author; }
    else { $('wlMsg').textContent = 'Enter a title or an ISBN.'; return; }
    $('wlMsg').textContent = 'Adding…';
    if (ONLINE) {
      try {
        const r = await wlExec('add', { body });
        const j = await r.json();
        $('wlMsg').textContent = r.ok ? LibraryCore.wishlistAddMessage(j) : 'Could not add.';
      } catch { $('wlMsg').textContent = 'Network error.'; }
    } else {
      await queueWishlist(body);   // offline → outbox, flushed on reconnect
      $('wlMsg').textContent = 'Queued — will sync when online.';
    }
    $('wlTitle').value = $('wlAuthor').value = $('wlIsbn').value = '';
    await loadWishlist(host);
  });
  loadWishlist(host);
}

async function loadWishlist(host) {
  let payload = null;
  try {
    const r = await wlExec('list');
    if (r.ok) { payload = await r.json(); await kvPut('wishlist', payload); }
  } catch {}
  if (!payload) payload = (await kvGet('wishlist')) || { items: [] };
  _wlRaw = {};
  (payload.items || []).forEach(it => { _wlRaw[it.id] = it; });
  const vm = LibraryCore.wishlistVM(payload, {});
  host.innerHTML = '';
  if (vm.empty) { host.innerHTML = '<p class="note">Your wishlist is empty.</p>'; return; }
  vm.groups.forEach(g => {
    const h = document.createElement('h3'); h.textContent = `${g.title} (${g.cards.length})`;
    h.style.cssText = 'margin:1rem 0 .3rem;font-size:1rem'; host.appendChild(h);
    g.cards.forEach(c => host.appendChild(wishlistCardEl(c, host)));
  });
}

function wishlistCardEl(c, host) {
  const el = document.createElement('div');
  el.className = 'wl-card';
  el.style.cssText = 'display:flex;flex-wrap:wrap;gap:.6rem;padding:.5rem;border:1px solid var(--border,#444);border-radius:.5rem;margin:.35rem 0';
  const cover = c.cover_url
    ? `<img src="${c.cover_url}" alt="" loading="lazy" style="width:44px;height:60px;object-fit:cover;border-radius:3px" onerror="this.style.visibility='hidden'">`
    : '<div style="width:44px;height:60px;background:var(--border,#333);border-radius:3px"></div>';
  const meta = [c.by, c.publisher, c.year].filter(Boolean).join(' · ');
  const badge = c.badge ? `<span class="pending" style="margin-left:.4rem">${c.badge}</span>` : '';
  const title = document.createElement('div'); title.textContent = c.title;
  el.innerHTML = `${cover}<div style="flex:1;min-width:60%"><div><strong>${title.innerHTML}</strong>${badge}</div>
    <div class="note">${meta}</div></div>
    <button class="btn ghost" data-del="${c.id}" title="Remove" style="align-self:flex-start">✕</button>`;
  el.querySelector('[data-del]').onclick = async () => {
    try { await wlExec('remove', { id: c.id }); } catch {}
    loadWishlist(host);
  };
  if (c.status === 'suspected') el.appendChild(wishlistSuspectActions(c.id, host));
  return el;
}

// "Might already own this" — confirm one candidate (= owned) or decline (keep wanted).
function wishlistSuspectActions(id, host) {
  const cands = (_wlRaw[id] || {}).candidates || [];
  const wrap = document.createElement('div');
  wrap.style.cssText = 'flex-basis:100%;border-top:1px dashed var(--border,#444);margin-top:.4rem;padding-top:.4rem';
  wrap.innerHTML = '<div class="note" style="margin-bottom:.3rem">Might already be in your library — is it one of these?</div>';
  cands.forEach(e => {
    const b = document.createElement('button'); b.className = 'btn ghost';
    b.textContent = '✓ ' + (e.title || ('edition #' + e.id)) + (e.forms && e.forms.length ? ` (${e.forms.join(', ')})` : '');
    b.style.cssText = 'margin:.15rem .3rem .15rem 0';
    b.onclick = async () => { try { await wlExec('confirm', { id, editionId: e.id }); } catch {} loadWishlist(host); };
    wrap.appendChild(b);
  });
  const no = document.createElement('button'); no.className = 'btn ghost'; no.textContent = 'No, different book';
  no.onclick = async () => { try { await wlExec('decline', { id }); } catch {} loadWishlist(host); };
  wrap.appendChild(no);
  return wrap;
}

async function queueWishlist(body) {
  const rec = { uuid: uuid(), kind: 'wishlist', source: 'pwa', ...body };
  await tx('outbox', 'readwrite', s => s.put(rec));
  await flushOutbox();
}

// ════════════════════════ router ════════════════════════
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }

// The PWA's nav sections (hash routes). Labels/icons/order come from the SHARED manifest
// (LibraryCore.APP_SECTIONS) so the Search→Books rename lands here in step with web + native; the
// PWA just declares WHICH keys it implements + maps each to its hash route, and keeps `active` in
// sync. 'books' is the book finder (#/search). The cross-entity 'search' section has no PWA page yet.
const NAV_ITEMS = LibraryCore.navItems(['home', 'books', 'read', 'content', 'wishlist', 'settings'], function (k) {
  return { home: '#/', books: '#/search', read: '#/read', content: '#/content', wishlist: '#/wishlist', settings: '#/settings' }[k];
});
let _navHandle = null;
function navKeyFor(h) {
  return h.startsWith('#/search') ? 'books'
    : h.startsWith('#/read') ? 'read'
      : h.startsWith('#/content') ? 'content'
        : h.startsWith('#/wishlist') ? 'wishlist'
          : h.startsWith('#/settings') ? 'settings'
            : h.startsWith('#/book') ? '' : 'home';
}
function setActiveTab(h) { if (_navHandle) _navHandle.setActive(navKeyFor(h)); }

// Settings via the shared renderer, plus a PWA-only "offline content search" section
// (download / status / remove), injected as opts.extra. The DOWNLOAD itself is owned by
// ContentIndex (a persistent job), so it keeps running across tab switches; this view just
// SUBSCRIBES and reflects it — re-entering Settings mid-download shows live progress again.
const fmtMB = b => (b / 1e6).toFixed(b < 1e7 ? 1 : 0) + ' MB';
let _ociUnsub = null;                      // drop the previous Settings view's subscription
function offlineContentSection() {
  const wrap = document.createElement('div');
  wrap.innerHTML =
    '<h3 style="margin:.2rem 0">Offline content search</h3>'
    + '<p class="note" style="margin:.2rem 0 .6rem">Download the full in-book text index so '
    + '“Text” search works with no connection. Keeps downloading if you switch tabs.</p>'
    + '<div id="oci-status" class="note"></div>'
    + '<div style="margin-top:.5rem;display:flex;gap:10px;flex-wrap:wrap">'
    + '<button class="btn" id="oci-get">Enable offline content search</button>'
    + '<button class="btn ghost" id="oci-del" style="display:none">Remove download</button></div>';
  const statusEl = wrap.querySelector('#oci-status');
  const getBtn = wrap.querySelector('#oci-get');
  const delBtn = wrap.querySelector('#oci-del');
  if (!window.ContentIndex) { statusEl.textContent = 'Not available on this device.'; getBtn.disabled = true; return wrap; }

  async function paint() {
    const job = ContentIndex.job();
    if (job.state === 'downloading') {       // live, regardless of which tab started it
      statusEl.textContent = `Downloading… ${fmtMB(job.bytes)} (keeps going if you leave this tab)`;
      getBtn.disabled = true; getBtn.textContent = 'Downloading…'; delBtn.style.display = 'none';
      return;
    }
    getBtn.disabled = false;
    const st = await ContentIndex.status();
    if (st.state === 'ready' || ContentIndex.available()) {
      statusEl.textContent = `Ready — searchable offline (${fmtMB(st.bytes || job.bytes || 0)}).`;
      getBtn.textContent = 'Re-download'; delBtn.style.display = '';
    } else if (st.state === 'stored') {
      statusEl.textContent = `Downloaded (${fmtMB(st.bytes || 0)}); loading…`;
      getBtn.textContent = 'Re-download'; delBtn.style.display = '';
    } else {
      statusEl.textContent = (job.state === 'error')
        ? ('Download failed' + (ONLINE ? ' — try again.' : ' — you appear to be offline.'))
        : (ONLINE ? 'Not downloaded — Text search needs a connection.'
                  : 'Not downloaded, and you are offline — connect to enable.');
      getBtn.textContent = 'Enable offline content search'; delBtn.style.display = 'none';
    }
  }
  getBtn.onclick = () => { ContentIndex.enable().catch(() => {}); paint(); };   // idempotent
  delBtn.onclick = async () => { await ContentIndex.disable(); paint(); };
  // Reflect live job changes while mounted; replace the previous view's subscription so dead
  // (detached) Settings sections don't pile up.
  if (_ociUnsub) _ociUnsub();
  _ociUnsub = ContentIndex.subscribe(() => paint());
  paint();
  return wrap;
}

// "Read" — open your most-recent book directly in the reader (the PWA analogue of the iOS Read tab).
async function renderRead(v) {
  v.innerHTML = '<p class="note" style="padding:1rem">Opening your last book…</p>';
  let eids = [];
  try { eids = await ReadingStore.recent(1); } catch (e) {}
  if (!eids.length) {
    v.innerHTML = '<p class="note" style="padding:1rem">No recent book yet — open one from Home or Books.</p>';
    return;
  }
  const eid = +eids[0];
  const edition = BY_ID.get(eid) || { edition_id: eid, title: '' };
  let holding = null;
  try {
    const d = await Platform.data.detail(eid);
    const hs = (d && d.holdings) || [];
    holding = hs.find(x => x.has_file) || hs[0] || null;
  } catch (e) {}
  if (holding) {
    location.hash = '#/book/' + eid;   // so closing the reader lands on the book's detail page
    Opener.open(edition, holding);
  } else {
    v.innerHTML = '<p class="note" style="padding:1rem">That book has no readable file.</p>';
  }
}

function route() {
  const h = location.hash || '#/';
  setActiveTab(h);
  const v = $('view');
  const m = h.match(/^#\/book\/(\d+)/);
  if (m) return void LibraryUI.detail(v, Platform, { eid: parseInt(m[1], 10), onBack: () => history.back() });
  const ms = h.match(/^#\/subject\/(.+)/);
  if (ms) return void renderSubject(decodeURIComponent(ms[1]));
  if (h.startsWith('#/read'))    return void renderRead(v);
  if (h.startsWith('#/search'))  return void LibraryUI.finder(v, Platform, { autofocus: true });
  if (h.startsWith('#/browse'))  return void LibraryUI.browse(v, Platform, { autofocus: true });
  if (h.startsWith('#/content')) return void LibraryUI.content(v, Platform, { autofocus: true });
  if (h.startsWith('#/wishlist')) return void renderWishlist(v);
  if (h.startsWith('#/settings')) return void LibraryUI.settings(v, Platform, { extra: offlineContentSection() });
  return renderHome();
}

// ════════════════════════ boot ════════════════════════
async function boot() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});

  // The PWA chooses the floating-button form of the shared Menu component (always, every width).
  // Protocol context: a phone client is not the host machine; desktop tracks the viewport.
  _navHandle = LibraryUI.nav(document.getElementById('navhost'), null, {
    items: NAV_ITEMS, activeKey: navKeyFor(location.hash || '#/'), variant: 'fab',
    ctx: { local: ['localhost', '127.0.0.1', '::1'].indexOf(location.hostname) !== -1,
           desktop: window.matchMedia('(min-width: 768px)').matches }
  });
  await loadCached(); route(); watchOfflineTiles();
  // Load a previously-downloaded offline content index, if any, so Text search works offline.
  if (window.ContentIndex) ContentIndex.load().catch(() => {});
  const online = await probe(); setStatus(online);
  if (online) { await refresh(); await refreshStarred(); await flushOutbox(); setStatus(true); route(); prefetchCovers(); }

  window.addEventListener('hashchange', route);
  // A star toggled on any cover → adopt the fresh (ordered) set, persist it, and splice the home
  // Starred rail in/out live from the SAME homeVM. syncStarredRail no-ops off the home page (no
  // Recent anchor), so starring from the search grid never injects a stray rail.
  document.addEventListener('starred:changed', ev => {
    STARRED_IDS = (ev.detail && ev.detail.ids) || [];
    kvPut('starred', STARRED_IDS).catch(() => {});
    Shelf.syncStarredRail($('view'), { replica: REPLICA, ids: STARRED_IDS, hrefFor: eid => `#/book/${eid}` });
  });
  $('home').onclick = () => go('#/');
  window.addEventListener('online', async () => { await refresh(); await refreshStarred(); await flushOutbox(); setStatus(true); route(); });
  window.addEventListener('offline', () => setStatus(false));
  // Returning to the tab / window revalidates the catalogue (cheap 304 when unchanged), so a new
  // edition/series appears without a manual reload — the web/iOS foreground-refresh, in the browser.
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') syncNow(); });
  window.addEventListener('focus', () => syncNow());
  installPullToRefresh();
}

// Pull-to-refresh (the shared gesture, DOM render): a downward drag from the very top triggers a sync.
// Passive listeners + no preventDefault, so it never fights the browser's own scroll/rubber-band.
function installPullToRefresh() {
  let startY = 0, armed = false;
  const THRESHOLD = 70;
  const atTop = () => (document.scrollingElement || document.documentElement).scrollTop <= 0;
  window.addEventListener('touchstart', e => { armed = atTop(); startY = e.touches[0].clientY; }, { passive: true });
  window.addEventListener('touchmove', e => {
    if (!armed) return;
    if (e.touches[0].clientY - startY > THRESHOLD) { armed = false; syncNow(); }
  }, { passive: true });
}
boot();
