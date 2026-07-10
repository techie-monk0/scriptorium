/* Shared home-shelf component — ONE implementation used by the web view (home.html) and
 * the PWA (app.js). The web builds shelves with its Jinja macro; the PWA builds them with
 * Shelf.render(); BOTH call Shelf.enhance() for identical behavior (art-mode swap, expand,
 * arrows, Dock magnification). Operates on the markup contract by class, so it doesn't care
 * who built the DOM.
 *
 *   Shelf.enhance(root=document)              — attach behaviors to every .shelf in root
 *   Shelf.render({title, books, count, moreUrl, hrefFor}) -> <section.shelf> element
 *       books: [{eid, title, by, spine_url?, cover_url?}]
 */
(function () {
  'use strict';

  // ── art-mode (spine | cover), read from <html data-shelf> (default cover) ──────
  function spineMode() {
    return document.documentElement.getAttribute('data-shelf') === 'spine';
  }
  function applyArtMode(root) {
    var mode = spineMode() ? 'spine' : 'cover';
    root.querySelectorAll('.tile img.art').forEach(function (img) {
      var url = img.getAttribute('data-' + mode);
      if (url && img.getAttribute('src') !== url) img.src = url;
    });
  }

  // ── real-bookshelf sizing: in spine mode give each book a DETERMINISTIC thickness
  // (--wjit) and height (--hjit) from its id, so the same book is always the same size
  // and the row reads like books of varying sizes on a shelf. Cleared in cover mode
  // (uniform posters). The CSS defaults both to 1 (no-JS → uniform). ──────────────
  function hash32(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function applySizes(root) {
    var spine = spineMode();
    // Set-tiles are uniform box-set posters, never jittered spines — skip them.
    root.querySelectorAll('.tile:not(.set-tile)').forEach(function (t) {
      if (!spine) { t.style.removeProperty('--wjit'); t.style.removeProperty('--hjit'); return; }
      var h = hash32(t.getAttribute('href') || t.title || '');
      var w = 0.62 + (h % 1000) / 1000 * 1.13;               // thickness 0.62–1.75× (thin → chunky)
      var hh = 0.86 + ((h >>> 10) % 1000) / 1000 * 0.20;     // height   0.86–1.06×
      t.style.setProperty('--wjit', w.toFixed(3));
      t.style.setProperty('--hjit', hh.toFixed(3));
    });
  }

  // ── tap a shelf title to expand; arrow buttons page the track ─────────────────
  function wireControls(root) {
    root.querySelectorAll('.title-toggle').forEach(function (btn) {
      if (btn._wired) return; btn._wired = true;
      btn.addEventListener('click', function () {
        var on = btn.closest('.shelf').classList.toggle('expanded');
        btn.setAttribute('aria-expanded', on ? 'true' : 'false');
      });
    });
    root.querySelectorAll('.arrow').forEach(function (btn) {
      if (btn._wired) return; btn._wired = true;
      btn.addEventListener('click', function () {
        var track = btn.parentElement.querySelector('.shelf-track');
        if (track) track.scrollBy({ left: btn.dataset.dir * (track.clientWidth * 0.85), behavior: 'smooth' });
      });
    });
  }

  // ── macOS-Dock magnification — pointer (cursor distance) OR touch (scroll-centre) ─
  // Books grow only upward (CSS transform-origin: bottom), so the whole bloom sits on top
  // of the shelf; MAX is tuned so that upward growth fits the track's top padding.
  var MAX = 1.25, R = 230;
  function magnify(track) {
    if (track._mag) return; track._mag = true;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var hoverable = window.matchMedia('(hover: hover)').matches;
    var pointerX = null, raf = null;

    function target() {
      if (hoverable) return pointerX;                        // cursor x
      var r = track.getBoundingClientRect();                 // touch: centre of the viewport
      return r.left + track.clientWidth / 2;
    }
    function apply() {
      raf = null;
      var t = target(), tiles = track.querySelectorAll('.tile');
      for (var i = 0; i < tiles.length; i++) {
        var r = tiles[i].getBoundingClientRect(), scale = 1;
        if (t != null) {
          var d = Math.abs(r.left + r.width / 2 - t);
          if (d < R) scale = 1 + (MAX - 1) * (0.5 + 0.5 * Math.cos(Math.PI * d / R));
        }
        tiles[i].style.setProperty('--mag', scale.toFixed(3));
        tiles[i].style.zIndex = scale > 1.001 ? String(Math.round(scale * 100)) : '';
      }
    }
    function schedule() { if (raf == null) raf = requestAnimationFrame(apply); }

    if (hoverable) {
      track.addEventListener('mousemove', function (e) { pointerX = e.clientX; schedule(); });
      track.addEventListener('mouseleave', function () { pointerX = null; schedule(); });
      track.addEventListener('scroll', function () { if (pointerX != null) schedule(); }, { passive: true });
    } else {
      track.addEventListener('scroll', schedule, { passive: true });
      window.addEventListener('resize', schedule);
      schedule();                                            // initial centre bloom
    }
  }

  function enhance(root) {
    root = root || document;
    applyArtMode(root);
    applySizes(root);
    wireControls(root);
    root.querySelectorAll('.shelf-track').forEach(magnify);
  }

  // ── markup builders — ONE source for a tile, shared by shelves + search grid ──
  function el(tag, cls) { var e = document.createElement(tag); if (cls) e.className = cls; return e; }

  // One book tile. opts.hrefFor(eid)->url. opts.art:'cover'|'spine' forces a fixed art
  // (e.g. the search grid → covers); omitted → mode-swappable per <html data-shelf>.
  function tile(b, opts) {
    opts = opts || {};
    var hrefFor = opts.hrefFor || function (eid) { return '/edition/' + eid + '/read'; };
    // Volume-aware display title (server-supplied; one rule for web/PWA/native), raw title fallback.
    var t = b.display_title || b.title;
    var a = el('a', 'tile'); a.href = hrefFor(b.eid); a.title = t + (b.by ? ' — ' + b.by : '');
    var cov = el('span', 'cover');
    var bt = el('span', 'blank-title'); var bs = el('span'); bs.textContent = t; bt.appendChild(bs); cov.appendChild(bt);
    var img = el('img', 'art');
    if (opts.art) {
      img.src = (opts.art === 'spine' ? b.spine_url : b.cover_url) || b.cover_url || b.spine_url || '';
    } else {
      if (b.spine_url) img.setAttribute('data-spine', b.spine_url);
      if (b.cover_url) img.setAttribute('data-cover', b.cover_url);
      // Load ONLY the active art mode's image (cover OR spine) — never both. The other
      // sits in data-* and loads lazily only if the operator flips the mode (applyArtMode).
      img.src = (spineMode() ? (b.spine_url || b.cover_url)
                             : (b.cover_url || b.spine_url)) || '';
    }
    img.loading = 'lazy'; img.alt = ''; img.setAttribute('onerror', 'this.remove()');
    cov.appendChild(img);
    // Star TOGGLE overlay — the primary affordance on every cover. Initial state from the card's
    // `starred` flag (homeVM) OR the shared client set (search grid etc.); a delegated click toggles it.
    if (b.eid != null) {
      var star = el('button', 'star'); star.type = 'button';
      star.setAttribute('data-eid', b.eid); star.setAttribute('aria-label', 'Star');
      star.setAttribute('aria-pressed', (b.starred || isStarred(b.eid)) ? 'true' : 'false');
      star.textContent = '★';                         // ★ — CSS colours filled vs outline by aria-pressed
      cov.appendChild(star);
    }
    // "New" badge — a freshly-added book in the merged Recent rail (homeVM sets card.badge).
    if (b.badge === 'New') { var nb = el('span', 'new-badge'); nb.textContent = 'New'; cov.appendChild(nb); }
    a.appendChild(cov); return a;
  }

  function render(opts) {
    var hrefFor = opts.hrefFor || function (eid) { return '/edition/' + eid + '/read'; };
    var sec = el('section', 'shelf');
    if (opts.kind) sec.setAttribute('data-rail', opts.kind);   // rail identity → live Starred-rail sync
    if (opts.headless) {                       // just the scrolling row (e.g. a series drawer)
      var hrow = el('div', 'shelf-row');
      hrow.innerHTML = '<button type="button" class="arrow left" data-dir="-1" aria-label="Scroll left">‹</button>' +
                       '<button type="button" class="arrow right" data-dir="1" aria-label="Scroll right">›</button>';
      var htrack = el('div', 'shelf-track');
      (opts.books || []).forEach(function (b) { htrack.appendChild(tile(b, { hrefFor: hrefFor })); });
      hrow.insertBefore(htrack, hrow.querySelector('.arrow.right'));
      sec.appendChild(hrow);
      return sec;
    }
    var head = el('div', 'shelf-head');
    if (opts.moreUrl) {
      // A shelf with a destination (subject / series): the TITLE navigates to that page
      // (what users expect when they tap a subject); the caret still expands in place.
      head.innerHTML = '<h2><a class="ttext tlink"></a>' +
        '<button type="button" class="title-toggle" aria-expanded="false" aria-label="Expand shelf">' +
        '<span class="caret">▾</span></button></h2>';
      var tl = head.querySelector('.ttext'); tl.href = opts.moreUrl; tl.textContent = opts.title;
    } else {
      head.innerHTML = '<h2><button type="button" class="title-toggle" aria-expanded="false">' +
        '<span class="ttext"></span> <span class="caret">▾</span></button></h2>';
      head.querySelector('.ttext').textContent = opts.title;
    }
    if (opts.count) { var c = el('span', 'count'); c.textContent = opts.count; head.appendChild(c); }
    if (opts.moreUrl && opts.count) { var m = el('a', 'more'); m.href = opts.moreUrl; m.textContent = 'all ' + opts.count + ' →'; head.appendChild(m); }
    sec.appendChild(head);

    var row = el('div', 'shelf-row');
    row.innerHTML = '<button type="button" class="arrow left" data-dir="-1" aria-label="Scroll left">‹</button>' +
                    '<button type="button" class="arrow right" data-dir="1" aria-label="Scroll right">›</button>';
    var track = el('div', 'shelf-track');
    (opts.books || []).forEach(function (b) { track.appendChild(tile(b, { hrefFor: hrefFor })); });
    row.insertBefore(track, row.querySelector('.arrow.right'));
    sec.appendChild(row);
    return sec;
  }

  // ── Series rail: ONE set-tile per series, each expanding a drawer of volumes ───
  // A series is one object, not a shelf of loose spines. Its tile shows a "display one
  // set" art (see SET_VIEWS) + a count badge; tapping it drops an accordion drawer of the
  // volumes (in order) right below the rail. One drawer open at a time.

  // ── "display one set" API ──────────────────────────────────────────────────────
  // Each SET_VIEW is an INDEPENDENT way to draw the collapsed art for ONE set into the
  // fixed cover box. Add a view = add an entry here. The active one is chosen by
  // <html data-set> (persisted in localStorage), so a client can switch & compare live.
  //   view.label                 — name shown in the style switcher
  //   view.renderArt(set, h)      — returns an Element filling the cover box
  //        set = {id, name, count, books:[{eid, cover_url, spine_url}]}  (books in order)
  //        h   = { el, hash32, jitter }  — helpers; jitter(seed,salt,lo,hi) is a STABLE
  //              pseudo-random in [lo,hi) from the seed (so a set looks the same each render).
  var HELP = {
    el: el, hash32: hash32,
    jitter: function (seed, salt, lo, hi) {
      var h = hash32(String(seed) + ':' + salt);
      return lo + (h % 100000) / 100000 * (hi - lo);
    }
  };

  function artImg(url, onerr) {
    var img = el('img', 'art');
    img.src = url || ''; img.loading = 'lazy'; img.alt = '';
    img.setAttribute('onerror', onerr || 'this.remove()');
    return img;
  }

  // Each view declares its own tile box size (w×h) — the set-tile sizes itself to the
  // ACTIVE style (applied inline as --set-w/--set-h), so switching styles can reshape it.
  var SET_VIEWS = {
    // 2×2 mosaic of the first volumes' covers (the original).
    collage: { label: 'Collage', w: 172, h: 200, renderArt: function (s, h) {
      var wrap = h.el('span', 'set-cover');
      var books = (s.books || []).slice(0, 4);
      wrap.setAttribute('data-n', String(books.length));
      books.forEach(function (b) {
        var cell = h.el('span', 'set-cell');
        cell.appendChild(artImg(b.cover_url || b.spine_url));
        wrap.appendChild(cell);
      });
      return wrap;
    }},

    // One representative cover (vol 1) with two card edges stepping behind it = "a stack".
    cover: { label: 'Single cover', w: 150, h: 200, renderArt: function (s, h) {
      var wrap = h.el('span', 'set-single');
      var b = (s.books || [])[0] || {};
      wrap.appendChild(h.el('span', 'set-edge e2'));
      wrap.appendChild(h.el('span', 'set-edge e1'));
      wrap.appendChild(artImg(b.cover_url || b.spine_url));
      return wrap;
    }},

    // A "cover-flow" stack: one cover sharp in the FOREGROUND (centred), the rest fanned
    // out BEHIND it alternating left/right — dimmed, blurred and scaled down, so only their
    // outer edges peek past the front cover on each side. Deepest layers paint first (lowest
    // z), foreground last (on top). Tunable: PEEK/STEP/SCALE/DIM below.
    fan: { label: 'Cover stack', w: 210, h: 200, renderArt: function (s, h) {
      var wrap = h.el('span', 'set-fan');
      var books = s.books || [];
      var mid = Math.floor(books.length / 2);           // the MIDDLE volume goes in front
      var PEEK = 24, STEP = 17, SCALE = 0.075, DIM = 0.84, BLUR = 0.7;
      // Up to 2 volumes on each side, by distance from the middle (earlier vols fan left,
      // later vols fan right); farthest painted first so nearer layers sit on top.
      var layers = [];
      for (var d = 2; d >= 1; d--) {
        if (books[mid - d]) layers.push({ b: books[mid - d], side: -1, depth: d });
        if (books[mid + d]) layers.push({ b: books[mid + d], side: 1, depth: d });
      }
      layers.forEach(function (L) {
        var bk = h.el('span', 'fan-book bg');
        bk.appendChild(artImg(L.b.cover_url || L.b.spine_url));
        bk.style.transform = 'translateX(' + (L.side * (PEEK + L.depth * STEP)) + 'px) ' +
                             'scale(' + (1 - L.depth * SCALE).toFixed(3) + ')';
        bk.style.zIndex = String(10 - L.depth);
        bk.style.opacity = (L.depth === 1 ? 0.85 : 0.65).toFixed(2);
        bk.style.filter = 'brightness(' + (DIM - L.depth * 0.12).toFixed(2) + ') ' +
                          'blur(' + (L.depth * BLUR).toFixed(1) + 'px)';
        wrap.appendChild(bk);
      });
      var fg = books[mid];                              // middle cover, sharp, on top
      if (fg) {
        var f = h.el('span', 'fan-book fg');
        f.appendChild(artImg(fg.cover_url || fg.spine_url));
        f.style.zIndex = '20';
        wrap.appendChild(f);
      }
      return wrap;
    }}
  };

  var SET_DEFAULT = 'fan';
  function activeSetStyle() {
    var a = document.documentElement.getAttribute('data-set');
    if (a && SET_VIEWS[a]) return a;
    var s = null; try { s = localStorage.getItem('setStyle'); } catch (e) {}
    return SET_VIEWS[s] ? s : SET_DEFAULT;
  }

  function setBadge(s) {
    var badge = el('span', 'set-badge');
    badge.textContent = s.count + (s.count === 1 ? ' vol' : ' vols');
    return badge;
  }

  function paintSetCover(cov, s, style) {
    cov.innerHTML = '';
    cov.appendChild(SET_VIEWS[style].renderArt(s, HELP));
    cov.appendChild(setBadge(s));
  }

  // Size the tile box to the active style's declared w×h (inline → overrides the CSS default).
  function applySetSize(tile, style) {
    var v = SET_VIEWS[style] || {};
    tile.style.setProperty('--set-w', (v.w || 210) + 'px');
    tile.style.setProperty('--set-h', (v.h || 200) + 'px');
  }

  function setTile(s) {
    var style = activeSetStyle();
    var b = el('button', 'tile set-tile'); b.type = 'button';
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('data-style', style); applySetSize(b, style);
    b.title = s.name + ' — ' + s.count + (s.count === 1 ? ' volume' : ' volumes');
    var cov = el('span', 'cover'); paintSetCover(cov, s, style);
    b.appendChild(cov);                                 // name lives in the tooltip + drawer header
    b._set = s;                                         // stash for live re-style
    return b;
  }

  // Switch the active set-art and re-paint every set-tile in place (no reload).
  function setSetStyle(name) {
    if (!SET_VIEWS[name]) return;
    document.documentElement.setAttribute('data-set', name);
    try { localStorage.setItem('setStyle', name); } catch (e) {}
    document.querySelectorAll('.set-tile').forEach(function (t) {
      if (!t._set) return;
      t.setAttribute('data-style', name); applySetSize(t, name);
      paintSetCover(t.querySelector('.cover'), t._set, name);
    });
  }

  // opts: { series:[{id,name,count,books}], hrefFor }
  function renderSeriesRail(opts) {
    var series = opts.series || [];
    var hrefFor = opts.hrefFor || function (eid) { return '/edition/' + eid + '/read'; };
    var sec = el('section', 'shelf series-rail');
    sec.setAttribute('data-rail', 'series');
    var head = el('div', 'shelf-head');
    head.innerHTML = '<h2><span class="ttext">Series</span></h2>';
    var c = el('span', 'count'); c.textContent = series.length; head.appendChild(c);
    // Live style switcher — flip the "display one set" view and re-paint in place.
    var sel = el('select', 'set-style-sel'); sel.setAttribute('aria-label', 'Series tile style');
    Object.keys(SET_VIEWS).forEach(function (k) {
      var op = document.createElement('option'); op.value = k; op.textContent = SET_VIEWS[k].label;
      if (k === activeSetStyle()) op.selected = true; sel.appendChild(op);
    });
    sel.addEventListener('change', function () { setSetStyle(sel.value); });
    head.appendChild(sel);
    sec.appendChild(head);

    var row = el('div', 'shelf-row');
    row.innerHTML = '<button type="button" class="arrow left" data-dir="-1" aria-label="Scroll left">‹</button>' +
                    '<button type="button" class="arrow right" data-dir="1" aria-label="Scroll right">›</button>';
    var track = el('div', 'shelf-track');
    var drawer = el('div', 'series-drawer');

    series.forEach(function (s) {
      var t = setTile(s);
      t.addEventListener('click', function () {
        var wasOpen = t.classList.contains('active');
        track.querySelectorAll('.set-tile.active').forEach(function (o) {  // accordion: close others
          o.classList.remove('active'); o.setAttribute('aria-expanded', 'false');
        });
        drawer.innerHTML = '';
        if (wasOpen) { drawer.classList.remove('open'); return; }          // tap again → close
        t.classList.add('active'); t.setAttribute('aria-expanded', 'true');
        var hd = el('div', 'drawer-head');
        hd.innerHTML = '<span class="dh-title"></span> <span class="dh-count"></span>';
        hd.querySelector('.dh-title').textContent = s.name;
        hd.querySelector('.dh-count').textContent =
          s.count + (s.count === 1 ? ' volume' : ' volumes');
        var inner = render({ books: s.books, headless: true, hrefFor: hrefFor });
        drawer.appendChild(hd); drawer.appendChild(inner);
        drawer.classList.add('open');
        enhance(drawer);                                                   // arrows + magnify + art-mode
      });
      track.appendChild(t);
    });
    row.insertBefore(track, row.querySelector('.arrow.right'));
    sec.appendChild(row); sec.appendChild(drawer);
    return sec;
  }

  // ── Starred state (shared across EVERY tile, any builder) ─────────────────────
  // One client-side set of starred edition ids so a cover painted by ANY view-model (home, search
  // grid, …) shows the highlight and toggles consistently. Seeded by setStarred() from the starred list.
  var STARRED = {};
  function isStarred(eid) { return !!STARRED[eid]; }
  function setStarred(ids) {
    STARRED = {}; (ids || []).forEach(function (id) { STARRED[id] = true; });
    refreshStarMarks();
  }
  function refreshStarMarks(root) {
    (root || document).querySelectorAll('.tile .star[data-eid]').forEach(function (s) {
      s.setAttribute('aria-pressed', isStarred(+s.getAttribute('data-eid')) ? 'true' : 'false');
    });
  }
  // The toggle: execute the shared LibraryCore.starredRequest mapper, optimistically flip, then adopt
  // the server's fresh list. No endpoint is hardcoded here — web/PWA/iOS issue identical requests.
  function toggleStar(eid) {
    if (!window.LibraryCore) return;
    var on = !isStarred(eid);
    if (on) STARRED[eid] = true; else delete STARRED[eid];
    refreshStarMarks();
    var req = LibraryCore.starredRequest(on ? 'star' : 'unstar', { eid: eid });
    fetch(req.path, { method: req.method, headers: { 'Content-Type': 'application/json' },
                      body: req.body ? JSON.stringify(req.body) : undefined })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) {
        var ids = data.editions || [];
        setStarred(ids);                                     // adopt the server's authoritative order
        // Broadcast the fresh (ordered) set so a derived surface — the home Starred rail — repaints
        // itself live, without a full reload. Fired ONLY on a discrete toggle, never on initial seed.
        document.dispatchEvent(new CustomEvent('starred:changed', { detail: { ids: ids } }));
      })
      .catch(function () {                                   // rollback on failure
        if (on) delete STARRED[eid]; else STARRED[eid] = true; refreshStarMarks();
      });
  }

  // Live-patch JUST the Starred rail after a star toggle so the home page reflects a new/removed
  // favourite without a full re-render (every OTHER rail's membership is starred-independent; their
  // cover marks are already handled by refreshStarMarks). The rail is recomputed from the SAME Tier-2
  // homeVM so its card shape/order matches the initial paint exactly, then spliced in at its canonical
  // position — right after the Recent rail (homeVM order). recentIds is irrelevant to the Starred rail
  // (its cards are the starred set, not the recent set), so the caller need not supply it. The Recent
  // anchor doubles as a guard: with no Recent rail present the container isn't the home page, so we
  // skip rather than inject a stray rail into (say) the search grid.
  function syncStarredRail(container, opts) {
    if (!container || !window.LibraryCore) return;
    var vm = LibraryCore.homeVM(opts.replica, [], opts.ids || [], {});
    var rail = null; vm.rails.forEach(function (r) { if (r.kind === 'starred') rail = r; });
    var existing = container.querySelector('.shelf[data-rail="starred"]');
    if (!rail) { if (existing) existing.remove(); return; }   // last star removed → drop the rail
    var sec = render({ title: rail.title, books: rail.cards, hrefFor: opts.hrefFor, kind: 'starred' });
    enhance(sec);
    if (existing) { existing.replaceWith(sec); return; }
    var recent = container.querySelector('.shelf[data-rail="recent"]');
    if (!recent) return;                                     // not the home page → no safe anchor
    container.insertBefore(sec, recent.nextSibling);         // right after Recent (null → append)
  }
  // One delegated handler for every cover star (tiles built now or later). The cover is an <a>, so the
  // star must swallow the click to toggle instead of navigating.
  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest && ev.target.closest('.tile .star[data-eid]');
    if (!btn) return;
    ev.preventDefault(); ev.stopPropagation();
    toggleStar(+btn.getAttribute('data-eid'));
  });

  window.Shelf = { enhance: enhance, render: render, tile: tile,
                   renderSeriesRail: renderSeriesRail, setViews: SET_VIEWS, setSetStyle: setSetStyle,
                   setStarred: setStarred, isStarred: isStarred, refreshStarMarks: refreshStarMarks,
                   syncStarredRail: syncStarredRail };
})();
