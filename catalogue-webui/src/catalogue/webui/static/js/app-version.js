/* app-version.js — the browser half of the app-version handshake (mirrors the iOS AppBuildContract).
 *
 * The server stamps every page with window.APP_BUILD (the build it served the page from) and
 * advertises the LIVE build + staleness on GET /version and GET /api/v1/health as
 * { app_build, server_stale }. This module compares the two and, on drift, shows one dismissible
 * banner so a stale pairing is visible + fixable instead of silently broken:
 *
 *   • server_stale === true      → the server is running older CODE than what's on disk (a restart is
 *                                  pending). Page routes are already blocked server-side; an OPEN page
 *                                  gets this banner. Fix = restart the server, then reload.
 *   • app_build !== APP_BUILD     → the server was replaced by a DIFFERENT build since this page loaded
 *                                  (it was restarted/redeployed). Reload to pick up fresh assets.
 *
 * No framework, no shared CSS: this must work on any surface (base pages + the reader), and must not
 * itself depend on assets that could be the stale ones.
 */
(function () {
  'use strict';

  var STATUS = { OK: 'ok', OUTDATED: 'outdated', SERVER_STALE: 'server_stale' };
  var _shown = null;          // which status a banner is currently showing (latch; re-checks are cheap)
  var _dismissed = {};        // status → true once the user closed that banner

  function pageBuild() { return (typeof window !== 'undefined' && window.APP_BUILD) || null; }

  /* Pure: map a handshake payload ({app_build, server_stale}) + the page's build to a status.
   * Missing fields (an older server) → OK, so this never nags against a server that predates the
   * handshake — same forgiving stance as the reader_sync contract check. */
  function classify(payload, forPageBuild) {
    payload = payload || {};
    var mine = (forPageBuild === undefined) ? pageBuild() : forPageBuild;
    if (payload.server_stale === true) return STATUS.SERVER_STALE;
    var live = payload.app_build;
    if (mine && live && live !== mine) return STATUS.OUTDATED;
    return STATUS.OK;
  }

  var TEXT = {};
  TEXT[STATUS.OUTDATED] = {
    msg: 'A newer version of the app is available.',
    action: 'Reload', run: function () { location.reload(); }
  };
  TEXT[STATUS.SERVER_STALE] = {
    msg: 'The server is running older code than what’s on disk — restart it, then reload.',
    action: 'Reload', run: function () { location.reload(); }
  };

  function banner(status) {
    if (status === STATUS.OK || _shown === status || _dismissed[status]) return;
    _shown = status;
    var t = TEXT[status];
    var bar = document.getElementById('app-version-banner');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'app-version-banner';
      bar.setAttribute('role', 'alert');
      bar.style.cssText = [
        'position:fixed', 'left:0', 'right:0', 'top:0', 'z-index:2147483647',
        'display:flex', 'gap:.75rem', 'align-items:center', 'justify-content:center',
        'flex-wrap:wrap', 'padding:.5rem .9rem', 'font:500 14px/1.4 system-ui,sans-serif',
        'background:#2f6feb', 'color:#fff', 'box-shadow:0 1px 6px rgba(0,0,0,.25)'
      ].join(';');
      document.body.appendChild(bar);
    }
    bar.style.display = 'flex';
    bar.textContent = '';
    var span = document.createElement('span'); span.textContent = t.msg; bar.appendChild(span);
    var act = document.createElement('button');
    act.textContent = t.action;
    act.style.cssText = 'font:inherit;cursor:pointer;border:1px solid #fff;background:#fff;color:#2f6feb;border-radius:6px;padding:.2rem .7rem';
    act.addEventListener('click', t.run);
    bar.appendChild(act);
    var x = document.createElement('button');
    x.setAttribute('aria-label', 'Dismiss'); x.textContent = '×';
    x.style.cssText = 'font:inherit;cursor:pointer;border:none;background:transparent;color:#fff;font-size:1.1rem;line-height:1';
    x.addEventListener('click', function () { _dismissed[status] = true; _shown = null; bar.style.display = 'none'; });
    bar.appendChild(x);
  }

  /* Feed a handshake payload (from /version or /api/v1/health) — shows the banner on drift. Returns
   * the status so callers (e.g. the PWA) can react further. */
  function apply(payload) {
    var status = classify(payload);
    if (status !== STATUS.OK) banner(status);
    return status;
  }

  /* Self-contained watcher for pages that don't already poll health: fetch /version now, on tab
   * focus, and every `intervalMs` (default 60s). Idempotent — safe to call once per page. */
  function watch(opts) {
    opts = opts || {};
    var url = opts.url || '/version';
    var every = opts.intervalMs || 60000;
    var poll = function () {
      fetch(url, { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) apply(d); })
        .catch(function () {});
    };
    poll();
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
      window.addEventListener('focus', poll);
    }
    setInterval(poll, every);
  }

  window.AppVersion = {
    STATUS: STATUS,
    pageBuild: pageBuild,
    classify: classify,     // pure — unit-testable in a browser without network
    apply: apply,
    watch: watch
  };
})();
