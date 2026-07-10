"""VIAF client — the best aggregate authority for MODERN/Western authors (it fuses
LoC, GND, BnF, NDL, … into one id per person). Used by verify.ViafPersonVerifier
to give modern authors a reconciliation path BDRC can't.

Uses the keyless AutoSuggest endpoint (`viaf.org/viaf/AutoSuggest?query=`), which
returns a ranked list with a `nametype` we filter to `personal`. Transport is
`(url) -> dict` for test injection; the default throttles + retries 429s and RAISES
`AuthorityUnavailable` on transport failure (not a miss). Genuine empty → [].

*** ENDPOINT CURRENTLY BROKEN (2026-06): VIAF migrated to OCLC's Next.js platform and
`/viaf/AutoSuggest?query=` now returns an HTML app shell, not JSON. So `suggest()`
yields [] for every query (the body fails to parse → MalformedResponse → swallowed),
and the verify chain simply falls through to BDRC. This is HARMLESS but means VIAF
contributes nothing until re-pointed at OCLC's new search API (a separate task). The
fix that mattered: a non-JSON 200 is a per-source MalformedResponse, NOT a transport
AuthorityUnavailable, so this dead endpoint no longer halts the whole verify pass. ***
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from .http_util import AuthorityUnavailable, ThrottledTransport

ViafTransport = Callable[[str], dict]


@dataclass
class VIAFClient:
    base_url: str = "https://viaf.org"
    transport: ViafTransport = field(default_factory=ThrottledTransport)

    def _url(self, text: str) -> str:
        return (f"{self.base_url.rstrip('/')}/viaf/AutoSuggest?"
                + urllib.parse.urlencode({"query": text}))

    def suggest(self, text: str, *, nametype: str = "personal"
                ) -> list[tuple[str, str]]:
        """Return `[(viaf_id, term), …]` filtered to `nametype` (default
        personal). [] on any failure. `viaf_id` is the bare numeric id."""
        try:
            data = self.transport(self._url(text))
        except AuthorityUnavailable:
            raise
        except Exception:
            return []
        out = []
        for r in (data or {}).get("result") or []:
            if not isinstance(r, dict):
                continue
            if nametype and r.get("nametype") != nametype:
                continue
            vid, term = r.get("viafid"), r.get("term")
            if vid and term:
                out.append((str(vid), term))
        return out
