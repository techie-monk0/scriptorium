/* Reader "open sessions" (tabs) — the web/PWA analogue of the iOS OpenSessionsStore. A small ordered
 * open-SET of books + an active pointer, most-recent-first, persisted per-device (localStorage by
 * default). Reading POSITION is NOT stored here (that stays in the server reading_position row /
 * IndexedDB ReadingStore); this only tracks which books are open as tabs. Storage is injectable so the
 * PWA can back it with something else; the default is localStorage (same-origin, offline-safe). */
(function (root) {
  'use strict';
  if (root.ReaderSessions) return;

  var KEY = 'reader-sessions';

  function defaultStore() {
    return {
      read: function () {
        try { return JSON.parse(root.localStorage.getItem(KEY) || '') || null; } catch (e) { return null; }
      },
      write: function (state) {
        try { root.localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
      }
    };
  }

  var store = defaultStore();

  function load() {
    var s = store.read();
    if (!s || !Array.isArray(s.books)) s = { books: [], activeEid: null };
    return s;
  }
  function save(s) { store.write(s); return s; }

  var ReaderSessions = {
    /** Swap the backing store (e.g. an IndexedDB adapter in the PWA). Must expose read()/write(). */
    useStore: function (s) { store = s || defaultStore(); },

    /** Open (or re-focus) a book: move it to the front and make it active. Idempotent by eid. */
    open: function (book) {
      if (!book || book.eid == null) return;
      var s = load();
      s.books = s.books.filter(function (b) { return b.eid !== book.eid; });
      s.books.unshift({ eid: book.eid, hid: book.hid, title: book.title || ('#' + book.eid) });
      s.activeEid = book.eid;
      save(s);
    },

    /** The open tabs, most-recent-first. */
    list: function () { return load().books.slice(); },

    /** The active tab's eid — the explicit pointer, or the front (most recent) as the default. */
    activeEid: function () {
      var s = load();
      return s.activeEid != null ? s.activeEid : (s.books[0] ? s.books[0].eid : null);
    },

    activate: function (eid) {
      var s = load();
      if (s.books.some(function (b) { return b.eid === eid; })) { s.activeEid = eid; save(s); }
    },

    /** Close a tab; if it was active, fall back to the front of what remains. */
    close: function (eid) {
      var s = load();
      s.books = s.books.filter(function (b) { return b.eid !== eid; });
      if (s.activeEid === eid) s.activeEid = s.books[0] ? s.books[0].eid : null;
      save(s);
    }
  };

  root.ReaderSessions = ReaderSessions;
})(typeof window !== 'undefined' ? window : this);
