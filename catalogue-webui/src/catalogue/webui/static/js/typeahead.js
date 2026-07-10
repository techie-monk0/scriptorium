/* ── Reusable type-ahead picker module (shared: web + PWA) ───────────────────
   Defines a global `window.Typeahead`. Self-contained: injects its own styles
   (once) so any surface that loads this file gets the complete component — no
   Jinja include required. The web build loads it via the `_typeahead.html`
   shim; the PWA loads it directly from app.html.

   A live, debounced search box that renders matches as a clickable picker list.

   Usage (JS):
     Typeahead.mount(hostEl, {
       searchUrl: q => `/picker/person/search?q=${encodeURIComponent(q)}&exclude=${id}`,
       parse:     json => json.matches,                 // → [{...}] (required)
       render:    m => `<code>#${m.id}</code> ${esc(m.name)}`,  // inner HTML/option (required; caller escapes)
       onPick:    m => doSomething(m),                  // required
       onEnter:   q => runSearch(q),                    // optional: override Enter (see below)
       placeholder: 'start typing a name…',             // optional
       emptyHtml:   q => `no match for "${q}"`,          // optional
       minChars: 2, debounce: 200, autofocus: true,      // optional
     });
   Returns the <input>. Options use class .cand-opt so, inside the book-browser
   shell, the shared 1–9 / ↵ keyboard selection works on them too. Standalone,
   Enter picks the top match and ↓ steps into the list; click always works.
   Pass `onEnter` to repurpose Enter (e.g. a search bar submits its query instead of
   binding the top hit) — picking a specific candidate then happens via click. */
(function () {
  if (window.Typeahead) return;                       // define once per page

  // Inject styles once. Depends only on the shared theme tokens (--border,
  // --surface-2, --nav-hover, --nav-active-bg/-fg, --muted, --link) that both
  // _base.html and pwa.css define, so it themes correctly on every surface.
  (function injectStyles() {
    if (document.getElementById('ta-styles')) return;
    var css =
      '.ta-q { width: 100%; max-width: 30rem; padding: .3rem .4rem; font: inherit; box-sizing: border-box; }' +
      '.ta-results { margin: .3rem 0; }' +
      '.ta-results .cand-list { display: flex; flex-direction: column; gap: .2rem; }' +
      '.ta-results .cand-opt { display: block; width: 100%; text-align: left; cursor: pointer;' +
      '  padding: .3rem .5rem; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-2); font: inherit; }' +
      '.ta-results .cand-opt:hover { background: var(--nav-hover); }' +
      '.ta-results .cand-opt.sel { background: var(--nav-active-bg); color: var(--nav-active-fg); border-color: var(--nav-active-bg); }' +
      '.ta-results .cand-opt code, .ta-results .cand-opt small { color: inherit; }' +
      '.ta-results .cand-opt .num { display: inline-block; min-width: 1.2em; font-weight: 700; color: var(--link); }' +
      '.ta-results .cand-opt.sel .num { color: #9cf; }' +
      '.ta-results .hint { color: var(--muted); font-size: .82rem; }' +
      '.ta-spin { display: inline-block; width: .85em; height: .85em; margin-right: .3rem; vertical-align: -2px;' +
      '  border: 2px solid #cdd6e6; border-top-color: var(--link); border-radius: 50%; animation: ta-spin .7s linear infinite; }' +
      '@keyframes ta-spin { to { transform: rotate(360deg); } }' +
      '.ta-elapsed { color: var(--muted); font-variant-numeric: tabular-nums; }';
    var el = document.createElement('style');
    el.id = 'ta-styles';
    el.textContent = css;
    document.head.appendChild(el);
  })();

  const esc = s => String(s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  // A spinner + live elapsed-seconds clock shown WHILE a (possibly slow, e.g. live BDRC /
  // Wikidata authority) search is in flight, so the operator sees it's working. Returns a
  // stop() that halts the clock; the caller replaces the box with results afterward.
  function startSearching(el, label) {
    const t0 = Date.now();
    el.innerHTML = '<p class="hint"><span class="ta-spin"></span>' + esc(label || 'searching…')
      + ' <span class="ta-elapsed">0.0s</span></p>';
    const span = el.querySelector('.ta-elapsed');
    const iv = setInterval(() => { if (span) span.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's'; }, 100);
    return () => clearInterval(iv);
  }

  function mount(host, opts) {
    const minChars = opts.minChars != null ? opts.minChars : 2;
    const debounceMs = opts.debounce != null ? opts.debounce : 200;
    host.innerHTML =
        '<div class="ta"><input class="ta-q" data-cand-search autocomplete="off" placeholder="'
      + esc(opts.placeholder || 'start typing…') + '">'
      + '<div class="ta-results">'
      + (opts.hint ? '<p class="hint">' + opts.hint + '</p>' : '') + '</div></div>';
    const inp = host.querySelector('.ta-q');
    const res = host.querySelector('.ta-results');
    let timer = null, reqSeq = 0;

    async function run() {
      const q = (inp.value || '').trim();
      // Bump the sequence FIRST so any in-flight response from a prior keystroke is
      // ignored — otherwise clearing the box (q < minChars) can be overwritten by a
      // slow fetch that resolves afterwards, leaving stale candidates on screen.
      const seq = ++reqSeq;
      if (q.length < minChars) { res.innerHTML = '<p class="hint">Keep typing…</p>'; return; }
      const stop = startSearching(res, opts.searchingLabel);   // spinner + ticking clock
      let items;
      try { items = opts.parse(await (await fetch(opts.searchUrl(q))).json()); }
      catch (e) { stop(); if (seq === reqSeq) res.innerHTML = '<p class="hint">search failed — retry.</p>'; return; }
      stop();
      if (seq !== reqSeq) return;                      // a newer keystroke already fired
      if (!items || !items.length) {
        res.innerHTML = '<p class="hint">'
          + (opts.emptyHtml ? opts.emptyHtml(esc(q)) : ('no match for “' + esc(q) + '”')) + '</p>';
        return;
      }
      res.innerHTML = '<div class="cand-list">' + items.map((m, n) =>
        '<button type="button" class="cand-opt" data-ta-idx="' + n + '">'
        + '<span class="num">' + (n + 1) + '</span>' + opts.render(m) + '</button>').join('') + '</div>';
      Array.from(res.querySelectorAll('.cand-opt')).forEach((b, n) =>
        b.addEventListener('click', () => opts.onPick(items[n])));
    }

    // Pre-fill + run a query on mount — lets a caller RE-OPEN the picker after a pick with
    // the same search still showing, so the OTHER candidates remain addable (multi-add).
    if (opts.initialQuery) { inp.value = opts.initialQuery; run(); }
    inp.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, debounceMs); });
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        // Opt-in: a caller (e.g. the Browse search bar) can make Enter run the query
        // instead of binding the top hit. Default stays "pick the first candidate".
        if (opts.onEnter) { opts.onEnter(inp.value); return; }
        const first = res.querySelector('.cand-opt'); if (first) first.click();
      } else if (e.key === 'ArrowDown') {
        const first = res.querySelector('.cand-opt');
        if (first) { e.preventDefault(); inp.blur(); first.classList.add('sel'); }
      }
    });
    if (opts.autofocus !== false) inp.focus();
    return inp;
  }

  window.Typeahead = { mount: mount, esc: esc };
})();
