/* ── library-core.js — Tier 2: platform-neutral presenter (NO DOM) ───────────
   The portable heart of the shared frontend. Given a PLATFORM ADAPTER, it turns
   raw data into neutral VIEW-MODELS for the four features (Search / Browse /
   Content search / Settings). It never touches the DOM, `window` (beyond the
   namespace attach), `localStorage`, or any rendering toolkit — so the exact
   same contract is reimplementable in Swift (iOS), Kotlin (Android), etc. The
   per-toolkit renderer (Tier 3: library-ui-dom.js for web + PWA; SwiftUI for
   iOS) consumes these view-models.

   ── Adapter protocol (each surface supplies ONE such object) ─────────────────
     adapter = {
       data: {
         search(q)        -> Promise<[Card]>            // Card below
         browse(q, only)  -> Promise<{groups:[Group]}>  // aggregate shape; hits carry url|ref
         content(q)       -> Promise<{books:[{eid,title,authors,snippets}], available}>
         suggest(q)       -> Promise<[{type,label,sublabel,url|ref}]>
       },
       nav:   { hrefFor(ref) -> string }   // ref → platform navigation target
       prefs: { get(key)->string|null, set(key,val), remove(key) }
       isOffline() -> bool
     }
   A Card is {eid, title, by, cover_url, spine_url} (snake_case to match the
   shared Shelf tile contract + replica rows). A nav `ref` is one of
   {kind:'edition'|'work'|'person', id} | {kind:'subject', q} | {kind:'url', url}. */
(function () {
  if (window.LibraryCore) return;

  // Diacritic + case + DIGRAPH fold, mirroring the server's `fold_key` (NFKD, strip combining
  // marks, lowercase, then IAST/phonetic digraph collapse) so client matching agrees with the
  // server's alias/metadata search: Śāntideva / Shantideva / Santideva → `santideva`, and
  // Bodhicaryāvatāra / Bodhicharyāvatāra both fold the same. Digraphs applied after strip+lowercase.
  var _DIGRAPHS = [['sh', 's'], ['ch', 'c'], ['ph', 'p'], ['th', 't'], ['kh', 'k'],
                   ['gh', 'g'], ['jh', 'j'], ['bh', 'b'], ['dh', 'd'], ['rh', 'r']];
  function _foldBase(s) { return (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase(); }
  function _digraph(t) { for (var i = 0; i < _DIGRAPHS.length; i++) t = t.split(_DIGRAPHS[i][0]).join(_DIGRAPHS[i][1]); return t; }
  function fold(s) { return _digraph(_foldBase(s)); }

  // ── Name canonicalization (SHARED) — regnal-ordinal fold ────────────────────
  // Mirror of the server's ordinal tables (names.py _WORD_ORD / _ROMAN_VAL, honorifics.is_ordinal).
  // `nameKey` folds then maps each ORDINAL token (14th / Fourteenth / XIV) to a `#<n>` marker, so the
  // three forms of one incumbent compare equal while staying distinct from a bare number (volume 14).
  // Used on BOTH the query (client) and the server-baked person forms, so name matching agrees.
  var _ORD_WORD = { first: 1, second: 2, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7, eighth: 8,
    ninth: 9, tenth: 10, eleventh: 11, twelfth: 12, thirteenth: 13, fourteenth: 14, fifteenth: 15,
    sixteenth: 16, seventeenth: 17, eighteenth: 18, nineteenth: 19, twentieth: 20, twentyfirst: 21,
    twentysecond: 22, twentythird: 23, twentyfourth: 24, twentyfifth: 25 };
  var _ROMAN_VAL = { i: 1, ii: 2, iii: 3, iv: 4, v: 5, vi: 6, vii: 7, viii: 8, ix: 9, x: 10, xi: 11,
    xii: 12, xiii: 13, xiv: 14, xv: 15, xvi: 16, xvii: 17, xviii: 18, xix: 19, xx: 20, xxi: 21,
    xxii: 22, xxiii: 23, xxiv: 24, xxv: 25 };
  function _ordinalValue(tok) {
    var t = tok.replace(/\.+$/, '');
    var m = t.match(/^(\d{1,2})(?:st|nd|rd|th)$/);          // 14th (suffix required; bare 14 stays a number)
    if (m) return parseInt(m[1], 10);
    if (_ORD_WORD[t] != null) return _ORD_WORD[t];
    if (t.length >= 2 && _ROMAN_VAL[t] != null) return _ROMAN_VAL[t];   // 2+ char roman; single letters = initials
    return null;
  }
  function nameKey(s) {
    // Detect ordinals on the diacritic-stripped (PRE-digraph) token — the digraph collapse would
    // otherwise turn '14th'→'14t' / 'fourteenth'→'fourteent' and break detection. Non-ordinals digraph.
    return _foldBase(s).split(/\s+/).map(function (t) {
      if (!t) return t;
      var n = _ordinalValue(t);
      return n != null ? ('#' + n) : _digraph(t);
    }).join(' ');
  }

  // ── Neutral nav references (toolkit-agnostic) ───────────────────────────
  function editionRef(eid) { return { kind: 'edition', id: eid }; }

  // Parse a server-emitted web URL (from /api/v1/find hits) into a neutral ref,
  // so a non-web UI can map it to its own navigation instead of following a URL.
  function refFromUrl(url) {
    if (!url) return null;
    var m;
    if ((m = url.match(/[?&]eid=(\d+)/)))            return { kind: 'edition', id: +m[1] };
    if ((m = url.match(/^\/edition\/(\d+)/)))         return { kind: 'edition', id: +m[1] };
    if ((m = url.match(/^\/work\/(\d+)/)))            return { kind: 'work', id: +m[1] };
    if ((m = url.match(/^\/person\/(\d+)/)))          return { kind: 'person', id: +m[1] };
    if ((m = url.match(/^\/subject\/(\d+)/)))         return { kind: 'subject', id: +m[1] };
    return { kind: 'url', url: url };
  }

  // Deterministic art handles (the server route is /edition/<id>/cover.jpg|spine.svg).
  // Keys are snake_case to match the shared Shelf tile contract + the replica rows.
  function artFor(eid) {
    return { cover_url: '/edition/' + eid + '/cover.jpg',
             spine_url: '/edition/' + eid + '/spine.svg' };
  }

  function _offline(platform) { return !!(platform.isOffline && platform.isOffline()); }
  function _errMsg(e) { return String((e && e.message) || e); }

  // ── View-models ─────────────────────────────────────────────────────────
  // Metadata "Search": a grid of book cards.
  async function searchVM(platform, q) {
    q = (q || '').trim();
    try {
      var cards = await platform.data.search(q);
      return { kind: 'search', q: q, cards: cards || [], empty: !(cards && cards.length) };
    } catch (e) {
      if (_offline(platform)) return { kind: 'search', q: q, cards: [], empty: true, offline: true };
      return { kind: 'search', q: q, cards: [], empty: true, error: _errMsg(e) };
    }
  }

  // "Browse": type-grouped results (editions / works / people / subjects / …).
  async function browseVM(platform, q, only) {
    q = (q || '').trim();
    only = only || null;
    if (!q) return { kind: 'browse', q: q, only: only, groups: [], empty: true };
    try {
      var doc = await platform.data.browse(q, only);
      var groups = (doc.groups || []).map(function (g) {
        return {
          key: g.key, label: g.label, labelPlural: g.label_plural || g.labelPlural || g.label,
          count: g.count != null ? g.count : (g.hits || []).length,
          hits: (g.hits || []).map(function (h) {
            return { type: h.type || g.label, label: h.label, sublabel: h.sublabel || '',
                     ref: h.ref || refFromUrl(h.url) };
          })
        };
      });
      var total = groups.reduce(function (n, g) { return n + g.hits.length; }, 0);
      return { kind: 'browse', q: q, only: only, groups: groups, empty: total === 0 };
    } catch (e) {
      if (_offline(platform)) return { kind: 'browse', q: q, only: only, groups: [], empty: true, offline: true };
      return { kind: 'browse', q: q, only: only, groups: [], empty: true, error: _errMsg(e) };
    }
  }

  // "Content search": full-text in-book hits grouped by edition.
  async function contentVM(platform, q) {
    q = (q || '').trim();
    if (!q) return { kind: 'content', q: q, books: [], empty: true, available: true };
    try {
      var doc = await platform.data.content(q);
      var books = (doc.books || []).map(function (b) {
        return { eid: b.eid, title: b.title, authors: b.authors || [],
                 snippets: b.snippets || [], ref: editionRef(b.eid) };
      });
      return { kind: 'content', q: q, books: books, empty: books.length === 0,
               available: doc.available !== false };
    } catch (e) {
      // Content search is the one live-only feature: offline without a local index → not available.
      if (_offline(platform)) return { kind: 'content', q: q, books: [], empty: true, available: false, offline: true };
      return { kind: 'content', q: q, books: [], empty: true, available: true, error: _errMsg(e) };
    }
  }

  // Book detail (read-only): one edition's full metadata. `platform.data.detail(eid)`
  // returns the per-edition row (replica shape) or null. The renderer turns `holdings`
  // into Read controls via `platform.openBook` (in-app reader) or `platform.nav.readHref`.
  async function detailVM(platform, eid) {
    try {
      var e = await platform.data.detail(eid);
      if (!e) return { kind: 'detail', eid: eid, missing: true };
      return {
        kind: 'detail', eid: eid,
        title: e.display_title || e.title || ('edition #' + eid),
        by: (e.authors || []).join(', ') || 'no author',
        authors: e.authors || [], translators: e.translators || [],
        subjects: e.subjects || [], isbns: e.isbns || [],
        publisher: e.publisher || null, year: e.year || null,
        tradition: e.tradition || null,
        workTitles: e.work_titles || [],
        connections: (e.connections || []).map(function (c) { return { eid: c.eid, title: c.title, ref: editionRef(c.eid) }; }),
        coverUrl: e.cover_url || artFor(eid).cover_url,
        holdings: (e.holdings || []).filter(function (h) { return h.has_file; }),
        ref: editionRef(eid)
      };
    } catch (err) {
      if (_offline(platform)) return { kind: 'detail', eid: eid, offline: true };
      return { kind: 'detail', eid: eid, error: _errMsg(err) };
    }
  }

  // ── Home shelves (PURE) ───────────────────────────────────────────────────
  // The home rails are computed from data the client ALREADY holds (the cached replica +
  // its own recently-opened history), so homeVM is a pure function — not an async fetch
  // like the others. This is the single composition of "which rails, in what order" for
  // EVERY surface (web/PWA/native); the per-toolkit renderer only paints it. Mirror of the
  // legacy server `library.home_shelves`, now retired in favour of this shared layer.
  //   replica   : { editions:[row], subject_forest:[node] }   (GET /api/v1/replica, schema≥4)
  //   recentIds : [eid]   the client's own last-opened order (local; empty before any open)
  function _homeCard(row) {
    var art = artFor(row.edition_id);
    return { eid: row.edition_id,
             title: row.display_title || row.title || ('edition #' + row.edition_id),
             by: (row.authors || []).join(', '),
             cover_url: row.cover_url || art.cover_url,
             spine_url: row.spine_url || art.spine_url };
  }
  // Volume sort key, mirroring subject_tree._volume_sort_key: leading integer first (2 < 10),
  // then casefolded string; blank/missing sort last. Returned as a comparable tuple.
  function _volumeSortKey(vol) {
    var s = String(vol == null ? '' : vol).trim();
    if (!s) return [1, 1 << 30, ''];
    var m = s.match(/^(\d+)/);
    return [0, m ? parseInt(m[1], 10) : (1 << 30), s.toLowerCase()];
  }
  function _cmpKey(a, b) {            // element-wise compare of equal-shape tuples
    for (var i = 0; i < a.length; i++) { if (a[i] < b[i]) return -1; if (a[i] > b[i]) return 1; }
    return 0;
  }
  function _cmpStr(a, b) { a = a.toLowerCase(); b = b.toLowerCase(); return a < b ? -1 : (a > b ? 1 : 0); }

  // homeVM also takes `starredIds` (the client's own /api/v1/starred set, like `recentIds` a sibling
  // input — NOT folded into the big replica so toggling a star stays cheap). It drives the Starred
  // rail AND tags every card with `starred` so each surface's cover paints the highlight from ONE
  // source. Recent cards additionally carry a `badge` ('New' on a newly-ADDED book, '' on an opened one).
  // Parse a catalogue timestamp to epoch-ms. Handles date-only ('2024-01-10'), SQLite
  // 'YYYY-MM-DD HH:MM:SS' (UTC, space, no zone), and ISO with a 'T'/zone. Returns NaN if empty.
  function _epochMs(s) {
    if (!s) return NaN;
    var t = s.indexOf('T') === -1 ? s.replace(' ', 'T') : s;
    if (t.length > 10 && !/[Z+]|-\d\d:\d\d$/.test(t.slice(10))) t += 'Z';   // assume UTC when zoneless
    return Date.parse(t);
  }

  function homeVM(replica, recentIds, starredIds, opts) {
    opts = opts || {};
    var perRow = opts.perRow || 40, recent = opts.recent || 24;
    // "Recently added" is bounded to a recency WINDOW (default 30 days) so old, never-read
    // books don't sit in Recent forever — Recent = recently READ ∪ recently ADDED only. The
    // window is measured from replica.exported_at (deterministic; ~= now since the replica is
    // rebuilt on every change). No/!parseable exported_at → no window (include all).
    var recentDays = opts.recentDays == null ? 30 : opts.recentDays;
    var exportedMs = _epochMs(replica && replica.exported_at);
    var addedCutoff = isNaN(exportedMs) ? null : exportedMs - recentDays * 86400000;
    var editions = (replica && replica.editions) || [];
    var forest = (replica && replica.subject_forest) || [];
    recentIds = recentIds || [];
    starredIds = starredIds || [];
    var starredSet = {}; starredIds.forEach(function (id) { starredSet[id] = true; });
    // mk = a home card tagged with its starred state + an optional 'New' badge (Recent rail only).
    function mk(row, badge) {
      var c = _homeCard(row);
      c.starred = !!starredSet[row.edition_id];
      c.badge = badge || '';
      return c;
    }

    var byId = {};
    editions.forEach(function (e) { byId[e.edition_id] = e; });
    // date_added DESC, eid DESC tiebreak; missing date sorts last.
    var byAdded = editions.slice().sort(function (a, b) {
      var da = a.date_added || '', db = b.date_added || '';
      if (da !== db) return da < db ? 1 : -1;
      return b.edition_id - a.edition_id;
    });

    var rails = [];

    // 1. Recent — the merged rail: recently OPENED first (local history, no badge), then the
    //    newest-ADDED books WITHIN THE RECENCY WINDOW not already shown, each badged 'New'. So a
    //    freshly-added book shows on first launch, but an old never-read book falls out over time.
    var seen = {}, recentCards = [];
    recentIds.forEach(function (id) {
      var row = byId[id];
      if (row && !seen[id]) { seen[id] = true; recentCards.push(mk(row, '')); }
    });
    byAdded.forEach(function (row) {
      if (recentCards.length >= recent || seen[row.edition_id]) return;
      if (addedCutoff !== null && _epochMs(row.date_added) < addedCutoff) return;   // outside the window
      seen[row.edition_id] = true; recentCards.push(mk(row, 'New'));
    });
    recentCards = recentCards.slice(0, recent);
    if (recentCards.length) rails.push({ kind: 'recent', title: 'Recent', cards: recentCards });

    // 2. Starred — the curated favourites, newest-starred first (the server returns them in that
    //    order). Editions no longer live are silently skipped (byId miss), same as recentIds.
    var starredCards = starredIds.map(function (id) { return byId[id]; })
                                 .filter(Boolean).map(function (row) { return mk(row, ''); });
    if (starredCards.length) rails.push({ kind: 'starred', title: 'Starred', cards: starredCards });

    // 3. Subject shelves — one per top-level topic, rolled up by name (descendant-inclusive
    //    via the '/' rule); fuller first, the protected safety-net shelf sunk to the bottom.
    var subj = forest.filter(function (n) { return (n.depth || 0) === 0; }).map(function (n) {
      var label = n.leaf_label || n.name;
      var members = editions.filter(function (e) {
        return (e.subjects || []).some(function (s) { return subjectTopLevel(s) === label; });
      }).sort(function (a, b) { return b.edition_id - a.edition_id; });
      return { node: n, label: label, members: members };
    }).filter(function (r) { return r.members.length; });
    subj.sort(function (a, b) {
      var pa = a.node.is_protected ? 1 : 0, pb = b.node.is_protected ? 1 : 0;
      if (pa !== pb) return pa - pb;
      if (a.members.length !== b.members.length) return b.members.length - a.members.length;
      return _cmpStr(a.label, b.label);
    });
    subj.forEach(function (r) {
      rails.push({ kind: 'subject', id: r.node.id, title: r.label, count: r.members.length,
                   cards: r.members.slice(0, perRow).map(function (e) { return mk(e); }) });
    });

    // 4. Series — group by series name, order each set by volume; one rail of sets the
    //    renderer collapses into tile + drawer.
    var bag = {}, order = [];
    editions.forEach(function (e) {
      (e.series || []).forEach(function (name) {
        if (!bag[name]) { bag[name] = []; order.push(name); }
        bag[name].push(e);
      });
    });
    order.sort(_cmpStr);
    var sets = order.map(function (name) {
      var rows = bag[name].slice().sort(function (a, b) {
        return _cmpKey(_volumeSortKey(a.volume), _volumeSortKey(b.volume)) || (a.edition_id - b.edition_id);
      });
      return { name: name, count: rows.length, cards: rows.slice(0, perRow).map(function (e) { return mk(e); }) };
    });
    if (sets.length) rails.push({ kind: 'series', title: 'Series', sets: sets });

    return { kind: 'home', rails: rails, empty: rails.length === 0 };
  }

  // ── Wishlist VM — books wanted but not yet owned ────────────────────────────
  // Pure function over the GET /api/v1/wishlist payload ({items:[item]}; each item is the
  // WishlistItem DTO). Groups items by resolution status into a fixed, attention-first display
  // order; each group's cards reuse the shelf card shape. A book the resolver couldn't identify is
  // NEVER dropped — it lands in an 'unresolved' (or 'ambiguous') group with a badge so the operator
  // can fix it later. `count` is the still-wanted total (everything not yet acquired). Shared by
  // web/PWA/iOS via the goldens, so every surface flags a half-identified book identically.
  var WISHLIST_GROUPS = [
    { status: 'ambiguous',  title: 'Choose an edition' },
    { status: 'suspected',  title: 'Might already be in your library' },
    { status: 'unresolved', title: 'Needs details' },
    { status: 'resolved',   title: 'Wishlist' },
    { status: 'owned',      title: 'Already in your library' },
    { status: 'acquired',   title: 'Acquired' }
  ];
  var WISHLIST_BADGES = {
    ambiguous: 'Choose edition', suspected: 'Confirm match', unresolved: 'Add details',
    resolved: '', owned: 'Already owned', acquired: 'Acquired'
  };

  function _wishlistCard(it) {
    var isbn = it.isbn || it.raw_isbn || null;
    var title = it.title || it.raw_title || (isbn ? 'ISBN ' + isbn : 'Untitled');
    var authors = (it.authors && it.authors.length) ? it.authors
                  : (it.raw_author ? [it.raw_author] : []);
    var cover = it.cover_url || (isbn ? 'https://covers.openlibrary.org/b/isbn/' + isbn + '-L.jpg' : null);
    return {
      id: it.id, title: title, by: authors.join(', '),
      year: it.year || null, publisher: it.publisher || null, isbn: isbn,
      status: it.status, badge: WISHLIST_BADGES[it.status] || '', cover_url: cover,
      candidate_count: (it.candidates || []).length,
      matched_edition_id: it.matched_edition_id || null
    };
  }

  function wishlistVM(wishlist, opts) {
    opts = opts || {};
    var items = (wishlist && wishlist.items) || [];
    var groups = WISHLIST_GROUPS.map(function (gdef) {
      var cards = items.filter(function (it) { return it.status === gdef.status; }).map(_wishlistCard);
      return { kind: gdef.status, title: gdef.title, cards: cards };
    }).filter(function (g) { return g.cards.length; });
    var count = items.filter(function (it) { return it.status !== 'acquired'; }).length;
    return { kind: 'wishlist', groups: groups, count: count, empty: items.length === 0 };
  }

  // ── Wishlist COMMAND path (shared, like the read VM) ────────────────────────
  // wishlistRequest maps a user INTENT → the backend request `{method, path, body}`. Every surface's
  // adapter EXECUTES this — none hardcodes a `/api/v1/...` endpoint, so web/PWA/iOS never drift.
  //   list                                  → GET    the list
  //   add    {body:{isbn|title,author|cip_text, source?}} → POST a new item
  //   remove {id}                           → DELETE (soft) an item
  //   pick   {id, index}                    → PATCH  resolve an ambiguous item from candidate N
  //   confirm{id, editionId}                → PATCH  a suspected item IS this owned edition
  //   decline{id}                           → PATCH  a suspected item is a different book
  function wishlistRequest(action, opts) {
    opts = opts || {};
    var base = '/api/v1/wishlist';
    switch (action) {
      case 'list':    return { method: 'GET',    path: base };
      case 'add':     return { method: 'POST',   path: base, body: opts.body || {} };
      case 'remove':  return { method: 'DELETE', path: base + '/' + opts.id };
      case 'pick':    return { method: 'PATCH',  path: base + '/' + opts.id, body: { pick: opts.index } };
      case 'confirm': return { method: 'PATCH',  path: base + '/' + opts.id, body: { confirm_owned: opts.editionId } };
      case 'decline': return { method: 'PATCH',  path: base + '/' + opts.id, body: { decline_suspected: true } };
      default: return null;
    }
  }

  // wishlistAddMessage maps the add RESPONSE → the one-wording-everywhere user message.
  function wishlistAddMessage(resp) {
    resp = resp || {};
    if (resp.owned) return 'You already own this — not added.';
    if (resp.duplicate) return 'Already on your wishlist.';
    var st = (resp.item || {}).status;
    if (st === 'suspected')  return 'Added — you might already own this; confirm below.';
    if (st === 'unresolved') return 'Added — needs details (couldn’t identify it).';
    if (st === 'ambiguous')  return 'Added — choose the right edition below.';
    return 'Added to wishlist.';
  }

  // ── Starred COMMAND path (shared, like wishlistRequest) ─────────────────────
  // starredRequest maps a star INTENT → the backend request `{method, path, body}`. The star button
  // on every cover is a TOGGLE: surfaces read the current state from their cached starred set and
  // execute 'star'/'unstar'. Each write returns the fresh list so the client refreshes that set.
  //   list                → GET    the starred edition ids
  //   star   {eid}        → POST   {edition_id}
  //   unstar {eid}        → DELETE /:eid
  function starredRequest(action, opts) {
    opts = opts || {};
    var base = '/api/v1/starred';
    switch (action) {
      case 'list':   return { method: 'GET',    path: base };
      case 'star':   return { method: 'POST',   path: base, body: { edition_id: opts.eid } };
      case 'unstar': return { method: 'DELETE', path: base + '/' + opts.eid };
      default: return null;
    }
  }

  // ── Replica metadata Search / Browse — the shared MATCHER ───────────────────
  // ONE implementation of offline Search + Browse over the cached replica, so PWA and native agree
  // exactly instead of each re-deriving its own blob (the source of cross-client search drift). Match
  // = fold(query) is a substring of the row's server-built `search_text`, re-folded with the SAME
  // client `fold` for consistency (so the query and the haystack are normalised identically — and
  // `fold` is itself JS⇄Swift parity-tested). People/Subjects match their folded names. Pure.
  //   NOTE: this is the single-blob client search; the server's multi-field (book/work/person/subject)
  //   + alias search is a richer, separate surface —.
  // The folded haystack: prefer the server-built `search_text`; fall back to a blob derived from the
  // row's own fields (replicas always carry search_text — the fallback covers thin/test rows).
  function _derivedBlob(row) {
    return [row.title, row.display_title, row.subtitle, row.publisher].filter(Boolean)
      .concat(row.authors || [], row.translators || [], row.isbns || [], row.subjects || [], row.work_titles || [])
      .join(' ');
  }
  function _hay(row) { return nameKey(row.search_text || _derivedBlob(row)); }
  function _searchCard(row) {
    var art = artFor(row.edition_id);
    var hs = row.holdings || [];
    return { eid: row.edition_id, title: row.title, display_title: row.display_title,
             by: (row.authors || [])[0] || '',
             holding_id: hs.length ? hs[0].holding_id : null,
             has_file: hs.some(function (h) { return h.has_file; }),
             cover_url: row.cover_url || art.cover_url, spine_url: row.spine_url || art.spine_url };
  }
  function _replicaRows(replica) { return (((replica && replica.editions) || []).slice()).reverse(); } // newest-first
  // Term-AND match: every whitespace-separated query term must be a substring (word order/spacing
  // independent) — the same rule the PWA already uses, now shared so every client agrees.
  function _terms(q) { return nameKey((q || '').trim()).split(/\s+/).filter(Boolean); }
  function _allIn(hay, terms) { return terms.every(function (t) { return hay.indexOf(t) !== -1; }); }
  // A row matches if every term is in its text — OR the whole query is the edition NUMBER (eid),
  // so the Edition search finds a book by title OR by its catalogue number.
  function _matchRow(r, terms) {
    if (_allIn(_hay(r), terms)) return true;
    return terms.length === 1 && /^\d+$/.test(terms[0]) && String(r.edition_id) === terms[0];
  }
  function searchReplica(replica, q) {
    var terms = _terms(q);
    return _replicaRows(replica).filter(function (r) { return !terms.length || _matchRow(r, terms); }).map(_searchCard);
  }
  // Typeahead suggestions — the top book matches, shaped as {type,label,sublabel,url}. Shared so the
  // PWA + native produce identical suggestions (the live web uses its own /…/search endpoints).
  function suggestReplica(replica, q) {
    return searchReplica(replica, q).slice(0, 8).map(function (c) {
      return { type: 'Book', label: c.display_title || c.title, sublabel: c.by, url: '/library?eid=' + c.eid };
    });
  }
  function _distinctMatching(values, terms) {
    var seen = {}, out = [];
    values.forEach(function (v) { var k = nameKey(v); if ((!terms.length || _allIn(k, terms)) && !seen[k]) { seen[k] = 1; out.push(v); } });
    return out.sort();
  }
  function browseReplica(replica, q, only) {
    var terms = _terms(q);
    var rows = _replicaRows(replica), all = (replica && replica.editions) || [], groups = [];
    var books = rows.filter(function (r) { return !terms.length || _matchRow(r, terms); });
    if ((!only || only === 'editions') && books.length)
      groups.push({ key: 'editions', label: 'Book', label_plural: 'Books', count: books.length,
        hits: books.map(function (r) { return { type: 'Book', label: r.display_title || r.title, url: '/library?eid=' + r.edition_id }; }) });
    // Works: match each contained work's folded ALL-alias blob (so any spelling matches), display its
    // canonical title. Merge by title across editions, so a work appears once.
    var workBlobs = {};
    all.forEach(function (r) { (r.works || []).forEach(function (w) {
      if (w && w.title != null) workBlobs[w.title] = (workBlobs[w.title] || '') + ' ' + nameKey(w.search || w.title);
    }); });
    var works = Object.keys(workBlobs).filter(function (t) { return !terms.length || _allIn(workBlobs[t], terms); }).sort();
    if ((!only || only === 'works') && works.length)
      groups.push({ key: 'works', label: 'Work', label_plural: 'Works', count: works.length,
        hits: works.map(function (n) { return { type: 'Work', label: n, url: null }; }) });
    var ppl = []; all.forEach(function (r) { ppl = ppl.concat(r.authors || [], r.translators || []); });
    var people = _distinctMatching(ppl, terms);
    if ((!only || only === 'people') && people.length)
      groups.push({ key: 'people', label: 'Person', label_plural: 'People', count: people.length,
        hits: people.map(function (n) { return { type: 'Person', label: n, url: null }; }) });
    var sidMap = {}; ((replica && replica.subject_forest) || []).forEach(function (n) { if (n && n.name != null) sidMap[n.name] = n.id; });
    var subs = []; all.forEach(function (r) { subs = subs.concat(r.subjects || []); });
    var subjects = _distinctMatching(subs, terms);
    if ((!only || only === 'subjects') && subjects.length)
      groups.push({ key: 'subjects', label: 'Subject', label_plural: 'Subjects', count: subjects.length,
        hits: subjects.map(function (n) { var id = sidMap[n]; return { type: 'Subject', label: n, url: id != null ? '/subject/' + id : null }; }) });
    return { groups: groups };
  }

  // ── Cover component contract (shared geometry + style enum) ─────────────────
  // A book cover is a 2:3 poster — height = BOOK_COVER_ASPECT × width. A SeriesCover (a box-set
  // tile) is sized RELATIVE to a book cover so the two read as a family on every surface: each
  // style declares its box as a ratio of the book-cover width/height. The renderArt is the
  // per-toolkit SeriesCover IMPLEMENTATION (DOM in shelf.js, SwiftUI SeriesCover, …); only the
  // ENUM (keys, labels, box ratios, default) lives here so a new style or resize lands everywhere.
  //   collage — 2×2 mosaic of the first volumes' covers
  //   cover   — one representative cover with stacked card-edges behind ("a stack")
  //   fan     — cover-flow: a sharp foreground cover, the rest fanned out behind
  var BOOK_COVER_ASPECT = 1.5;                    // cover height / width (2:3 poster)
  var SERIES_COVER_STYLES = [
    { key: 'collage', label: 'Collage',      wRatio: 1.23, hRatio: 0.95 },
    { key: 'cover',   label: 'Single cover', wRatio: 1.07, hRatio: 0.95 },
    { key: 'fan',     label: 'Cover stack',  wRatio: 1.50, hRatio: 0.95 }
  ];
  var SERIES_COVER_DEFAULT = 'fan';
  function seriesCoverStyle(key) {
    for (var i = 0; i < SERIES_COVER_STYLES.length; i++) if (SERIES_COVER_STYLES[i].key === key) return SERIES_COVER_STYLES[i];
    return null;
  }

  // ── Subject page (PURE) — the offline subject browse, shared web-replica/PWA/native ──
  // Composes one subject's page from the cached replica (mirrors the server `/api/v1/subject` shape):
  // crumbs from the '/' path, immediate child sub-subjects (each with its own books shelf), the full
  // descendant-inclusive book list, and the "leftover" books under no child. Pure — `_homeCard` cards.
  function subjectVM(replica, name) {
    name = String(name || '');
    var editions = (replica && replica.editions) || [];
    var eds = editions.filter(function (e) { return (e.subjects || []).some(function (s) { return isUnderSubject(s, name); }); });
    var segs = name.split('/');
    var crumbs = segs.map(function (seg, i) { return { name: segs.slice(0, i + 1).join('/'), label: seg }; });
    var childSet = {}, childNames = [];
    eds.forEach(function (e) {
      (e.subjects || []).forEach(function (s) {
        if (s.indexOf(name + '/') === 0) {
          var c = name + '/' + s.slice(name.length + 1).split('/')[0];
          if (!childSet[c]) { childSet[c] = 1; childNames.push(c); }
        }
      });
    });
    childNames.sort();
    var children = childNames.map(function (c) {
      return { name: c, leaf: c.split('/').pop(),
               books: eds.filter(function (e) { return (e.subjects || []).some(function (s) { return isUnderSubject(s, c); }); }).map(_homeCard) };
    });
    var covered = {};
    children.forEach(function (ch) { ch.books.forEach(function (b) { covered[b.eid] = 1; }); });
    var leftover = eds.filter(function (e) { return !covered[e.edition_id]; }).map(_homeCard);
    return { kind: 'subject', name: name, leaf: segs[segs.length - 1], count: eds.length,
             crumbs: crumbs, children: children, books: eds.map(_homeCard), leftover: leftover };
  }

  // Settings: device prefs. Application of the choice (set an attribute, persist)
  // is the renderer's job; the neutral keys + semantics are documented for parity:
  //   key 'theme'       ∈ {auto,light,dark};         'auto' means REMOVE the key (follow OS).
  //   key 'shelfArt'    ∈ {spine,cover};             default 'cover'.
  //   key 'setStyle'    ∈ SERIES_COVER_STYLES keys;  default 'fan' (the SeriesCover style).
  //   key 'shelfTitles' ∈ {on,off};                  default OFF — a Shelf shows covers only, the
  //                                                   title appears below each cover only when on.
  var THEME_OPTIONS = [
    { value: 'auto', label: '🖥 Auto' }, { value: 'light', label: '☀ Light' }, { value: 'dark', label: '🌙 Dark' }];
  var SHELF_OPTIONS = [{ value: 'spine', label: 'Spines' }, { value: 'cover', label: 'Covers' }];

  function settingsVM(platform) {
    var get = (platform.prefs && platform.prefs.get) ? platform.prefs.get : function () { return null; };
    var theme = get('theme'); if (theme !== 'light' && theme !== 'dark') theme = 'auto';
    var shelf = get('shelfArt') === 'spine' ? 'spine' : 'cover';
    var setStyle = get('setStyle'); if (!seriesCoverStyle(setStyle)) setStyle = SERIES_COVER_DEFAULT;
    var shelfTitles = get('shelfTitles') === 'on';
    return { kind: 'settings', theme: theme, shelfArt: shelf, seriesCoverStyle: setStyle, shelfTitles: shelfTitles,
             themeOptions: THEME_OPTIONS, shelfOptions: SHELF_OPTIONS, seriesCoverStyles: SERIES_COVER_STYLES };
  }

  // ── Visibility protocols ─────────────────────────────────────────────────
  // A *protocol* is a named capability gate deciding whether a section/menu-item is shown, from a
  // runtime CONTEXT { local, desktop }. Every section declares a protocol; the built-in 'default'
  // is always visible. MIRROR of catalogue/domain/protocols.py so web/PWA/native gate identically.
  //   local   — on the machine running the catalogue (host)
  //   desktop — a desktop-class client (large screen / not a phone)
  var PROTOCOLS = {
    'default': function () { return true; },
    'local': function (ctx) { return !!(ctx && ctx.local); },
    'desktop': function (ctx) { return !!(ctx && ctx.desktop); }
  };
  function protocolVisible(protocol, ctx) {
    return (PROTOCOLS[protocol] || PROTOCOLS['default'])(ctx || {});
  }

  // ── App sections — the SINGLE enumerated nav manifest (shared web/PWA/native) ──
  // ONE canonical list of the app's sections: key (stable id), label (user-facing), icon (an SF
  // Symbol name — native draws it directly; web/PWA render it as SVG), and protocol gate. Every
  // surface renders the SUBSET it implements (it owns the key→route/screen mapping) but takes the
  // label/icon/order/gating from HERE — so a rename or reorder lands on web, PWA and the app at
  // once instead of being re-typed in three places. Functions, not labels, are the stable meaning:
  //   books   — the BOOK finder (title/author/ISBN metadata)
  //   search  — the cross-ENTITY finder (people / works / subjects / books, grouped)
  //   content — in-book FULL-TEXT search
  // Mirror in Swift `AppSections` (parity-tested against this via goldens.json).
  var APP_SECTIONS = [
    { key: 'home',     label: 'Home',     icon: 'house',               protocol: 'default' },
    { key: 'books',    label: 'Books',    icon: 'books.vertical',      protocol: 'default' },
    { key: 'read',     label: 'Read',     icon: 'book',                protocol: 'default' },
    { key: 'search',   label: 'Search',   icon: 'magnifyingglass',     protocol: 'default' },
    { key: 'content',  label: 'Text',     icon: 'doc.text',            protocol: 'default' },
    { key: 'review',   label: 'Review',   icon: 'checklist',           protocol: 'desktop' },
    { key: 'scan',     label: 'Scan',     icon: 'viewfinder',          protocol: 'desktop' },
    { key: 'capture',  label: 'Capture',  icon: 'camera',              protocol: 'default' },
    { key: 'wishlist', label: 'Wishlist', icon: 'star',                protocol: 'default' },
    { key: 'settings', label: 'Settings', icon: 'slider.horizontal.3', protocol: 'default' }
  ];
  // Look up one section's metadata by key (a surface maps the key to its own route/screen).
  function sectionFor(key) {
    for (var i = 0; i < APP_SECTIONS.length; i++) if (APP_SECTIONS[i].key === key) return APP_SECTIONS[i];
    return null;
  }
  // Build navVM-shaped items for a surface: pick `keys` (in canonical APP_SECTIONS order), attach
  // each surface's own href via hrefForKey(key). Unknown keys are dropped. The label/icon/protocol
  // always come from the manifest, so no surface can drift on naming.
  function navItems(keys, hrefForKey) {
    var want = {}; (keys || []).forEach(function (k) { want[k] = true; });
    return APP_SECTIONS.filter(function (s) { return want[s.key]; }).map(function (s) {
      return { key: s.key, label: s.label, icon: s.icon, protocol: s.protocol,
               href: hrefForKey ? hrefForKey(s.key) : null };
    });
  }

  // ── Search-screen component contract (the named "bits", shared web/PWA/native) ──
  // The Search screen decomposes into enumerable components each surface implements its own way:
  // the INPUT fields (each a typeahead box with a suggestion Picker) and the BookDetailsPane
  // SECTIONS. Like APP_SECTIONS, only the enumeration is shared (keys/labels + each field's suggest
  // source and what a pick resolves to); the boxes/sections are drawn per toolkit. detailVM is the
  // shared data for the pane sections. Mirror in Swift (parity-tested).
  //   SEARCH_FIELDS       — book_title / work_title / person / subject finders (web has all four;
  //                         PWA/native expose a single combined box today — a documented gap).
  //   BOOK_DETAIL_SECTIONS — basics / holdings / works / connections (web has all; native is filling
  //                         them in; PWA is partial; `connections` needs detailVM cross-link data).
  var SEARCH_FIELDS = [
    { key: 'book_title', label: 'Book title', suggest: '/editions/search',        picks: 'edition' },
    { key: 'work_title', label: 'Work title', suggest: '/works/search',           picks: 'work' },
    { key: 'person',     label: 'Person',     suggest: '/library/suggest/person', picks: 'person' },
    { key: 'subject',    label: 'Subject',    suggest: '/library/suggest/subject', picks: 'subject' }
  ];
  var BOOK_DETAIL_SECTIONS = [
    { key: 'basics',      label: 'Edition Basics' },
    { key: 'holdings',    label: 'Holdings' },
    { key: 'works',       label: 'Works In This Edition' },
    { key: 'connections', label: 'Connections' }
  ];

  // App navigation — the neutral Menu MODEL (not its look). Each surface supplies its own item
  // list (the available features differ per surface) and the renderer presents it in whatever
  // FORM that implementation chooses (top bar, floating button, native tab bar…). An item is
  // {key, label, icon, href, protocol?}; `href` is the platform's own target (web URL | PWA hash
  // | native route) since nav chrome routes to app sections, not data `ref`s. Items whose protocol
  // isn't satisfied by `ctx` are dropped here (same protocol layer as the server sections). The VM
  // marks the active item; it knows nothing about hamburger-vs-bar — that's a presentation choice.
  function navVM(items, activeKey, ctx) {
    return {
      kind: 'nav',
      items: (items || [])
        .filter(function (it) { return protocolVisible(it.protocol || 'default', ctx); })
        .map(function (it) {
          return { key: it.key, label: it.label, icon: it.icon || '',
                   href: it.href != null ? it.href : null,
                   active: activeKey != null && it.key === activeKey };
        })
    };
  }

  // Subject-hierarchy helpers — the '/' path is the hierarchy (one shared rule for
  // every client). topLevel('Buddhism/Emptiness') === 'Buddhism'; isUnderSubject lets a
  // name-keyed client (the PWA replica) roll up a subject's descendants prefix-inclusively.
  function subjectTopLevel(name) { return String(name || '').split('/')[0]; }
  function isUnderSubject(name, ancestor) {
    name = String(name || ''); ancestor = String(ancestor || '');
    return name === ancestor || name.startsWith(ancestor + '/');
  }

  // ── PDF "reflow to text": raw page text → paragraphs. Pure + shared (Swift port in CatalogueCore),
  // golden-tested. De-hyphenates line-wrapped words, joins intra-paragraph line breaks, splits on blank
  // lines. Heuristic only — no layout reconstruction. ──
  function reflowPageText(raw) {
    if (raw == null) return [];
    var text = String(raw).replace(/\r\n?/g, '\n').replace(/­/g, '');   // normalize EOL + soft hyphens
    var lines = text.split('\n');
    var paras = [], cur = '', joinNext = false;
    function flush() { var t = cur.replace(/[ \t]+/g, ' ').trim(); if (t) paras.push(t); cur = ''; joinNext = false; }
    for (var i = 0; i < lines.length; i++) {
      if (/^\s*$/.test(lines[i])) { flush(); continue; }             // blank line → paragraph break
      var content = lines[i].replace(/^\s+|\s+$/g, '');
      if (cur === '') cur = content;
      else if (joinNext) cur += content;                            // glued after a de-hyphenated word
      else cur += ' ' + content;                                    // wrapped line → single space
      if (/[A-Za-zÀ-ɏ]-$/.test(cur)) { cur = cur.slice(0, -1); joinNext = true; }
      else joinNext = false;
    }
    flush();
    return paras;
  }

  // ── Reader icon config: the SINGLE source of truth for every reader-chrome control's icon, shared by
  // all surfaces. Keyed by the control `id` from `readerChromeVM`. `sf` is the iOS SF Symbol name (iOS
  // renders SF Symbols, never glyphs); `sfActive` is the toggled-on variant where one exists; `web` is
  // the glyph the web/PWA toolbars show. Change an icon HERE and it updates everywhere: web/PWA read this
  // object directly; iOS reads a generated `reader-icons.json` copy (see Tools/gen_goldens.mjs).
  // Note: SF Symbols has no straight-tip highlighter — `highlighter` is the only one; swap `highlight.sf`
  // here if a different symbol is preferred.
  var READER_ICONS = {
    done:         { sf: 'chevron.left',              web: '‹' },
    toc:          { sf: 'list.bullet',               web: '☰' },
    search:       { sf: 'magnifyingglass',           web: '🔍' },
    refresh:      { sf: 'arrow.clockwise',           web: '⟳' },
    textSmaller:  { sf: 'textformat.size.smaller',   web: 'A' },
    textLarger:   { sf: 'textformat.size.larger',    web: 'A' },
    zoomOut:      { sf: 'minus.magnifyingglass',     web: '−' },
    zoomIn:       { sf: 'plus.magnifyingglass',      web: '+' },
    fitWidth:     { sf: 'arrow.left.and.right',      web: '↔' },
    reflow:       { sf: 'doc.plaintext', sfActive: 'doc.richtext.fill', web: '¶' },
    goto:         { sf: 'arrow.forward.to.line',     web: '⇥' },
    theme:        { sf: 'circle.lefthalf.filled',    web: '◐' },
    undo:         { sf: 'arrow.uturn.backward',      web: '↶' },
    redo:         { sf: 'arrow.uturn.forward',       web: '↷' },
    highlight:    { sf: 'highlighter',               web: '🖍' },
    underline:    { sf: 'underline',                 web: 'U̲' },
    strike:       { sf: 'strikethrough',             web: 'S̶' },
    note:         { sf: 'note.text',                 web: '🅝' },
    draw:         { sf: 'pencil.tip.crop.circle', sfActive: 'pencil.tip.crop.circle.fill', web: '✎' },
    erase:        { sf: 'eraser.line.dashed',        web: '⌫' },
    annList:      { sf: 'list.bullet.rectangle',     web: '▦' },
    export:       { sf: 'square.and.arrow.up',       web: '⬇' },
    bookmarkAdd:  { sf: 'bookmark',                  web: '★' },
    bookmarkList: { sf: 'bookmark.circle',           web: '▤' },
    pin:          { sf: 'pin', sfActive: 'pin.fill', web: '📌' }   // iOS: pin a reader bar to float it
  };

  // ── Reader chrome spec: the SHARED "bars" abstraction. Enumerates the reader's control set ONCE —
  // which bar each control lives in (general = leading, text = trailing), whether it collapses into the
  // ⋯ overflow, and its active state. It is CAPABILITY-DRIVEN: the caller passes the caps its surface +
  // document format actually support, and this enumerates the matching controls with NO hardcoded
  // format/surface checks. So "all surfaces share one capability set; each excludes what its mode can't
  // do" — e.g. EPUB omits strike/note (epub.js can't), web omits draw (no ink), PDF omits text-resize.
  // Every surface RENDERS this spec in its own toolkit. Ported 1:1 to CatalogueCore and golden-tested.
  //
  // `input.compact` (phone / narrow) collapses the annotation + mode-specific controls into the ⋯
  // overflow; on a regular width (iPad / desktop) they sit inline in the trailing bar. The primary
  // controls (done/toc/search/star/goto/theme) and the secondary ones (bookmarks / annotation list) keep
  // a fixed placement regardless of width. ──
  function readerChromeVM(input) {
    input = input || {};
    var caps = input.caps || {};
    var st = input.state || {};
    var compact = !!input.compact;   // narrow width → annotation + mode controls go to the ⋯ overflow
    var canEdit = !!(caps.markText || caps.strike || caps.note || caps.draw || caps.erase);  // any annotation ability
    var out = [];
    function c(id, bar, overflow, active) { out.push({ id: id, bar: bar, overflow: !!overflow, active: !!active }); }
    // Annotation + mode-specific controls: inline on a regular width, collapsed into ⋯ on a phone.
    function tool(id, active) { c(id, 'text', compact, active); }

    // General options bar (leading): exit + navigate + find + identity.
    c('done', 'general', false, false);
    if (caps.ready) {
      c('toc', 'general', false, false);
      if (caps.search) c('search', 'general', false, false);
      if (caps.star) c('star', 'general', false, false);

      // Text options bar (trailing). Mode-specific first (size / reflow), then the shared jump/theme,
      // then the annotation vocabulary, then secondary management (annotations list / bookmarks).
      if (caps.resizeText) { tool('textSmaller', false); tool('textLarger', false); }   // EPUB: font A±
      if (caps.zoom) { tool('zoomOut', false); tool('zoomIn', false); tool('fitWidth', false); }  // PDF: magnifier ± + fit width
      if (caps.reflow) tool('reflow', !!st.reflow);
      c('goto', 'text', false, false);    // jump to a page (PDF) / position (EPUB)
      c('theme', 'text', false, false);   // a direct cycle button (never in the ⋯)
      // Document-level undo/redo — present whenever the surface can annotate; the renderer disables each
      // when its history is empty.
      if (canEdit) { tool('undo', false); tool('redo', false); }
      // The shared annotation vocabulary — each gated by its own capability so a mode/surface excludes
      // only what it can't do.
      if (caps.markText) { tool('highlight', false); tool('underline', false); }
      if (caps.strike) tool('strike', false);
      if (caps.note) tool('note', false);
      if (caps.draw) tool('draw', !!st.draw);
      if (caps.erase) tool('erase', false);
      if (caps.export) tool('export', false);
      // Secondary — always in the ⋯ overflow, both widths.
      if (caps.annList) c('annList', 'text', true, false);
      c('bookmarkAdd', 'text', true, false);
      c('bookmarkList', 'text', true, false);
    }
    return out;
  }

  // ── SDUI-lite: a page as an ordered list of SECTIONS ({type, title, subject, cards, crumbs}). Every
  // surface renders sections through a tiny component registry keyed by `type` (crumbs | rail | grid),
  // so "define the page shape once, render everywhere". `subjectSections` is the first: it turns the
  // rich subjectVM into breadcrumbs + a rail per child (+ a leftover rail), or a single grid when the
  // subject has no children — the SAME shape web/PWA already build ad-hoc and iOS was ignoring. ──
  function subjectSections(vm) {
    vm = vm || {};
    var out = [];
    if (vm.crumbs && vm.crumbs.length) out.push({ type: 'crumbs', crumbs: vm.crumbs, cards: [] });
    var children = vm.children || [];
    if (children.length) {
      children.forEach(function (ch) {
        out.push({ type: 'rail', title: ch.leaf, subject: ch.name, cards: ch.books || [], crumbs: [] });
      });
      if (vm.leftover && vm.leftover.length) {
        out.push({ type: 'rail', title: vm.leaf, subject: vm.name, cards: vm.leftover, crumbs: [] });
      }
    } else {
      out.push({ type: 'grid', title: vm.leaf, subject: vm.name, cards: vm.books || [], crumbs: [] });
    }
    return out;
  }

  // ── Freshness / sync status (Tier-2, surface-agnostic). One pure function maps the sync engine's
  // observable `SyncState` to the spec every surface renders (the status chip + whether a manual
  // pull is enabled). Golden-locked JS↔Swift↔(Kotlin) like `readerChromeVM`; the *engine* that drives
  // refresh/push/pull is impure and lives per-surface, but WHAT the user sees is decided here once. ──
  function syncVM(state) {
    state = state || {};
    var online = !!state.online, syncing = !!state.syncing;
    var err = state.lastError || null;
    var exportedAt = state.exportedAt || null;
    var pending = state.pendingWrites || 0;
    var day = exportedAt ? String(exportedAt).slice(0, 10) : null;     // YYYY-MM-DD, like the PWA chip
    var canPull = online && !syncing;                                  // manual pull-to-refresh gate
    function vm(s, label, tone, detail) { return { state: s, label: label, tone: tone, detail: detail || null, canPull: canPull }; }
    if (syncing) return vm('syncing', 'Syncing…', 'muted', null);
    if (err)     return vm('error', 'Sync failed', 'error', err);
    if (!online) return vm('offline', day ? ('Offline · ' + day) : 'Offline', 'warn', pending ? (pending + ' unsynced') : null);
    return vm('live', 'Live', 'ok', pending ? (pending + ' syncing') : null);
  }

  window.LibraryCore = {
    fold: fold, nameKey: nameKey, editionRef: editionRef, refFromUrl: refFromUrl, artFor: artFor,
    reflowPageText: reflowPageText, readerChromeVM: readerChromeVM, READER_ICONS: READER_ICONS,
    subjectSections: subjectSections,
    syncVM: syncVM,
    subjectTopLevel: subjectTopLevel, isUnderSubject: isUnderSubject,
    searchVM: searchVM, browseVM: browseVM, contentVM: contentVM, detailVM: detailVM,
    homeVM: homeVM, searchReplica: searchReplica, browseReplica: browseReplica, suggestReplica: suggestReplica,
    subjectVM: subjectVM, wishlistVM: wishlistVM,
    wishlistRequest: wishlistRequest, wishlistAddMessage: wishlistAddMessage,
    starredRequest: starredRequest,
    settingsVM: settingsVM, navVM: navVM,
    PROTOCOLS: PROTOCOLS, protocolVisible: protocolVisible,
    APP_SECTIONS: APP_SECTIONS, sectionFor: sectionFor, navItems: navItems,
    SEARCH_FIELDS: SEARCH_FIELDS, BOOK_DETAIL_SECTIONS: BOOK_DETAIL_SECTIONS,
    BOOK_COVER_ASPECT: BOOK_COVER_ASPECT, SERIES_COVER_STYLES: SERIES_COVER_STYLES,
    SERIES_COVER_DEFAULT: SERIES_COVER_DEFAULT, seriesCoverStyle: seriesCoverStyle,
    THEME_OPTIONS: THEME_OPTIONS, SHELF_OPTIONS: SHELF_OPTIONS
  };
})();
