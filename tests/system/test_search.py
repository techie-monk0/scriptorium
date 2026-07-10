"""System tests — §4.5 full-text search.

Plan invariants verified through the HTTP surface only:
  - "tokenize = unicode61 remove_diacritics 2 — index-only folding, so
    `tathagatagarbha` matches `tathāgatagarbha` while stored
    text/snippet()/highlight() stay fully diacriticked." (§4.5)
  - "Folding handles diacritic/spacing variants in body text, but NOT
    non-reversible phonetic↔Wylie pairs — those are linked only at the
    entity level." (§4.5 — deferred query-expansion)
  - "Search is a composable pipeline, not a raw MATCH — every query
    passes through a search service: normalize → [expand] → match →
    rank. In v1 expansion is a no-op pass-through." (§4.5, §12.6)

Setup uses direct SQL to seed text (no /seed endpoint exists). Content search is
now client-rendered (the shared frontend layer), so the data lives in the JSON
contract `GET /api/v1/content?q=…` rather than the server-rendered `/search` HTML;
assertions go through that JSON (the same `SearchService.search_grouped` the page
renders). The `/search` shell itself stays reachable (see the nav test).
"""
from __future__ import annotations


def _seed_text(seed, edition_id: int, page: int, content: str) -> None:
    seed("INSERT OR IGNORE INTO edition (id, title) VALUES (?, 'e')",
         (edition_id,))
    seed("INSERT INTO edition_text (edition_id, page, content) VALUES (?, ?, ?)",
         (edition_id, page, content))


def _snippets(client, q: str) -> str:
    """All snippet text returned by the content-search JSON, joined."""
    doc = client.get(f"/api/v1/content?q={q}").get_json()
    return " ".join(s for b in doc["books"] for s in b["snippets"])


def test_bare_latin_query_finds_diacriticked_body(app_env, seed):
    """§4.5 worked example, observable via the content JSON."""
    c, _, _ = app_env
    _seed_text(seed, 1, 1, "Discussion of tathāgatagarbha doctrine.")
    # Snippet renders with diacritics intact — folding is index-only.
    assert "tathāgatagarbha" in _snippets(c, "tathagatagarbha")


def test_diacriticked_query_also_finds_diacriticked_body(app_env, seed):
    c, _, _ = app_env
    _seed_text(seed, 1, 1, "Śāntideva wrote the Bodhicaryāvatāra.")
    assert "Śāntideva" in _snippets(c, "Śāntideva")


def test_phonetic_query_does_not_match_wylie_body(app_env, seed):
    """§4.5 deferred query-expansion: phonetic↔Wylie pairs (`byang chub`
    / `jangchub`) MUST NOT match via folding. They are linked only at
    the entity level, and that link is the deferred feature."""
    c, _, _ = app_env
    _seed_text(seed, 1, 1, "the Tibetan term byang chub means awakening")
    doc = c.get("/api/v1/content?q=jangchub").get_json()
    assert doc["books"] == []                 # no match landed


def test_empty_query_renders_without_crashing(app_env):
    """Empty query short-circuits — no FTS5 syntax errors."""
    c, _, _ = app_env
    assert c.get("/search?q=").status_code == 200            # the shell still loads
    assert c.get("/api/v1/content?q=").get_json()["books"] == []


def test_search_is_reachable_from_nav(app_env):
    # Search is the sectioned module at /search; the full-text content search moved to /text.
    # Both are in the nav. Section nav is the shared client-rendered floating menu
    # (LibraryUI.nav) — its items carry the routes.
    c, _, _ = app_env
    home = c.get("/")
    assert home.status_code == 200
    assert b"'/search'" in home.data and b"'/text'" in home.data
    assert c.get("/search").status_code == 200
    assert c.get("/text").status_code == 200
