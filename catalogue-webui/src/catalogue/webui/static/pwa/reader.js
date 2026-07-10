/* In-app reader (P8 / reader plan N5) — the PWA shell now drives the SHARED engine
 * (`reader-core` + `overlay`), the same one the Flask reader uses (§A.1: one engine, two adapter
 * sets). It receives the platform adapters (FileStore, ReadingStore, Net) and builds the adapter
 * surface `reader-core` consumes (io / position / bookmarks / annotations), wiring annotations +
 * bookmarks through an OFFLINE OP-QUEUE (IndexedDB outbox) that flushes to /sync/reader on reconnect.
 * A native wrap/rewrite still replaces this whole module without touching the rest of the app.
 *
 * NOTE: runtime-unverified in CI (no PWA browser harness) — node-checked + faithful to the Flask
 * shell's `ReaderCore.mount(...)` call. The prior self-contained engine is in git history.
 */
'use strict';
(function () {
  function loadScript(src) {
    return new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = src; s.onload = res; s.onerror = () => rej(new Error('load ' + src));
      document.head.appendChild(s);
    });
  }

  // Load the shared reader engine + its chrome CSS once (same assets as the Flask reader).
  let _engine = null;
  function ensureEngine() {
    if (_engine) return _engine;
    _engine = (async () => {
      if (!document.querySelector('link[data-reader-css]')) {
        const l = document.createElement('link');
        l.rel = 'stylesheet'; l.href = '/static/reader/reader.css'; l.setAttribute('data-reader-css', '1');
        document.head.appendChild(l);
        const th = document.createElement('link');   // generated reading themes (palette.json)
        th.rel = 'stylesheet'; th.href = '/static/reader/reader-themes.css';
        document.head.appendChild(th);
      }
      if (!window.ReaderVendor) await loadScript('/static/reader/vendor.js');
      if (!window.ReaderOverlay) await loadScript('/static/reader/overlay.js');
      if (!window.ReaderCore) await loadScript('/static/reader/reader-core.js');
      if (!window.ReaderSessions) await loadScript('/static/reader/reader-sessions.js');
    })();
    return _engine;
  }

  // ── the PWA's own IndexedDB handle for the marks cache + outbox (the stores app.js created;
  //    open with no version so we never trigger an upgrade — just reuse them) ──────────────────
  function _idb() {
    return new Promise((res, rej) => {
      const r = indexedDB.open('library-device');
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
  }
  function _tx(store, mode, fn) {
    return _idb().then(db => new Promise((res, rej) => {
      let t;
      try { t = db.transaction(store, mode); } catch (e) { return rej(e); }   // store may not exist yet
      const rq = fn(t.objectStore(store));
      t.oncomplete = () => res(rq ? rq.result : undefined);
      t.onerror = () => rej(t.error);
    }));
  }
  const _uuid = () => (self.crypto && crypto.randomUUID) ? crypto.randomUUID()
    : ('id-' + Date.now() + '-' + Math.random().toString(16).slice(2));

  // ── offline-first marks channel: a local cache (kv) + a pending outbox, flushed on reconnect.
  //    `commit` applies locally, posts to /sync/reader when online, else queues. `list` merges the
  //    cache with a fresh server pull when reachable. Shared by bookmarks + annotations. ──────────
  function markChannel(type, HID, Net) {
    const cacheKey = `marks:${type}:${HID}`;
    const field = type === 'bookmark' ? 'bookmarks' : 'annotations';
    const cacheGet = async () => { try { return (await _tx('kv', 'readonly', s => s.get(cacheKey))) || {}; } catch (e) { return {}; } };
    const cachePut = async (m) => { try { await _tx('kv', 'readwrite', s => s.put(m, cacheKey)); } catch (e) {} };
    const lww = (a, b) => ((a.updated_at || '') >= (b.updated_at || '') ? a : b);

    async function applyLocal(rec) {
      const m = await cacheGet();
      m[rec.id] = m[rec.id] ? lww(rec, m[rec.id]) : rec;
      await cachePut(m);
    }
    async function post(ops) {
      return Net.fetch('/sync/reader', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ops }) });
    }
    async function commit(rec) {
      await applyLocal(rec);
      const op = Object.assign({ type }, rec);
      let ok = false;
      if (navigator.onLine !== false) { try { const r = await post([op]); ok = !!(r && r.ok); } catch (e) {} }
      if (!ok) { try { await _tx('outbox', 'readwrite', s => s.put({ uuid: op.id + ':' + (op.updated_at || ''), reader: 1, op })); } catch (e) {} }
      return rec;
    }
    async function list() {
      const m = await cacheGet();
      if (navigator.onLine !== false) {
        try {
          const r = await Net.fetch(`/sync/reader?holding=${HID}&since=0`);
          if (r && r.ok) { const d = await r.json(); for (const rec of (d[field] || [])) m[rec.id] = rec; await cachePut(m); }
        } catch (e) {}
      }
      return Object.values(m).filter(r => !r.deleted_at);
    }
    return { commit, list };
  }

  // Flush any queued reader ops to /sync/reader; on success, drop them from the outbox.
  async function flushOutbox(Net) {
    let pending;
    try { pending = await _tx('outbox', 'readonly', s => s.getAll()); } catch (e) { return; }
    const reader = (pending || []).filter(p => p && p.reader);
    if (!reader.length) return;
    try {
      const r = await Net.fetch('/sync/reader', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ops: reader.map(p => p.op) }) });
      if (r && r.ok) for (const p of reader) { try { await _tx('outbox', 'readwrite', s => s.delete(p.uuid)); } catch (e) {} }
    } catch (e) {}
  }

  // ── file bytes: cached (offline) or fetched + cached on first read (unchanged from the old engine) ──
  async function getBlob(holding, { FileStore, Net, canDownload = true }) {
    const key = holding.holding_id;
    const blob = await FileStore.get(key);
    if (blob) return blob;
    const likelyOffline = (typeof navigator !== 'undefined' && navigator.onLine === false);
    let r;
    try {
      r = await Net.fetch(`/holding/${key}/file`, { signal: AbortSignal.timeout(likelyOffline ? 5000 : 12000) });
    } catch (e) { throw new Error('server-unreachable'); }
    if (r.status === 202) throw new Error('cloud-fetching');
    if (!r.ok) throw new Error('fetch ' + r.status);
    if (canDownload) await FileStore.put(key, r.clone());
    return r.blob();
  }

  // ── the reader chrome (the els reader-core needs) — a full-screen overlay over the app shell.
  //    Editor mode: the PWA user owns the library, so all tools are present. ────────────────────
  function buildChrome(title) {
    const root = document.createElement('div');
    root.className = 'reader-host';
    root.style.cssText = 'position:fixed;inset:0;z-index:1000;display:flex;flex-direction:column;background:var(--reader-bg,#fff);';
    root.innerHTML = `
      <div class="topbar">
        <div class="bar bar-general">
          <button type="button" class="fbtn" id="rclose" title="Close" aria-label="Close reader">‹</button>
          <button type="button" class="fbtn" id="tocBtn" title="Contents">☰</button>
          <button type="button" class="fbtn" id="searchBtn" title="Search in book">🔍</button>
          <button type="button" class="fbtn" id="refreshBtn" title="Refresh marks from other devices" aria-label="Refresh">⟳</button>
        </div>
        <span class="title">${title || ''}</span>
        <div class="bar bar-text">
          <button type="button" class="fbtn small" id="fdec" title="Smaller text">A</button>
          <button type="button" class="fbtn big" id="finc" title="Larger text">A</button>
          <button type="button" class="fbtn" id="reflowBtn" title="Reflow to text" aria-pressed="false">¶</button>
          <button type="button" class="fbtn" id="gotoBtn" title="Go to page">⇥</button>
          <button type="button" class="fbtn" id="themeBtn" title="Reading theme">◐</button>
          <button type="button" class="fbtn tool" id="annotateBtn" title="Annotate">✍</button>
          <button type="button" class="fbtn tool" id="hlBtn" title="Highlight">🖍</button>
          <button type="button" class="fbtn tool" id="ulBtn" title="Underline">U̲</button>
          <button type="button" class="fbtn tool" id="strikeBtn" title="Strikethrough">S̶</button>
          <button type="button" class="fbtn tool" id="inkBtn" title="Draw">✎</button>
          <button type="button" class="fbtn tool" id="noteBtn" title="Note">🅝</button>
          <button type="button" class="fbtn tool" id="eraseBtn" title="Erase">⌫</button>
          <button type="button" class="fbtn" id="annBtn" title="Annotations">▦</button>
          <button type="button" class="fbtn" id="bmAdd" title="Bookmark this spot">★</button>
          <button type="button" class="fbtn" id="bmList" title="Bookmarks">▤</button>
        </div>
      </div>
      <div id="viewer"></div>
      <div id="tocPanel" class="bmpanel tocpanel hidden"></div>
      <div id="searchPanel" class="bmpanel searchpanel hidden"></div>
      <div id="bmPanel" class="bmpanel hidden"></div>
      <div id="annPanel" class="bmpanel annpanel hidden"></div>
      <div id="penOpts" class="penopts hidden"></div>
      <div id="hlPopup" class="hlpopup hidden"></div>
      <button type="button" class="pager left" id="pgPrev" aria-label="Previous page">‹</button>
      <button type="button" class="pager right" id="pgNext" aria-label="Next page">›</button>`;
    document.body.appendChild(root);
    return root;
  }

  function elsOf(root) {
    const g = id => root.querySelector('#' + id);
    return {
      viewer: g('viewer'), prog: g('prog'), topbar: root.querySelector('.topbar'),
      pgPrev: g('pgPrev'), pgNext: g('pgNext'), fdec: g('fdec'), finc: g('finc'),
      bmAdd: g('bmAdd'), bmList: g('bmList'), bmPanel: g('bmPanel'),
      tocBtn: g('tocBtn'), tocPanel: g('tocPanel'), themeBtn: g('themeBtn'), reflowBtn: g('reflowBtn'), gotoBtn: g('gotoBtn'),
      refreshBtn: g('refreshBtn'),
      searchBtn: g('searchBtn'), searchPanel: g('searchPanel'), hlPopup: g('hlPopup'),
      annotateBtn: g('annotateBtn'), hlBtn: g('hlBtn'), ulBtn: g('ulBtn'), strikeBtn: g('strikeBtn'),
      inkBtn: g('inkBtn'), noteBtn: g('noteBtn'), eraseBtn: g('eraseBtn'),
      annBtn: g('annBtn'), annPanel: g('annPanel'), penOpts: g('penOpts'),
    };
  }

  async function open(edition, holding, adapters) {
    const { ReadingStore, Net, canDownload = true } = adapters;
    const HID = holding.holding_id, EID = edition.edition_id;
    const EXT = (holding.kind || holding.format || 'pdf').toLowerCase();
    const root = buildChrome(edition.title);
    // Track this book in the shared open-set (tabs). The visual PWA tab strip is a follow-up; recording
    // here keeps the set consistent with web/iOS in the meantime.
    if (window.ReaderSessions) { try { ReaderSessions.open({ eid: EID, hid: HID, title: edition.title }); } catch (e) {} }
    const close = () => { try { window.removeEventListener('online', onOnline); } catch (e) {} root.remove(); };
    root.querySelector('#rclose').onclick = close;
    root.querySelector('#viewer').innerHTML = '<div class="loadmsg">Opening…</div>';
    const onOnline = () => flushOutbox(Net);

    try {
      await ensureEngine();
      const blob = await getBlob(holding, adapters);
      const bytes = await blob.arrayBuffer();
      root.querySelector('#viewer').innerHTML = '';

      const FILE_URL = `/holding/${HID}/file`, POS_URL = `/holding/${HID}/position`;
      const io = {
        // The PWA reads LOCAL bytes (cached/fetched), so hand pdf.js the whole buffer (a fresh view
        // each call so it can't be detached); EPUB gets the same buffer.
        pdfSource: () => ({ data: new Uint8Array(bytes) }),
        epubData: async () => bytes,
      };
      const position = {
        get: async () => {
          const loc = (await ReadingStore.state(EID)).location;
          return (loc && loc.locator != null) ? { locator: loc.locator, fraction: loc.fraction } : {};
        },
        save: (locator, fraction) => {
          ReadingStore.setLocation(EID, { locator, fraction });
          if (navigator.onLine !== false) {
            try { Net.fetch(POS_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ locator, fraction }), keepalive: true }); } catch (e) {}
          }
        },
        saveBeacon: (locator, fraction) => {
          ReadingStore.setLocation(EID, { locator, fraction });
          try { navigator.sendBeacon(POS_URL, new Blob([JSON.stringify({ locator, fraction })], { type: 'application/json' })); } catch (e) {}
        },
      };

      const bmCh = markChannel('bookmark', HID, Net);
      const annCh = markChannel('annotation', HID, Net);
      const bookmarks = {
        list: () => bmCh.list().then(a => a.sort((x, y) => (x.fraction || 0) - (y.fraction || 0))),
        add: ({ locator, fraction, label }) => {
          const now = new Date().toISOString();
          return bmCh.commit({ id: _uuid(), holding_id: HID, locator: String(locator), fraction, label,
            created_at: now, updated_at: now });
        },
        rename: (bm, label) => bmCh.commit({ id: bm.id, holding_id: HID, locator: bm.locator,
          fraction: bm.fraction, label, created_at: bm.created_at, updated_at: new Date().toISOString() }),
        remove: (bm) => bmCh.commit({ id: bm.id, holding_id: HID, locator: bm.locator, fraction: bm.fraction,
          label: bm.label, created_at: bm.created_at, deleted_at: new Date().toISOString(),
          updated_at: new Date().toISOString() }),
      };
      const annotations = {
        list: () => annCh.list(),
        add: (a) => {
          const id = _uuid(), now = new Date().toISOString();
          return annCh.commit(Object.assign({ id, holding_id: HID, created_at: now, updated_at: now }, a))
            .then(() => Object.assign({ id }, a));
        },
        update: (a, fields) => annCh.commit(Object.assign(
          { id: a.id, holding_id: HID, kind: a.kind, cfi_range: a.cfi_range, page: a.page, rect: a.rect,
            color: a.color, note_text: a.note_text, ink: a.ink, created_at: a.created_at },
          fields, { updated_at: new Date().toISOString() })).then(() => Object.assign({}, a, fields)),
        remove: (a) => annCh.commit({ id: a.id, holding_id: HID, kind: a.kind, cfi_range: a.cfi_range,
          page: a.page, rect: a.rect, color: a.color, note_text: a.note_text, ink: a.ink,
          created_at: a.created_at, deleted_at: new Date().toISOString(), updated_at: new Date().toISOString() }),
      };

      window.addEventListener('online', onOnline);
      flushOutbox(Net);   // catch up anything queued from a previous offline session

      window.ReaderCore.mount({
        ext: EXT,
        els: elsOf(root),
        io, position,
        // A read-only guest never owns marks; editors get the full annotation/bookmark surface.
        bookmarks: canDownload ? bookmarks : null,
        annotations: canDownload ? annotations : null,
        downloadUrl: canDownload ? FILE_URL : null,
        onExit: close,
      });
    } catch (err) {
      const m = (err && err.message) || '';
      const conn = m === 'server-unreachable' || /^fetch \d+$/.test(m);
      let html;
      if (m === 'cloud-fetching') {
        html = 'This book lives in the cloud and the Mac is fetching it now. Give it a few seconds and reopen.';
      } else if (conn && canDownload === false) {
        html = 'Couldn’t reach the library server. This account reads books live, so it needs the Mac (and tunnel) reachable — try again in a moment.';
      } else if (conn) {
        html = 'Couldn’t reach the Mac to load this book. Open it once while the Mac is reachable to cache it on this device, then it’ll work offline.';
      } else {
        html = 'Couldn’t open this book.';
      }
      root.querySelector('#viewer').innerHTML = `<div class="loadmsg">${html}</div>`;
    }
  }

  window.Reader = { open };
})();
