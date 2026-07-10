"""ThrottledTransport — throttle + 429 backoff + raise-don't-swallow.

This is the fix for the bulk-pass rate-limit bug: a 429 must NOT look like a
'no match'. Tests inject a fake urlopen + fake clock/sleep so they're instant
and never touch the network.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from catalogue.services.http_util import (
    ThrottledTransport, AuthorityUnavailable, MalformedResponse,
)


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []
    def now(self):
        return self.t
    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _resp(payload):
    body = json.dumps(payload).encode("utf-8")
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body
    return _R()


def _raw(body_bytes):
    """A 200 response carrying an arbitrary (possibly non-JSON) body."""
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body_bytes
    return _R()


def _http_error(code, retry_after=None):
    hdrs = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("http://x", code, "err", hdrs, None)


def _transport(clock, **kw):
    t = ThrottledTransport(min_interval=0.0, **kw)
    t._sleep = clock.sleep
    t._now = clock.now
    return t


# ── success + throttle spacing ──────────────────────────────────────────────────
def test_success_returns_parsed_json(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _resp({"ok": 1}))
    t = _transport(clock)
    assert t("http://x") == {"ok": 1}


def test_throttle_spaces_calls(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _resp({}))
    t = ThrottledTransport(min_interval=0.5)
    t._sleep = clock.sleep
    t._now = clock.now
    t("http://x"); t("http://x")
    # second call must have waited ~min_interval
    assert any(s >= 0.4 for s in clock.slept)


# ── 429 retry/backoff ───────────────────────────────────────────────────────────
def test_retries_429_then_succeeds(monkeypatch):
    clock = _Clock()
    calls = {"n": 0}
    def fake(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429)
        return _resp({"ok": 1})
    monkeypatch.setattr("urllib.request.urlopen", fake)
    t = _transport(clock, max_retries=3)
    assert t("http://x") == {"ok": 1}
    assert calls["n"] == 2
    assert clock.slept                      # backed off before retry


def test_429_honours_retry_after(monkeypatch):
    clock = _Clock()
    calls = {"n": 0}
    def fake(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, retry_after="7")
        return _resp({"ok": 1})
    monkeypatch.setattr("urllib.request.urlopen", fake)
    t = _transport(clock, max_retries=3)
    t("http://x")
    assert 7.0 in clock.slept


def test_429_exhausted_raises_authority_unavailable(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_error(429)))
    t = _transport(clock, max_retries=2)
    with pytest.raises(AuthorityUnavailable):
        t("http://x")


def test_network_error_exhausted_raises(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(OSError("down")))
    t = _transport(clock, max_retries=1)
    with pytest.raises(AuthorityUnavailable):
        t("http://x")


def test_200_with_non_json_body_is_malformed_not_unavailable(monkeypatch):
    """THE regression: VIAF's dead endpoint returns 200 + an HTML app shell. That
    must be a per-source MalformedResponse (caller skips the source), NOT an
    AuthorityUnavailable (which would HALT the whole verify pass). And it must not
    be retried — the body is fine, the format changed."""
    clock = _Clock()
    calls = {"n": 0}
    def fake(req, timeout=0):
        calls["n"] += 1
        return _raw(b"<!DOCTYPE html><html>not json</html>")
    monkeypatch.setattr("urllib.request.urlopen", fake)
    t = _transport(clock, max_retries=3)
    with pytest.raises(MalformedResponse):
        t("http://x")
    assert not isinstance(MalformedResponse(), AuthorityUnavailable)  # distinct types
    assert calls["n"] == 1                       # parse failure is NOT retried


def test_non_retryable_http_error_raises_immediately(monkeypatch):
    clock = _Clock()
    calls = {"n": 0}
    def fake(req, timeout=0):
        calls["n"] += 1
        raise _http_error(404)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    t = _transport(clock, max_retries=3)
    with pytest.raises(AuthorityUnavailable):
        t("http://x")
    assert calls["n"] == 1                   # 404 is not retried


# ── clients raise (not swallow) on AuthorityUnavailable ─────────────────────────
def test_wikidata_search_propagates_authority_unavailable():
    from catalogue.services.wikidata import WikidataClient
    def boom(url):
        raise AuthorityUnavailable("429")
    with pytest.raises(AuthorityUnavailable):
        WikidataClient(transport=boom).search("x")


def test_wikidata_entity_propagates_authority_unavailable():
    from catalogue.services.wikidata import WikidataClient
    def boom(url):
        raise AuthorityUnavailable("429")
    with pytest.raises(AuthorityUnavailable):
        WikidataClient(transport=boom).entity("Q1")


def test_viaf_propagates_authority_unavailable():
    from catalogue.services.viaf import VIAFClient
    def boom(url):
        raise AuthorityUnavailable("429")
    with pytest.raises(AuthorityUnavailable):
        VIAFClient(transport=boom).suggest("x")


def test_wikidata_still_swallows_ordinary_errors():
    """A non-AuthorityUnavailable failure (parse glitch) stays a graceful []/None."""
    from catalogue.services.wikidata import WikidataClient
    def bad(url):
        raise ValueError("garbage json")
    assert WikidataClient(transport=bad).search("x") == []
    assert WikidataClient(transport=bad).entity("Q1") is None
