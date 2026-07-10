/* reader-overlay — the format-agnostic annotation capture + render layer (L2 of
 * reader_module_plan.md §3.1 "overlay.js"). This is the SWAP POINT for renderer changes:
 * the engine (reader-core.js, L1) is renderer-agnostic and talks ONLY to the interface below;
 * PDF.js / epub.js / perfect-freehand live here, behind it. Swapping epub.js→foliate-js, or the
 * ink renderer, touches only an implementation in this file — never the engine.
 *
 *   const ov = ReaderOverlay.create(format, ctx)   // format: 'pdf' | 'epub'
 *
 * The interface every implementation returns:
 *   setTool(tool)            // 'select'|'highlight'|'underline'|'strikeout'|'note'|'ink'|'erase'
 *   setPen({color,width,mode})  // mode: 'pen' | 'marker'
 *   tool()                   // current tool (the engine reads it to decide gesture yield)
 *   supports(kind)           // capability probe — engine hides unsupported tool buttons
 *   load()                   // async: pull persisted annotations, index + paint them
 *   list()                   // [{a, label, go()}] for the annotation-list panel
 *   pageRendered(div)        // PDF only: (re)paint marks after a page (re)renders
 *   repaint()                // re-render visible marks (e.g. after a PDF zoom)
 *
 * `ctx` carries the host seams the overlay needs (persistence + the engine's popups + shared
 * input state), so the overlay never talks to Flask, IndexedDB, or the toolbar directly:
 *   annotations  — the persistence ADAPTER (add/list/update/remove; L3, host-supplied)
 *   freehand     — getStroke (perfect-freehand); loaded by the host when ANNOTATE MODE is armed
 *                  (never on the reading-only open path), so reading has zero ink-layer overhead
 *   pickColor(cb)            — open the engine's colour picker; cb(color) on choose
 *   editExisting(a, onRemove)— open the engine's note/remove popup for an existing mark
 *   penActive()              — shared palm-rejection state owned by the engine
 *   getPenDefault()          — last pen {color,width,mode}
 *  PDF:  { viewer, pages() , annotations, freehand, pickColor, editExisting, penActive }
 *  EPUB: { viewer, rendition(), book(), annotations, freehand, pickColor, editExisting,
 *          penActive, currentCfi(), currentSpine() }
 */
'use strict';
(function () {

  // ── shared: build an SVG path string from a perfect-freehand outline polygon ──
  function svgPathFromStroke(pts) {
    if (!pts || !pts.length) return '';
    const d = pts.reduce((acc, p0, i, arr) => {
      const p1 = arr[(i + 1) % arr.length];
      acc.push(p0[0], p0[1], (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2);
      return acc;
    }, ['M', pts[0][0], pts[0][1], 'Q']);
    d.push('Z');
    return d.join(' ');
  }

  const NS = 'http://www.w3.org/2000/svg';
  const DEFAULT_COLORS = { highlight: '#ffd54a', underline: '#90caf9',
                           strikeout: '#f48fb1', ink: '#222222', note: '#ffd54a' };

  // ════════════════════════════════════════════════════════════════════════════
  // PDF overlay — PDF.js text layer (text marks) + per-page SVG (ink). All marks
  // anchor to page + normalised coords (0..1 of the page box) so they survive zoom.
  // ════════════════════════════════════════════════════════════════════════════
  function pdfOverlay(ctx) {
    let tool = 'select';
    let pen = ctx.getPenDefault ? ctx.getPenDefault() : { color: '#222', width: 3, mode: 'pen' };
    const byPage = new Map();           // page -> [annotation]  (highlight/underline/strikeout)
    const inkByPage = new Map();        // page -> [annotation]  (kind:'ink')
    const noteByPage = new Map();       // page -> [annotation]  (kind:'note')
    const all = new Map();              // id -> annotation (for the list panel)

    const pageW = (d) => d.offsetWidth || 1, pageH = (d) => d.offsetHeight || 1;
    const pageDivFromEl = (el) => el && el.closest && el.closest('.pdf-page');
    const pageNo = (d) => +d.dataset.page;

    function index(a) {
      if (!a || a.deleted_at) return;
      all.set(a.id, a);
      if (a.kind === 'ink' && a.page != null) {
        if (!inkByPage.has(a.page)) inkByPage.set(a.page, []);
        inkByPage.get(a.page).push(a);
      } else if (a.kind === 'note' && a.page != null) {
        if (!noteByPage.has(a.page)) noteByPage.set(a.page, []);
        noteByPage.get(a.page).push(a);
      } else if (a.page != null && a.rect &&
                 (a.kind === 'highlight' || a.kind === 'underline' || a.kind === 'strikeout')) {
        if (!byPage.has(a.page)) byPage.set(a.page, []);
        byPage.get(a.page).push(a);
      }
    }
    function deindex(a) {
      all.delete(a.id);
      for (const m of [byPage, inkByPage, noteByPage]) {
        const arr = m.get(a.page); if (!arr) continue;
        const i = arr.indexOf(a); if (i >= 0) arr.splice(i, 1);
      }
    }

    // ── paint one page: text marks as boxes/lines, ink as an SVG ──────────────
    function paintPage(d) {
      const n = pageNo(d);
      d.querySelectorAll('.pdf-hl, .pdf-ul, .pdf-strike, .pdf-ink, .pdf-note').forEach(x => x.remove());
      const W = pageW(d), H = pageH(d);
      for (const a of (byPage.get(n) || [])) {
        let rects; try { rects = JSON.parse(a.rect); } catch (e) { continue; }
        for (const r of rects) {
          const box = document.createElement('div');
          if (a.kind === 'highlight') {
            box.className = 'pdf-hl';
            box.style.left = (r[0] * W) + 'px'; box.style.top = (r[1] * H) + 'px';
            box.style.width = (r[2] * W) + 'px'; box.style.height = (r[3] * H) + 'px';
            box.style.background = a.color || DEFAULT_COLORS.highlight;
          } else {                                   // underline / strikeout: a thin line
            box.className = a.kind === 'strikeout' ? 'pdf-strike' : 'pdf-ul';
            const yy = a.kind === 'strikeout' ? (r[1] + r[3] / 2) : (r[1] + r[3]);
            box.style.left = (r[0] * W) + 'px'; box.style.top = (yy * H) + 'px';
            box.style.width = (r[2] * W) + 'px';
            box.style.background = a.color || DEFAULT_COLORS[a.kind];
          }
          d.appendChild(box);
        }
      }
      const inks = inkByPage.get(n);
      if (inks && inks.length) {
        const svg = inkSvg(d);
        for (const a of inks) paintInk(svg, a, W, H);
      }
      for (const a of (noteByPage.get(n) || [])) {
        let pt; try { pt = JSON.parse(a.rect); } catch (e) { pt = [0.5, 0.5]; }
        const pin = document.createElement('button');
        pin.className = 'pdf-note'; pin.textContent = '🅝';
        pin.title = a.note_text || 'Note';
        pin.style.left = ((pt[0] || 0.5) * W) + 'px'; pin.style.top = ((pt[1] || 0.5) * H) + 'px';
        pin.addEventListener('click', (e) => {
          e.stopPropagation();
          ctx.editExisting(a, {
            onRemove: async () => { deindex(a); paintPage(d); try { await ctx.annotations.remove(a); } catch (x) {} },
            onSave: async (text) => { try { await ctx.annotations.update(a, { note_text: text }); a.note_text = text; } catch (x) {} paintPage(d); },
          });
        });
        d.appendChild(pin);
      }
    }
    function inkSvg(d) {
      let svg = d.querySelector('svg.pdf-ink');
      if (!svg) {
        svg = document.createElementNS(NS, 'svg');
        svg.setAttribute('class', 'pdf-ink');
        svg.setAttribute('preserveAspectRatio', 'none');
        d.appendChild(svg);
      }
      svg.setAttribute('viewBox', `0 0 ${pageW(d)} ${pageH(d)}`);
      return svg;
    }
    function paintInk(svg, a, W, H) {
      let data; try { data = JSON.parse(a.ink); } catch (e) { return; }
      for (const st of (data.strokes || [])) {
        const px = (st.points || []).map(p => [p[0] * W, p[1] * H, p[2] == null ? 0.5 : p[2]]);
        const size = (st.width || 0.004) * W * (st.mode === 'marker' ? 2.4 : 1);
        const outline = ctx.freehand(px, { size, thinning: st.mode === 'marker' ? 0.2 : 0.6,
                                           smoothing: 0.6, streamline: 0.5 });
        const path = document.createElementNS(NS, 'path');
        path.setAttribute('d', svgPathFromStroke(outline));
        path.setAttribute('fill', st.color || DEFAULT_COLORS.ink);
        if (st.mode === 'marker') path.setAttribute('fill-opacity', '0.4');
        path.dataset.id = a.id;
        svg.appendChild(path);
      }
    }

    const visiblePages = () => Array.from(ctx.viewer.querySelectorAll('.pdf-page'))
      .filter(p => p.querySelector('canvas'));
    function repaint() { visiblePages().forEach(paintPage); }

    // ── text-mark capture: a selection over the text layer → kind per active tool ──
    function rectsFromSelection(sel) {
      const range = sel.getRangeAt(0);
      const s = range.startContainer;
      const el = s.nodeType === 1 ? s : s.parentNode;
      const pageDiv = pageDivFromEl(el);
      if (!pageDiv) return null;
      const pr = pageDiv.getBoundingClientRect(), W = pr.width, H = pr.height, rects = [];
      for (const cr of range.getClientRects()) {
        if (cr.width < 1 || cr.height < 1) continue;
        const x = (cr.left - pr.left) / W, y = (cr.top - pr.top) / H;
        if (x < -0.05 || x > 1.05 || y < -0.05 || y > 1.05) continue;
        rects.push([Math.max(0, x), Math.max(0, y), cr.width / W, cr.height / H]);
      }
      return rects.length ? { pageDiv, rects } : null;
    }

    async function createTextMark(kind, pageDiv, rects, color) {
      let saved = null;
      try {
        saved = await ctx.annotations.add(
          { kind, page: pageNo(pageDiv), rect: JSON.stringify(rects), color });
      } catch (e) {}
      if (saved) { index(saved); paintPage(pageDiv); }
    }

    // Highlight is SELECTION-based (precise range + colour pick). Underline/strikeout are
    // DRAG-band based (see below) so a pencil drag under text becomes a real text underline —
    // selection is unreliable for an Apple Pencil. So mouseup here handles only highlight + the
    // select-mode click-to-edit.
    ctx.viewer.addEventListener('mouseup', (ev) => {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.rangeCount && tool === 'highlight') {
        const hit = rectsFromSelection(sel); if (!hit) return;
        if (ctx.pickColor) ctx.pickColor((color) => { sel.removeAllRanges(); createTextMark('highlight', hit.pageDiv, hit.rects, color); });
        return;
      }
      if (tool !== 'select') return;
      // select mode → a plain click hit-tests existing marks to edit/remove them.
      hitTestEdit(ev);
    });

    // ── underline / strikeout: a drag BAND over text → the text rects it covers ──
    // Works identically for pencil, finger, and mouse (no reliance on text selection). The band
    // is the dragged path's bounding box; we keep every text-layer span whose vertical centre is
    // within ~0.7 line of the band and whose horizontal extent overlaps it.
    function spanRectsUnderBand(pageDiv, p0, p1) {
      const pr = pageDiv.getBoundingClientRect(), W = pr.width, H = pr.height;
      const x0 = Math.min(p0.x, p1.x), x1 = Math.max(p0.x, p1.x);
      const yMid = (p0.y + p1.y) / 2;
      const out = [];
      pageDiv.querySelectorAll('.textLayer span').forEach(sp => {
        const r = sp.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return;
        const cy = (r.top + r.bottom) / 2;
        if (Math.abs(cy - (pr.top + yMid)) > r.height * 0.7) return;     // not on this line
        const sx0 = r.left - pr.left, sx1 = r.right - pr.left;
        if (sx1 < x0 || sx0 > x1) return;                                // no horizontal overlap
        const lo = Math.max(sx0, x0), hi = Math.min(sx1, x1);
        out.push([lo / W, (r.top - pr.top) / H, (hi - lo) / W, r.height / H]);
      });
      return out;
    }
    let band = null;   // {pageDiv, start:{x,y}} during an underline/strikeout drag
    function bandStart(ev) {
      const pageDiv = pageDivFromEl(ev.target); if (!pageDiv) return false;
      const pr = pageDiv.getBoundingClientRect();
      band = { pageDiv, start: { x: ev.clientX - pr.left, y: ev.clientY - pr.top } };
      try { ctx.viewer.setPointerCapture(ev.pointerId); } catch (e) {}
      return true;
    }
    function bandEnd(ev) {
      if (!band) return;
      const b = band; band = null;
      const pr = b.pageDiv.getBoundingClientRect();
      const end = { x: ev.clientX - pr.left, y: ev.clientY - pr.top };
      const rects = spanRectsUnderBand(b.pageDiv, b.start, end);
      try { window.getSelection().removeAllRanges(); } catch (e) {}    // drop any stray selection
      if (rects.length) createTextMark(tool, b.pageDiv, rects, pen.color || DEFAULT_COLORS[tool]);
    }

    function hitTestEdit(ev) {
      const pageDiv = pageDivFromEl(ev.target); if (!pageDiv) return;
      const n = pageNo(pageDiv), pr = pageDiv.getBoundingClientRect();
      const px = (ev.clientX - pr.left) / pr.width, py = (ev.clientY - pr.top) / pr.height;
      for (const a of (byPage.get(n) || [])) {
        let rects; try { rects = JSON.parse(a.rect); } catch (e) { continue; }
        if (rects.some(r => px >= r[0] && px <= r[0] + r[2] && py >= r[1] && py <= r[1] + r[3])) {
          ctx.editExisting(a, {
            onRemove: async () => { deindex(a); paintPage(pageDiv); try { await ctx.annotations.remove(a); } catch (e) {} },
            onSave: async (text) => { try { await ctx.annotations.update(a, { note_text: text }); a.note_text = text; } catch (e) {} },
          });
          return true;
        }
      }
      return false;
    }

    // ── ink + note + erase via pointer events ─────────────────────────────────
    let drawing = null;   // {pageDiv, pts, W, H, previewSvg, previewPath}
    function isDrawPointer(ev) {
      if (ev.pointerType === 'pen') return true;          // pencil always draws (pencil-smart)
      if (ev.pointerType === 'touch') return false;       // finger never draws (it scrolls)
      return tool === 'ink';                              // mouse follows the toolbar
    }
    function rejectTouch(ev) { return ev.pointerType === 'touch' && ctx.penActive && ctx.penActive(); }

    ctx.viewer.addEventListener('pointerdown', (ev) => {
      if (rejectTouch(ev)) { ev.preventDefault(); return; }   // palm while writing
      if (tool === 'erase') { eraseAt(ev); return; }
      if (tool === 'note') { placeNote(ev); return; }
      if (tool === 'underline' || tool === 'strikeout') { if (bandStart(ev)) ev.preventDefault(); return; }
      const inkMode = (tool === 'ink') || (ev.pointerType === 'pen' && tool !== 'underline' &&
                       tool !== 'strikeout' && tool !== 'highlight' && tool !== 'select');
      if (!inkMode || !isDrawPointer(ev)) return;
      const pageDiv = pageDivFromEl(ev.target); if (!pageDiv) return;
      const W = pageW(pageDiv), H = pageH(pageDiv);
      const pr = pageDiv.getBoundingClientRect();
      drawing = { pageDiv, W, H, pts: [], pr, t0: ev.timeStamp };
      try { ctx.viewer.setPointerCapture(ev.pointerId); } catch (e) {}
      const svg = inkSvg(pageDiv);
      drawing.previewPath = document.createElementNS(NS, 'path');
      drawing.previewPath.setAttribute('fill', pen.color || DEFAULT_COLORS.ink);
      if (pen.mode === 'marker') drawing.previewPath.setAttribute('fill-opacity', '0.4');
      svg.appendChild(drawing.previewPath);
      pushPoint(ev);
      ev.preventDefault();
    });
    ctx.viewer.addEventListener('pointermove', (ev) => {
      if (!drawing) return;
      const evs = ev.getCoalescedEvents ? ev.getCoalescedEvents() : [ev];
      for (const e of evs) pushPoint(e);
      renderPreview();
      ev.preventDefault();
    });
    function endDraw(ev) {
      if (!drawing) return;
      const d = drawing; drawing = null;
      if (d.previewPath) d.previewPath.remove();
      if (d.pts.length < 2) return;
      const width = (pen.width || 3) / d.W;            // store width as a fraction of page width
      const ink = JSON.stringify({ strokes: [{ points: d.pts, width, color: pen.color || DEFAULT_COLORS.ink, mode: pen.mode }] });
      (async () => {
        let saved = null;
        try { saved = await ctx.annotations.add({ kind: 'ink', page: pageNo(d.pageDiv), ink }); } catch (e) {}
        if (saved) { index(saved); paintPage(d.pageDiv); }
      })();
    }
    ctx.viewer.addEventListener('pointerup', (ev) => { endDraw(ev); bandEnd(ev); });
    ctx.viewer.addEventListener('pointercancel', () => { if (drawing) { drawing.previewPath && drawing.previewPath.remove(); drawing = null; } band = null; });

    function pushPoint(ev) {
      if (!drawing) return;
      const x = (ev.clientX - drawing.pr.left) / drawing.W;
      const y = (ev.clientY - drawing.pr.top) / drawing.H;
      const pressure = (ev.pressure && ev.pressure > 0) ? ev.pressure : 0.5;
      // 4th slot = ms from stroke start (online HWR); additive — render reads p[0..2].
      drawing.pts.push([x, y, pressure, Math.round(ev.timeStamp - drawing.t0)]);
    }
    function renderPreview() {
      if (!drawing || !drawing.previewPath) return;
      const px = drawing.pts.map(p => [p[0] * drawing.W, p[1] * drawing.H, p[2]]);
      const size = (pen.width || 3) * (pen.mode === 'marker' ? 2.4 : 1);
      const outline = ctx.freehand(px, { size, thinning: pen.mode === 'marker' ? 0.2 : 0.6,
                                         smoothing: 0.6, streamline: 0.5 });
      drawing.previewPath.setAttribute('d', svgPathFromStroke(outline));
    }

    function eraseAt(ev) {
      const pageDiv = pageDivFromEl(ev.target); if (!pageDiv) return;
      const n = pageNo(pageDiv), pr = pageDiv.getBoundingClientRect();
      const px = (ev.clientX - pr.left) / pr.width, py = (ev.clientY - pr.top) / pr.height;
      // ink first (a point-near-stroke test), then text marks.
      for (const a of (inkByPage.get(n) || []).slice()) {
        let data; try { data = JSON.parse(a.ink); } catch (e) { continue; }
        const near = (data.strokes || []).some(st => (st.points || []).some(p =>
          Math.hypot(p[0] - px, p[1] - py) < 0.02));
        if (near) { removeAnn(a, pageDiv); return; }
      }
      for (const a of (byPage.get(n) || []).slice()) {
        let rects; try { rects = JSON.parse(a.rect); } catch (e) { continue; }
        if (rects.some(r => px >= r[0] - 0.01 && px <= r[0] + r[2] + 0.01 &&
                            py >= r[1] - 0.01 && py <= r[1] + r[3] + 0.01)) { removeAnn(a, pageDiv); return; }
      }
    }
    async function removeAnn(a, pageDiv) {
      deindex(a); paintPage(pageDiv);
      try { await ctx.annotations.remove(a); } catch (e) {}
    }

    function placeNote(ev) {
      const pageDiv = pageDivFromEl(ev.target); if (!pageDiv) return;
      const pr = pageDiv.getBoundingClientRect();
      const x = (ev.clientX - pr.left) / pr.width, y = (ev.clientY - pr.top) / pr.height;
      ctx.editExisting({ kind: 'note', page: pageNo(pageDiv), note_text: '', _isNew: true }, {
        onSave: async (text) => {
          if (!text) return;
          let saved = null;
          try { saved = await ctx.annotations.add({ kind: 'note', page: pageNo(pageDiv),
            rect: JSON.stringify([x, y]), note_text: text }); } catch (e) {}
          if (saved) { index(saved); paintPage(pageDiv); }
        },
      });
    }

    return {
      setTool(t) { tool = t; },
      setPen(p) { pen = Object.assign({}, pen, p); },
      tool() { return tool; },
      supports() { return true; },        // PDF supports every kind
      async load() {
        let list = []; try { list = await ctx.annotations.list(); } catch (e) {}
        list.forEach(index); repaint();
      },
      list() {
        return Array.from(all.values())
          .filter(a => !a.deleted_at)
          .sort((a, b) => (a.page || 0) - (b.page || 0))
          .map(a => ({ a, label: labelFor(a), go: () => ctx.goto(String(a.page)) }));
      },
      pageRendered(d) { paintPage(d); },
      repaint,
    };
    function labelFor(a) {
      const tag = a.kind === 'ink' ? '✎' : a.kind === 'note' ? '🅝'
        : a.kind === 'strikeout' ? 'S̶' : a.kind === 'underline' ? 'U̲' : '▮';
      const note = a.note_text ? ' — ' + a.note_text : '';
      return `${tag}  p.${a.page}${note}`;
    }
  }

  // ════════════════════════════════════════════════════════════════════════════
  // EPUB overlay — epub.js native highlight/underline (CFI-anchored text marks) +
  // a best-effort ink layer anchored to the spine section (reflow caveat, §4.3).
  // Strikeout is NOT a native epub.js type → reported unsupported (engine hides it).
  // ════════════════════════════════════════════════════════════════════════════
  function epubOverlay(ctx) {
    let tool = 'select';
    let pen = ctx.getPenDefault ? ctx.getPenDefault() : { color: '#222', width: 3, mode: 'pen' };
    const all = new Map();              // id -> annotation
    const painted = new Map();          // id -> cfiRange (epub.js text marks, to unpaint)

    const rendition = () => ctx.rendition();

    function paintTextMark(a) {
      if (!a || a.deleted_at || painted.has(a.id) || !a.cfi_range) return;
      if (a.kind !== 'highlight' && a.kind !== 'underline') return;
      const type = a.kind;             // epub.js supports 'highlight' and 'underline'
      try {
        rendition().annotations.add(type, a.cfi_range, { id: a.id },
          () => ctx.editExisting(a, {
            onRemove: async () => { unpaintTextMark(a); deindex(a); try { await ctx.annotations.remove(a); } catch (e) {} },
            onSave: async (text) => { try { await ctx.annotations.update(a, { note_text: text }); a.note_text = text; } catch (e) {} },
          }),
          'reader-' + type,
          type === 'highlight'
            ? { fill: a.color || DEFAULT_COLORS.highlight, 'fill-opacity': '0.35', 'mix-blend-mode': 'multiply' }
            : { stroke: a.color || DEFAULT_COLORS.underline, 'stroke-width': '2' });
        painted.set(a.id, a.cfi_range);
      } catch (e) {}
    }
    function unpaintTextMark(a) {
      const cfi = painted.get(a.id); if (!cfi) return;
      try { rendition().annotations.remove(cfi, a.kind === 'underline' ? 'underline' : 'highlight'); } catch (e) {}
      painted.delete(a.id);
    }

    function index(a) { if (a && !a.deleted_at) all.set(a.id, a); }
    function deindex(a) { all.delete(a.id); }

    // ── ink layer over the epub iframe (best-effort; anchored to spine index) ──
    let inkSvgEl = null;
    function ensureInkSvg() {
      if (inkSvgEl && inkSvgEl.isConnected) return inkSvgEl;
      inkSvgEl = document.createElementNS(NS, 'svg');
      inkSvgEl.setAttribute('class', 'epub-ink');
      inkSvgEl.setAttribute('preserveAspectRatio', 'none');
      ctx.viewer.appendChild(inkSvgEl);
      return inkSvgEl;
    }
    function repaintInk() {
      const svg = ensureInkSvg();
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      const W = ctx.viewer.clientWidth, H = ctx.viewer.clientHeight;
      svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
      const spine = ctx.currentSpine();
      for (const a of all.values()) {
        if (a.kind !== 'ink' || a.deleted_at || a.page !== spine) continue;
        let data; try { data = JSON.parse(a.ink); } catch (e) { continue; }
        for (const st of (data.strokes || [])) {
          const px = (st.points || []).map(p => [p[0] * W, p[1] * H, p[2] == null ? 0.5 : p[2]]);
          const size = (st.width || 0.004) * W * (st.mode === 'marker' ? 2.4 : 1);
          const outline = ctx.freehand(px, { size, thinning: st.mode === 'marker' ? 0.2 : 0.6, smoothing: 0.6, streamline: 0.5 });
          const path = document.createElementNS(NS, 'path');
          path.setAttribute('d', svgPathFromStroke(outline));
          path.setAttribute('fill', st.color || DEFAULT_COLORS.ink);
          if (st.mode === 'marker') path.setAttribute('fill-opacity', '0.4');
          path.dataset.id = a.id;
          svg.appendChild(path);
        }
      }
    }

    // ink capture on the overlay svg (the engine routes pointer events here only when
    // the ink tool is active / a pen is drawing — the svg sits above the iframe then).
    let drawing = null;
    function inkActive() { return tool === 'ink'; }
    function attachInkCapture() {
      const svg = ensureInkSvg();
      svg.addEventListener('pointerdown', (ev) => {
        if (!inkActive()) return;
        if (ev.pointerType === 'touch' && ctx.penActive && ctx.penActive()) { ev.preventDefault(); return; }
        const pr = svg.getBoundingClientRect();
        drawing = { pr, W: pr.width, H: pr.height, pts: [], t0: ev.timeStamp };
        try { svg.setPointerCapture(ev.pointerId); } catch (e) {}
        drawing.path = document.createElementNS(NS, 'path');
        drawing.path.setAttribute('fill', pen.color || DEFAULT_COLORS.ink);
        if (pen.mode === 'marker') drawing.path.setAttribute('fill-opacity', '0.4');
        svg.appendChild(drawing.path);
        pushPoint(ev); ev.preventDefault();
      });
      svg.addEventListener('pointermove', (ev) => {
        if (!drawing) return;
        const evs = ev.getCoalescedEvents ? ev.getCoalescedEvents() : [ev];
        for (const e of evs) pushPoint(e);
        const px = drawing.pts.map(p => [p[0] * drawing.W, p[1] * drawing.H, p[2]]);
        const size = (pen.width || 3) * (pen.mode === 'marker' ? 2.4 : 1);
        drawing.path.setAttribute('d', svgPathFromStroke(ctx.freehand(px, { size, thinning: pen.mode === 'marker' ? 0.2 : 0.6, smoothing: 0.6, streamline: 0.5 })));
        ev.preventDefault();
      });
      const finish = () => {
        if (!drawing) return; const d = drawing; drawing = null;
        if (d.path) d.path.remove();
        if (d.pts.length < 2) return;
        const width = (pen.width || 3) / d.W;
        const ink = JSON.stringify({ strokes: [{ points: d.pts, width, color: pen.color || DEFAULT_COLORS.ink, mode: pen.mode }] });
        (async () => {
          let saved = null;
          try { saved = await ctx.annotations.add({ kind: 'ink', page: ctx.currentSpine(), cfi_range: ctx.currentCfi(), ink }); } catch (e) {}
          if (saved) { index(saved); repaintInk(); }
        })();
      };
      svg.addEventListener('pointerup', finish);
      svg.addEventListener('pointercancel', () => { if (drawing) { drawing.path && drawing.path.remove(); drawing = null; } });
    }
    function pushPoint(ev) {
      if (!drawing) return;
      drawing.pts.push([(ev.clientX - drawing.pr.left) / drawing.W,
                        (ev.clientY - drawing.pr.top) / drawing.H,
                        (ev.pressure && ev.pressure > 0) ? ev.pressure : 0.5,
                        // 4th slot = ms from stroke start (online HWR); render reads p[0..2].
                        Math.round(ev.timeStamp - drawing.t0)]);
    }

    // text-mark capture: epub.js fires 'selected' with the cfiRange.
    function onSelected(cfiRange) {
      if (tool === 'highlight' && ctx.pickColor) {
        ctx.pickColor(async (color) => { clearSel(); await createTextMark('highlight', cfiRange, color); });
      } else if (tool === 'underline') {
        clearSel(); createTextMark('underline', cfiRange, pen.color || DEFAULT_COLORS.underline);
      }
    }
    function clearSel() { try { rendition().getContents().forEach(c => c.window.getSelection().removeAllRanges()); } catch (e) {} }
    async function createTextMark(kind, cfiRange, color) {
      let saved = null;
      try { saved = await ctx.annotations.add({ kind, cfi_range: cfiRange, color }); } catch (e) {}
      if (saved) { index(saved); paintTextMark(saved); }
    }

    return {
      setTool(t) {
        tool = t;
        // The ink svg only intercepts pointers while the ink tool is active; otherwise it
        // stays click-through so text selection / links work.
        const svg = ensureInkSvg();
        svg.style.pointerEvents = (t === 'ink') ? 'auto' : 'none';
      },
      setPen(p) { pen = Object.assign({}, pen, p); },
      tool() { return tool; },
      supports(kind) { return kind !== 'strikeout'; },   // no native epub.js strikethrough
      async load() {
        try { rendition().on('selected', onSelected); } catch (e) {}
        attachInkCapture();
        let list = []; try { list = await ctx.annotations.list(); } catch (e) {}
        list.forEach(a => { index(a); if (a.kind === 'highlight' || a.kind === 'underline') paintTextMark(a); });
        repaintInk();
      },
      list() {
        return Array.from(all.values()).filter(a => !a.deleted_at).map(a => ({
          a, label: labelFor(a),
          go: () => { if (a.cfi_range) { try { rendition().display(a.cfi_range); } catch (e) {} } },
        }));
      },
      pageRendered() {},
      repaint() { repaintInk(); },
      relocated() { repaintInk(); },     // engine calls this on epub 'relocated'
    };
    function labelFor(a) {
      const tag = a.kind === 'ink' ? '✎' : a.kind === 'note' ? '🅝' : a.kind === 'underline' ? 'U̲' : '▮';
      const note = a.note_text ? ' — ' + a.note_text : '';
      return `${tag}  ${a.kind}${note}`;
    }
  }

  function create(format, ctx) {
    return (format === 'epub') ? epubOverlay(ctx) : pdfOverlay(ctx);
  }

  window.ReaderOverlay = { create };
})();
