/* Device-local "recently opened books" for the WEB (Flask) surface.
 *
 * The Flask pages are server-rendered and keep no per-device reading store the way the PWA does
 * (IndexedDB `ReadingStore`) or iOS does (`OpenSessionsStore`). By decision the reading list is
 * DEVICE-LOCAL and NOT synced to the server, so this lives in this browser's `localStorage` only.
 * It is written when the reader opens a book (reader.html) and read by the Read menu's resume page
 * (/read → read_resume.html). Most-recent-first, deduped by book, capped.
 */
(function () {
  'use strict';
  var KEY = 'webRecentBooks';
  var MAX = 24;
  function load() {
    try { var v = JSON.parse(localStorage.getItem(KEY) || '[]'); return Array.isArray(v) ? v : []; }
    catch (e) { return []; }
  }
  function save(list) { try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX))); } catch (e) {} }
  var same = function (a, b) {
    return (a.hid != null && a.hid === b.hid) || (a.hid == null && b.hid == null && a.eid === b.eid);
  };
  window.WebRecent = {
    // Record an open: move this book to the front (deduped), timestamped.
    record: function (book) {
      if (!book || (book.hid == null && book.eid == null)) return;
      var entry = { hid: book.hid != null ? book.hid : null, eid: book.eid != null ? book.eid : null,
                    title: book.title || '', ext: book.ext || '', at: new Date().toISOString() };
      var list = load().filter(function (b) { return !same(b, entry); });
      list.unshift(entry);
      save(list);
    },
    // Most-recent-first list (all, or the first n).
    recent: function (n) { var l = load(); return n ? l.slice(0, n) : l; }
  };
})();
