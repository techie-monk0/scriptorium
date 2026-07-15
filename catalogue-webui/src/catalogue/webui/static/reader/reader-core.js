/* reader-core — the standalone reader engine (Phase 1 of reader_module_plan.md).
 *
 * Lifted verbatim from the old reader.html inline <script>, with every environment touch-
 * point pulled out behind an adapter seam so the SAME engine can be driven by the Flask web
 * shell today and the offline PWA / a future WKWebView next.
 * Behaviour is identical to the pre-extraction reader.html.
 *
 *   ReaderCore.mount({
 *     ext,                       // 'epub' | 'pdf' | … (anything else → PDF.js)
 *     els: { viewer, prog, topbar, pgPrev, pgNext, fdec, finc },   // host-owned chrome
 *     io: {                       // byte source — the host decides URL-stream vs cached bytes
 *       pdfSource(),              //   → arg for pdfjsLib.getDocument (web: {url}; PWA: {data})
 *       epubData(),               //   → Promise<ArrayBuffer>
 *     },
 *     position: {                 // where the reading spot is read/written
 *       get(),                    //   → Promise<{locator, fraction}>  ({} if none)
 *       save(locator, fraction),  //   normal debounced write (best-effort)
 *       saveBeacon(locator, fraction),   // hide/unload write (must use keepalive/beacon)
 *     },
 *     downloadUrl,                // string | null — the "Download instead" affordance on error
 *     onExit,                     // optional () => void for Escape (default: go to '/')
 *   })
 *
 * The delicate PDF pinch/pan/rail/zoom code and the EPUB reflow handling are copied as-is.
 */
'use strict';
(function () {

  function mount(opts) {
    const EXT = (opts.ext || '').toLowerCase();
    const els = opts.els;
    const io = opts.io;
    const position = opts.position;
    const downloadUrl = opts.downloadUrl || null;
    const onExit = opts.onExit || (() => { location.href = '/'; });

    const viewer = els.viewer;
    const progEl = els.prog;

    // Shared controller: each viewer fills these in; the chevrons / keys / A± call them.
    // `current()` returns the current spot as {locator, fraction, label} (for a bookmark);
    // `goto(locator)` jumps to one; `toc()` returns the outline as [{label, depth, go()}];
    // `setTheme(name)` recolours the content; `search(q)` returns [{label, go()}] hits.
    // All set by the active engine (PDF / EPUB).
    const ctrl = { prev() {}, next() {}, bigger() {}, smaller() {},
                   current() { return null; }, goto() {}, toc() { return null; },
                   setTheme() {}, search() { return null; } };

    // ── Position save (debounced; flushed on hide/unload via the beacon adapter) ──────
    let pendLoc = null, pendFrac = null, lastSent = null, saveTimer = null;
    function queueSave(locator, fraction) {
      if (locator == null) return;
      pendLoc = String(locator); pendFrac = (fraction == null ? null : fraction);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(flushSave, 1000);
      if (pendFrac != null) progEl.textContent = Math.round(pendFrac * 100) + '%';
    }
    function flushSave() {
      if (pendLoc == null || pendLoc === lastSent) return;
      lastSent = pendLoc;
      try { position.save(pendLoc, pendFrac); } catch (e) {}
    }
    function beaconSave() {
      if (pendLoc == null || pendLoc === lastSent) return;
      lastSent = pendLoc;
      try { position.saveBeacon(pendLoc, pendFrac); } catch (e) {}
    }
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') beaconSave();
    });
    window.addEventListener('pagehide', beaconSave);

    const getSaved = () => Promise.resolve(position.get()).catch(() => ({}));
    const fail = (msg, err) => {
      const detail = err ? `<br><small style="opacity:.7">${(err && err.message) || err}</small>` : '';
      const dl = downloadUrl ? `<br><a href="${downloadUrl}">Download the file</a> instead.` : '';
      viewer.innerHTML = `<p class="msg">${msg}${detail}${dl}</p>`;
      if (err) console.error(err);
    };
    window.addEventListener('error', (e) => { if (!viewer.querySelector('.msg')) fail('Reader error.', e.error || e.message); });
    window.addEventListener('unhandledrejection', (e) => { if (!viewer.querySelector('.msg')) fail('Reader error.', e.reason); });

    // ── Loading / download progress overlay ───────────────────────────────────
    // A book opened remotely (e.g. through the Cloudflare tunnel) must transfer over the network
    // before it can render — an EPUB especially, since the whole zip is needed before ANY page
    // shows. Without feedback that's an ambiguous blank page. This overlay says "Downloading… X/Y"
    // with a bar, driven by the byte-source adapter's onProgress callback.
    const fmtMB = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB'
                          : Math.max(1, Math.round(b / 1024)) + ' KB');
    let _loadEl = null;
    function showLoad(text, frac) {
      if (!_loadEl) {
        _loadEl = document.createElement('div'); _loadEl.className = 'loadmsg';
        _loadEl.innerHTML = '<div class="loadtext"></div>'
          + '<div class="loadbar"><div class="loadbar-fill"></div></div>';
        document.body.appendChild(_loadEl);
      }
      _loadEl.querySelector('.loadtext').textContent = text;
      const bar = _loadEl.querySelector('.loadbar');
      const fill = _loadEl.querySelector('.loadbar-fill');
      if (frac == null || !isFinite(frac)) {           // unknown total → indeterminate (no width)
        bar.classList.add('indet'); fill.style.width = '40%';
      } else {
        bar.classList.remove('indet'); fill.style.width = Math.round(Math.min(1, frac) * 100) + '%';
      }
    }
    function hideLoad() { if (_loadEl) { _loadEl.remove(); _loadEl = null; } }
    // Progress callback handed to the byte-source adapter: (received, total) bytes.
    const onLoadProgress = (received, total) => {
      if (total) showLoad('Downloading… ' + fmtMB(received) + ' / ' + fmtMB(total), received / total);
      else showLoad('Downloading… ' + fmtMB(received));
    };

    // ── Global controls: side chevrons, A± font/zoom, keyboard ────────────────
    els.pgPrev.addEventListener('click', () => ctrl.prev());
    els.pgNext.addEventListener('click', () => ctrl.next());
    if (els.fdec) els.fdec.addEventListener('click', () => ctrl.smaller());
    if (els.finc) els.finc.addEventListener('click', () => ctrl.bigger());
    if (els.fitWidth) els.fitWidth.addEventListener('click', () => ctrl.fitWidth && ctrl.fitWidth());   // PDF: fit page width

    // ── Navigation history: "Back to where I was" (Apple Books style) ──────────
    // Record the current spot ONLY on a JUMP (bookmark / TOC / search / go-to / in-content link) —
    // never on a page turn — so the pill returns you to the jump ORIGIN, not the previous page. The
    // pill PERSISTS (no timer): it goes away when you use it, dismiss it, or it retargets on a new jump.
    const backStack = [];
    let backPillEl = null;
    function ensureBackPill() {
      if (backPillEl) return backPillEl;
      const host = (els.viewer && els.viewer.parentElement) || document.body;
      const el = document.createElement('div');
      el.className = 'reader-backpill hidden';
      el.innerHTML = '<button type="button" class="rbp-go"></button><button type="button" class="rbp-x" aria-label="Dismiss">×</button>';
      el.querySelector('.rbp-go').addEventListener('click', goBack);
      el.querySelector('.rbp-x').addEventListener('click', dismissBack);
      host.appendChild(el);
      backPillEl = el;
      return el;
    }
    function refreshBackPill() {
      const el = ensureBackPill();
      const top = backStack[backStack.length - 1];
      if (!top) { el.classList.add('hidden'); return; }
      el.querySelector('.rbp-go').textContent = '↩ Back to ' + (top.label || 'where I was');
      el.classList.remove('hidden');
    }
    function recordBackTarget() {
      const cur = ctrl.current && ctrl.current();
      if (!cur || cur.locator == null) return;
      backStack.push(cur);
      refreshBackPill();
    }
    function goBack() {                                  // tap the pill → return, then retarget the next
      const t = backStack.pop();
      if (t) { try { ctrl.goto(t.locator); } catch (e) {} }
      refreshBackPill();
    }
    function dismissBack() { backStack.pop(); refreshBackPill(); }   // ✕ → drop it, don't navigate

    // The top-bar dropdowns (bookmarks / contents / search) are mutually exclusive.
    const closeAllPanels = () =>
      [els.bmPanel, els.tocPanel, els.searchPanel, els.annPanel].forEach(p => p && p.classList.add('hidden'));

    // ── Bookmarks (optional; wired only when the host supplies the adapter + chrome) ──
    // The engine owns ctrl.current()/goto(); this is the format-agnostic add-button + list
    // panel on top. The adapter (host-supplied) persists/syncs — here we just call it.
    const bookmarks = opts.bookmarks;
    if (bookmarks && els.bmAdd && els.bmList && els.bmPanel) {
      const panel = els.bmPanel;
      let bmCache = [];                              // last-known list (for dedupe without a panel open)
      async function loadBookmarks() {
        try { bmCache = await bookmarks.list(); } catch (e) { bmCache = []; }
        return bmCache;
      }
      const sameSpot = (loc) => bmCache.some(b => String(b.locator) === String(loc));
      const flash = (glyph) => {
        const was = els.bmAdd.textContent; els.bmAdd.textContent = glyph;
        setTimeout(() => { els.bmAdd.textContent = was; }, 800);
      };

      // Turn a row's label into an inline editor (rename). Enter / blur commits; Esc cancels.
      function startRename(row, bm) {
        if (row.querySelector('.bm-edit-input')) return;
        const input = document.createElement('input');
        input.className = 'bm-edit-input'; input.value = bm.label || '';
        const go = row.querySelector('.bm-go');
        row.replaceChild(input, go);
        input.focus(); input.select();
        let done = false;
        const commit = async (save) => {
          if (done) return; done = true;
          const name = input.value.trim();
          if (save && name && name !== (bm.label || '')) {
            try { await bookmarks.rename(bm, name); } catch (e) {}
          }
          await renderBookmarks();
        };
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); commit(true); }
          else if (e.key === 'Escape') { e.preventDefault(); commit(false); }
        });
        input.addEventListener('blur', () => commit(true));
      }

      async function renderBookmarks() {
        await loadBookmarks();
        panel.innerHTML = '';
        if (!bmCache.length) { panel.innerHTML = '<div class="bm-empty">No bookmarks yet.</div>'; return; }
        // Clear-all header — tombstones every bookmark (so the removal syncs to the server / other
        // surfaces, not just this device).
        const head = document.createElement('div'); head.className = 'bm-head';
        const clr = document.createElement('button'); clr.className = 'bm-clear';
        clr.textContent = 'Clear all'; clr.title = 'Remove all bookmarks for this book';
        clr.addEventListener('click', async () => {
          if (!window.confirm('Clear all bookmarks for this book?')) return;
          for (const b of bmCache.slice()) { try { await bookmarks.remove(b); } catch (e) {} }
          renderBookmarks();
        });
        head.appendChild(clr); panel.appendChild(head);
        for (const bm of bmCache) {
          const row = document.createElement('div'); row.className = 'bm-row';
          const go = document.createElement('button'); go.className = 'bm-go';
          go.textContent = bm.label || ('@ ' + bm.locator);
          go.addEventListener('click', () => { panel.classList.add('hidden'); recordBackTarget(); ctrl.goto(bm.locator); });
          row.appendChild(go);
          if (bookmarks.rename) {                     // rename affordance (optional adapter method)
            const ed = document.createElement('button'); ed.className = 'bm-edit';
            ed.textContent = '✎'; ed.title = 'Rename'; ed.setAttribute('aria-label', 'Rename bookmark');
            ed.addEventListener('click', (e) => { e.stopPropagation(); startRename(row, bm); });
            row.appendChild(ed);
          }
          const del = document.createElement('button'); del.className = 'bm-del';
          del.textContent = '×'; del.title = 'Remove bookmark'; del.setAttribute('aria-label', 'Remove bookmark');
          del.addEventListener('click', async (e) => {
            e.stopPropagation();
            try { await bookmarks.remove(bm); } catch (x) {}
            renderBookmarks();
          });
          row.appendChild(del); panel.appendChild(row);
        }
      }

      els.bmAdd.addEventListener('click', async () => {
        const cur = ctrl.current && ctrl.current();
        if (!cur || cur.locator == null) return;
        await loadBookmarks();
        if (sameSpot(cur.locator)) { flash('✓'); return; }   // already bookmarked here → no duplicate
        try { await bookmarks.add(cur); } catch (e) {}
        await loadBookmarks();
        flash('✓');
        if (!panel.classList.contains('hidden')) renderBookmarks();
      });
      els.bmList.addEventListener('click', () => {
        const wasOpen = !panel.classList.contains('hidden');
        closeAllPanels();
        if (!wasOpen) { panel.classList.remove('hidden'); renderBookmarks(); }
      });
      loadBookmarks();                                // warm the cache so the first ★ dedupes
    }

    // ── Table of contents / outline (engine-derived; available to viewers too) ──
    if (els.tocBtn && els.tocPanel) {
      const tpanel = els.tocPanel;
      async function renderToc() {
        let items = null;
        try { items = ctrl.toc && await ctrl.toc(); } catch (e) {}
        tpanel.innerHTML = '';
        if (!items || !items.length) {
          tpanel.innerHTML = '<div class="bm-empty">No contents.</div>'; return;
        }
        for (const it of items) {
          const b = document.createElement('button'); b.className = 'toc-item';
          b.textContent = it.label || '—';
          b.style.paddingLeft = (10 + 14 * (it.depth || 0)) + 'px';   // indent by nesting depth
          b.addEventListener('click', () => { tpanel.classList.add('hidden'); recordBackTarget(); try { it.go(); } catch (e) {} });
          tpanel.appendChild(b);
        }
      }
      els.tocBtn.addEventListener('click', () => {
        const wasOpen = !tpanel.classList.contains('hidden');
        closeAllPanels();
        if (!wasOpen) { tpanel.classList.remove('hidden'); renderToc(); }
      });
    }

    // ── In-book search (engine-derived; available to viewers too) ──────────────
    if (els.searchBtn && els.searchPanel) {
      const spanel = els.searchPanel;
      const input = document.createElement('input');
      input.type = 'search'; input.className = 'bm-edit-input search-input';
      input.placeholder = 'Search in book…';
      const results = document.createElement('div'); results.className = 'search-results';
      spanel.appendChild(input); spanel.appendChild(results);
      let seq = 0;
      async function runSearch() {
        const q = input.value.trim();
        const mine = ++seq;                           // ignore a stale slow search if re-run
        results.innerHTML = q ? '<div class="bm-empty">Searching…</div>' : '';
        if (!q) return;
        let hits = [];
        try { hits = (ctrl.search && await ctrl.search(q)) || []; } catch (e) {}
        if (mine !== seq) return;
        results.innerHTML = '';
        if (!hits.length) { results.innerHTML = '<div class="bm-empty">No matches.</div>'; return; }
        for (const h of hits) {
          const b = document.createElement('button'); b.className = 'toc-item';
          b.textContent = h.label || '—';
          b.addEventListener('click', () => { spanel.classList.add('hidden'); recordBackTarget(); try { h.go(); } catch (e) {} });
          results.appendChild(b);
        }
      }
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runSearch(); } });
      els.searchBtn.addEventListener('click', () => {
        const wasOpen = !spanel.classList.contains('hidden');
        closeAllPanels();
        if (!wasOpen) { spanel.classList.remove('hidden'); input.focus(); }
      });
    }

    // ── Reading theme (Apple Books style: white / sepia / gray / night) — persisted ──
    // The host CSS reacts to body[data-reader-theme] for the chrome + frame (generated
    // reader-themes.css, sourced from palette.json); ctrl.setTheme recolours the content
    // (PDF filter / EPUB themes). Legacy light/dark keys migrate to white/night.
    const THEMES = ['auto', 'white', 'sepia', 'gray', 'night'];   // 'auto' follows the device theme
    const THEME_ALIAS = { light: 'white', dark: 'night' };
    function readerTheme() {
      let t = localStorage.getItem('readerTheme');
      t = THEME_ALIAS[t] || t;
      return THEMES.indexOf(t) >= 0 ? t : 'auto';
    }
    const _darkMQ = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    function resolveTheme(t) { return t === 'auto' ? (_darkMQ && _darkMQ.matches ? 'night' : 'white') : t; }
    function applyTheme(t) {
      localStorage.setItem('readerTheme', t);
      const resolved = resolveTheme(t);
      document.body.setAttribute('data-reader-theme', resolved);
      document.body.setAttribute('data-reader-theme-pref', t);   // remember 'auto' vs an explicit pick
      try { ctrl.setTheme(resolved); } catch (e) {}
    }
    applyTheme(readerTheme());                         // honour the persisted choice on open
    if (_darkMQ) { try { _darkMQ.addEventListener('change', () => { if (readerTheme() === 'auto') applyTheme('auto'); }); } catch (e) {} }
    if (els.themeBtn) {
      els.themeBtn.addEventListener('click', () => {
        applyTheme(THEMES[(THEMES.indexOf(readerTheme()) + 1) % THEMES.length]);
      });
    }

    // Reflow-to-text toggle (PDF only; ctrl.reflow is defined by the PDF engine). Reads the page's
    // text and shows it as paragraphs — a phone reading mode.
    if (els.reflowBtn) {
      els.reflowBtn.addEventListener('click', async () => {
        if (!ctrl.reflow) return;
        const on = await ctrl.reflow();
        els.reflowBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
        document.body.classList.toggle('reflow-on', on);
      });
    }

    // ── Go to page (PDF) / position (EPUB) — a TYPED jump (records a back target so the pill returns) ──
    let gotoPop = null;
    function openGoto() {
      if (gotoPop) { gotoPop.remove(); gotoPop = null; return; }   // toggle off
      const isPdf = ctrl.kind === 'pdf';
      const max = isPdf ? (ctrl.pageCount || 1) : 100;
      const cur = ctrl.current && ctrl.current();
      const start = isPdf ? (parseInt(cur && cur.locator, 10) || 1)
                          : Math.round(((cur && cur.fraction) || 0) * 100);
      const pop = document.createElement('div');
      pop.className = 'reader-gotopop';
      pop.innerHTML = (isPdf
        ? '<label>Page <input type="number" inputmode="numeric" min="1" max="' + max + '" value="' + start + '"> <span class="rgp-of">of ' + max + '</span></label>'
        : '<label>Go to <input type="number" inputmode="numeric" min="0" max="100" value="' + start + '"> %</label>')
        + '<button type="button" class="rgp-go">Go</button>';
      const input = pop.querySelector('input');
      const submit = () => {
        let v = parseInt(input.value, 10);
        if (!isFinite(v)) { pop.remove(); gotoPop = null; return; }
        recordBackTarget();
        if (isPdf) { v = Math.max(1, Math.min(max, v)); ctrl.goto(String(v)); }
        else if (ctrl.gotoFraction) { v = Math.max(0, Math.min(100, v)); ctrl.gotoFraction(v / 100); }
        pop.remove(); gotoPop = null;
      };
      pop.querySelector('.rgp-go').addEventListener('click', submit);
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
      ((els.viewer && els.viewer.parentElement) || document.body).appendChild(pop);
      gotoPop = pop; input.focus(); input.select();
    }
    if (els.gotoBtn) els.gotoBtn.addEventListener('click', () => { closeAllPanels(); openGoto(); });

    // Shared annotation popup — PURE CHROME (no persistence here; the overlay owns that). Two
    // modes: a colour picker (pickColor) and an existing/new mark editor with a note field +
    // Remove (editExisting). The overlay (L2) calls these seams; the engine never touches a
    // renderer. Exposed below as opts-level `pickColor`/`editExisting` passed into the overlay.
    function makePopup() {
      const pop = els.hlPopup;
      const COLORS = [['#ffd54a', 'Yellow'], ['#a5d6a7', 'Green'], ['#90caf9', 'Blue'], ['#f48fb1', 'Pink']];
      const hide = () => { pop.classList.add('hidden'); pop.innerHTML = ''; };
      function pickColor(onPick) {
        pop.innerHTML = '';
        COLORS.forEach(([c, name]) => {
          const b = document.createElement('button'); b.className = 'hl-swatch';
          b.style.background = c; b.title = name; b.setAttribute('aria-label', 'Colour ' + name);
          b.addEventListener('click', async () => { hide(); try { await onPick(c); } catch (e) {} });
          pop.appendChild(b);
        });
        const x = document.createElement('button'); x.className = 'hl-x'; x.textContent = '✕';
        x.addEventListener('click', hide); pop.appendChild(x);
        pop.classList.remove('hidden');
      }
      // editExisting(a, {onRemove?, onSave?}) — `a.note_text` prefills the field; Enter (or blur)
      // calls onSave(text); Remove calls onRemove. A new note (a._isNew) shows no Remove.
      function editExisting(a, handlers) {
        handlers = handlers || {};
        pop.innerHTML = '';
        const input = document.createElement('input');
        input.className = 'bm-edit-input'; input.placeholder = 'Note…'; input.value = a.note_text || '';
        let done = false;
        const commit = async () => {
          if (done) return; done = true;
          const note = input.value.trim();
          if (handlers.onSave && note !== (a.note_text || '')) { try { await handlers.onSave(note); } catch (e) {} }
          hide();
        };
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); commit(); }
          else if (e.key === 'Escape') { e.preventDefault(); done = true; hide(); } });
        pop.appendChild(input);
        if (handlers.onRemove) {
          const del = document.createElement('button'); del.className = 'hl-del'; del.textContent = 'Remove';
          del.addEventListener('click', async () => { done = true; try { await handlers.onRemove(); } catch (e) {} hide(); });
          pop.appendChild(del);
        }
        pop.classList.remove('hidden'); input.focus();
      }
      return { pickColor, editExisting, hide };
    }
    const popup = (opts.annotations && els.hlPopup) ? makePopup() : null;

    // ── Annotation tools (toolbar) + pencil-smart + palm rejection ────────────
    // The engine owns the active tool + the shared pen/palm state; the overlay receives the tool
    // via setTool and consults `penActive()` for palm rejection (so pan/swipe and capture agree).
    let tool = 'select';
    let overlay = null;                              // the L2 capture/render layer (armed on demand)
    let armAnnotate = null;                          // each engine registers how to build its overlay
    let annotateOn = false;                          // reading mode by default; toggle arms annotation
    const penIds = new Set(); let lastPenTs = 0;     // pen pointer tracking → palm rejection
    const PEN_GRACE = 700;                            // ms after the last pen event we still reject touch
    function penActive() { return penIds.size > 0 || (Date.now() - lastPenTs) < PEN_GRACE; }
    // Track pen pointers app-wide so palm rejection works even on elements without their own
    // handlers (the resting hand registers as touch and must be ignored mid-stroke). While a pen
    // is down we also flip the viewer to touch-action:none so the stroke isn't stolen by a scroll
    // — but only for the duration of the stroke, so between strokes a FINGER still scrolls freely
    // (the pencil-smart model: pencil draws, finger always scrolls).
    function syncTouchAction() { viewer.style.touchAction = penActive() ? 'none' : ''; }
    document.addEventListener('pointerdown', (e) => { if (e.pointerType === 'pen') { penIds.add(e.pointerId); lastPenTs = Date.now(); syncTouchAction(); } }, true);
    document.addEventListener('pointermove', (e) => { if (e.pointerType === 'pen') lastPenTs = Date.now(); }, true);
    const dropPen = (e) => { if (e.pointerType === 'pen') { penIds.delete(e.pointerId); lastPenTs = Date.now(); setTimeout(syncTouchAction, PEN_GRACE); } };
    document.addEventListener('pointerup', dropPen, true);
    document.addEventListener('pointercancel', dropPen, true);

    // Pen presentation (device-local, like theme/font). The Draw popover sets these.
    const PEN_KEY = 'readerPen';
    function getPen() {
      try { return Object.assign({ color: '#222222', width: 3, mode: 'pen' }, JSON.parse(localStorage.getItem(PEN_KEY) || '{}')); }
      catch (e) { return { color: '#222222', width: 3, mode: 'pen' }; }
    }
    function setPen(p) {
      const merged = Object.assign(getPen(), p);
      localStorage.setItem(PEN_KEY, JSON.stringify(merged));
      if (overlay) overlay.setPen(merged);
    }

    function setTool(t) {
      tool = t;
      if (overlay) overlay.setTool(t);
      // Reflect the active tool on the toolbar + take over touch-action while a tool is live so
      // the browser doesn't scroll/select under a draw/mark gesture.
      [['select', els.selectBtn], ['underline', els.ulBtn], ['strikeout', els.strikeBtn],
       ['note', els.noteBtn], ['erase', els.eraseBtn],
       ['highlight', els.hlBtn]].forEach(([name, btn]) => {          // no 'ink' — web has no ink tool
        if (btn) btn.classList.toggle('tool-active', tool === name);
      });
      if (els.penOpts) els.penOpts.classList.add('hidden');          // ink pen options never shown on web
    }
    // The overlay seam the engines hand to ReaderOverlay.create — pure host capabilities.
    const overlaySeam = popup ? {
      annotations: opts.annotations, pickColor: popup.pickColor, editExisting: popup.editExisting,
      penActive, getPenDefault: getPen,
    } : null;

    // ── Reading mode vs annotate mode ─────────────────────────────────────────
    // An edition opens in PURE READING mode: NO overlay, NO annotation fetch, NO ink library, NO
    // per-page paint hooks — identical cost to the reader before annotations existed. Flipping the
    // ✍ toggle ARMS annotation (the engine builds its overlay + loads marks then, once). This keeps
    // open/scroll fast and makes handwriting strictly opt-in.
    function enableAnnotate() {
      if (annotateOn) return;
      annotateOn = true;
      document.body.classList.add('annotate-on');
      if (els.annotateBtn) els.annotateBtn.classList.add('tool-active');
      // Build the overlay (async: loads the ink lib + the holding's marks); flag `annotate-ready`
      // when live so the UI/tests know the tools are wired.
      Promise.resolve(armAnnotate && armAnnotate())
        .then(() => document.body.classList.add('annotate-ready')).catch(() => {});
    }
    function disableAnnotate() {
      annotateOn = false;
      document.body.classList.remove('annotate-on');
      if (els.annotateBtn) els.annotateBtn.classList.remove('tool-active');
      setTool('select');
    }

    // Toolbar buttons → setTool. A tool toggles off (back to select) on a second click.
    if (overlaySeam) {
      if (els.annotateBtn)
        els.annotateBtn.addEventListener('click', () => annotateOn ? disableAnnotate() : enableAnnotate());
      const wire = (btn, name) => {
        if (!btn) return;
        btn.addEventListener('click', () => { closeAllPanels(); setTool(tool === name ? 'select' : name); });
      };
      wire(els.hlBtn, 'highlight'); wire(els.ulBtn, 'underline'); wire(els.strikeBtn, 'strikeout');
      wire(els.noteBtn, 'note'); wire(els.eraseBtn, 'erase');   // no ink tool on web
      if (els.selectBtn) els.selectBtn.addEventListener('click', () => { closeAllPanels(); setTool('select'); });

      // Manual "refresh marks": re-pull the holding's annotations from the server and repaint, so a
      // highlight/ink/note made on ANOTHER device appears without closing the book. Pull-to-refresh
      // would fight the reading scroll, so it's a chrome button (mirrors iOS's foreground/poll refresh);
      // also fires on tab-return. No-op until marks are painted (`overlay` armed) — nothing to refresh.
      async function reloadMarks() {
        if (!overlay) return;
        if (els.viewer && !document.body.contains(els.viewer)) return;   // reader closed → stale-listener no-op
        try { if (overlay.load) await overlay.load(); if (overlay.repaint) overlay.repaint(); } catch (e) {}
      }
      if (els.refreshBtn) els.refreshBtn.addEventListener('click', () => { closeAllPanels(); reloadMarks(); });
      document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') reloadMarks(); });

      // Pen options popover (Draw tool): colour swatches, width, pen/marker. Persisted device-local.
      if (els.penOpts) {
        const pen0 = getPen();
        const COLORS = ['#222222', '#e53935', '#1e88e5', '#43a047', '#fb8c00'];
        const sw = document.createElement('div'); sw.className = 'pen-swatches';
        COLORS.forEach(c => {
          const b = document.createElement('button'); b.className = 'hl-swatch'; b.style.background = c;
          b.addEventListener('click', () => { setPen({ color: c }); markPenUI(); });
          b.dataset.color = c; sw.appendChild(b);
        });
        const range = document.createElement('input');
        range.type = 'range'; range.min = '1'; range.max = '10'; range.value = String(pen0.width);
        range.className = 'pen-width';
        range.addEventListener('input', () => setPen({ width: +range.value }));
        const modeBtn = document.createElement('button'); modeBtn.className = 'pen-mode';
        const syncMode = () => { modeBtn.textContent = getPen().mode === 'marker' ? 'Marker' : 'Pen'; };
        modeBtn.addEventListener('click', () => { setPen({ mode: getPen().mode === 'marker' ? 'pen' : 'marker' }); syncMode(); });
        els.penOpts.append(sw, range, modeBtn);
        function markPenUI() {
          const cur = getPen().color;
          sw.querySelectorAll('.hl-swatch').forEach(b => b.classList.toggle('sel', b.dataset.color === cur));
        }
        syncMode(); markPenUI();
      }

      // Annotation list panel — every mark in the book; tap to jump, × to remove. Reuses the
      // bookmark-panel chrome over the overlay's indexed records (overlay.list()/.goto()).
      if (els.annBtn && els.annPanel) {
        const panel = els.annPanel;
        function renderAnns() {
          panel.innerHTML = '';
          const items = (overlay && overlay.list && overlay.list()) || [];
          if (!items.length) { panel.innerHTML = '<div class="bm-empty">No annotations yet.</div>'; return; }
          for (const it of items) {
            const row = document.createElement('div'); row.className = 'bm-row';
            const go = document.createElement('button'); go.className = 'bm-go'; go.textContent = it.label;
            go.addEventListener('click', () => { panel.classList.add('hidden'); try { it.go(); } catch (e) {} });
            const del = document.createElement('button'); del.className = 'bm-del'; del.textContent = '×';
            del.title = 'Remove'; del.setAttribute('aria-label', 'Remove annotation');
            del.addEventListener('click', async (e) => {
              e.stopPropagation();
              try { await opts.annotations.remove(it.a); } catch (x) {}
              if (overlay && overlay.repaint) overlay.repaint();
              renderAnns();
            });
            row.append(go, del); panel.appendChild(row);
          }
        }
        els.annBtn.addEventListener('click', () => {
          const wasOpen = !panel.classList.contains('hidden');
          closeAllPanels();
          if (!wasOpen) { panel.classList.remove('hidden'); renderAnns(); }
        });
      }
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { onExit(); }
      else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); ctrl.prev(); }
      else if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); ctrl.next(); }
      else if (e.key === '+' || e.key === '=') { ctrl.bigger(); }
      else if (e.key === '-' || e.key === '_') { ctrl.smaller(); }
    });

    // Swipe right → previous page, swipe left → next. preventDefault on a horizontal
    // drag also suppresses the browser's swipe-to-go-back. Attach to a document (EPUB
    // iframe) or an element (the PDF viewer); both support addEventListener.
    function attachSwipe(target) {
      let x0 = null, y0 = null, t0 = 0, horiz = false;
      target.addEventListener('touchstart', (e) => {
        if (penActive() || e.touches.length !== 1) { x0 = null; return; }   // palm while writing → ignore
        x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; t0 = Date.now(); horiz = false;
      }, { passive: true });
      target.addEventListener('touchmove', (e) => {
        if (x0 == null || e.touches.length !== 1) return;
        const dx = e.touches[0].clientX - x0, dy = e.touches[0].clientY - y0;
        if (!horiz && Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy) * 1.3) horiz = true;
        if (horiz && e.cancelable) e.preventDefault();   // block back-nav / vertical hijack
      }, { passive: false });
      target.addEventListener('touchend', (e) => {
        if (x0 == null) return;
        const t = e.changedTouches[0];
        const dx = t.clientX - x0, dy = t.clientY - y0, dt = Date.now() - t0;
        x0 = null;
        if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.3 && dt < 900) {
          if (dx > 0) ctrl.prev(); else ctrl.next();
        }
      }, { passive: true });
    }
    // PDF one-finger touch. In an edge RAIL we take FULL control of scrolling — preventDefault
    // and move only scrollTop (with a flick of inertia), never scrollLeft — so the page can't
    // drift sideways no matter how diagonal the swipe is. We can't lean on `touch-action:pan-y`
    // here because iOS Safari still scrolls horizontally on a diagonal drag of a wide element.
    // In the CENTRE, a horizontal-dominant drag pans; vertical stays native (keeps momentum).
    const RAIL = 44;
    function attachPdfPan(el) {
      let x0 = null, y0 = null, sl0 = 0, st0 = 0, decided = false, horiz = false, inRail = false;
      let vy = 0, lastY = 0, lastT = 0, raf = 0;
      const now = () => (window.performance && performance.now) ? performance.now() : Date.now();
      const stopInertia = () => { if (raf) { cancelAnimationFrame(raf); raf = 0; } };
      el.addEventListener('touchstart', (e) => {
        if (penActive() || e.touches.length !== 1) { x0 = null; return; }   // palm while writing → ignore
        stopInertia();
        const t = e.touches[0], x = t.clientX;
        inRail = x <= RAIL || x >= el.clientWidth - RAIL;
        x0 = x; y0 = t.clientY; sl0 = el.scrollLeft; st0 = el.scrollTop; decided = false; horiz = false;
        vy = 0; lastY = t.clientY; lastT = now();
      }, { passive: true });
      el.addEventListener('touchmove', (e) => {
        if (x0 == null || e.touches.length !== 1) return;
        const t = e.touches[0], dx = t.clientX - x0, dy = t.clientY - y0;
        if (inRail) {                          // dedicated vertical zone: we own the scroll
          if (e.cancelable) e.preventDefault();
          el.scrollTop = st0 - dy;             // never touches scrollLeft
          const tt = now(); if (tt > lastT) vy = (t.clientY - lastY) / (tt - lastT);
          lastY = t.clientY; lastT = tt;
          return;
        }
        if (!decided && (Math.abs(dx) > 6 || Math.abs(dy) > 6)) { decided = true; horiz = Math.abs(dx) > Math.abs(dy); }
        if (decided && horiz) { if (e.cancelable) e.preventDefault(); el.scrollLeft = sl0 - dx; }
      }, { passive: false });
      el.addEventListener('touchend', () => {
        if (inRail && Math.abs(vy) > 0.05) {   // flick → glide vertically, friction-decayed
          let v = vy;
          const step = () => { el.scrollTop -= v * 16; v *= 0.95;
            raf = (Math.abs(v) > 0.02) ? requestAnimationFrame(step) : 0; };
          raf = requestAnimationFrame(step);
        }
        x0 = null;
      }, { passive: true });
    }

    // ── PDF (PDF.js) ─────────────────────────────────────────────────────────
    async function startPdf() {
      document.body.classList.add('pdf');
      // Drop the edge rails below the top bar so they cover only the page area.
      const tbH = els.topbar ? els.topbar.offsetHeight : 0;
      document.querySelectorAll('.vrail').forEach(r => r.style.top = tbH + 'px');
      const pdfjsLib = await ReaderVendor.ensurePdfjs();
      let doc;
      showLoad('Opening PDF…');
      try {
        const task = pdfjsLib.getDocument(io.pdfSource());
        // pdf.js reports overall download bytes (range/stream mode) — show it until the document
        // is ready enough to render page 1 (the doc.promise resolves before the full file lands).
        task.onProgress = (p) => onLoadProgress(p.loaded || 0, p.total || 0);
        doc = await task.promise;
      }
      catch (e) { hideLoad(); return fail('Could not open this PDF.', e); }
      hideLoad();
      const numPages = doc.numPages;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);

      // PDFs always open at fit-to-width (userZoom 1). The buttons zoom IN from there
      // (zoom-out floors at fit-width); pinch goes finer. Not persisted — fit-width is the
      // starting view every time. Relabel the shared A−/A+ buttons as zoom controls.
      let userZoom = 1;
      const fdec = els.fdec, finc = els.finc;
      fdec.textContent = '−'; fdec.title = 'Zoom out'; fdec.setAttribute('aria-label', 'Zoom out');
      finc.textContent = '+'; finc.title = 'Zoom in';  finc.setAttribute('aria-label', 'Zoom in');
      const first = await doc.getPage(1);
      const base = first.getViewport({ scale: 1 });
      const targetW = () => Math.min(viewer.clientWidth - 16, 1100);
      let scale = targetW() / base.width * userZoom;

      // Pages live in a stack we can CSS-transform for a live pinch preview before
      // committing to a crisp re-render.
      const stack = document.createElement('div');
      stack.id = 'pdfStack';
      viewer.appendChild(stack);

      const pages = [];
      for (let i = 1; i <= numPages; i++) {
        const d = document.createElement('div');
        d.className = 'pdf-page'; d.dataset.page = i;
        d.style.width = Math.round(base.width * scale) + 'px';
        d.style.height = Math.round(base.height * scale) + 'px';
        stack.appendChild(d); pages.push(d);
      }

      // page n -> { canvas, task, textLayer }. Tracking the RenderTask lets us cancel an
      // in-flight render on zoom — otherwise a stale or cancelled render can leave a blank page.
      const rendered = new Map();
      let pdfPaintHl = null;                       // set by the highlight block; repaints a page
      // iOS Safari blanks any canvas over ~16M px² — cap the device-pixel ratio per page so
      // deep zoom degrades to slightly-soft instead of blank-white.
      const MAXDIM = 8192, MAXAREA = 16777216;
      async function render(d) {
        const n = +d.dataset.page;
        if (rendered.has(n)) return;
        const entry = {};
        rendered.set(n, entry);                    // claim the slot synchronously
        let page;
        try { page = await doc.getPage(n); }
        catch (e) { if (rendered.get(n) === entry) rendered.delete(n); return; }
        if (rendered.get(n) !== entry) return;     // cleared by a zoom while we awaited
        const vp = page.getViewport({ scale });
        let rdpr = Math.min(dpr, MAXDIM / vp.width, MAXDIM / vp.height,
                            Math.sqrt(MAXAREA / (vp.width * vp.height)));
        rdpr = Math.max(0.75, rdpr);
        const canvas = document.createElement('canvas');
        canvas.width = Math.floor(vp.width * rdpr);
        canvas.height = Math.floor(vp.height * rdpr);
        d.style.width = Math.round(vp.width) + 'px';
        d.style.height = Math.round(vp.height) + 'px';
        d.appendChild(canvas);
        entry.canvas = canvas;
        entry.task = page.render({ canvasContext: canvas.getContext('2d'),
                                   viewport: page.getViewport({ scale: scale * rdpr }) });
        entry.task.promise.catch(() => {});        // cancel() rejects — swallow it
        // A selectable text layer over the canvas — what makes text selection + highlighting
        // possible. Positioned in CSS px (the `scale` viewport, not the dpr-scaled one).
        if (pdfjsLib.TextLayer) {
          const tdiv = document.createElement('div'); tdiv.className = 'textLayer';
          tdiv.style.setProperty('--scale-factor', String(scale));
          d.appendChild(tdiv);
          try {
            entry.textLayer = new pdfjsLib.TextLayer(
              { textContentSource: page.streamTextContent(), container: tdiv, viewport: vp });
            entry.textLayer.render();
          } catch (e) {}
        }
        if (pdfPaintHl) pdfPaintHl(d);             // (re)apply any highlight overlay for this page
      }
      function clearPage(n) {
        const e = rendered.get(n);
        if (!e) return;
        if (e.task) { try { e.task.cancel(); } catch (x) {} }
        if (e.textLayer) { try { e.textLayer.cancel(); } catch (x) {} }
        if (e.canvas) e.canvas.remove();
        const d = pages[n - 1];                    // drop the text layer + highlight overlay too
        if (d) d.querySelectorAll('.textLayer, .pdf-hl, .pdf-ul, .pdf-strike, .pdf-ink, .pdf-note').forEach(x => x.remove());
        rendered.delete(n);
      }
      function unload(d) { clearPage(+d.dataset.page); }
      const ioObs = new IntersectionObserver((entries) => {
        for (const e of entries) { if (e.isIntersecting) render(e.target); else unload(e.target); }
      }, { root: viewer, rootMargin: '800px 0px' });
      pages.forEach(p => ioObs.observe(p));

      // Current page = the page occupying the viewport's vertical centre, computed from scroll
      // geometry. NOT an IntersectionObserver threshold: a >0.5-visible test never fires for a
      // page taller than the window (common at fit-to-width), which would pin the page at 1 —
      // giving every bookmark the label "Page 1" and freezing the progress %.
      let cur = 1;
      function updateCur() {
        const mid = viewer.scrollTop + viewer.clientHeight / 2;
        let p = 1;
        for (const d of pages) { if (d.offsetTop <= mid) p = +d.dataset.page; else break; }
        cur = p;
        queueSave(cur, cur / numPages);
      }
      let curRaf = 0;
      viewer.addEventListener('scroll', () => {
        if (curRaf) return;                       // coalesce scroll bursts to one rAF
        curRaf = requestAnimationFrame(() => { curRaf = 0; updateCur(); });
      }, { passive: true });

      // Resize every page div to the current scale; cancel + drop every canvas.
      function applyScale() {
        scale = targetW() / base.width * userZoom;
        for (const d of pages) {
          clearPage(+d.dataset.page);
          d.style.width = Math.round(base.width * scale) + 'px';
          d.style.height = Math.round(base.height * scale) + 'px';
        }
      }
      function renderVisible() {
        const r = viewer.getBoundingClientRect();
        pages.forEach(p => { const b = p.getBoundingClientRect();
          if (b.bottom > r.top - 800 && b.top < r.bottom + 800) render(p); });
      }
      const clamp01 = (v) => Math.min(1, Math.max(0, v));
      // Zoom + crisp re-render, keeping the content under (ax,ay) — viewport-relative —
      // fixed. Vertical anchor is per-page (offsetTop) so the non-scaling 12px gaps don't
      // make it drift; horizontal is re-derived from the centred stack so the page never
      // lands off-centre. Both default to the viewport centre (the A−/A+ buttons).
      function zoomTo(z, ax, ay) {
        userZoom = Math.max(1, Math.min(3, z));   // never smaller than fit-to-width
        ax = (ax == null) ? viewer.clientWidth / 2 : ax;
        ay = (ay == null) ? viewer.clientHeight / 2 : ay;
        const cY = viewer.scrollTop + ay;
        let ad = pages[0], fY = 0;
        for (const d of pages) {
          if (d.offsetTop + d.offsetHeight > cY) { ad = d; fY = clamp01((cY - d.offsetTop) / (d.offsetHeight || 1)); break; }
          ad = d;
        }
        const idx = +ad.dataset.page;
        const w0 = ad.offsetWidth || 1, left0 = Math.max(0, (stack.offsetWidth - w0) / 2);
        const fX = (viewer.scrollLeft + ax - left0) / w0;
        applyScale();
        const ad2 = pages[idx - 1];
        const w1 = ad2.offsetWidth || 1, left1 = Math.max(0, (stack.offsetWidth - w1) / 2);
        viewer.scrollTop  = Math.max(0, ad2.offsetTop + fY * ad2.offsetHeight - ay);
        viewer.scrollLeft = Math.max(0, left1 + fX * w1 - ax);
        renderVisible();
      }

      // Reflow-to-text: the CURRENT page's text as paragraphs (shared LibraryCore.reflowPageText) — a
      // phone reading mode. Hides the page stack while on; the pager turns pages + re-renders.
      let reflowDiv = null, reflowFontPx = 18;
      const reflowIsOn = () => !!reflowDiv && !reflowDiv.classList.contains('hidden');
      async function pageRawText(n) {
        const page = await doc.getPage(n);
        const tc = await page.getTextContent();
        const lines = []; let line = '', y = null;
        for (const it of tc.items) {
          if (typeof it.str !== 'string') continue;
          const ty = it.transform ? Math.round(it.transform[5]) : y;   // group items into lines by y
          if (y !== null && ty !== null && Math.abs(ty - y) > 2) { lines.push(line); line = ''; }
          y = ty; line += it.str;
          if (it.hasEOL) { lines.push(line); line = ''; y = null; }
        }
        if (line) lines.push(line);
        return lines.join('\n');
      }
      async function renderReflow() {
        const raw = await pageRawText(cur);
        const paras = window.LibraryCore ? LibraryCore.reflowPageText(raw) : (raw ? [raw] : []);
        reflowDiv.innerHTML = '';
        if (!paras.length) {
          const e = document.createElement('p'); e.className = 'reflow-empty';
          e.textContent = 'No extractable text on this page — it may be a scan.';
          reflowDiv.appendChild(e);
        }
        paras.forEach((p) => { const el = document.createElement('p'); el.textContent = p; reflowDiv.appendChild(el); });
      }
      ctrl.reflow = async (on) => {
        if (!reflowDiv) {
          reflowDiv = document.createElement('div'); reflowDiv.className = 'reflow hidden';
          reflowDiv.style.fontSize = reflowFontPx + 'px'; viewer.appendChild(reflowDiv);
        }
        if (on === undefined) on = !reflowIsOn();
        if (on) { await renderReflow(); reflowDiv.classList.remove('hidden'); stack.style.display = 'none'; }
        else { reflowDiv.classList.add('hidden'); stack.style.display = ''; }
        return on;
      };

      ctrl.prev = () => { if (reflowIsOn()) { if (cur > 1) { cur--; renderReflow(); reflowDiv.scrollTop = 0; } }
        else viewer.scrollBy({ top: -(viewer.clientHeight - 48), behavior: 'smooth' }); };
      ctrl.next = () => { if (reflowIsOn()) { if (cur < numPages) { cur++; renderReflow(); reflowDiv.scrollTop = 0; } }
        else viewer.scrollBy({ top: (viewer.clientHeight - 48), behavior: 'smooth' }); };
      ctrl.bigger = () => { if (reflowIsOn()) { reflowFontPx = Math.min(28, reflowFontPx + 2); reflowDiv.style.fontSize = reflowFontPx + 'px'; }
        else zoomTo(Math.min(3, userZoom + 0.15)); };
      ctrl.smaller = () => { if (reflowIsOn()) { reflowFontPx = Math.max(12, reflowFontPx - 2); reflowDiv.style.fontSize = reflowFontPx + 'px'; }
        else zoomTo(Math.max(1, userZoom - 0.15)); };   // zoom-out stops at fit-width
      ctrl.fitWidth = () => { if (!reflowIsOn()) zoomTo(1); };   // fit-to-width is userZoom 1
      // Bookmark a PDF spot by page number (the same opaque locator the position save uses).
      ctrl.current = () => ({ locator: String(cur), fraction: cur / numPages, label: 'Page ' + cur });
      ctrl.goto = (loc) => { const n = parseInt(loc, 10);
        if (n >= 1 && n <= numPages) pages[n - 1].scrollIntoView(); };
      ctrl.kind = 'pdf'; ctrl.pageCount = numPages;   // for the typed "Go to page 1…N" control
      // Outline → flat [{label, depth, go}] resolving each entry's dest to a page. Built lazily
      // (one getOutline) and memoised so reopening the panel is instant.
      let tocCache = null;
      ctrl.toc = async () => {
        if (tocCache) return tocCache;
        const out = [];
        const jump = (dest) => async () => {
          try {
            const d = typeof dest === 'string' ? await doc.getDestination(dest) : dest;
            if (d && d[0]) { const i = await doc.getPageIndex(d[0]); if (pages[i]) pages[i].scrollIntoView(); }
          } catch (e) {}
        };
        const walk = (nodes, depth) => {
          for (const o of nodes || []) {
            out.push({ label: (o.title || '').trim(), depth, go: jump(o.dest) });
            if (o.items && o.items.length) walk(o.items, depth + 1);
          }
        };
        try { walk(await doc.getOutline(), 0); } catch (e) {}
        tocCache = out;
        return out;
      };
      // Theme a PDF (page canvases are images): night = invert for reading in the dark; sepia = warm
      // tint; white/gray leave the page as-is (the frame/gutter is themed by reader-themes.css).
      ctrl.setTheme = (t) => {
        stack.style.filter = t === 'night' ? 'invert(1) hue-rotate(180deg)'
          : t === 'sepia' ? 'sepia(0.55)' : '';
      };
      ctrl.setTheme(document.body.getAttribute('data-reader-theme') || 'white');   // apply persisted now
      // In-book search over the text layer. Bounded (cap hits) and yields every few pages so a
      // big book doesn't freeze the UI. Each hit jumps to its page.
      ctrl.search = async (q) => {
        const needle = (q || '').toLowerCase();
        if (!needle) return [];
        const out = [];
        for (let n = 1; n <= numPages && out.length < 80; n++) {
          let text = '';
          try { const pg = await doc.getPage(n);
            text = (await pg.getTextContent()).items.map(i => i.str).join(' '); }
          catch (e) { continue; }
          const hay = text.toLowerCase();
          let idx = hay.indexOf(needle);
          while (idx >= 0 && out.length < 80) {
            const snip = text.slice(Math.max(0, idx - 30), idx + needle.length + 30)
              .replace(/\s+/g, ' ').trim();
            out.push({ label: 'p.' + n + ' — …' + snip + '…',
              go: ((p) => () => { if (pages[p - 1]) pages[p - 1].scrollIntoView(); })(n) });
            idx = hay.indexOf(needle, idx + needle.length);
          }
          if (n % 8 === 0) await new Promise(r => setTimeout(r, 0));   // breathe
        }
        return out;
      };
      attachPdfPan(viewer);

      // ── Annotations (PDF) — delegated to the L2 overlay ─────────────────────
      // Highlights/underline/strikeout/notes/ink all live in overlay.js (PdfOverlay); the engine
      // only feeds it the page geometry + the host seam, and repaints a page after (re)render.
      // Annotation overlay is built ONLY when the ✍ toggle arms it — never on the reading-open
      // path. Arming loads the ink library + the holding's marks, wires the per-page paint hook,
      // and repaints the pages already on screen.
      if (overlaySeam && window.ReaderOverlay) armAnnotate = async () => {
        if (overlay) return;
        const fh = await ReaderVendor.ensureFreehand().catch(() => null);
        overlay = ReaderOverlay.create('pdf', Object.assign({
          viewer, pages: () => pages, freehand: fh, goto: (loc) => ctrl.goto(loc),
        }, overlaySeam));
        overlay.setTool(tool); overlay.setPen(getPen());
        pdfPaintHl = (d) => overlay.pageRendered(d);   // future page renders repaint marks
        await overlay.load();                           // index + paint persisted marks
        overlay.repaint();                              // paint the pages already rendered
      };

      // ── Pinch to zoom: live CSS-transform preview, crisp re-render on lift ────
      let pinch = null;
      const dist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      viewer.addEventListener('touchstart', (e) => {
        if (penActive() || e.touches.length !== 2) return;
        const r = viewer.getBoundingClientRect();
        const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - r.left;
        const my = (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top;
        pinch = { d0: dist(e.touches[0], e.touches[1]), base: userZoom, ratio: 1,
                  cx: viewer.scrollLeft + mx, cy: viewer.scrollTop + my, mx, my };
        stack.style.transformOrigin = pinch.cx + 'px ' + pinch.cy + 'px';
      }, { passive: true });
      viewer.addEventListener('touchmove', (e) => {
        if (!pinch || e.touches.length !== 2) return;
        if (e.cancelable) e.preventDefault();
        const want = Math.max(1, Math.min(3, pinch.base * dist(e.touches[0], e.touches[1]) / pinch.d0));
        pinch.ratio = want / pinch.base;
        stack.style.transform = 'scale(' + pinch.ratio + ')';
      }, { passive: false });
      function endPinch() {
        if (!pinch) return;
        const p = pinch; pinch = null;
        stack.style.transform = ''; stack.style.transformOrigin = '';
        const finalZoom = Math.max(1, Math.min(3, p.base * p.ratio));
        if (Math.abs(finalZoom - p.base) > 0.005) zoomTo(finalZoom, p.mx, p.my);
      }
      viewer.addEventListener('touchend', (e) => { if (pinch && e.touches.length < 2) endPinch(); }, { passive: true });
      viewer.addEventListener('touchcancel', endPinch, { passive: true });

      // Restore: explicit #page=N wins, else the saved page.
      const hash = (location.hash.match(/page=(\d+)/) || [])[1];
      const saved = await getSaved();
      const start = Math.max(1, Math.min(numPages,
        parseInt(hash || saved.locator || '1', 10) || 1));
      cur = start;                                // so ctrl.current() is right before any scroll
      if (start > 1) pages[start - 1].scrollIntoView();
      progEl.textContent = Math.round(start / numPages * 100) + '%';

      let rt;
      window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => zoomTo(userZoom), 200); });
    }

    // ── EPUB (epub.js) ───────────────────────────────────────────────────────
    async function startEpub() {
      document.body.classList.add('epub');
      attachSwipe(viewer);   // swipes on the frame around the iframe (chevrons stay too)
      const ePub = await ReaderVendor.ensureEpub();
      // epub.js decides archive-vs-directory by file EXTENSION; our `/holding/<id>/file`
      // has none, so a URL hands it the wrong mode and it silently never opens. Fetch the
      // bytes and open from the ArrayBuffer (unambiguous archive) instead.
      let buf;
      // The whole EPUB must arrive before epub.js can render anything (the zip is read whole), so
      // show download progress — otherwise a big book over the tunnel is just a blank page.
      showLoad('Loading book…');
      try { buf = await io.epubData(onLoadProgress); }
      catch (e) { hideLoad(); return fail('Could not load this EPUB.', e); }
      showLoad('Opening book…');
      let book, rendition;
      try {
        book = ePub(buf);
        rendition = book.renderTo(viewer, { width: '100%', height: '100%',
          flow: 'paginated', spread: 'auto', allowScriptedContent: true });
      } catch (e) { hideLoad(); return fail('Could not open this EPUB.', e); }

      // Font size — reflows the text; persisted across books. `themes.fontSize` alone only sizes <body>,
      // so a book that hard-codes font-size on its text elements changes only spacing (not glyphs); we
      // ALSO inject a stylesheet into each rendered section forcing text elements to inherit the scaled
      // base, so the letters actually resize.
      let fontPct = parseInt(localStorage.getItem('readerEpubFontPct')) || 100;
      const applyFont = () => {
        const css = 'html,body{font-size:' + fontPct + '% !important;}' +
          'p,li,dd,dt,blockquote,span,a,em,strong,i,b,small,sub,sup,td,th,cite,q,figcaption,label' +
          '{font-size:inherit !important;}';
        try {
          rendition.themes.fontSize(fontPct + '%');
          (rendition.getContents() || []).forEach((c) => {
            try {
              const doc = c && c.document; if (!doc) return;
              const prev = doc.getElementById('rc-font'); if (prev) prev.remove();
              const st = doc.createElement('style'); st.id = 'rc-font'; st.textContent = css;
              (doc.head || doc.documentElement).appendChild(st);
            } catch (e) {}
          });
        } catch (e) {}
      };
      const setFont = (p) => { fontPct = Math.max(60, Math.min(250, p));
        localStorage.setItem('readerEpubFontPct', fontPct); applyFont(); };
      applyFont();
      rendition.on('rendered', () => { if (fontPct !== 100) applyFont(); });   // new sections start fresh

      ctrl.prev = () => rendition.prev();
      ctrl.next = () => rendition.next();
      ctrl.bigger = () => setFont(fontPct + 10);
      ctrl.smaller = () => setFont(fontPct - 10);
      // Bookmark an EPUB spot by CFI (updated below on every relocate); label by progress %.
      // epub.js only fills loc.start.percentage once book.locations is generated — so we
      // generate it (below) and otherwise compute the percentage of the current page's START
      // CFI ourselves. Until that lands, label by chapter href so it's never a bare "0%".
      let curCfi = null, curFrac = null, curLabel = null, locationsReady = false;
      ctrl.current = () => ({ locator: curCfi, fraction: curFrac,
        label: (curFrac != null ? Math.round(curFrac * 100) + '%' : (curLabel || 'Bookmark')) });
      ctrl.goto = (loc) => { if (loc) rendition.display(loc).catch(() => {}); };
      ctrl.kind = 'epub';
      ctrl.gotoFraction = (f) => {                     // "Go to N%" — needs generated locations
        try {
          if (book.locations && book.locations.length && book.locations.length()) {
            const cfi = book.locations.cfiFromPercentage(Math.max(0, Math.min(1, f)));
            if (cfi) rendition.display(cfi).catch(() => {});
          }
        } catch (e) {}
      };
      // Theme the EPUB content — pull the colours from the generated reader-theme CSS vars (set by
      // reader-themes.css on body[data-reader-theme]) so EPUB matches palette.json, the single source.
      ctrl.setTheme = () => {
        const cs = getComputedStyle(document.body);
        const bg = (cs.getPropertyValue('--reader-bg') || '#ffffff').trim();
        const fg = (cs.getPropertyValue('--reader-fg') || '#111111').trim();
        try { rendition.themes.override('color', fg); rendition.themes.override('background', bg); }
        catch (e) {}
      };
      ctrl.setTheme();   // apply persisted now (body[data-reader-theme] already set by applyTheme)
      // In-book search: load each spine section, find the query (epub.js Section.find →
      // [{cfi, excerpt}]), unload it. Bounded; each hit displays its CFI.
      ctrl.search = async (q) => {
        const needle = (q || '').trim();
        if (!needle) return [];
        const out = [];
        for (const item of (book.spine && book.spine.spineItems) || []) {
          if (out.length >= 80) break;
          try {
            await item.load(book.load.bind(book));
            for (const r of (item.find(needle) || [])) {
              if (out.length >= 80) break;
              out.push({ label: (r.excerpt || needle).replace(/\s+/g, ' ').trim(),
                go: ((cfi) => () => rendition.display(cfi).catch(() => {}))(r.cfi) });
            }
          } catch (e) {}
          finally { try { item.unload(); } catch (e) {} }
        }
        return out;
      };

      // ── Annotations (EPUB) — delegated to the L2 overlay ────────────────────
      // Highlights/underline (CFI-anchored, epub.js native) + best-effort ink + notes all live in
      // overlay.js (EpubOverlay). The engine feeds it the rendition/book handles + the host seam,
      // and pings overlay.relocated() on page turns so the spine-anchored ink repaints. The
      // current spine index for ink anchoring is tracked off `relocated` (see below).
      let curSpine = 0;
      // Built only when the ✍ toggle arms it (see PDF note) — never on the reading-open path, so
      // the EPUB's first rendition.display() is never blocked by the annotation layer.
      if (overlaySeam && window.ReaderOverlay) armAnnotate = async () => {
        if (overlay) return;
        const fh = await ReaderVendor.ensureFreehand().catch(() => null);
        overlay = ReaderOverlay.create('epub', Object.assign({
          viewer, rendition: () => rendition, book: () => book, freehand: fh,
          currentCfi: () => curCfi, currentSpine: () => curSpine,
        }, overlaySeam));
        overlay.setTool(tool); overlay.setPen(getPen());
        await overlay.load();
      };

      // EPUB text renders inside an iframe, so the parent's swipe listener never sees
      // those touches — attach to each rendered section's document.
      try { rendition.hooks.content.register((contents) => attachSwipe(contents.document)); }
      catch (e) {}

      // Chapter labels (filename → title) from the TOC, so a bookmark made before locations are
      // generated still reads as e.g. "Chapter Two" rather than a bare "0%". Keyed by FILENAME
      // (not the raw href) because a TOC href ("../text/ch2.xhtml") and the relocate href
      // ("text/ch2.xhtml") differ by directory — basename is the stable join key.
      const fileKey = (h) => (h || '').split('#')[0].split('/').pop();
      let chapterByHref = {};
      book.loaded.navigation.then((nav) => {
        const walk = (items) => (items || []).forEach((it) => {
          if (it.href) chapterByHref[fileKey(it.href)] = (it.label || '').trim();
          if (it.subitems) walk(it.subitems);
        });
        walk(nav.toc);
      }).catch(() => {});
      // Navigate to a TOC href. Robust because real books' TOC hrefs don't always match a
      // spine href directly (a nav doc in a different folder yields '../text/x.xhtml', or there
      // are leading './' / '/' / encoding quirks) — and a display() that can't resolve the href
      // rejects silently, so the link "does nothing". Try the href as-is first (keeps the
      // in-section #fragment), then fall back to resolving a spine section by progressively
      // looser keys and navigating by its index.
      const epubGo = (href) => {
        if (!href) return;
        let p; try { p = rendition.display(href); } catch (e) { p = Promise.reject(e); }
        Promise.resolve(p).catch(() => {
          const bare = href.split('#')[0];
          const cands = [bare, bare.replace(/^\.\//, ''), bare.replace(/^\/+/, ''),
                         bare.replace(/^(\.\.\/)+/, ''), decodeURIComponent(bare)];
          for (const c of cands) {
            let sec = null; try { sec = book.spine.get(c); } catch (e) {}
            if (sec) { try { rendition.display(sec.index).catch(() => {}); } catch (e) {} return; }
          }
          // Last resort: match by FILENAME across the spine — handles any directory mismatch
          // between the TOC document's hrefs and the spine's (the common real-world break).
          const base = bare.split('/').pop();
          const items = (book.spine && book.spine.spineItems) || [];
          const m = items.find(s => ((s.href || '').split('/').pop() === base)
                                  || ((s.url || '').split('/').pop() === base));
          if (m) { try { rendition.display(m.index).catch(() => {}); } catch (e) {} }
        });
      };
      // IN-CONTENT links (a TOC page inside the book, footnotes, cross-refs): intercept the click and
      // route it through the robust epubGo, so a raw sub-frame navigation (which WebKit cancels with
      // error 102 → "link does nothing", esp. in an iOS WKWebView) never happens.
      rendition.hooks.content.register((contents) => {
        contents.document.addEventListener('click', (e) => {
          const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
          if (!a) return;
          const href = a.getAttribute('href') || '';
          if (/^(https?:|mailto:|tel:)/i.test(href) || !href || href.charAt(0) === '#') return;
          e.preventDefault(); e.stopPropagation();
          recordBackTarget();                         // in-content link is a jump → offer "Back"
          epubGo(href);
        }, false);
      });
      ctrl.toc = async () => {
        let nav;
        try { nav = await book.loaded.navigation; } catch (e) { return []; }
        const out = [];
        const walk = (items, depth) => (items || []).forEach((it) => {
          out.push({ label: (it.label || '').trim(), depth, go: () => epubGo(it.href) });
          if (it.subitems) walk(it.subitems, depth + 1);
        });
        walk(nav.toc, 0);
        return out;
      };

      // Generate locations so percentage is REAL (epub.js leaves loc.start.percentage at 0
      // until this runs). Background; when ready, refresh from the last-known CFI.
      book.ready.then(() => book.locations.generate(1600)).then(() => {
        locationsReady = true;
        if (curCfi) {
          const p = book.locations.percentageFromCfi(curCfi);
          if (p != null && p >= 0) { curFrac = p; progEl.textContent = Math.round(p * 100) + '%'; }
        }
      }).catch(() => {});

      const saved = await getSaved();
      // Hide the loading overlay once the first section has actually painted.
      rendition.display(saved.locator || undefined)
        .catch(() => rendition.display())
        .finally(hideLoad);

      rendition.on('relocated', (loc) => {
        const start = loc && loc.start;
        const cfi = start && start.cfi;
        if (!cfi) return;
        curCfi = cfi;                                    // the live spot of the CURRENT page
        const ck = fileKey(start.href);
        if (chapterByHref[ck]) curLabel = chapterByHref[ck];
        // Track the spine index (ink is anchored to it) + repaint the spine's ink for this page.
        if (start.index != null) curSpine = start.index;
        else { try { const si = book.spine.get(start.href); if (si) curSpine = si.index; } catch (e) {} }
        if (overlay && overlay.relocated) overlay.relocated();
        // Percentage of the current page's start CFI. Use epub.js's own value only when it's
        // been populated (> 0); otherwise derive it from generated locations once ready.
        let frac = null;
        if (start.percentage != null && start.percentage > 0) frac = start.percentage;
        else if (locationsReady) {
          const p = book.locations.percentageFromCfi(cfi);
          if (p != null && p >= 0) frac = p;
        }
        if (frac != null) curFrac = frac;
        queueSave(cfi, (frac != null && frac > 0) ? frac : null);
      });
      // Arrow keys pressed while focus is inside the epub iframe.
      rendition.on('keyup', (e) => {
        if (e.key === 'ArrowLeft') ctrl.prev(); else if (e.key === 'ArrowRight') ctrl.next();
      });
    }

    if (EXT === 'epub') startEpub();
    else startPdf();   // pdf + anything else PDF.js can open
  }

  window.ReaderCore = { mount };
})();
