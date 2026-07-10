"""Courteous HTTP transport for the authority clients (Wikidata, VIAF).

Why this exists: a bulk verify pass fires hundreds of lookups back-to-back. Public
authorities throttle that with HTTP 429, and the old clients swallowed the 429 into
an empty result — so a throttled lookup was indistinguishable from a genuine "no
match" (the cause of the ~10%-match / "Lama Zopa didn't match" bug). This transport:

  1. **Throttles** — enforces a minimum interval between requests (default ~3/s;
     override with CATALOGUE_HTTP_MIN_INTERVAL), so we stay under the limit.
  2. **Retries 429/503 with backoff** — honours a `Retry-After` header when given,
     else exponential backoff, up to `max_retries` attempts.
  3. **Raises, never lies** — on final failure (exhausted retries, timeout, network
     error) it raises `AuthorityUnavailable` instead of returning {}. Callers must
     distinguish a transport failure (retry later) from a real empty result, so a
     throttle is never cached or counted as "unmatched".

Transport stays a plain `(url) -> dict` callable, so tests still inject canned JSON
and never touch the network.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class AuthorityUnavailable(Exception):
    """A TRANSPORT-level failure (429/503/timeout/network) after retries — NOT a
    'no results' answer. Callers treat this as 'retry later' and the walk HALTS
    cleanly (resumable). Reserved for "the server is unreachable", never for a
    server that answered with junk."""


class MalformedResponse(Exception):
    """A successful HTTP response whose body could not be parsed as JSON — i.e.
    the endpoint changed/broke (e.g. VIAF's old AutoSuggest now returns an HTML
    app shell). This is a per-source problem, NOT a transport outage: callers
    swallow it into an empty result and move to the next source, so one dead
    source can't halt the whole pass. (Distinct from AuthorityUnavailable.)"""


def _min_interval_default() -> float:
    try:
        return float(os.environ.get("CATALOGUE_HTTP_MIN_INTERVAL", "0.34"))
    except ValueError:
        return 0.34


@dataclass
class ThrottledTransport:
    """A `(url) -> dict` transport that spaces requests, retries 429/503 with
    backoff, and raises `AuthorityUnavailable` on final failure. One instance per
    client so the spacing is per-host-ish (each authority client owns its own)."""
    user_agent: str = "library_cataloging/1.0 (Buddhist library catalogue; authority reconciliation)"
    min_interval: float = field(default_factory=_min_interval_default)
    timeout: float = 8.0
    max_retries: int = 4
    backoff_base: float = 1.0          # seconds; doubled each retry
    max_backoff: float = 30.0
    _last_call: float = field(default=0.0, init=False, repr=False)
    _sleep: object = field(default=time.sleep, repr=False)        # injectable for tests
    _now: object = field(default=time.monotonic, repr=False)      # injectable for tests

    def _throttle(self) -> None:
        wait = self.min_interval - (self._now() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = self._now()

    def __call__(self, url: str) -> dict:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    body = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 503) and attempt < self.max_retries:
                    self._sleep(self._retry_wait(e, attempt))
                    continue
                raise AuthorityUnavailable(f"HTTP {e.code} for {url}") from e
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                # GENUINE transport failure — retry, then halt the pass cleanly.
                last_err = e
                if attempt < self.max_retries:
                    self._sleep(min(self.backoff_base * (2 ** attempt), self.max_backoff))
                    continue
                raise AuthorityUnavailable(f"{type(e).__name__} for {url}") from e
            # Got a 200 (or other readable body). A parse failure means the endpoint
            # answered with non-JSON (changed/broken source) — that's a per-source
            # MalformedResponse the caller swallows, NOT a transport outage. Do not
            # retry, do not halt the walk.
            try:
                return json.loads(body)
            except ValueError as e:
                raise MalformedResponse(f"non-JSON body from {url}: {str(e)[:80]}") from e
        raise AuthorityUnavailable(f"exhausted retries for {url}: {last_err}")

    def _retry_wait(self, err: "urllib.error.HTTPError", attempt: int) -> float:
        """Honour Retry-After (seconds form) when present; else exponential."""
        ra = None
        try:
            ra = err.headers.get("Retry-After") if err.headers else None
        except Exception:
            ra = None
        if ra:
            try:
                return min(float(ra), self.max_backoff)
            except ValueError:
                pass
        return min(self.backoff_base * (2 ** attempt), self.max_backoff)


@dataclass
class ThrottledOpener:
    """A `(url, timeout) -> bytes` opener for the bibliographic sources
    (catalogue/edition_verify OpenLibrary/Google Books, catalogue/isbn), which parse
    JSON themselves. Spaces requests and RETRIES 429/5xx with backoff so a transient
    rate-limit on a bulk run is not mistaken for a 'no record' miss — the exact trap
    that turned a rate-limited ISBN into a wrong LLM title. On FINAL failure it
    re-raises (the source's own `except URLError` turns that into [] — a clean miss);
    unlike ThrottledTransport it does not parse JSON or raise AuthorityUnavailable."""
    user_agent: str = "library_cataloging/1.0 (Buddhist library catalogue; bibliographic lookup)"
    min_interval: float = field(default_factory=_min_interval_default)
    max_retries: int = 4
    backoff_base: float = 1.0
    max_backoff: float = 30.0
    _last_call: float = field(default=0.0, init=False, repr=False)
    _sleep: object = field(default=time.sleep, repr=False)        # injectable for tests
    _now: object = field(default=time.monotonic, repr=False)      # injectable for tests

    def _throttle(self) -> None:
        wait = self.min_interval - (self._now() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = self._now()

    def __call__(self, url: str, timeout: float) -> bytes:
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    self._sleep(min(self.backoff_base * (2 ** attempt), self.max_backoff))
                    continue
                raise                       # 404 etc. → genuine miss (source → [])
            except (urllib.error.URLError, OSError, TimeoutError):
                if attempt < self.max_retries:
                    self._sleep(min(self.backoff_base * (2 ** attempt), self.max_backoff))
                    continue
                raise
        raise RuntimeError("unreachable")   # loop always returns or raises
