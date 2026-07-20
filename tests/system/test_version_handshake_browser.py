"""Real-browser e2e for the client half of the app-version handshake (static/js/app-version.js).

Drives the actual JS in headless Chromium against a live server: the page is stamped with
window.APP_BUILD + loads the helper, the pure classify() rule matches the Python/Swift rule, and a
drift payload actually renders the reload/restart banner in the DOM.
"""
from __future__ import annotations

import os
import threading

import pytest


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_installed(),
    reason="playwright chromium not installed (run: uv run playwright install chromium)",
)


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    """A live server; the home page loads _base.html → app-version.js + window.APP_BUILD."""
    from werkzeug.serving import make_server
    from catalogue.webui.web import create_app

    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(tmp_path / "vh.db")
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown(); t.join()


def test_page_stamps_build_and_helper_loads(live_home, page):
    page.goto(f"{live_home}/")
    assert page.evaluate("typeof window.APP_BUILD") == "string"
    assert page.evaluate("!!(window.APP_BUILD && window.APP_BUILD.length)") is True
    assert page.evaluate("!!window.AppVersion") is True


def test_classify_rule_matches_other_surfaces(live_home, page):
    """The pure JS rule, exercised in the real browser — 1:1 with app_version.py + AppBuildContract."""
    page.goto(f"{live_home}/")
    assert page.evaluate("AppVersion.classify({app_build:'a',server_stale:false}, 'a')") == "ok"
    assert page.evaluate("AppVersion.classify({app_build:'b',server_stale:false}, 'a')") == "outdated"
    assert page.evaluate("AppVersion.classify({app_build:'a',server_stale:true}, 'a')") == "server_stale"
    # forgiving: older server that omits the fields, or no baseline yet → ok
    assert page.evaluate("AppVersion.classify({}, 'a')") == "ok"
    assert page.evaluate("AppVersion.classify({app_build:'a',server_stale:false}, null)") == "ok"


def test_outdated_payload_renders_reload_banner(live_home, page):
    page.goto(f"{live_home}/")
    # A live build different from the one this page was served with → "reload" banner.
    page.evaluate("AppVersion.apply({app_build: 'a-different-build', server_stale: false})")
    page.wait_for_selector("#app-version-banner")
    assert "newer version" in page.inner_text("#app-version-banner").lower()


def test_server_stale_payload_renders_restart_banner(live_home, page):
    page.goto(f"{live_home}/")
    page.evaluate("AppVersion.apply({server_stale: true})")
    page.wait_for_selector("#app-version-banner")
    assert "restart" in page.inner_text("#app-version-banner").lower()


def test_no_banner_when_in_sync(live_home, page):
    """The build the page was served with matches the live one → no banner."""
    page.goto(f"{live_home}/")
    page.evaluate("AppVersion.apply({app_build: window.APP_BUILD, server_stale: false})")
    assert page.locator("#app-version-banner").count() == 0


def test_pwa_service_worker_refresh_roundtrip(live_home, page):
    """The SW's REFRESH_ASSETS handler re-fetches the shell and replies ASSETS_REFRESHED — the round
    trip app-version.js's reloadFresh() drives, so a PWA 'Reload' lands on genuinely fresh assets
    instead of the stale-while-revalidate cache."""
    page.goto(f"{live_home}/app")
    # the service worker installs, activates, and claims this page
    page.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.controller", timeout=20000)
    ok = page.evaluate("""() => new Promise((resolve) => {
        const sw = navigator.serviceWorker;
        const onMsg = (e) => {
            if (e.data && e.data.type === 'ASSETS_REFRESHED') {
                sw.removeEventListener('message', onMsg); resolve(true);
            }
        };
        sw.addEventListener('message', onMsg);
        sw.controller.postMessage({ type: 'REFRESH_ASSETS' });
        setTimeout(() => resolve(false), 10000);
    })""")
    assert ok is True
