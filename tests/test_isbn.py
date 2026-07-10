"""Step-3b regression tests — ISBN-13 validation + Open Library lookup."""
from __future__ import annotations

import json

import pytest

from catalogue.services.isbn import lookup, normalize_isbn, validate_isbn13


# ── Normalize ─────────────────────────────────────────────────────────────
def test_normalize_strips_hyphens_and_spaces():
    assert normalize_isbn("978-0-205-30902-3") == "9780205309023"
    assert normalize_isbn("  978 0205 309023  ") == "9780205309023"
    assert normalize_isbn("") == ""
    assert normalize_isbn(None) == ""  # tolerate None for missing fields


# ── Validate (checksum) ───────────────────────────────────────────────────
def test_valid_isbn13_passes_checksum():
    # Known-good ISBN-13s (manually computed).
    assert validate_isbn13("9780205309023")     # Strunk & White (real)
    assert validate_isbn13("978-0-205-30902-3") # same with hyphens


def test_wrong_checksum_fails():
    # Flip the last digit — should fail.
    assert not validate_isbn13("9780205309022")


def test_wrong_length_fails():
    assert not validate_isbn13("123")
    assert not validate_isbn13("12345678901234")   # 14 digits
    assert not validate_isbn13("")


def test_non_digit_input_doesnt_explode():
    assert validate_isbn13("not a barcode") is False


# ── Open Library lookup ──────────────────────────────────────────────────
_FAKE_OK_BODY = json.dumps({
    "ISBN:9780205309023": {
        "title": "The Elements of Style",
        "authors": [{"name": "William Strunk Jr."}, {"name": "E. B. White"}],
        "publishers": [{"name": "Penguin"}],
        "publish_date": "1999",
    }
}).encode("utf-8")


def test_lookup_parses_a_real_record():
    seen_url = {}

    def opener(url, timeout):
        seen_url["url"] = url
        seen_url["timeout"] = timeout
        return _FAKE_OK_BODY

    md = lookup("978-0-205-30902-3", opener=opener)
    assert md is not None
    assert md["title"] == "The Elements of Style"
    assert md["authors"] == ["William Strunk Jr.", "E. B. White"]
    assert md["publishers"] == ["Penguin"]
    assert md["publish_date"] == "1999"
    assert md["isbn_13"] == "9780205309023"
    assert md["source"] == "openlibrary"
    # ISBN sent to OL is normalized (no hyphens).
    assert "9780205309023" in seen_url["url"]


def test_lookup_returns_none_on_invalid_isbn():
    # Invalid checksum → don't even hit the network.
    def boom(*_a, **_kw):
        raise AssertionError("network should not be called for invalid ISBN")

    assert lookup("9780205309022", opener=boom) is None


def test_lookup_returns_none_on_empty_record():
    """Open Library returns `{}` for unknown ISBNs."""
    def empty(url, timeout):
        return b"{}"
    assert lookup("9780205309023", opener=empty) is None


def test_lookup_returns_none_on_network_error():
    def boom(url, timeout):
        raise OSError("simulated network failure")
    # MUST NOT raise — capture falls back to manual path.
    assert lookup("9780205309023", opener=boom) is None


def test_lookup_returns_none_on_malformed_json():
    def garbage(url, timeout):
        return b"<html>not json</html>"
    assert lookup("9780205309023", opener=garbage) is None


# ── Google Books fallback (OpenLibrary miss) ─────────────────────────────────
import json as _json
from catalogue.services.isbn import search_by_title

_GBOOKS_ISBN = _json.dumps({"items": [{"volumeInfo": {
    "title": "The Bodhisattva Ideal", "authors": ["Thupten Jinpa"],
    "publisher": "Wisdom Publications", "publishedDate": "2024-05",
    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9798890700292"}],
    "imageLinks": {"thumbnail": "http://books.google.com/books/content?id=x&img=1"}}}]}).encode()

_GBOOKS_TITLE = _json.dumps({"items": [
    {"volumeInfo": {"title": "The Bodhisattva Ideal", "authors": ["Thupten Jinpa"],
                    "publishedDate": "2024",
                    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9798890700292"}],
                    "imageLinks": {"thumbnail": "http://x/y"}}},
    {"volumeInfo": {"title": "The Bodhisattva Ideal in Buddhism", "authors": ["Sangharakshita"],
                    "publishedDate": "1999"}}]}).encode()


def _ol_miss_then_google(isbn_body, title_body):
    """An opener where OpenLibrary misses (empty) and Google Books answers."""
    def opener(url, timeout):
        if "googleapis.com/books" in url:
            return isbn_body if "isbn" in url else title_body
        if "openlibrary.org/api/books" in url:
            return b"{}"                       # OL has no record (e.g. a 979 ISBN)
        if "openlibrary.org/search.json" in url:
            return b'{"docs": []}'             # OL title search empty
        return b"{}"
    return opener


def test_lookup_falls_back_to_google_books():
    # 9798890700292 is a valid ISBN-13 OL doesn't carry (a 979-prefix 2024 title).
    assert validate_isbn13("9798890700292")
    md = lookup("9798890700292", opener=_ol_miss_then_google(_GBOOKS_ISBN, _GBOOKS_TITLE))
    assert md is not None
    assert md["title"] == "The Bodhisattva Ideal"
    assert md["source"] == "googlebooks"
    assert md["isbn_13"] == "9798890700292"
    # A cover comes back (http upgraded to https so it isn't blocked as mixed content).
    assert md["cover_url"].startswith("https://")


def test_search_by_title_falls_back_to_google_books():
    cands = search_by_title("Bodhisattva Ideal", "Jinpa",
                            opener=_ol_miss_then_google(_GBOOKS_ISBN, _GBOOKS_TITLE))
    assert len(cands) == 2
    assert cands[0]["title"] == "The Bodhisattva Ideal"
    assert cands[0]["isbn_13"] == "9798890700292"
    assert cands[0]["source"] == "googlebooks"
    assert cands[0]["cover_url"]


def test_openlibrary_still_preferred_when_present():
    # When OL has the record, Google is never consulted (boom if it is).
    def opener(url, timeout):
        if "openlibrary.org/api/books" in url:
            return _FAKE_OK_BODY
        raise AssertionError("Google Books should not be called when OL has the record")
    md = lookup("978-0-205-30902-3", opener=opener)
    assert md["source"] == "openlibrary"
