"""Regression: /editions/search (the Browse book-title typeahead + works-review merge
target) must fold BOTH sides of the title match the same way.

A previous version folded only the query (`fold_key`, which strips diacritics AND folds
aspirate digraphs: "Buddha"→"budda") and compared it to a merely lower-cased column, so an
ASCII query never matched a diacritic/aspirate title — e.g. typing "Buddha" or "Buddhapalita"
found nothing for a title stored as "Buddhāpālita's…".
"""
from __future__ import annotations

TITLE = "Buddhapālita’s Commentary on Nāgārjuna’s Middle Way: (Buddhapālita-Mūlamadhyamaka-Vṛtti)"


def test_editions_search_folds_diacritics_and_digraphs(app_env, seed):
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES (?)", (TITLE,)).lastrowid
    # All three are plain-ASCII queries against a diacritic + aspirate-digraph title.
    for q in ("Buddhapalita", "Buddha", "Nagarjuna", "Middle Way"):
        matches = c.get(f"/editions/search?q={q}").get_json()["matches"]
        assert any(m["edition_id"] == eid for m in matches), f"{q!r} should match {TITLE!r}"


def test_editions_search_by_isbn_fragment_and_exclude(app_env, seed):
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title, isbn) VALUES ('Some Plain Title', '9781949163209')").lastrowid
    by_isbn = c.get("/editions/search?q=9781949163209").get_json()["matches"]
    assert any(m["edition_id"] == eid for m in by_isbn)
    # `exclude` removes a row (used by the works-review merge typeahead).
    excluded = c.get(f"/editions/search?q=Plain&exclude={eid}").get_json()["matches"]
    assert all(m["edition_id"] != eid for m in excluded)


def test_editions_search_blank_is_empty(app_env):
    c, _, _ = app_env
    assert c.get("/editions/search?q=").get_json()["matches"] == []


def test_works_search_editions_branch_folds_diacritics(app_env, seed):
    # The works-review "attach existing" picker (?editions=1) shares the same fold bug
    # surface — an ASCII query must find a diacritic/aspirate edition title here too.
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES (?)", (TITLE,)).lastrowid
    matches = c.get("/works/search?q=Buddhapalita&editions=1").get_json()["matches"]
    assert any(m.get("kind") == "edition" and m.get("edition_id") == eid for m in matches)
