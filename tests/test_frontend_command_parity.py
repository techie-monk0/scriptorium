"""Architecture guard — the wishlist COMMAND path stays in shared Tier-2, not in renderers.

The 4-tier rule (see the `frontend-tiers-principle` memory + private/plans/frontend_tiers_and_home_upgrade.md)
applies to WRITE actions too, not just the read VM: the intent→request map (`wishlistRequest`) and the
response→message map (`wishlistAddMessage`) live ONCE in `library-core.js` (+ Swift port), and every
surface EXECUTES them. This test fails the build if a Tier-3 renderer (web template / PWA app.js /
SwiftUI) hardcodes a wishlist endpoint or an add-response message string — which is exactly how the
surfaces drifted on wishlist v1. Parity of the two mappers themselves is enforced by the Swift goldens
(`testWishlistCommandParity`); this guards that renderers actually go THROUGH them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The native iOS app lives at catalogue-app/.
# Key the skip on a specific tracked Swift SOURCE file (removed by `git rm` in the
# public tree) — not the directory, which can survive as leftover build artifacts.
pytestmark = pytest.mark.skipif(
    not (ROOT / "catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueUI/Screens.swift").exists(),
    reason="native app sources not present in this checkout",
)

# Tier-3 renderers + the iOS transport adapter — must contain NO wishlist endpoint literal and NO
# add-response message literal (they route through the shared mappers instead).
RENDERERS = [
    "catalogue-webui/src/catalogue/webui/templates/wishlist.html",
    "catalogue-webui/src/catalogue/webui/static/pwa/app.js",
    "catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueUI/Screens.swift",
    "catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueData/CatalogueAPI.swift",
]
# The shared Tier-2 sources — the ONE place the endpoint + messages are allowed to live.
SHARED = [
    "catalogue-webui/src/catalogue/webui/static/js/library-core.js",
    "catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueCore/ViewModels.swift",
]
# Distinctive substrings of each add-response message (apostrophes/em-dashes trimmed to stay literal).
MESSAGE_PHRASES = [
    "Already on your wishlist",
    "You already own this",
    "Added to wishlist",
    "needs details (couldn",
    "choose the right edition",
]


def _read(rel: str) -> str:
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


@pytest.mark.parametrize("rel", RENDERERS)
def test_renderer_has_no_wishlist_endpoint_literal(rel):
    assert "/api/v1/wishlist" not in _read(rel), (
        f"{rel} hardcodes a wishlist endpoint — route it through LibraryCore.wishlistRequest")


@pytest.mark.parametrize("rel", RENDERERS)
def test_renderer_has_no_wishlist_message_literal(rel):
    txt = _read(rel)
    hits = [p for p in MESSAGE_PHRASES if p in txt]
    assert not hits, (
        f"{rel} hardcodes wishlist add-message text {hits} — use LibraryCore.wishlistAddMessage")


@pytest.mark.parametrize("phrase", MESSAGE_PHRASES)
def test_messages_exist_in_shared_tier2(phrase):
    # Sanity: each message really does live in the shared layer (both JS + Swift mirror it).
    assert all(phrase in _read(s) for s in SHARED), (
        f"message {phrase!r} missing from a shared Tier-2 source — JS/Swift must both define it")


# ── Starred command path — same rule as wishlist ────────────────────────────────
# The star toggle (`starredRequest`) lives ONCE in the shared layer; every surface EXECUTES it. No
# Tier-3 renderer (web templates / shelf.js / PWA / SwiftUI / the iOS transport) hardcodes the endpoint.
STARRED_RENDERERS = [
    "catalogue-webui/src/catalogue/webui/templates/home.html",
    "catalogue-webui/src/catalogue/webui/templates/library.html",
    "catalogue-webui/src/catalogue/webui/templates/reader.html",
    "catalogue-webui/src/catalogue/webui/static/js/shelf.js",
    "catalogue-webui/src/catalogue/webui/static/pwa/app.js",
    "catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueUI/Screens.swift",
    "catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueData/CatalogueAPI.swift",
    "catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueUI/AppModel.swift",
]


@pytest.mark.parametrize("rel", STARRED_RENDERERS)
def test_renderer_has_no_starred_endpoint_literal(rel):
    assert "/api/v1/starred" not in _read(rel), (
        f"{rel} hardcodes the starred endpoint — route it through LibraryCore.starredRequest")


def test_starred_request_lives_in_shared_tier2():
    assert all("starredRequest" in _read(s) for s in SHARED), (
        "starredRequest must be defined in BOTH shared Tier-2 sources (JS + Swift)")
