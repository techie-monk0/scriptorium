/* ── content-index.js — offline full-text "Content search" for the PWA ───────
 * GENERIC SEAM: the client (app.js Platform.data.content + Settings) depends ONLY on the
 * stable `window.ContentIndex` facade — { available, status, job, load, enable, disable,
 * search, subscribe }. The actual storage + query mechanism lives in a swappable ENGINE
 * behind it, chosen at runtime by capability — so the implementation can change with NO
 * client change.
 *
 * Engines (both run the SAME match_fts query → offline results == online results):
 *   • OpfsEngine (preferred) — streams the download straight into an OPFS file via the
 *     SQLite "OPFS SAH Pool" VFS, then queries it PAGE-ON-DEMAND. Constant memory for both
 *     download AND query, so it handles a multi-hundred-MB index on a phone. No COOP/COEP
 *     headers and no SharedArrayBuffer needed. Requires a secure context (HTTPS/localhost)
 *     with OPFS — exactly where service workers already work.
 *   • MemEngine (fallback) — streams the download into IndexedDB, then deserializes the whole
 *     DB into memory to query. Fine on desktop; can exceed a phone's memory on a big library.
 *     Used when OPFS isn't available (e.g. plain-LAN http).
 *
 * The download is a PERSISTENT job owned here (not by any view), so it survives tab switches;
 * views subscribe() to reflect live progress.
 */
(function () {
  if (window.ContentIndex) return;

  const ENDPOINT = '/api/v1/content-index';
  const SQLITE_MJS = '/static/vendor/sqlite3.mjs';
  const SNIPPETS_PER_BOOK = 5;
  const FLUSH_BYTES = 4 * 1024 * 1024;     // MemEngine: coalesce stream chunks before each IDB put

  // ── dedicated IndexedDB (MemEngine storage + small meta) ──────────────────
  const IDB_NAME = 'library-content', STORE = 'kv';
  function idb() {
    return new Promise((res, rej) => {
      const r = indexedDB.open(IDB_NAME, 1);
      r.onupgradeneeded = () => { if (!r.result.objectStoreNames.contains(STORE)) r.result.createObjectStore(STORE); };
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
  }
  function kv(mode, fn) {
    return idb().then(db => new Promise((res, rej) => {
      const t = db.transaction(STORE, mode), rq = fn(t.objectStore(STORE));
      t.oncomplete = () => res(rq ? rq.result : undefined); t.onerror = () => rej(t.error);
    }));
  }
  const kvGet = k => kv('readonly', s => s.get(k));
  const kvPut = (k, v) => kv('readwrite', s => s.put(v, k));
  const kvDel = k => kv('readwrite', s => s.delete(k));
  const kvKeys = () => kv('readonly', s => s.getAllKeys());

  // ── shared SQLite-WASM init (one engine instance for whichever backend) ───
  let _sqlitePromise = null;
  function loadSqlite() {
    if (!_sqlitePromise) _sqlitePromise = import(SQLITE_MJS).then(m => (m.default || m)());
    return _sqlitePromise;
  }

  // Mirror the server query: normalize (NFKD, strip marks, lowercase, collapse ws), then match
  // the WHOLE query as ONE FTS5 phrase (catalogue/domain/search.py: normalize→expand_noop→OR).
  function ftsQuery(q) {
    const norm = (q || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
    return norm ? '"' + norm.replace(/"/g, '""') + '"' : null;
  }

  // Shared grouped query over any oo1 DB → same shape as GET /api/v1/content.
  function queryDb(db, q) {
    const match = ftsQuery(q);
    if (!match) return { q, books: [], available: true };
    const order = new Map(), books = [];
    db.exec({
      sql: "SELECT et.edition_id, snippet(edition_text_fts, 0, '[', ']', '…', 16) "
         + "FROM edition_text_fts JOIN edition_text et ON et.id = edition_text_fts.rowid "
         + "WHERE edition_text_fts MATCH ? ORDER BY bm25(edition_text_fts) LIMIT 400",
      bind: [match], rowMode: 'array',
      callback: (row) => {
        const eid = row[0]; let g = order.get(eid);
        if (!g) {
          const ed = db.selectObject('SELECT title, authors FROM edition WHERE id = ?', [eid]) || {};
          g = { eid, title: ed.title || ('edition #' + eid),
                authors: (ed.authors || '').split('\n').filter(Boolean), snippets: [] };
          order.set(eid, g); books.push(g);
        }
        if (g.snippets.length < SNIPPETS_PER_BOOK) g.snippets.push(row[1]);
      }
    });
    return { q, books, available: true };
  }

  // ── OpfsEngine: OPFS SAH-Pool VFS — page-on-demand, constant memory ───────
  const OpfsEngine = (function () {
    const DBFILE = '/lib-content.sqlite3';
    let _pool = null, _db = null, _meta = null;

    function supported() {
      return typeof navigator !== 'undefined' && navigator.storage
        && typeof navigator.storage.getDirectory === 'function';
    }
    async function pool() {
      if (_pool) return _pool;
      const s = await loadSqlite();
      _pool = await s.installOpfsSAHPoolVfs({ name: 'lib-content-pool' });   // throws if unusable
      return _pool;
    }
    function openDb(p) { if (_db) try { _db.close(); } catch (e) {} _db = new p.OpfsSAHPoolDb(DBFILE); }

    return {
      name: 'opfs',
      supported,
      probe() { return pool().then(() => true); },     // selection: succeeds only if SAH pool installs
      ready() { return !!_db; },

      async status() {
        if (_db) return { state: 'ready', etag: _meta && _meta.etag, bytes: _meta && _meta.bytes };
        const meta = await kvGet('opfs-meta'); _meta = meta || _meta;
        try { const p = await pool(); if (p.getFileNames().indexOf(DBFILE) >= 0) return { state: 'stored', etag: meta && meta.etag, bytes: meta && meta.bytes }; } catch (e) {}
        return meta ? { state: 'stored', bytes: meta.bytes } : { state: 'none' };
      },
      async load() {
        if (_db) return true;
        const p = await pool();
        if (p.getFileNames().indexOf(DBFILE) < 0) return false;
        _meta = (await kvGet('opfs-meta')) || null;
        openDb(p);
        return true;
      },
      async install(onProgress) {
        const p = await pool();
        const r = await fetch(ENDPOINT);
        if (!r.ok) throw new Error('download failed (' + r.status + ')');
        if (!r.body || !r.body.getReader) throw new Error('streaming unsupported');
        const etag = r.headers.get('ETag') || '';
        const reader = r.body.getReader();
        let total = 0;
        try {
          // importDbChunked pulls chunks until the callback returns undefined, writing each
          // straight to the OPFS handle — the whole file never sits in memory.
          await p.importDbChunked(DBFILE, async () => {
            const { done, value } = await reader.read();
            if (done) return undefined;
            total += value.length; if (onProgress) onProgress(total);
            return value;
          });
        } catch (e) {
          try { p.unlink(DBFILE); } catch (_) {}
          throw e;
        }
        _meta = { etag, bytes: total };
        await kvPut('opfs-meta', _meta);
        openDb(p);
        return _meta;
      },
      async remove() {
        if (_db) { try { _db.close(); } catch (e) {} _db = null; }
        _meta = null; await kvDel('opfs-meta');
        try { const p = await pool(); if (p.getFileNames().indexOf(DBFILE) >= 0) p.unlink(DBFILE); } catch (e) {}
      },
      async query(q) { return _db ? queryDb(_db, q) : { q, books: [], available: false }; },
    };
  })();

  // ── MemEngine: stream→IndexedDB, deserialize-to-memory query (fallback) ───
  const MemEngine = (function () {
    let _db = null, _meta = null;
    async function assembleFromIdb(meta) {
      const out = new Uint8Array(meta.bytes); let off = 0;
      for (let i = 0; i < meta.chunks; i++) {
        const blob = await kvGet('chunk:' + i);
        if (!blob) throw new Error('missing chunk ' + i);
        const ab = await blob.arrayBuffer();
        out.set(new Uint8Array(ab), off); off += ab.byteLength;
      }
      return out;
    }
    async function openFromBytes(u8) {
      const s = await loadSqlite();
      const p = s.wasm.allocFromTypedArray(u8);
      const db = new s.oo1.DB();
      const rc = s.capi.sqlite3_deserialize(
        db.pointer, 'main', p, u8.length, u8.length,
        s.capi.SQLITE_DESERIALIZE_FREEONCLOSE | s.capi.SQLITE_DESERIALIZE_RESIZEABLE);
      if (rc) { db.close(); throw new Error('sqlite3_deserialize rc=' + rc); }
      if (_db) try { _db.close(); } catch (e) {}
      _db = db;
    }
    return {
      name: 'memory',
      supported() { return true; },
      probe() { return Promise.resolve(true); },
      ready() { return !!_db; },
      async status() {
        if (_db) return { state: 'ready', etag: _meta && _meta.etag, bytes: _meta && _meta.bytes };
        const meta = await kvGet('meta');
        return meta ? { state: 'stored', etag: meta.etag, bytes: meta.bytes } : { state: 'none' };
      },
      async load() {
        if (_db) return true;
        const meta = await kvGet('meta');
        if (!meta) return false;
        _meta = meta;
        await openFromBytes(await assembleFromIdb(meta));
        return true;
      },
      async install(onProgress) {
        await this.remove();
        const r = await fetch(ENDPOINT);
        if (!r.ok) throw new Error('download failed (' + r.status + ')');
        if (!r.body || !r.body.getReader) throw new Error('streaming unsupported');
        const etag = r.headers.get('ETag') || '';
        const reader = r.body.getReader();
        let idx = 0, total = 0, buf = [], bufLen = 0;
        const flush = async () => { if (!bufLen) return; const blob = new Blob(buf); buf = []; bufLen = 0; await kvPut('chunk:' + idx, blob); idx++; };
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buf.push(value); bufLen += value.length; total += value.length;
            if (onProgress) onProgress(total);
            if (bufLen >= FLUSH_BYTES) await flush();
          }
          await flush();
          _meta = { etag, bytes: total, chunks: idx };
          await kvPut('meta', _meta);
          await openFromBytes(await assembleFromIdb(_meta));
          return _meta;
        } catch (e) { await this.remove(); throw e; }
      },
      async remove() {
        if (_db) { try { _db.close(); } catch (e) {} _db = null; }
        _meta = null;
        const keys = (await kvKeys()) || [];
        for (const k of keys) if (k === 'meta' || (typeof k === 'string' && k.indexOf('chunk:') === 0)) await kvDel(k);
      },
      async query(q) { return _db ? queryDb(_db, q) : { q, books: [], available: false }; },
    };
  })();

  // ── engine selection (capability-based; OPFS preferred) ───────────────────
  let _active = null, _activePromise = null;
  function activeEngine() {
    if (_active) return Promise.resolve(_active);
    if (!_activePromise) _activePromise = (async () => {
      if (OpfsEngine.supported()) {
        try { await OpfsEngine.probe(); _active = OpfsEngine; return _active; } catch (e) {}
      }
      _active = MemEngine; return _active;
    })();
    return _activePromise;
  }

  // ── persistent download job (owned here, NOT by any view) ─────────────────
  let _job = { state: 'idle', bytes: 0, error: null };   // idle | downloading | ready | error
  let _promise = null;
  const _subs = new Set();
  function _set(patch) { _job = Object.assign({}, _job, patch); _subs.forEach(fn => { try { fn(_job); } catch (e) {} }); }
  async function _run() {
    _set({ state: 'downloading', bytes: 0, error: null });
    try {
      const eng = await activeEngine();
      const meta = await eng.install(n => _set({ bytes: n }));
      _set({ state: 'ready', bytes: meta.bytes });
    } catch (e) {
      _set({ state: 'error', error: String((e && e.message) || e) });
    } finally { _promise = null; }
  }

  // ── generic facade (the ONLY surface app.js / Settings touch) ─────────────
  window.ContentIndex = {
    engineName() { return _active ? _active.name : 'auto'; },
    available() { return !!(_active && _active.ready()); },     // sync (Platform.data.content)
    status() { return activeEngine().then(e => e.status()); },
    job() { return Object.assign({}, _job); },
    load() { return activeEngine().then(e => e.load()); },
    enable() { if (!_promise) _promise = _run(); return _promise; },   // idempotent
    disable() { _set({ state: 'idle', bytes: 0, error: null }); return activeEngine().then(e => e.remove()); },
    search(q) { return activeEngine().then(e => e.query(q)); },
    subscribe(fn) { _subs.add(fn); return () => _subs.delete(fn); },
  };
})();
