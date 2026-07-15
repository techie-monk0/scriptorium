/* ── library-ui-dom.js — Tier 3: DOM renderer (web + PWA) ────────────────────
   Turns LibraryCore view-models into DOM. Shared by the web build and the PWA
   because both are HTML/DOM — this is the "one frontend for both surfaces". A
   native UI (iOS/SwiftUI) replaces THIS file only; Tiers 1–2 (the JSON contract
   + LibraryCore view-models) are reused unchanged.

   Each feature renders a self-contained view into a host element, given a
   platform adapter (see library-core.js). Navigation uses real <a href> built
   from `platform.nav.hrefFor(ref)` — works for web (URLs) and PWA (hash routes)
   with no per-surface click handling.

     LibraryUI.search(host, platform, opts)    — metadata book grid
     LibraryUI.browse(host, platform, opts)    — type-grouped results + chips
     LibraryUI.content(host, platform, opts)   — full-text snippets per book
     LibraryUI.settings(host, platform, opts)  — theme + shelf-art (+ opts.extra)
     LibraryUI.prefs                           — applyTheme/applyShelf (DOM)
   opts: {initialQuery, placeholder, only, autofocus, extra}. */
(function () {
  if (window.LibraryUI) return;
  var C = window.LibraryCore;

  (function injectStyles() {
    if (document.getElementById('libui-ui-styles')) return;
    var css =
      '.libui-feature { margin: .4rem 0 1rem; }' +
      '.libui-q { width: 100%; max-width: 40rem; padding: .45rem .6rem; font: inherit; box-sizing: border-box;' +
      '  border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--fg); }' +
      '.libui-finder-bar { display: flex; gap: .5rem; align-items: center; max-width: 40rem; }' +
      '.libui-finder-bar .libui-q { flex: 1; }' +
      '.libui-finder-mode { padding: .45rem .5rem; font: inherit; border: 1px solid var(--border);' +
      '  border-radius: 8px; background: var(--surface); color: var(--fg); }' +
      '.libui-hits { display: flex; flex-direction: column; }' +
      '.libui-results { margin-top: .7rem; }' +
      '.libui-msg { color: var(--muted); }' +
      '.libui-chips { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .6rem; }' +
      '.libui-chip { font: inherit; font-size: .85rem; padding: .25rem .6rem; border-radius: 999px;' +
      '  border: 1px solid var(--border); background: var(--surface-2); color: var(--fg); cursor: pointer; }' +
      '.libui-chip.active { background: var(--nav-active-bg); color: var(--nav-active-fg); border-color: var(--nav-active-bg); }' +
      '.libui-group { margin: 1rem 0; }' +
      '.libui-group-head { margin: 0 0 .4rem; font-size: .95rem; color: var(--muted); font-weight: 700; }' +
      '.libui-hits { display: flex; flex-direction: column; gap: .25rem; }' +
      '.libui-hit { display: flex; flex-direction: column; gap: .05rem; text-decoration: none; color: inherit;' +
      '  padding: .4rem .55rem; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); }' +
      '.libui-hit:hover { background: var(--nav-hover); }' +
      '.libui-hit-label { color: var(--link); }' +
      '.libui-hit-sub { color: var(--muted); font-size: .82rem; }' +
      '.libui-cbook { margin: .9rem 0; padding-bottom: .6rem; border-bottom: 1px solid var(--border); }' +
      '.libui-cbook-title { font-weight: 600; color: var(--link); text-decoration: none; }' +
      '.libui-cbook-title:hover { text-decoration: underline; }' +
      '.libui-cbook-by { color: var(--muted); }' +
      '.libui-snips { list-style: none; margin: .4rem 0 0; padding: 0; display: flex; flex-direction: column; gap: .3rem; }' +
      '.libui-snip { color: var(--subtle-fg); font-size: .92rem; line-height: 1.4; }' +
      '.libui-snip mark { background: var(--nav-hover); color: inherit; padding: 0 .1em; border-radius: 3px; }' +
      '.libui-back { background: none; border: none; color: var(--link); font: inherit; padding: .3rem 0; cursor: pointer; }' +
      '.libui-detail-row { display: flex; gap: 1rem; flex-wrap: wrap; margin-top: .4rem; }' +
      '.libui-detail-cover { width: 150px; aspect-ratio: 2/3; object-fit: contain; border-radius: 10px;' +
      ' border: 1px solid var(--card-border); background: var(--bg); }' +
      '.libui-detail-info { flex: 1; min-width: 220px; }' +
      '.libui-detail-title { font-size: 1.3rem; margin: 0 0 .25rem; }' +
      '.libui-detail-by { color: var(--subtle-fg); }' +
      '.libui-detail-dl { margin: .9rem 0 0; font-size: .9rem; }' +
      '.libui-detail-dl dt { color: var(--muted); font-size: .78rem; margin-top: .5rem; }' +
      '.libui-detail-dl dd { margin: 0; }' +
      '.libui-detail-actions { display: flex; flex-wrap: wrap; gap: .6rem; margin-top: 1rem; }' +
      '.libui-btn { font: inherit; padding: .5rem .9rem; border-radius: 8px; cursor: pointer; text-decoration: none;' +
      ' border: 1px solid var(--accent); background: var(--accent); color: #fff; }' +
      '.libui-field { display: flex; align-items: center; gap: .8rem; margin: .7rem 0; }' +
      '.libui-field-label { min-width: 9rem; color: var(--fg); }' +
      '.libui-select { font: inherit; padding: .35rem .5rem; border-radius: 6px;' +
      '  border: 1px solid var(--border); background: var(--surface); color: var(--fg); }' +
      '.libui-settings-extra { margin-top: 1.2rem; padding-top: 1rem; border-top: 1px solid var(--border); }' +
      // ── floating nav (the shared app-menu chrome) ───────────────────────────
      // A button at the bottom-right corner; tapping it fans the items out on a quarter-circle
      // ARC centred on that corner, so a right thumb can reach them all. A dimmed scrim gives
      // the menu a floating feel above the page. The arc maths live in the renderer (layout()).
      '.libui-nav { position: fixed; z-index: 1000;' +
      '  right: calc(env(safe-area-inset-right, 0px) + 44px);' +
      '  bottom: calc(env(safe-area-inset-bottom, 0px) + 44px); width: 0; height: 0; }' +
      '.libui-nav-scrim { position: fixed; inset: 0; background: rgba(0,0,0,.42);' +
      '  opacity: 0; pointer-events: none; transition: opacity .2s ease;' +
      '  -webkit-backdrop-filter: blur(1.5px); backdrop-filter: blur(1.5px); }' +
      '.libui-nav.open .libui-nav-scrim { opacity: 1; pointer-events: auto; }' +
      '.libui-fab { position: absolute; right: -28px; bottom: -28px; width: 56px; height: 56px;' +
      '  border-radius: 50%; border: none; background: var(--accent); color: #fff; font-size: 24px;' +
      '  line-height: 1; cursor: pointer; box-shadow: 0 4px 16px rgba(0,0,0,.45); padding: 0;' +
      '  display: flex; align-items: center; justify-content: center; -webkit-tap-highlight-color: transparent; }' +
      '.libui-fab-icon { transition: transform .25s ease; }' +
      '.libui-nav.open .libui-fab-icon { transform: rotate(90deg); }' +
      '.libui-fabmenu { position: absolute; right: 0; bottom: 0; width: 0; height: 0; }' +
      // Each item: an icon-only circular chip (no border, no caption). Collapsed onto the button
      // when closed; translated to its arc position (CSS vars --dx/--dy set by layout()) when open.
      '.libui-navitem { position: absolute; right: 0; bottom: 0; width: 44px; height: 44px;' +
      '  border-radius: 50%; display: flex; align-items: center; justify-content: center;' +
      '  text-decoration: none; color: var(--fg); background: var(--surface); box-shadow: 0 3px 10px rgba(0,0,0,.4);' +
      '  opacity: 0; pointer-events: none; transform: translate(50%, 50%) scale(.3);' +
      '  transition: transform .24s cubic-bezier(.2,.8,.3,1.05), opacity .18s ease;' +
      '  -webkit-tap-highlight-color: transparent; }' +
      '.libui-nav.open .libui-navitem { opacity: 1; pointer-events: auto;' +
      '  transform: translate(calc(50% + var(--dx, 0px)), calc(50% + var(--dy, 0px))); }' +
      '.libui-navitem.active { background: var(--accent); color: #fff; }' +
      '.libui-navsvg { width: 22px; height: 22px; display: block; }' +
      // ── bar form: a normal horizontal menu. Shown whenever the renderer is asked for the
      // 'bar' variant — WHEN to use it (vs the floating button) is the implementation's choice.
      '.libui-navbar { display: flex; flex-wrap: wrap; align-items: center; gap: .15rem;' +
      '  margin: .1rem 0 .6rem; padding-bottom: .4rem; border-bottom: 1px solid var(--border); }' +
      '.libui-barlink { display: inline-flex; align-items: center; gap: .4rem; padding: .4rem .7rem;' +
      '  border-radius: 8px; text-decoration: none; color: var(--link); font-weight: 500; }' +
      '.libui-barlink:hover { background: var(--nav-hover); }' +
      '.libui-barlink.active { background: var(--nav-active-bg); color: var(--nav-active-fg); }' +
      '.libui-barlink .libui-navsvg { width: 18px; height: 18px; }' +
      '.libui-barlabel { font-size: .95rem; }' +
      // ── Ask: grounded Q&A panel ─────────────────────────────────────────────
      '.libui-ask { display: flex; flex-direction: column; height: 100%; max-width: 46rem; }' +
      '.libui-ask-thread { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: .8rem; padding: .2rem 0 1rem; }' +
      '.libui-ask-turn { display: flex; flex-direction: column; gap: .3rem; }' +
      '.libui-ask-bubble { padding: .55rem .75rem; border-radius: 12px; border: 1px solid var(--border); line-height: 1.5; }' +
      '.libui-ask-user { align-items: flex-end; }' +
      '.libui-ask-user .libui-ask-bubble { background: var(--nav-active-bg); color: var(--nav-active-fg);' +
      '  border-color: var(--nav-active-bg); max-width: 85%; }' +
      '.libui-ask-assistant .libui-ask-bubble { background: var(--surface); color: var(--fg); }' +
      '.libui-ask-bubble p { margin: .4rem 0; } .libui-ask-bubble p:first-child { margin-top: 0; }' +
      '.libui-ask-bubble p:last-child { margin-bottom: 0; }' +
      '.libui-ask-bubble code { background: var(--surface-2); padding: 0 .25em; border-radius: 4px; }' +
      '.libui-ask-sources { padding: .4rem .6rem; border-left: 2px solid var(--border); font-size: .88rem; }' +
      '.libui-ask-sources-head { color: var(--muted); font-weight: 700; margin-bottom: .25rem; }' +
      '.libui-ask-src { display: block; text-decoration: none; color: var(--fg); padding: .12rem 0; }' +
      'a.libui-ask-src { color: var(--link); } a.libui-ask-src:hover { text-decoration: underline; }' +
      '.libui-ask-src-loc { color: var(--muted); } .libui-ask-src-file { color: var(--muted); font-size: .82rem; }' +
      '.libui-ask-timing { color: var(--muted); font-size: .8rem; font-style: italic; }' +
      '.libui-ask-form { display: flex; gap: .5rem; align-items: flex-end; padding-top: .5rem; border-top: 1px solid var(--border); }' +
      '.libui-ask-input { flex: 1; resize: none; min-height: 2.4rem; max-height: 9rem; padding: .5rem .6rem; font: inherit;' +
      '  border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--fg); box-sizing: border-box; }' +
      '.libui-ask-model { padding: .45rem .5rem; font: inherit; border: 1px solid var(--border); border-radius: 8px;' +
      '  background: var(--surface); color: var(--fg); }' +
      '.libui-ask-send { padding: .5rem .9rem; border: 1px solid var(--accent); background: var(--accent); color: #fff;' +
      '  border-radius: 8px; cursor: pointer; font: inherit; }' +
      '.libui-ask-send:disabled { opacity: .5; cursor: default; }';
    var el = document.createElement('style');
    el.id = 'libui-ui-styles'; el.textContent = css;
    document.head.appendChild(el);
  })();

  var esc = (window.Typeahead && window.Typeahead.esc) || function (s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  };
  function el(tag, cls) { var e = document.createElement(tag); if (cls) e.className = cls; return e; }
  function msg(text) { var p = el('p', 'libui-msg'); p.textContent = text; return p; }
  function debounce(fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; }
  function input(opts, ph) {
    var i = el('input', 'libui-q'); i.type = 'search'; i.autocomplete = 'off';
    i.placeholder = opts.placeholder || ph; if (opts.initialQuery) i.value = opts.initialQuery;
    return i;
  }

  // FTS snippet() emits plain text with [..] around matches and … for elision.
  // Escape everything, then turn the literal brackets into <mark> highlights.
  function markSnippet(s) { return esc(s).replace(/\[(.*?)\]/g, '<mark>$1</mark>'); }

  // ── Metadata Search: a poster grid (reuses the shared Shelf tile) ─────────
  function search(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var wrap = el('div', 'libui-feature libui-search');
    var inp = input(opts, 'Search the library…');
    var res = el('div', 'libui-results');
    wrap.appendChild(inp); wrap.appendChild(res); host.appendChild(wrap);
    var hrefFor = function (eid) { return platform.nav.hrefFor(C.editionRef(eid)); };
    async function run() {
      var vm = await C.searchVM(platform, inp.value);
      res.innerHTML = '';
      if (vm.error) { res.appendChild(msg('Search failed — ' + vm.error)); return; }
      if (vm.empty) {
        res.appendChild(msg(vm.offline ? 'Offline.' :
          vm.q ? 'No books match “' + vm.q + '”.' : 'No books.'));
        return;
      }
      var grid = el('div', 'tile-grid');
      vm.cards.forEach(function (b) { grid.appendChild(window.Shelf.tile(b, { art: 'cover', hrefFor: hrefFor })); });
      res.appendChild(grid);
      window.Shelf.enhance(res);
    }
    inp.addEventListener('input', debounce(run, 180));
    run();
    if (opts.autofocus) inp.focus();
    return inp;
  }

  // ── Browse: type-grouped results with filter chips ────────────────────────
  function browse(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var wrap = el('div', 'libui-feature libui-browse');
    var inp = input(opts, 'Search editions, works, people, subjects…');
    var chips = el('div', 'libui-chips');
    var res = el('div', 'libui-results');
    wrap.appendChild(inp); wrap.appendChild(chips); wrap.appendChild(res); host.appendChild(wrap);
    var state = { only: opts.only || null, vm: null };

    function chip(text, key) {
      var b = el('button', 'libui-chip' + (state.only === key ? ' active' : ''));
      b.type = 'button'; b.textContent = text;
      b.addEventListener('click', function () { state.only = key; paint(); });
      return b;
    }
    function paint() {
      chips.innerHTML = ''; res.innerHTML = '';
      var vm = state.vm; if (!vm) return;
      if (vm.error) { res.appendChild(msg('Search failed — ' + vm.error)); return; }
      if (vm.empty) {
        res.appendChild(msg(vm.offline ? 'Offline — Browse needs the library data loaded.' :
          vm.q ? 'No matches for “' + vm.q + '”.' : ''));
        return;
      }
      var groups = vm.groups.filter(function (g) { return g.hits.length; });
      if (groups.length > 1) {
        chips.appendChild(chip('All', null));
        groups.forEach(function (g) { chips.appendChild(chip(g.labelPlural + ' (' + g.hits.length + ')', g.key)); });
      }
      groups.forEach(function (g) {
        if (state.only && g.key !== state.only) return;
        var sec = el('section', 'libui-group');
        var h = el('h3', 'libui-group-head'); h.textContent = g.labelPlural + ' · ' + g.hits.length; sec.appendChild(h);
        var hits = el('div', 'libui-hits');
        g.hits.forEach(function (hit) {
          var href = hit.ref ? platform.nav.hrefFor(hit.ref) : null;
          var a = el('a', 'libui-hit'); if (href) a.href = href;
          a.innerHTML = '<span class="libui-hit-label">' + esc(hit.label) + '</span>' +
            (hit.sublabel ? '<span class="libui-hit-sub">' + esc(hit.sublabel) + '</span>' : '');
          hits.appendChild(a);
        });
        sec.appendChild(hits); res.appendChild(sec);
      });
    }
    async function run() { state.vm = await C.browseVM(platform, inp.value, null); paint(); }
    inp.addEventListener('input', debounce(run, 220));
    if (inp.value) run();
    if (opts.autofocus) inp.focus();
    return inp;
  }

  // ── Content search: full-text snippets grouped by book ────────────────────
  function content(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var wrap = el('div', 'libui-feature libui-content');
    var inp = input(opts, 'Search inside books…');
    var res = el('div', 'libui-results');
    wrap.appendChild(inp); wrap.appendChild(res); host.appendChild(wrap);
    async function run() {
      if (!inp.value.trim()) { res.innerHTML = ''; res.appendChild(msg('Type to search the full text of every book.')); return; }
      res.innerHTML = ''; res.appendChild(msg('Searching…'));
      var vm = await C.contentVM(platform, inp.value);
      res.innerHTML = '';
      if (vm.error) { res.appendChild(msg('Content search failed — ' + vm.error)); return; }
      if (!vm.available) {
        res.appendChild(msg(vm.offline
          ? 'Content search is offline. Turn on “offline content search” in Settings to search inside books without a connection.'
          : 'Content search is unavailable (the in-book text index isn’t built).'));
        return;
      }
      if (vm.empty) { res.appendChild(msg('No passages match “' + vm.q + '”.')); return; }
      vm.books.forEach(function (b) {
        var card = el('div', 'libui-cbook');
        var href = platform.nav.hrefFor(b.ref);
        var head = '<a class="libui-cbook-title"' + (href ? ' href="' + esc(href) + '"' : '') + '>' + esc(b.title) + '</a>';
        if (b.authors && b.authors.length) head += '<span class="libui-cbook-by"> — ' + esc(b.authors.slice(0, 3).join(', ')) + '</span>';
        card.innerHTML = '<div class="libui-cbook-head">' + head + '</div>';
        var snips = el('ul', 'libui-snips');
        b.snippets.forEach(function (s) { var li = el('li', 'libui-snip'); li.innerHTML = markSnippet(s); snips.appendChild(li); });
        card.appendChild(snips); res.appendChild(card);
      });
    }
    inp.addEventListener('input', debounce(run, 300));
    run();
    if (opts.autofocus) inp.focus();
    return inp;
  }

  // ── Book detail (read-only): cover + metadata + Read controls ─────────────
  // opts.eid (required). opts.onBack → renders a ‹ Back control. The Read action uses
  // platform.openBook(eid, holding) when present (in-app reader), else an <a> to
  // platform.nav.readHref(eid, holding) (falling back to /holding/<id>/file).
  function detail(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var wrap = el('div', 'libui-feature libui-detail');
    if (opts.onBack) {
      var back = el('button', 'libui-back'); back.type = 'button'; back.textContent = '‹ Back';
      back.addEventListener('click', opts.onBack); wrap.appendChild(back);
    }
    var body = el('div'); wrap.appendChild(body); host.appendChild(wrap);
    body.appendChild(msg('Loading…'));
    (async function () {
      var vm = await C.detailVM(platform, opts.eid);
      body.innerHTML = '';
      if (vm.missing) { body.appendChild(msg('Not found.')); return; }
      if (vm.offline) { body.appendChild(msg('Offline — this book isn’t in the local copy.')); return; }
      if (vm.error) { body.appendChild(msg('Couldn’t load this book — ' + vm.error)); return; }
      var row = el('div', 'libui-detail-row');
      var cover = el('img', 'libui-detail-cover'); cover.alt = ''; cover.src = vm.coverUrl;
      cover.setAttribute('onerror', 'this.remove()');
      var info = el('div', 'libui-detail-info');
      var h1 = el('h1', 'libui-detail-title'); h1.textContent = vm.title; info.appendChild(h1);
      var by = el('div', 'libui-detail-by'); by.textContent = vm.by; info.appendChild(by);
      var dl = el('dl', 'libui-detail-dl');
      function addRow(k, val) { if (!val) return; var dt = el('dt'); dt.textContent = k; var dd = el('dd'); dd.textContent = val; dl.appendChild(dt); dl.appendChild(dd); }
      // Names → clickable subject links via the neutral ref (web → /subject or /find,
      // PWA → name-keyed #/subject page). Falls back to plain text if no nav target.
      function addSubjectRow(k, names) {
        if (!names || !names.length) return;
        var dt = el('dt'); dt.textContent = k; var dd = el('dd');
        names.forEach(function (name, i) {
          if (i) dd.appendChild(document.createTextNode(' · '));
          var href = platform.nav && platform.nav.hrefFor
            ? platform.nav.hrefFor({ kind: 'subject', q: name }) : null;
          if (href) { var a = el('a'); a.href = href; a.textContent = name; dd.appendChild(a); }
          else dd.appendChild(document.createTextNode(name));
        });
        dl.appendChild(dt); dl.appendChild(dd);
      }
      if (vm.translators.length) addRow('Translators', vm.translators.join(', '));
      addSubjectRow('Subjects', vm.subjects);
      if (vm.isbns.length) addRow('ISBN', vm.isbns.join(', '));
      if (vm.publisher || vm.year) addRow('Published', [vm.publisher, vm.year].filter(Boolean).join(', '));
      if (vm.tradition) addRow('Tradition', vm.tradition);
      if (vm.workTitles.length) addRow('Works', vm.workTitles.join(' · '));
      info.appendChild(dl);
      var actions = el('div', 'libui-detail-actions');
      vm.holdings.forEach(function (h) {
        var label = ('Read ' + String(h.kind || h.format || '').toUpperCase()).trim();
        if (typeof platform.openBook === 'function') {
          var b = el('button', 'libui-btn'); b.type = 'button'; b.textContent = label;
          b.addEventListener('click', function () { platform.openBook(vm.eid, h); });
          actions.appendChild(b);
        } else {
          var a = el('a', 'libui-btn');
          a.href = (platform.nav && platform.nav.readHref) ? platform.nav.readHref(vm.eid, h)
            : (h.holding_id != null ? '/holding/' + h.holding_id + '/file' : '#');
          a.textContent = label; actions.appendChild(a);
        }
      });
      if (!vm.holdings.length) { var p = el('div', 'libui-msg'); p.textContent = 'No file on record.'; actions.appendChild(p); }
      info.appendChild(actions);
      row.appendChild(cover); row.appendChild(info); body.appendChild(row);
      if (opts.onReady) opts.onReady(vm);
    })();
    return wrap;
  }

  // ── Device prefs application (DOM side of the neutral pref keys) ───────────
  var prefs = {
    applyTheme: function (v) {
      if (v === 'light' || v === 'dark') document.documentElement.setAttribute('data-theme', v);
      else document.documentElement.removeAttribute('data-theme');
    },
    applyShelf: function (v) {
      document.documentElement.setAttribute('data-shelf', v === 'cover' ? 'cover' : 'spine');
    }
  };

  function field(label, control) {
    var f = el('label', 'libui-field');
    var s = el('span', 'libui-field-label'); s.textContent = label;
    f.appendChild(s); f.appendChild(control); return f;
  }
  function selectFor(options, current, onChange) {
    var sel = el('select', 'libui-select');
    options.forEach(function (o) {
      var op = document.createElement('option'); op.value = o.value; op.textContent = o.label;
      if (o.value === current) op.selected = true; sel.appendChild(op);
    });
    sel.addEventListener('change', function () { onChange(sel.value); });
    return sel;
  }

  // ── Settings: theme + shelf art; host injects platform-specific extras ────
  function settings(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var vm = C.settingsVM(platform);
    var wrap = el('div', 'libui-feature libui-settings');
    wrap.appendChild(field('Colour theme', selectFor(vm.themeOptions, vm.theme, function (v) {
      if (v === 'auto') platform.prefs.remove('theme'); else platform.prefs.set('theme', v);
      prefs.applyTheme(v);
    })));
    wrap.appendChild(field('Shelf appearance', selectFor(vm.shelfOptions, vm.shelfArt, function (v) {
      platform.prefs.set('shelfArt', v); prefs.applyShelf(v);
    })));
    host.appendChild(wrap);
    if (opts.extra) {
      if (typeof opts.extra === 'string') { var d = el('div', 'libui-settings-extra'); d.innerHTML = opts.extra; host.appendChild(d); }
      else host.appendChild(opts.extra);
    }
    return wrap;
  }

  // Nav icons are named after iOS SF Symbols (the native renderer draws them with
  // Image(systemName:)). SF Symbols' font isn't web-embeddable, so web/PWA draw the SAME
  // symbols as inline stroke SVGs keyed by that name — one name, two faithful renderings.
  var SF_ICONS = {
    'house': '<path d="M3.5 11 L12 4.5 L20.5 11"/><path d="M5.7 9.6 V19.5 H18.3 V9.6"/><path d="M9.8 19.5 v-4.2 h4.4 v4.2"/>',
    'magnifyingglass': '<circle cx="10.5" cy="10.5" r="6"/><line x1="15" y1="15" x2="20.5" y2="20.5"/>',
    'books.vertical': '<rect x="4.3" y="4" width="4" height="16" rx="1"/><rect x="9.3" y="4" width="4" height="16" rx="1"/><rect x="14.4" y="5.4" width="4" height="14.6" rx="1" transform="rotate(11 16.4 12.7)"/>',
    'doc.text': '<rect x="5" y="3.5" width="14" height="17" rx="2.2"/><line x1="8" y1="8.5" x2="16" y2="8.5"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="15.5" x2="13" y2="15.5"/>',
    'viewfinder': '<path d="M4 8.5 V6 A2 2 0 0 1 6 4 H8.5"/><path d="M15.5 4 H18 A2 2 0 0 1 20 6 V8.5"/><path d="M20 15.5 V18 A2 2 0 0 1 18 20 H15.5"/><path d="M8.5 20 H6 A2 2 0 0 1 4 18 V15.5"/><line x1="4.5" y1="12" x2="19.5" y2="12"/>',
    'camera': '<rect x="3.5" y="7" width="17" height="12.5" rx="2.6"/><circle cx="12" cy="13.2" r="3.2"/><path d="M8.8 7 L10.2 4.6 H13.8 L15.2 7"/>',
    'checklist': '<path d="M3.8 6.4 l1.4 1.4 L8 5"/><line x1="11" y1="6.5" x2="20" y2="6.5"/><path d="M3.8 12 l1.4 1.4 L8 10.6"/><line x1="11" y1="12" x2="20" y2="12"/><path d="M3.8 17.6 l1.4 1.4 L8 16.2"/><line x1="11" y1="17.7" x2="20" y2="17.7"/>',
    'slider.horizontal.3': '<line x1="3.5" y1="8.5" x2="20.5" y2="8.5"/><line x1="3.5" y1="15.5" x2="20.5" y2="15.5"/><circle cx="15" cy="8.5" r="2.4"/><circle cx="9" cy="15.5" r="2.4"/>',
    'book': '<path d="M12 6.4 C10 5 6.6 4.6 4.2 5 V17.8 C6.6 17.4 10 17.8 12 19.2 C14 17.8 17.4 17.4 19.8 17.8 V5 C17.4 4.6 14 5 12 6.4 Z"/><line x1="12" y1="6.4" x2="12" y2="19.2"/>',
    'text.bubble': '<path d="M4 6.6 A2.2 2.2 0 0 1 6.2 4.4 H17.8 A2.2 2.2 0 0 1 20 6.6 V13.4 A2.2 2.2 0 0 1 17.8 15.6 H9.5 L6 19 V15.6 A2.2 2.2 0 0 1 4 13.4 Z"/><line x1="7.5" y1="8.4" x2="16.5" y2="8.4"/><line x1="7.5" y1="11.6" x2="13.5" y2="11.6"/>'
  };
  function iconSVG(name) {
    var inner = SF_ICONS[name] || '<circle cx="12" cy="12" r="2.5"/>';
    return '<svg class="libui-navsvg" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
      ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + inner + '</svg>';
  }

  // ── Navigation menu (shared Menu component) ───────────────────────────────
  // A Menu RENDERER that offers presentation FORMS; it does NOT decide which form to use — the
  // calling implementation does (web: bar on desktop / fab on phone; PWA: always fab; native:
  // its own). opts: { items:[{key,label,icon,href,protocol?}], activeKey, ctx (protocol context
  // {local,desktop}), variant:'bar'|'fab' (default 'fab'), label?(fab aria-label), icon?(fab
  // glyph, default ☰) }. Items whose protocol isn't satisfied by ctx are dropped (navVM). Returns
  // { el, setActive(key), open, close, destroy } so an SPA can re-highlight without re-rendering.
  function nav(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var vm = C.navVM(opts.items || [], opts.activeKey, opts.ctx);
    return opts.variant === 'bar' ? navBar(host, vm, opts) : navFab(host, vm, opts);
  }

  // Form 1 — a normal horizontal top menu bar (icon + label links).
  function navBar(host, vm, opts) {
    var byKey = {};
    var bar = el('nav', 'libui-navbar');
    bar.setAttribute('aria-label', opts.ariaLabel || 'Library menu');
    vm.items.forEach(function (it) {
      var a = el('a', 'libui-barlink' + (it.active ? ' active' : ''));
      if (it.href) a.href = it.href;
      a.innerHTML = iconSVG(it.icon) + '<span class="libui-barlabel">' + esc(it.label) + '</span>';
      if (it.key != null) byKey[it.key] = a;
      bar.appendChild(a);
    });
    host.appendChild(bar);
    return {
      el: bar, open: function () {}, close: function () {}, destroy: function () {},
      setActive: function (key) {
        Object.keys(byKey).forEach(function (k) { byKey[k].classList.toggle('active', k === String(key)); });
      }
    };
  }

  // Form 2 — a floating bottom-right button whose items fan out on a quarter-circle arc
  // (right-thumb reach) over a dimmed scrim.
  function navFab(host, vm, opts) {
    var byKey = {}, itemEls = [];
    var navEl = el('nav', 'libui-nav');
    navEl.setAttribute('aria-label', opts.ariaLabel || 'Library menu');

    var scrim = el('div', 'libui-nav-scrim');
    var menu = el('div', 'libui-fabmenu');
    menu.setAttribute('role', 'menu'); menu.setAttribute('aria-hidden', 'true');
    vm.items.forEach(function (it) {
      var a = el('a', 'libui-navitem' + (it.active ? ' active' : ''));
      if (it.href) a.href = it.href;
      a.setAttribute('role', 'menuitem'); if (it.key != null) a.dataset.navkey = it.key;
      a.innerHTML = iconSVG(it.icon);                       // icon-only; label is for a11y
      if (it.label) { a.setAttribute('aria-label', it.label); a.title = it.label; }
      if (it.key != null) byKey[it.key] = a;
      itemEls.push(a); menu.appendChild(a);
    });

    var toggle = el('button', 'libui-fab'); toggle.type = 'button';
    toggle.setAttribute('aria-label', opts.label || 'Menu');
    toggle.setAttribute('aria-haspopup', 'true'); toggle.setAttribute('aria-expanded', 'false');
    toggle.innerHTML = '<span class="libui-fab-icon">' + esc(opts.icon || '☰') + '</span>';

    navEl.appendChild(scrim); navEl.appendChild(menu); navEl.appendChild(toggle);
    host.appendChild(navEl);

    // Place each item on a quarter-circle arc from straight-up to straight-left, centred on the
    // button. Radius grows with the item count (so they don't crowd) but is capped to the
    // viewport. A small per-item delay makes them bloom outward. CSS reads --dx/--dy when open.
    var RAD = Math.PI / 180;
    function layout() {
      var n = itemEls.length; if (!n) return;
      var a0 = (90 + 6) * RAD, a1 = (180 - 6) * RAD;             // up→left, small edge padding
      var span = a1 - a0;
      var reach = Math.min(window.innerWidth, window.innerHeight) * 0.55;
      var need = n > 1 ? (54 * (n - 1)) / (span || 1) : 0;        // arc-length / angle = radius
      var R = Math.max(96, Math.min(reach, need));
      itemEls.forEach(function (a, i) {
        var ang = n > 1 ? a0 + span * (i / (n - 1)) : (a0 + a1) / 2;
        a.style.setProperty('--dx', (R * Math.cos(ang)).toFixed(1) + 'px');   //  0 → -R (leftward)
        a.style.setProperty('--dy', (-R * Math.sin(ang)).toFixed(1) + 'px');  // -R → 0 (upward, neg y)
        a.style.transitionDelay = (i * 18) + 'ms';
      });
    }

    function close() { navEl.classList.remove('open'); toggle.setAttribute('aria-expanded', 'false'); menu.setAttribute('aria-hidden', 'true'); }
    function open() { layout(); navEl.classList.add('open'); toggle.setAttribute('aria-expanded', 'true'); menu.setAttribute('aria-hidden', 'false'); }
    toggle.addEventListener('click', function (e) { e.stopPropagation(); navEl.classList.contains('open') ? close() : open(); });
    menu.addEventListener('click', close);                                  // any item picked → collapse
    scrim.addEventListener('click', close);
    // document/window listeners outlive navEl, so keep refs to remove on destroy() (the web
    // re-mounts the menu when the viewport crosses the bar↔fab breakpoint).
    var onDocClick = function (e) { if (!navEl.contains(e.target)) close(); };
    var onKey = function (e) { if (e.key === 'Escape') close(); };
    var onResize = function () { if (navEl.classList.contains('open')) layout(); };
    document.addEventListener('click', onDocClick);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', onResize);

    return {
      el: navEl, open: open, close: close,
      destroy: function () {
        document.removeEventListener('click', onDocClick);
        document.removeEventListener('keydown', onKey);
        window.removeEventListener('resize', onResize);
      },
      setActive: function (key) {
        Object.keys(byKey).forEach(function (k) { byKey[k].classList.toggle('active', k === String(key)); });
      }
    };
  }

  // ── Finder: the n-way Search — one box + a MODE selector (Edition / Work / Person / Subject/Series)
  // over the shared C.SEARCH_FIELDS. Each mode scopes the SAME C.browseVM matcher to one group, so the
  // web, PWA and the native app run IDENTICAL search logic (this is the DOM implementation of the app's
  // mode selector). Replaces the single-box `search` for the PWA's Books tab.
  function finder(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var FIELDS = C.SEARCH_FIELDS || [];
    var GROUP = { book_title: 'editions', work_title: 'works', person: 'people', subject: 'subjects' };
    var LABEL = { book_title: 'Edition', work_title: 'Work', person: 'Person', subject: 'Subject/Series' };
    var PROMPT = { book_title: 'Edition title or number…', work_title: 'Work title…',
                   person: 'Author or translator…', subject: 'Subject or series…' };
    var field = (FIELDS[0] && FIELDS[0].key) || 'book_title';

    var wrap = el('div', 'libui-feature libui-finder');
    var bar = el('div', 'libui-finder-bar');
    var sel = el('select', 'libui-finder-mode'); sel.setAttribute('aria-label', 'Search by');
    FIELDS.forEach(function (f) {
      var o = document.createElement('option'); o.value = f.key; o.textContent = LABEL[f.key] || f.label; sel.appendChild(o);
    });
    var inp = input(opts, PROMPT[field]);
    bar.appendChild(sel); bar.appendChild(inp);
    var res = el('div', 'libui-results');
    wrap.appendChild(bar); wrap.appendChild(res); host.appendChild(wrap);

    async function run() {
      var vm = await C.browseVM(platform, inp.value, GROUP[field]);
      res.innerHTML = '';
      if (vm.error) { res.appendChild(msg('Search failed — ' + vm.error)); return; }
      if (vm.empty) {
        res.appendChild(msg(vm.offline ? 'Offline.' :
          (vm.q ? 'No ' + (LABEL[field] || '').toLowerCase() + ' matches “' + vm.q + '”.' : '')));
        return;
      }
      vm.groups.forEach(function (g) {
        var hits = el('div', 'libui-hits');
        g.hits.forEach(function (hit) {
          var href = hit.ref ? platform.nav.hrefFor(hit.ref) : null;
          var a = el('a', 'libui-hit'); if (href) a.href = href;
          a.innerHTML = '<span class="libui-hit-label">' + esc(hit.label) + '</span>' +
            (hit.sublabel ? '<span class="libui-hit-sub">' + esc(hit.sublabel) + '</span>' : '');
          hits.appendChild(a);
        });
        res.appendChild(hits);
      });
    }
    sel.addEventListener('change', function () { field = sel.value; inp.placeholder = PROMPT[field]; run(); });
    inp.addEventListener('input', debounce(run, 180));
    if (inp.value) run();
    if (opts.autofocus) inp.focus();
    return inp;
  }

  // ── Ask: grounded Q&A against the RAG backend ─────────────────────────────
  // The DOM renderer for the "Ask" feature. It depends ONLY on the platform ADAPTER methods
  // `data.ask(model, messages)` and `data.askModels()` (web → /api/v1/ask; a PWA or native
  // adapter implements the same two). Everything here — history, citation deep-links, timing —
  // is shared across surfaces, exactly like the other LibraryUI components.

  // Cut the markdown Sources/⏱ footers off the answer body: we render those STRUCTURALLY
  // (from vm.sources / vm.timing), so they mustn't also appear as text in the bubble.
  function stripFooters(content) {
    if (!content) return '';
    var marks = ['\n\n---', '\n\n_⏱', '\n\n_(No sources retrieved'];
    var cut = content.length;
    marks.forEach(function (m) { var i = content.indexOf(m); if (i >= 0 && i < cut) cut = i; });
    return content.slice(0, cut).trim();
  }

  // The backend hides its /source multi-turn state in an HTML comment (<!--BDRAG_SCOPE …-->).
  // It MUST stay in the message history we send back (so scoping survives across turns), but must
  // never be shown — and since renderMarkdown escapes HTML first, an un-stripped comment would leak
  // as visible text. So strip HTML comments for DISPLAY only.
  function stripHtmlComments(s) { return (s || '').replace(/<!--[\s\S]*?-->/g, ''); }

  // Minimal, safe markdown → HTML: escape FIRST, then a few inlines + paragraphs. Enough for a
  // skeleton; a real markdown renderer can drop in here without touching the rest of the component.
  function renderMarkdown(src) {
    return esc(src || '').split(/\n{2,}/).map(function (para) {
      var html = para
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
        .replace(/\n/g, '<br>');
      return '<p>' + html + '</p>';
    }).join('');
  }

  function askSources(sources, platform) {
    var box = el('div', 'libui-ask-sources');
    var head = el('div', 'libui-ask-sources-head'); head.textContent = 'Sources'; box.appendChild(head);
    sources.forEach(function (s) {
      // Deep-link the citation into the catalogue when the source resolved to a local edition
      // (s.ref set by askVM); otherwise render it as plain text.
      var href = s.ref && platform.nav && platform.nav.hrefFor ? platform.nav.hrefFor(s.ref) : null;
      var row = el(href ? 'a' : 'div', 'libui-ask-src'); if (href) row.href = href;
      var label = '[' + esc(String(s.n)) + '] ' + esc(s.title || 'source');
      if (s.location) label += ' <span class="libui-ask-src-loc">· ' + esc(s.location) + '</span>';
      if (s.file) label += ' <span class="libui-ask-src-file">(' + esc(s.file) + ')</span>';
      row.innerHTML = label; box.appendChild(row);
    });
    return box;
  }

  function askTiming(t) {
    if (!t || t.total_ms == null) return null;
    var sec = function (ms, dp) { return (Number(ms || 0) / 1000).toFixed(dp) + 's'; };
    var gen = t.cached ? 'generate cached' : 'generate ' + sec(t.generate_ms, 1);
    var p = el('div', 'libui-ask-timing');
    p.textContent = '⏱ retrieve ' + sec(t.retrieve_ms, 2) + ' · rerank ' + sec(t.rerank_ms, 2) +
      ' · ' + gen + ' · total ' + sec(t.total_ms, 1);
    return p;
  }

  // opts: { model, placeholder, autofocus, greeting }. Returns { el, focus, reset }.
  function ask(host, platform, opts) {
    opts = opts || {}; host.innerHTML = '';
    var messages = [];                       // OpenAI-style history — sent verbatim every turn
    var model = opts.model || 'library-fast';
    var busy = false;

    var wrap = el('div', 'libui-feature libui-ask');
    var thread = el('div', 'libui-ask-thread');
    var form = el('form', 'libui-ask-form');
    var modelSel = el('select', 'libui-ask-model'); modelSel.setAttribute('aria-label', 'Model');
    var ta = el('textarea', 'libui-ask-input'); ta.rows = 1;
    ta.placeholder = opts.placeholder || 'Ask the library…';
    var send = el('button', 'libui-ask-send'); send.type = 'submit'; send.textContent = 'Ask';
    form.appendChild(modelSel); form.appendChild(ta); form.appendChild(send);
    wrap.appendChild(thread); wrap.appendChild(form); host.appendChild(wrap);

    if (opts.greeting) {
      var g = el('div', 'libui-ask-turn libui-ask-assistant');
      var gb = el('div', 'libui-ask-bubble'); gb.innerHTML = renderMarkdown(opts.greeting);
      g.appendChild(gb); thread.appendChild(g);
    }

    C.askModelsVM(platform).then(function (vm) {
      modelSel.innerHTML = '';
      vm.models.forEach(function (m) {
        var o = document.createElement('option'); o.value = m.id; o.textContent = m.label || m.id;
        if (m.id === model) o.selected = true; modelSel.appendChild(o);
      });
    });
    modelSel.addEventListener('change', function () { model = modelSel.value; });

    function scrollDown() { thread.scrollTop = thread.scrollHeight; }
    function addUser(text) {
      var turn = el('div', 'libui-ask-turn libui-ask-user');
      var b = el('div', 'libui-ask-bubble'); b.textContent = text; turn.appendChild(b);
      thread.appendChild(turn); scrollDown();
    }
    function addPending() {
      var turn = el('div', 'libui-ask-turn libui-ask-assistant');
      var b = el('div', 'libui-ask-bubble'); b.appendChild(msg('Thinking…')); turn.appendChild(b);
      thread.appendChild(turn); scrollDown(); return turn;
    }
    function renderAssistant(turn, vm) {
      turn.innerHTML = '';
      var b = el('div', 'libui-ask-bubble');
      if (!vm.available) { b.appendChild(msg(vm.error || 'Ask is offline.')); turn.appendChild(b); scrollDown(); return; }
      // Strip the markdown Sources/⏱ footers ONLY when we have structured data to render in their
      // place. Against a backend that returns markdown-only (no structured sources/timing), keep
      // the content verbatim so citations still show.
      var hasStructured = (vm.sources && vm.sources.length) || (vm.timing && vm.timing.total_ms != null);
      var shown = hasStructured ? stripFooters(vm.content) : vm.content;
      b.innerHTML = renderMarkdown(stripHtmlComments(shown).trim());
      turn.appendChild(b);
      if (vm.sources && vm.sources.length) turn.appendChild(askSources(vm.sources, platform));
      var tl = askTiming(vm.timing); if (tl) turn.appendChild(tl);
      scrollDown();
    }

    async function submit() {
      var text = ta.value.trim();
      if (!text || busy) return;
      busy = true; send.disabled = true; ta.value = ''; ta.style.height = 'auto';
      addUser(text);
      messages.push({ role: 'user', content: text });
      var pending = addPending();
      var vm = await C.askVM(platform, model, messages);
      renderAssistant(pending, vm);
      // Keep the FULL assistant content in history (it may carry the backend's hidden /source
      // scoping marker), so multi-turn scoping survives. Only push on a real answer.
      if (vm.available) messages.push({ role: 'assistant', content: vm.content });
      busy = false; send.disabled = false; ta.focus();
    }

    form.addEventListener('submit', function (e) { e.preventDefault(); submit(); });
    ta.addEventListener('keydown', function (e) {              // Enter sends; Shift+Enter = newline
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
    });
    ta.addEventListener('input', function () {                 // auto-grow the textarea
      ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 144) + 'px';
    });
    if (opts.autofocus) ta.focus();
    return { el: wrap, focus: function () { ta.focus(); },
             reset: function () { messages = []; thread.innerHTML = ''; } };
  }

  window.LibraryUI = { search: search, browse: browse, finder: finder, content: content, ask: ask, detail: detail, settings: settings, nav: nav, prefs: prefs };
})();
