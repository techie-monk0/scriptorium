"""Tests for the EditionVerifier (catalogue/edition_verify.py).

Engine logic uses stub sources; the Open Library / Google Books sources are
exercised through their pure parse functions and an injected opener (no network).
"""
from __future__ import annotations

import json

import catalogue.services.edition_verify as EV


class _StubSource(EV.EditionSource):
    def __init__(self, name, isbn_recs=None, tp_recs=None):
        self.name = name
        self._isbn = isbn_recs or []
        self._tp = tp_recs or []

    def by_isbn(self, isbn):
        return list(self._isbn)

    def by_title_publisher(self, title, *, publisher=None, year=None):
        return list(self._tp)


def _rec(source, **kw):
    return EV.EditionRecord(source=source, **kw)


# A valid ISBN-13 for the isbn-path tests (Open Library's "The Way of the
# Bodhisattva" — checksum-valid).
VALID_ISBN = "9781590303887"


# ── path selection ─────────────────────────────────────────────────────────────
def test_isbn_path_preferred(tmp_path):
    v = EV.EditionVerifier(sources=[
        _StubSource("s", isbn_recs=[_rec("s", title="The Way of the Bodhisattva",
                                          authors=("Shantideva",),
                                          publisher="Shambhala", year=2006)])
    ], db=None)
    rep = v.verify({"title": "The Way of the Bodhisattva",
                    "authors": ["Shantideva"], "publisher": "Shambhala",
                    "year": 2006}, isbn=VALID_ISBN)
    assert rep.matched and rep.by == "isbn"
    # title/publisher/year/authors all confirmed; translators unverified (neither
    # side supplied one) → honest verdict is "partial", not "confirmed".
    assert rep.overall == "partial"
    assert rep.get_field("authors").status == "confirmed"


def test_title_publisher_fallback_when_no_isbn():
    v = EV.EditionVerifier(sources=[
        _StubSource("s", tp_recs=[_rec("s", title="Foo", authors=("Bar",),
                                       publisher="Wisdom")])
    ], db=None)
    rep = v.verify({"title": "Foo", "authors": ["Bar"], "publisher": "Wisdom"})
    assert rep.matched and rep.by == "title_publisher"


def test_no_records_is_unverified():
    v = EV.EditionVerifier(sources=[_StubSource("s")], db=None)
    rep = v.verify({"title": "Nothing matches"})
    assert not rep.matched and rep.overall == "unverified" and rep.by == "none"


# ── field verdicts ─────────────────────────────────────────────────────────────
def test_scalar_mismatch_and_confirm():
    v = EV.EditionVerifier(sources=[
        _StubSource("s", tp_recs=[_rec("s", title="Real Title",
                                       publisher="Penguin", year=1999)])
    ], db=None)
    rep = v.verify({"title": "Real Title", "publisher": "Random House",
                    "year": 1999})
    assert rep.get_field("title").status == "confirmed"
    assert rep.get_field("publisher").status == "mismatch"
    assert rep.get_field("year").status == "confirmed"
    assert rep.overall == "mismatch"


def test_title_match_is_diacritic_insensitive():
    v = EV.EditionVerifier(sources=[
        _StubSource("s", tp_recs=[_rec("s", title="Bodhicaryāvatāra")])
    ], db=None)
    rep = v.verify({"title": "Bodhicaryavatara"})
    assert rep.get_field("title").status == "confirmed"


def test_author_set_breakdown():
    v = EV.EditionVerifier(sources=[
        _StubSource("s", tp_recs=[_rec("s", title="T",
                                       authors=("Jane Doe", "Extra Editor"))])
    ], db=None)
    rep = v.verify({"title": "T", "authors": ["Jane Doe", "Unknown Person"]})
    f = rep.get_field("authors")
    assert f.status == "mismatch"
    assert "Jane Doe" in f.detail["confirmed"]
    assert "Unknown Person" in f.detail["inferred_only"]
    assert "Extra Editor" in f.detail["authority_only"]


def test_unverified_field_when_authority_silent():
    v = EV.EditionVerifier(sources=[
        _StubSource("s", tp_recs=[_rec("s", title="T")])     # no authors given
    ], db=None)
    rep = v.verify({"title": "T", "authors": ["Somebody"]})
    assert rep.get_field("authors").status == "unverified"
    assert rep.overall in ("confirmed", "partial")           # title confirmed


# ── Open Library source ────────────────────────────────────────────────────────
_OL_ISBN_BODY = json.dumps({
    f"ISBN:{VALID_ISBN}": {
        "title": "The Way of the Bodhisattva",
        "authors": [{"name": "Śāntideva"}, {"name": "Padmakara Translation Group"}],
        "publishers": [{"name": "Shambhala"}],
        "publish_date": "2006",
    }
}).encode("utf-8")


def test_openlibrary_by_isbn_parses(monkeypatch):
    src = EV.OpenLibrarySource(opener=lambda url, t: _OL_ISBN_BODY)
    recs = src.by_isbn(VALID_ISBN)
    assert len(recs) == 1
    r = recs[0]
    assert r.title == "The Way of the Bodhisattva"
    assert r.publisher == "Shambhala" and r.year == 2006
    assert "Śāntideva" in r.authors


def test_parse_ol_search():
    data = {"docs": [{"title": "X", "author_name": ["A", "B"],
                      "publisher": ["Wisdom"], "first_publish_year": 1990,
                      "isbn": ["9780000000000"]}]}
    recs = EV._parse_ol_search(data, "openlibrary")
    assert recs[0].title == "X" and recs[0].year == 1990
    assert recs[0].authors == ("A", "B") and recs[0].publisher == "Wisdom"


# ── Google Books source ────────────────────────────────────────────────────────
def test_parse_gbooks():
    data = {"items": [{"volumeInfo": {
        "title": "Y", "authors": ["C"], "publisher": "Snow Lion",
        "publishedDate": "1987-05",
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9781111111111"}],
    }}]}
    recs = EV._parse_gbooks(data, "googlebooks")
    assert recs[0].title == "Y" and recs[0].year == 1987
    assert recs[0].publisher == "Snow Lion" and recs[0].isbn == "9781111111111"


def test_gbooks_by_isbn_with_injected_opener():
    body = json.dumps({"items": [{"volumeInfo": {
        "title": "Z", "authors": ["D"], "publishedDate": "2001"}}]}).encode("utf-8")
    src = EV.GoogleBooksSource(opener=lambda url, t: body)
    recs = src.by_isbn(VALID_ISBN)
    assert recs and recs[0].title == "Z" and recs[0].year == 2001


# ── year helper ────────────────────────────────────────────────────────────────
def test_year_extraction():
    assert EV._year("2006") == 2006
    assert EV._year("March 1997") == 1997
    assert EV._year(1985) == 1985
    assert EV._year("n.d.") is None


# ── registry ───────────────────────────────────────────────────────────────────
def test_registry():
    assert "openlibrary" in EV._SOURCES and "googlebooks" in EV._SOURCES
    built = EV.build_sources(["openlibrary"])
    assert len(built) == 1 and built[0].name == "openlibrary"
