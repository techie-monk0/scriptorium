"""Cross-format "already in catalogue" verdict (catalogue/domain/intake_match).

Pins the layered match (exact ISBN → OL work-key cluster → title fuzzy), the
form roll-up across an edition's holdings, and the never-raise contract when the
injected work-key lookup is slow/down.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from catalogue.db_store import db as dbmod
from catalogue.services import intake_match


@pytest.fixture
def conn():
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    c = dbmod.init_db(fd.name)
    # One work, two editions (print + epub) sharing an OL work key but with
    # DIFFERENT ISBNs — the cross-format case the feature exists for.
    c.execute("INSERT INTO edition (id, title, isbn, ol_work_key) "
              "VALUES (10, 'The Way of the Bodhisattva', '9781111111116', '/works/OL1W')")
    c.execute("INSERT INTO edition (id, title, isbn, ol_work_key) "
              "VALUES (11, 'The Way of the Bodhisattva (ebook)', '9782222222227', '/works/OL1W')")
    c.execute("INSERT INTO holding (edition_id, form, holding_type) VALUES (10, 'physical', 'physical')")
    c.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (11, 'electronic', 'x.epub')")
    c.commit()
    yield c
    c.close()
    Path(fd.name).unlink()


def test_exact_isbn_match(conn):
    v = intake_match.catalogue_verdict(conn, "9781111111116")
    assert v["in_catalogue"] is True
    assert v["matched_by"] == "isbn"
    assert v["editions"][0]["id"] == 10


def test_cross_format_work_key_match(conn):
    # A print ISBN we DON'T hold, but its OL work key matches editions we do.
    v = intake_match.catalogue_verdict(
        conn, "9783333333338", ol_work_key_fetch=lambda i: "/works/OL1W")
    assert v["matched_by"] == "work_key"
    forms = sorted(f for e in v["editions"] for f in e["forms"])
    assert forms == ["epub", "physical"]   # both formats surfaced


def test_title_only_fallback_is_uncertain(conn):
    # Editions 10/11 carry no author data, OL gives only a title → title-only is a
    # partial match: surfaced as uncertain, not auto-accepted.
    v = intake_match.catalogue_verdict(
        conn, "9784444444449",
        ol_work_key_fetch=lambda i: None,
        isbn_lookup=lambda i: {"title": "The Way of the Bodhisattva"})
    assert v["in_catalogue"] is False
    assert {u["id"] for u in v["uncertain"]} == {10, 11}


def test_title_containment_matches_long_subtitle(conn):
    # The real bug: OL returns the short title, the catalogue title has a subtitle.
    # With no author data it's a title-only (uncertain) match — still surfaced.
    conn.execute("INSERT INTO edition (id, title, isbn) VALUES "
                 "(20, 'Mind Seeing Mind: Mahamudra and the Geluk Tradition', '9781614296010')")
    conn.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (20, 'electronic', 'm.pdf')")
    conn.commit()
    v = intake_match.catalogue_verdict(
        conn, "9781614295778",
        ol_work_key_fetch=lambda i: None,
        isbn_lookup=lambda i: {"title": "Mind Seeing Mind"})
    assert v["matched_by"] == "title"
    assert {u["id"] for u in v["uncertain"]} == {20}
    assert v["uncertain"][0]["forms"] == ["pdf"]


def test_clear_conflict_rejected(conn):
    # Same title, but a DIFFERENT publisher AND author on both sides, no overlap →
    # a different book, dropped (not even surfaced as uncertain).
    conn.execute("INSERT INTO work (id) VALUES (1)")
    conn.execute("INSERT INTO edition (id, title, isbn, publisher) VALUES "
                 "(30, 'Compassion', '9781614296099', 'Shambhala')")
    conn.execute("INSERT INTO edition_work (edition_id, work_id) VALUES (30, 1)")
    conn.execute("INSERT INTO person (id, primary_name) VALUES (1, 'Pema Chodron')")
    conn.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (1, 1, 'author')")
    conn.commit()
    v = intake_match.catalogue_verdict(
        conn, "9781614296100",
        ol_work_key_fetch=lambda i: None,
        isbn_lookup=lambda i: {"title": "Compassion", "authors": ["Tenzin Gyatso"],
                               "publishers": ["Wisdom Publications"]})
    assert v["in_catalogue"] is False
    assert v["uncertain"] == []


def test_partial_author_match_is_uncertain_not_rejected(conn):
    # Two authors on the book; OL names one of them (matches) plus one we don't have.
    conn.execute("INSERT INTO work (id) VALUES (1)")
    conn.execute("INSERT INTO edition (id, title, isbn) VALUES (40, 'Compassion', '9781614296200')")
    conn.execute("INSERT INTO edition_work (edition_id, work_id) VALUES (40, 1)")
    conn.execute("INSERT INTO person (id, primary_name) VALUES (1, 'Thubten Chodron')")
    conn.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (1, 1, 'author')")
    conn.commit()
    v = intake_match.catalogue_verdict(
        conn, "9781614296300",
        ol_work_key_fetch=lambda i: None,
        isbn_lookup=lambda i: {"title": "Compassion",
                               "authors": ["Thubten Chodron", "Tenzin Gyatso"]})
    assert v["in_catalogue"] is False            # not auto-accepted
    assert v["matched_by"] == "title"
    u = v["uncertain"]
    assert len(u) == 1 and u[0]["id"] == 40
    assert "Thubten Chodron" in u[0]["authors_matched"]
    assert u[0]["authors_unmatched_lookup"] == ["Tenzin Gyatso"]


def test_full_author_agreement_is_confirmed(conn):
    conn.execute("INSERT INTO work (id) VALUES (1)")
    conn.execute("INSERT INTO edition (id, title, isbn) VALUES (41, 'Compassion', '9781614296400')")
    conn.execute("INSERT INTO edition_work (edition_id, work_id) VALUES (41, 1)")
    conn.execute("INSERT INTO person (id, primary_name) VALUES (1, 'Thubten Chodron')")
    conn.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (1, 1, 'author')")
    conn.commit()
    v = intake_match.catalogue_verdict(
        conn, "9781614296500",
        ol_work_key_fetch=lambda i: None,
        isbn_lookup=lambda i: {"title": "Compassion", "authors": ["Thubten Chodron"]})
    assert v["in_catalogue"] is True and v["matched_by"] == "title"


def test_title_only_no_authors_is_uncertain(conn):
    # OL gives only a title (no authors/publisher) → surface for confirmation.
    conn.execute("INSERT INTO edition (id, title, isbn) VALUES "
                 "(42, 'Mind Seeing Mind: Mahamudra', '9781614296600')")
    conn.commit()
    v = intake_match.catalogue_verdict(
        conn, "9781614296700",
        ol_work_key_fetch=lambda i: None,
        isbn_lookup=lambda i: {"title": "Mind Seeing Mind"})
    assert v["in_catalogue"] is False
    assert [u["id"] for u in v["uncertain"]] == [42]


def test_miss_and_never_raises_on_slow_lookup(conn):
    def boom(_isbn):
        raise TimeoutError("openlibrary slow")
    v = intake_match.catalogue_verdict(
        conn, "9785555555556", ol_work_key_fetch=boom, isbn_lookup=boom)
    assert v["in_catalogue"] is False
    assert v["matched_by"] is None
    assert v["editions"] == []


def test_ensure_ol_work_key_sets_when_missing(conn):
    conn.execute("INSERT INTO edition (id, title, isbn) VALUES (50, 'X', '9781614296010')")
    conn.commit()
    key = intake_match.ensure_ol_work_key(conn, 50, fetch=lambda i: "/works/OL5W")
    assert key == "/works/OL5W"
    assert conn.execute("SELECT ol_work_key FROM edition WHERE id=50").fetchone()[0] == "/works/OL5W"


def test_ensure_ol_work_key_noops_without_isbn_or_fetch(conn):
    conn.execute("INSERT INTO edition (id, title) VALUES (51, 'no isbn')")
    conn.commit()
    assert intake_match.ensure_ol_work_key(conn, 51, fetch=lambda i: "/works/OL9W") is None
    # fetch=None is a no-op even with an ISBN
    conn.execute("INSERT INTO edition (id, title, isbn) VALUES (52, 'y', '9781614296010')")
    conn.commit()
    assert intake_match.ensure_ol_work_key(conn, 52, fetch=None) is None


def test_ensure_ol_work_key_does_not_overwrite(conn):
    conn.execute("INSERT INTO edition (id, title, isbn, ol_work_key) VALUES "
                 "(53, 'z', '9781614296010', '/works/EXISTING')")
    conn.commit()
    intake_match.ensure_ol_work_key(conn, 53, fetch=lambda i: "/works/NEW")
    assert conn.execute("SELECT ol_work_key FROM edition WHERE id=53").fetchone()[0] == "/works/EXISTING"


# ── cip_verdict: copyright-page intake (§14.9) ────────────────────────────────
from types import SimpleNamespace


def _rec(**kw):
    """A duck-typed cip.CipRecord stand-in for the fields cip_verdict reads."""
    kw.setdefault("isbns", [])
    kw.setdefault("title", None)
    kw.setdefault("authors", [])
    kw.setdefault("publisher", None)
    return SimpleNamespace(**kw)


def test_cip_verdict_found_by_isbn(conn):
    v = intake_match.cip_verdict(conn, _rec(isbns=["9781111111116"]))
    assert v["in_catalogue"] is True
    assert v["matched_by"] == "isbn"
    assert v["isbn"] == "9781111111116"
    assert v["editions"][0]["id"] == 10


def test_cip_verdict_tries_every_isbn(conn):
    # First ISBN is unheld; the second matches edition 11 — the CIP often lists
    # several (hardback/paper/ebook) and ANY hit means we hold the book.
    v = intake_match.cip_verdict(conn, _rec(isbns=["9789999999990", "9782222222227"]))
    assert v["in_catalogue"] is True
    assert v["isbn"] == "9782222222227"


def test_cip_verdict_title_only_is_uncertain(conn):
    v = intake_match.cip_verdict(conn, _rec(title="The Way of the Bodhisattva"))
    assert v["in_catalogue"] is False
    assert v["matched_by"] == "title"
    assert v["uncertain"] and v["uncertain"][0]["id"] in (10, 11)


def test_cip_verdict_unknown_not_found(conn):
    v = intake_match.cip_verdict(conn, _rec(title="A Totally Unrelated Book"))
    assert v["in_catalogue"] is False
    assert v["matched_by"] is None
    assert v["editions"] == [] and v["uncertain"] == []


def test_cip_verdict_never_raises_on_bad_fetch(conn):
    def boom(_i):
        raise TimeoutError("down")
    v = intake_match.cip_verdict(conn, _rec(isbns=["9789999999990"]),
                                 ol_work_key_fetch=boom, isbn_lookup=boom)
    assert v["in_catalogue"] is False   # swallowed; isbn echoed as the primary
    assert v["isbn"] == "9789999999990"


# ── editions_now_holding: local-only "has it been catalogued since?" ──────────
def test_now_holding_exact_isbn(conn):
    eds = intake_match.editions_now_holding(conn, isbn="9781111111116")
    assert [e["id"] for e in eds] == [10]


def test_now_holding_cross_edition_by_title_and_shared_author(conn):
    # Different ISBN than anything held, but same title + a SHARED author (with a
    # spurious extra one) → matched cross-edition, no network.
    conn.execute("INSERT INTO person (id, primary_name) VALUES (1, 'Shantideva')")
    conn.execute("INSERT INTO edition_author (edition_id, person_id) VALUES (10, 1)")
    conn.commit()
    eds = intake_match.editions_now_holding(
        conn, isbn="9789999999990",
        meta={"title": "The Way of the Bodhisattva",
              "authors": ["Shantideva", "Some Subject"]})
    assert 10 in [e["id"] for e in eds]


def test_now_holding_title_only_does_not_match(conn):
    # Title containment but NO author corroboration → not enough to call it held
    # (avoids hiding a genuinely-missing same-title book).
    eds = intake_match.editions_now_holding(
        conn, isbn="9789999999990",
        meta={"title": "The Way of the Bodhisattva", "authors": []})
    assert eds == []


def test_now_holding_unknown_is_empty(conn):
    eds = intake_match.editions_now_holding(
        conn, isbn="9789999999990",
        meta={"title": "A Totally Unrelated Book", "authors": ["Nobody"]})
    assert eds == []
