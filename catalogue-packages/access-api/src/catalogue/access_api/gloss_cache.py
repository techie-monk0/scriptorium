"""Title-gloss memoization cache — the gateway-bound access surface (`acc.gloss_cache`).

`gloss_cache` memoizes an LLM's English gloss of a native-script title, keyed by (text_key, model),
so a re-run never re-calls the model for a title it already glossed. A pure cache (not an entity) —
a flat policy-gated repo: reads over RO, writes STAGE on the caller's connection. NULL-gloss rows
(a past failure) are ignored on read so a now-working credential retries. See entity_api_model.md §8.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

_RESOURCE = "gloss_cache"


class _Reads:
    def __init__(self, access):
        self._a = access

    def get(self, text_key: str, model: str):
        """The cached non-NULL gloss for (text_key, model), or None (miss / past failure → retry)."""
        self._a.authorize(Action(_RESOURCE, "get", AccessMode.READ))
        r = self._a.ro.execute(
            "SELECT gloss FROM gloss_cache WHERE text_key = ? AND model = ? AND gloss IS NOT NULL",
            (text_key, model)).fetchone()
        return r[0] if r else None


class _Writes:
    def __init__(self, access):
        self._a = access

    def put(self, text_key: str, model: str, gloss: str) -> None:
        """Memoize a SUCCESSFUL gloss (caller only stores truthy glosses). Staged; caller commits."""
        self._a.authorize(Action(_RESOURCE, "put", AccessMode.WRITE))
        self._a.rw.execute(
            "INSERT OR REPLACE INTO gloss_cache (text_key, model, gloss) VALUES (?, ?, ?)",
            (text_key, model, gloss))


class GlossCacheRepo:
    """`.reads.get` + `.writes.put` over a bound `Access` — a flat memoization cache repo."""

    def __init__(self, access):
        self.reads = _Reads(access)
        self.writes = _Writes(access)
