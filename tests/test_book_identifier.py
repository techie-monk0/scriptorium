"""Tests for the BookIdentifier API (catalogue/book_identifier.py).

Covers: per-scheme extraction/normalization/validation, scheme-agnostic resolution
against fake authority sources, and that a NEW scheme plugs in without touching the
facade — the modularity guarantee the design promises.
"""
from __future__ import annotations

from catalogue.services import book_identifier as BI
from catalogue.services.edition_verify import EditionRecord


# ── fake authority sources (no network) ─────────────────────────────────────────────
class _FakeSource:
    """Maps (method, value) → records, recording which method was called."""
    name = "fake"

    def __init__(self, by_isbn=None, by_lccn=None):
        self._isbn = by_isbn or {}
        self._lccn = by_lccn or {}
        self.calls = []

    def by_isbn(self, isbn):
        self.calls.append(("isbn", isbn))
        return list(self._isbn.get(isbn, []))

    def by_lccn(self, lccn):
        self.calls.append(("lccn", lccn))
        return list(self._lccn.get(lccn, []))


def _rec(title, **kw):
    return EditionRecord(source="fake", title=title, **kw)


# ── ISBN scheme ──────────────────────────────────────────────────────────────────
def test_isbn_extracted_from_title_string():
    bi = BI.BookIdentifier()
    ids = bi.extract(title="Some Book -- Author -- 9781614298939 -- Anna’s Archive")
    assert [str(i) for i in ids] == ["isbn:9781614298939"]
    assert ids[0].found_in == "title"


def test_isbn_extracted_from_front_matter_with_hyphens():
    bi = BI.BookIdentifier()
    ids = bi.extract(front_matter="… copyright page … ISBN 978-1-891868-23-8 …")
    assert [i.value for i in ids] == ["9781891868238"]
    assert ids[0].found_in == "front_matter"


def test_isbn10_converted_to_13():
    s = BI.IsbnScheme()
    v = s.normalize("0-86171-345-6")          # a valid ISBN-10
    assert v and len(v) == 13 and v.startswith("978")
    assert s.validate(v)


def test_invalid_isbn_rejected():
    bi = BI.BookIdentifier()
    # 13 digits but bad checksum → dropped by validate
    assert bi.extract(title="x 9781614298930 y") == []


# ── OCR-tolerant, checksum-validated ISBN extraction ────────────────────────────────
def test_isbn_ocr_variants_via_checksum():
    s = BI.IsbnScheme()

    def got(t):
        return [v for _, v in s.spans(t)]
    assert got("ISBN 978-1-61429-893-9") == ["9781614298939"]      # clean
    assert got("ISBN 978  1  61429  8939") == ["9781614298939"]    # double spaces
    assert got("ISBN 9 7 8 1 6 1 4 2 9 8 9 3 9") == ["9781614298939"]   # letter-spaced
    assert got("ISBN Number: 0-918753-08-2 (V.1)") == ["9780918753083"]  # label + ISBN-10
    assert got("ISBN O-918753-O8-2") == ["9780918753083"]          # O↔0 OCR confusion
    # a random 13-digit run that fails the checksum is NOT emitted (loose ≠ unsafe)
    assert got("order no. 9781614298930 shipped") == []


def test_isbn_checksum_guards_against_false_positives():
    s = BI.IsbnScheme()
    # a long digit string near no anchor / failing checksum yields nothing
    assert [v for _, v in s.spans("phone 9785551234567 call now")] == []


def test_no_identifier_returns_empty():
    bi = BI.BookIdentifier()
    assert bi.extract(title="The Torch for the Definitive Meaning",
                      front_matter="a clean page with no number") == []


# ── LCCN scheme ──────────────────────────────────────────────────────────────────
def test_lccn_extracted_from_front_matter():
    bi = BI.BookIdentifier()
    ids = bi.extract(front_matter="Library of Congress Control Number: 2009925465")
    assert [str(i) for i in ids] == ["lccn:2009925465"]


def test_lccn_old_hyphenated_card_number():
    s = BI.LccnScheme()
    assert s.find("Library of Congress Catalog Card Number: 75-189390")
    assert s.normalize("75-189390") == "75189390"


# ── ordering: most-trusted scheme first ─────────────────────────────────────────────
def test_isbn_ordered_before_lccn():
    bi = BI.BookIdentifier()
    ids = bi.extract(
        title="Book -- 9781614298939",
        front_matter="Library of Congress Control Number: 2009925465")
    assert [i.scheme for i in ids] == ["isbn", "lccn"]   # isbn.priority < lccn.priority


# ── scheme-agnostic resolution ──────────────────────────────────────────────────────
def test_resolve_uses_first_identifier_that_hits():
    bi = BI.BookIdentifier()
    src = _FakeSource(by_isbn={"9781614298939": [_rec("The Real Title",
                                                      authors=("A. Author",))]})
    ids = bi.extract(title="junk -- 9781614298939")
    res = bi.resolve(ids, sources=[src])
    assert res is not None
    assert res.record.title == "The Real Title"
    assert res.identifier.scheme == "isbn"


def test_resolve_falls_through_isbn_miss_to_lccn():
    """ISBN present but unknown to the source; LCCN resolves. The caller never
    branches on scheme — resolve() just walks the identifier list."""
    bi = BI.BookIdentifier()
    src = _FakeSource(by_isbn={}, by_lccn={"2009925465": [_rec("Via LCCN")]})
    ids = bi.extract(title="b -- 9781614298939",
                     front_matter="LCCN: 2009925465")
    res = bi.resolve(ids, sources=[src])
    assert res.record.title == "Via LCCN" and res.identifier.scheme == "lccn"
    # both lookups were attempted, ISBN first
    assert src.calls == [("isbn", "9781614298939"), ("lccn", "2009925465")]


def test_resolve_text_convenience():
    bi = BI.BookIdentifier()
    src = _FakeSource(by_isbn={"9781614298939": [_rec("T")]})
    res = bi.resolve_text("x -- 9781614298939", sources=[src])
    assert res and res.record.title == "T"


def test_resolve_none_when_nothing_hits():
    bi = BI.BookIdentifier()
    assert bi.resolve(bi.extract(title="9781614298939"),
                      sources=[_FakeSource()]) is None


# ── find_in_text: whole-text, CIP-aware (EPUBs land the copyright page anywhere) ────
import re as _re
_CIP = _re.compile(r"(?i)cataloging.in.publication|library of congress|copyright\s")


def test_find_in_text_locates_isbn_in_the_middle():
    bi = BI.BookIdentifier()
    text = ("body text " * 4000
            + "Library of Congress Cataloging-in-Publication Data ISBN 978-1-61429-472-6 "
            + "more body " * 4000)            # ISBN ~35% in, neither head nor tail
    ident = bi.find_in_text(text, markers=_CIP)
    assert ident and ident.value == "9781614294726"


def test_find_in_text_prefers_cip_isbn_over_series_list():
    bi = BI.BookIdentifier()
    text = ("Also in this series: Volume One ISBN 978-1-55939-188-7 . "
            + "filler " * 3000
            + "Library of Congress Cataloging-in-Publication Data … ISBN 978-1-55939-066-8")
    ident = bi.find_in_text(text, markers=_CIP)
    assert ident.value == "9781559390668"    # the CIP one, not the first (series-list)


def test_find_in_text_isbn10_in_copyright_block():
    bi = BI.BookIdentifier()
    text = "x " * 5000 + "Copyright 1998. ISBN 1-55939-066-2 (alk. paper)"  # valid ISBN-10
    ident = bi.find_in_text(text, markers=_CIP)
    assert ident and ident.value == "9781559390668"   # ISBN-10 → 13, found deep


def test_find_in_text_none_when_no_identifier():
    bi = BI.BookIdentifier()
    assert bi.find_in_text("just prose, no numbers here", markers=_CIP) is None


# ── a NEW scheme plugs in without touching the facade ───────────────────────────────
def test_custom_scheme_is_discovered_and_used():
    class _OclcScheme(BI.IdentifierScheme):
        name = "oclc"
        priority = 5

        def find(self, text):
            import re
            return [m.group(1) for m in re.finditer(r"(?i)OCLC[:\s]*(\d{6,})", text or "")]

        def lookup(self, value, sources):
            for s in sources:
                recs = getattr(s, "by_oclc", lambda v: [])(value)
                if recs:
                    return recs
            return []

    bi = BI.BookIdentifier(schemes=BI.default_schemes() + [_OclcScheme()])
    ids = bi.extract(front_matter="OCLC: 123456789")
    assert [str(i) for i in ids] == ["oclc:123456789"]

    class _OclcSrc:
        name = "o"
        def by_oclc(self, v):
            return [_rec("From OCLC")] if v == "123456789" else []

    res = bi.resolve(ids, sources=[_OclcSrc()])
    assert res.record.title == "From OCLC"
