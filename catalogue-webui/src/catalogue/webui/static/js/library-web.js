/* ── library-web.js — the WEB platform adapter ──────────────────────────────
   The only web-specific code in the shared frontend: it implements the
   LibraryCore adapter protocol for a server-backed browser page — live JSON
   from /api/v1/*, real-URL navigation, localStorage prefs. The PWA supplies its
   own adapter (replica + hash routes); a native app supplies a third. Everything
   above the adapter (LibraryCore + LibraryUI) is shared. */
(function () {
  if (window.LibraryWeb) return;

  function getJSON(url) { return fetch(url).then(function (r) { return r.json(); }); }
  function enc(s) { return encodeURIComponent(s || ''); }

  function adapter() {
    return {
      data: {
        search: function (q) {
          return getJSON('/api/v1/library?q=' + enc(q)).then(function (doc) {
            return (doc.rows || []).map(function (r) {
              return { eid: r.id, title: r.title, display_title: r.display_title, by: r.subtitle || '',
                       cover_url: '/edition/' + r.id + '/cover.jpg',
                       spine_url: '/edition/' + r.id + '/spine.svg' };
            });
          });
        },
        content: function (q) { return getJSON('/api/v1/content?q=' + enc(q)); },
        detail: function (eid) { return getJSON('/api/v1/edition/' + eid); },  // read-only book detail
        // Ask (grounded Q&A): POST the full OpenAI-style history to the same-origin proxy; the
        // browser never touches the model host. Returns { available, content, sources, timing }.
        ask: function (model, messages) {
          return fetch('/api/v1/ask', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: model, messages: messages })
          }).then(function (r) { return r.json(); });
        },
        askModels: function () { return getJSON('/api/v1/ask/models'); }
      },
      nav: {
        hrefFor: function (ref) {
          if (!ref) return null;
          switch (ref.kind) {
            case 'url':     return ref.url;
            case 'edition': return '/library?eid=' + ref.id;
            case 'work':    return '/work/' + ref.id;
            case 'person':  return '/person/' + ref.id;
            // Canonical subject target: the descendant-inclusive browse page; with no
            // id, fall back to the Search page filtered by the subject name.
            case 'subject': return ref.id != null ? '/subject/' + ref.id
                                                  : '/search?subject=' + enc(ref.q || '');
            default:        return null;
          }
        },
        // Where the read-only detail's "Read" control points (web has no in-app reader →
        // the shared renderer makes an <a> to this since the web adapter has no openBook).
        readHref: function (eid, h) { return h && h.holding_id != null ? '/holding/' + h.holding_id + '/file' : '#'; }
      },
      prefs: {
        get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
        set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
        remove: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
      },
      isOffline: function () { return navigator.onLine === false; }
    };
  }

  window.LibraryWeb = { adapter: adapter };
})();
