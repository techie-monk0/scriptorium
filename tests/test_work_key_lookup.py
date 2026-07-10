"""isbn.work_key_for_isbn — OL work-key resolution (offline, injected opener)."""
from __future__ import annotations

from catalogue.services.isbn import work_key_for_isbn


def test_parses_work_key():
    opener = lambda url, t: b'{"works":[{"key":"/works/OL42W"}]}'
    assert work_key_for_isbn("9780861711765", opener=opener) == "/works/OL42W"


def test_no_works_returns_none():
    assert work_key_for_isbn("9780861711765", opener=lambda u, t: b"{}") is None


def test_network_failure_returns_none():
    def boom(u, t):
        raise TimeoutError("slow")
    assert work_key_for_isbn("9780861711765", opener=boom) is None


def test_invalid_isbn_short_circuits():
    # Must not even call the opener for a bad checksum.
    def must_not_call(u, t):
        raise AssertionError("opener should not run for an invalid ISBN")
    assert work_key_for_isbn("not-an-isbn", opener=must_not_call) is None


def test_rejects_non_work_key():
    opener = lambda u, t: b'{"works":[{"key":"/books/OL1M"}]}'
    assert work_key_for_isbn("9780861711765", opener=opener) is None
