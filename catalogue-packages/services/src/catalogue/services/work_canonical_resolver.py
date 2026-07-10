"""Resolver — stub + live BDRC/84000 (§9, §4.3).

`ResolverStub` is the always-None v1 baseline (still useful for tests and
for environments without network access). `LiveResolver` composes a BDRC
HTTP client and an optional 84000 TEI snapshot (cloned from
github.com/84000/data-tei) per §4.3:

  - **BDRC** — `purl.bdrc.io` `lds-pdi` JSON `Res_byName` templates over
    HTTPS. Curated variant names already exist there; no SPARQL needed
    for the common lookup.
  - **84000** — no confirmed public REST. We consume their *published
    TEI* (cloned locally) and extract Toh → {english, tibetan, sanskrit}
    titles from each `<teiHeader>`. The clone is opt-in (the user runs
    `python -m catalogue.services.work_canonical_resolver fetch-84000`); when the snapshot is
    absent the rung silently no-ops, matching §4.3 ("consume published
    data, not live queries").

All rungs go through `resolver_cache` (`(query_hash, resolver_version)`)
so settled queries never re-fetch — bumping `version` invalidates cleanly.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .bdrc import BDRCClient, bdrc_lang_order, entity_type, entity_type
from .contributors import (
    ContributorResolver, ContributorResult, contributor_result_from_dict,
)


@dataclass
class ResolverResult:
    canonical_name: Optional[str]
    canonical_system: Optional[str]   # 'toh' | 'bdrc' | …
    canonical_number: Optional[str]
    aliases: list[str]
    source: str                        # 'stub' | 'bdrc' | '84000' | …


class ResolverStub:
    """Always returns None. The signature is the contract the live
    resolver (Step 9 / post-launch) must satisfy."""

    version: int = 1
    source: str = "stub"

    def resolve_work(
        self, conn, text: str, scheme: Optional[str] = None
    ) -> Optional[ResolverResult]:
        return self._lookup_or_stub(conn, "work", text, scheme)

    def resolve_person(
        self, conn, text: str, scheme: Optional[str] = None
    ) -> Optional[ResolverResult]:
        return self._lookup_or_stub(conn, "person", text, scheme)

    def resolve_contributors(
        self, conn, *, cache_key: str, edition_title: Optional[str],
        front_matter: str = "", meta: Optional[dict] = None, ladder=None,
    ) -> ContributorResult:
        """Offline baseline: title-string + embedded-metadata HINTS only, no LLM
        title-page verification (so it stays deterministic and free for tests /
        no-network runs). `ladder` is accepted for contract parity but ignored."""
        return _cached_contributors(
            conn, CONTRIBUTOR_VERSION, self.source, cache_key,
            lambda: ContributorResolver().resolve(
                edition_title=edition_title, front_matter=front_matter,
                meta=meta, ladder=None))

    def _lookup_or_stub(self, conn, kind: str, text: str,
                        scheme: Optional[str]) -> Optional[ResolverResult]:
        from catalogue.access_api import system_conn
        rc = system_conn(conn).resolver_cache
        qh = _query_hash(kind, text, scheme)
        row = rc.get(qh, self.version)
        if row:
            data = json.loads(row[0]) if row[0] else None
            if data is None:
                return None
            return ResolverResult(**data)

        # Stub: write a None record so we don't re-query on re-runs,
        # and bumping `version` invalidates cleanly (§12.3).
        rc.put(qh, self.version, self.source, None)
        return None


# Contributor cache version — independent of the resolver version so contributor
# logic/input changes (title-page reading order, front-matter depth, prompt) can
# invalidate WITHOUT re-querying the expensive BDRC/84000 work/person cache.
CONTRIBUTOR_VERSION = 3


def _cached_contributors(conn, version: int, source: str, cache_key: str,
                         compute) -> ContributorResult:
    """`resolver_cache` discipline for book contributors (§6, §9). Keyed
    `('contributors', cache_key)` under the resolver's version, so it never
    collides with work/person lookups and bumping the version re-resolves. The
    cached row is the full `ContributorResult` — a hit avoids re-calling the LLM
    title-page verifier. Reads pass through `StagingConn` to the live DB; the
    write is journaled and replayed by run_load (all-primitive params)."""
    from catalogue.access_api import system_conn
    rc = system_conn(conn).resolver_cache
    qh = _query_hash("contributors", cache_key or "", None)
    cached = rc.get(qh, version)
    if cached is not None:
        data = json.loads(cached[0]) if cached[0] else None
        return contributor_result_from_dict(data or {})
    result = compute()
    rc.put(qh, version, result.source or source, json.dumps(result.to_dict()))
    return result


def _query_hash(kind: str, text: str, scheme: Optional[str]) -> str:
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    h.update(b"\x00")
    h.update((scheme or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


# ── Generic per-source cache (shared by WorkAuthorityResolver / EditionVerifier) ─
def cached_rows(conn, *, namespace: str, source: str, query: str, version: int,
                compute, write: bool = True, cache_empty: bool = True) -> list[dict]:
    """Memoize a per-source lookup in `resolver_cache`.

    `compute()` returns a list of JSON-serializable dicts (already-parsed rows).
    The cache key hashes (namespace, source, query) so the person/work verifier
    chain, the work-authority resolver and the edition verifier never collide on
    the shared table. `conn=None` disables caching (pure-parse tests).

    `write=False` is CACHE-ONLY (offline): a hit is returned, a miss returns []
    WITHOUT calling `compute` (no network) and WITHOUT writing a marker, so a
    later live run still performs the real lookup.

    `cache_empty=False` does NOT persist an empty result — a transient miss (rate
    limit, network blip) would otherwise poison the entry as a permanent miss (the
    bug `bdrc.lookup_strict` warns about). With it off, only real hits are cached;
    an empty lookup is simply retried next run. Returns the list of dict rows."""
    if conn is None:
        return list(compute())
    from catalogue.access_api import system_conn
    rc = system_conn(conn).resolver_cache
    qh = hashlib.sha256(
        f"{namespace}\x00{source}\x00{query}".encode("utf-8")
    ).hexdigest()
    cached = rc.get(qh, version)
    if cached is not None:
        return json.loads(cached[0]) if cached[0] else []
    if not write:
        return []
    rows = list(compute())
    if rows or cache_empty:
        rc.put(qh, version, source, json.dumps(rows))
        conn.commit()
    return rows


# ── BDRC live client ──────────────────────────────────────────────────────
# Extracted to catalogue/bdrc.py so the BDRC integration is a single reviewable
# module and verification backends (catalogue/verify.py) can be swapped. The
# `bdrc` field below holds that client; this module only adds the cache layer.


# ── 84000 — local TEI snapshot ────────────────────────────────────────────
# Cloning `github.com/84000/data-tei` is the documented path (§4.3: "no
# confirmed public REST — consume published data"). The clone is opt-in:
# absent → silent no-op rung; present → we lazy-build a Toh→titles index.

DEFAULT_84000_SNAPSHOT = Path.home() / ".library_cataloging" / "84000-tei"

_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


@dataclass
class EightyFourThousandIndex:
    """On-disk Toh→titles index built once from a local TEI snapshot.

    Lookup by Toh number (`'73'`, `'182'`) or by case-folded English
    title fragment. The index file lives next to the snapshot so a fresh
    `git pull` followed by `rebuild()` is the only maintenance.
    """

    snapshot_dir: Path = field(default_factory=lambda: DEFAULT_84000_SNAPSHOT)
    _index: Optional[dict] = field(default=None, init=False, repr=False)

    @property
    def index_path(self) -> Path:
        return self.snapshot_dir / "_toh_index.json"

    def available(self) -> bool:
        return self.snapshot_dir.is_dir() and any(
            self.snapshot_dir.rglob("*.xml")
        )

    def _ensure_loaded(self) -> dict:
        if self._index is not None:
            return self._index
        if self.index_path.exists():
            try:
                cached = json.loads(self.index_path.read_text("utf-8"))
                # Auto-upgrade a stale cache that predates the native-title maps
                # (by_sanskrit/by_tibetan) — rebuild so they're populated.
                if "by_sanskrit" in cached:
                    self._index = cached
                    return self._index
            except (OSError, json.JSONDecodeError):
                pass    # corrupt cache → rebuild
        self._index = self.rebuild()
        return self._index

    def rebuild(self) -> dict:
        """Parse every TEI file's `<teiHeader>` for Toh# + titles. ~1k
        files at ~kB each; takes seconds, written to `_toh_index.json`."""
        from catalogue.db_store import fold_key
        out: dict = {"by_toh": {}, "by_english": {}, "by_sanskrit": {}, "by_tibetan": {}}
        if not self.available():
            return out
        for xml_path in self.snapshot_dir.rglob("*.xml"):
            try:
                root = ET.parse(xml_path).getroot()
            except ET.ParseError:
                continue
            titles = _extract_titles(root)
            toh = _extract_toh(root) or _toh_from_filename(xml_path.name)
            if not toh:
                continue
            entry = {**titles, "toh": toh, "file": str(
                xml_path.relative_to(self.snapshot_dir)
            )}
            out["by_toh"][toh] = entry
            if titles.get("english"):
                out["by_english"][titles["english"].lower()] = entry
            # Fold-keyed native-title maps (the language-independent work identity):
            # diacritic + digraph fold so OCR/spelling variants of the Skt/Tib title
            # collapse onto the canonical Toh entry. (sanskrit_works_plan.md §1.)
            if titles.get("sanskrit"):
                out["by_sanskrit"][_native_key(titles["sanskrit"])] = entry
            if titles.get("tibetan"):
                out["by_tibetan"][_native_key(titles["tibetan"])] = entry
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(out, ensure_ascii=False))
        return out

    def by_toh(self, toh: str) -> Optional[dict]:
        return self._ensure_loaded().get("by_toh", {}).get(str(toh).strip())

    def by_english(self, title: str) -> Optional[dict]:
        return self._ensure_loaded().get("by_english", {}).get((title or "").strip().lower())

    def by_sanskrit(self, title: str) -> Optional[dict]:
        """Toh entry whose Sanskrit title fold-matches `title` (IAST), ignoring
        word spaces/hyphens (`tārā-mūla-kalpa` == `Tārāmūlakalpa`)."""
        return self._ensure_loaded().get("by_sanskrit", {}).get(_native_key(title or ""))

    def by_tibetan(self, title: str) -> Optional[dict]:
        """Toh entry whose Tibetan title fold-matches `title` (Wylie), ignoring
        spaces and the shad — CIP-derived EWTS vs 84000's stored form."""
        return self._ensure_loaded().get("by_tibetan", {}).get(_native_key(title or ""))

    def search(self, query: str, *, limit: int = 20, lang: "str | None" = None) -> list:
        """Free-text authority search over the Toh index — substring on the English
        title, separator-insensitive on the Sanskrit (IAST) / Tibetan (Wylie) titles.
        Powers the works authority autocomplete (so the operator types a name in any of
        the three scripts instead of looking up the Toh number). Exact Toh# also matches.

        `lang` restricts which title is matched: 'english' | 'sanskrit' | 'tibetan'
        (None = any). Lets a `skt:`/`tib:` query prefix scope the search to that script.
        Returns `[{toh, english, sanskrit, tibetan}]`, best (English-title) hits first."""
        q = (query or "").strip()
        if not q:
            return []
        by_toh = self._ensure_loaded().get("by_toh", {})
        ql, qk = q.lower(), _native_key(q)
        scored = []
        for entry in by_toh.values():
            en = (entry.get("english") or "").lower()
            sa, bo = entry.get("sanskrit") or "", entry.get("tibetan") or ""
            hit_en = bool(ql) and ql in en
            hit_sa = bool(qk) and qk in _native_key(sa)
            hit_bo = bool(qk) and qk in _native_key(bo)
            rank = None
            if lang is None and str(entry.get("toh", "")).strip() == q:
                rank = (0, 0, en)
            elif lang in (None, "english") and hit_en:
                rank = (1, en.index(ql), en)            # English substring (prefix ranks higher)
            elif lang in (None, "sanskrit") and hit_sa:
                rank = (2, 0, en)
            elif lang in (None, "tibetan") and hit_bo:
                rank = (2, 0, en)
            if rank is not None:
                scored.append((rank, entry))
        scored.sort(key=lambda t: t[0])
        rows = [{"toh": e.get("toh"), "english": e.get("english"),
                 "sanskrit": e.get("sanskrit"), "tibetan": e.get("tibetan")}
                for _r, e in scored]
        return self.disambiguate(rows)[:limit]

    @staticmethod
    def disambiguate(matches: "list[dict]") -> "list[dict]":
        """Pick the canonical Toh AUTHORITY entry when one text is catalogued more than once —
        identical English + Sanskrit + Tibetan titles (a sūtra listed under several Toh numbers,
        or a text duplicated across the Kangyur/Tengyur). Among the duplicates `_toh_rank` keeps
        the best (rules analogous to BDRC's): an ATOMIC text over a range/collection container,
        then RICHER metadata, then the lower (more canonical) Toh number — the rest go in
        `variants`. So a title search surfaces the work, not every Toh listing of it.
        Order-preserving (first appearance of each text); idempotent. Entries with no title to
        compare on stay distinct (keyed by their own Toh#)."""
        buckets: dict = {}
        order: list = []
        for e in matches or []:
            sig = (_native_key(e.get("english") or ""),
                   _native_key(e.get("sanskrit") or ""),
                   _native_key(e.get("tibetan") or ""))
            if sig == ("", "", ""):
                sig = ("\0toh", str(e.get("toh")))      # nothing to compare on → keep distinct
            if sig not in buckets:
                buckets[sig] = []
                order.append(sig)
            buckets[sig].append(e)
        out = []
        for sig in order:
            entries = buckets[sig]
            best = min(entries, key=_toh_rank)          # stable: ties keep first appearance
            row = dict(best)
            variants = [str(x.get("toh")) for x in entries if x is not best]
            if variants:
                row["variants"] = variants
            out.append(row)
        return out


_LANG_PREFIX = {
    "en": "english", "eng": "english", "english": "english",
    "skt": "sanskrit", "skrt": "sanskrit", "sa": "sanskrit", "san": "sanskrit", "sanskrit": "sanskrit",
    "tib": "tibetan", "bo": "tibetan", "wylie": "tibetan", "tibetan": "tibetan",
}


@functools.lru_cache(maxsize=1)
def shared_84000_index() -> "EightyFourThousandIndex":
    """A process-wide, lazily-loaded 84000 index. Constructing a fresh
    `EightyFourThousandIndex()` re-reads + json-parses `_toh_index.json` (≈1k works) on
    EVERY call — and the works-search authority autocomplete builds one per request, so
    that parse was paid on every keystroke (≈170 ms each), which made the type-ahead feel
    sluggish. Reusing one instance loads the snapshot once, then serves it from memory.
    The instance is read-only after load, so it's safe to share across the dev server's
    request threads (a benign double-load is the worst a race can do)."""
    return EightyFourThousandIndex()


def parse_lang_prefix(query: str):
    """Split a leading `lang:` scope off a search query so the operator can aim the
    authority search at one script: `skt: tārā` → ('sanskrit', 'tārā'),
    `tib: dbu ma` → ('tibetan', 'dbu ma'), `en: heart` → ('english', 'heart').
    No (recognised) prefix → (None, query)."""
    import re
    m = re.match(r"^\s*([A-Za-z]+)\s*:\s*(.*)$", query or "")
    if m and m.group(1).lower() in _LANG_PREFIX:
        return _LANG_PREFIX[m.group(1).lower()], m.group(2).strip()
    return None, (query or "").strip()


def _toh_sort_key(toh) -> tuple:
    """Sort key making the canonical Toh# the smallest: '182' < '182a' < '182b' < '183'.
    Splits the leading number from a sub-letter; unparseable ids sort last."""
    import re
    m = re.match(r"\s*(\d+)\s*([a-z]*)", str(toh or "").lower())
    return (int(m.group(1)), m.group(2)) if m else (10 ** 9, str(toh or ""))


def _toh_is_atomic(toh) -> bool:
    """True for a single text's Toh# ('182', '182a'); False for a RANGE / container
    ('98-100', '1-1108', a comma list) — the Toh analogue of a BDRC SerialWork."""
    import re
    return bool(re.fullmatch(r"\d+[a-z]?", str(toh or "").strip()))


def _toh_rank(entry: dict) -> tuple:
    """Authority-preference key for a Toh entry (lower = better), used to pick the canonical
    record when one text is catalogued more than once: an ATOMIC text ('182') beats a
    range/collection container ('98-100'), then the lower (more canonical, Kangyur-before-
    Tengyur) Toh number wins. (Metadata richness isn't a tiebreaker here — duplicates are
    bucketed on all three folded titles, so within a bucket the populated fields are equal.)"""
    return (0 if _toh_is_atomic(entry.get("toh")) else 1,) + _toh_sort_key(entry.get("toh"))


def _native_key(text: str) -> str:
    """Separator-insensitive fold key for native (Sanskrit IAST / Tibetan Wylie)
    (diacritic + digraph fold) then strip every non-alphanumeric, so word spacing
    and hyphenation (which IAST renders arbitrarily) don't block a match —
    `tārā-mūla-kalpa` == `Tārā mūla kalpa` == `Tārāmūlakalpa`."""
    import re
    from catalogue.db_store import fold_key
    return re.sub(r"[^a-z0-9]+", "", fold_key(text or ""))


def _extract_titles(root: ET.Element) -> dict:
    """Pull mainTitle (English) and parallel-language titles from a TEI
    teiHeader. Returns `{english?, tibetan?, sanskrit?}`."""
    out: dict = {}
    for t in root.iterfind(".//tei:titleStmt/tei:title", _TEI_NS):
        kind = t.get("type") or ""
        lang = t.get("{http://www.w3.org/XML/1998/namespace}lang") or ""
        text = (t.text or "").strip()
        if not text:
            continue
        lang = lang.lower()
        if kind == "mainTitle" and lang in ("", "en"):
            out["english"] = text
        elif lang.startswith("bo"):       # 'bo' or 84000's 'Bo-Ltn' (romanized Tibetan)
            out["tibetan"] = text
        elif lang.startswith("sa"):       # 'sa' or 84000's 'Sa-Ltn' (romanized Sanskrit/IAST)
            out["sanskrit"] = text
    return out


def _extract_toh(root: ET.Element) -> Optional[str]:
    """Try the structured form first: `<idno type="toh">N</idno>` or
    `<idno type="TohNumber">N</idno>`. Falls through to None so the
    filename heuristic can take over."""
    for idno in root.iterfind(".//tei:idno", _TEI_NS):
        kind = (idno.get("type") or "").lower()
        if "toh" in kind:
            v = (idno.text or "").strip()
            if v:
                return v
    return None


def _toh_from_filename(name: str) -> Optional[str]:
    """`061-002_toh182-the_sutra_...xml` → `'182'`."""
    import re
    m = re.search(r"toh(\d+[a-z]?)", name.lower())
    return m.group(1) if m else None


# ── LiveResolver — composes BDRC + 84000 with the cache discipline ────────
@dataclass
class LiveResolver:
    """Drop-in replacement for `ResolverStub` (§9). Uses the same
    `resolver_cache` contract — caller code does not change.

    Order: BDRC by scheme-appropriate language tag → 84000 (if snapshot
    present and the entry looks like a Toh number or an English title we
    have indexed). First non-empty result wins; misses are cached too so
    we don't re-query (bump `version` to invalidate)."""

    version: int = 3                    # v3: BDRC results type-filtered (person vs
    #                                     work) — v2 cached wrong-typed top hits;
    #                                     bump invalidates them cleanly (§12.3).
    source: str = "live"
    bdrc: BDRCClient = field(default_factory=BDRCClient)
    eighty4000: EightyFourThousandIndex = field(
        default_factory=EightyFourThousandIndex
    )
    contributors: ContributorResolver = field(default_factory=ContributorResolver)

    def resolve_work(
        self, conn, text: str, scheme: Optional[str] = None, *, offline: bool = False
    ) -> Optional["ResolverResult"]:
        return self._resolve(conn, "work", text, scheme, offline=offline)

    def resolve_contributors(
        self, conn, *, cache_key: str, edition_title: Optional[str],
        front_matter: str = "", meta: Optional[dict] = None, ladder=None,
    ) -> ContributorResult:
        """Book author(s)/translator(s), VERIFIED against the title page locally
        via the LLM `ladder` (§4.9) — embedded metadata + filename are only hints.
        Cached so a re-run never re-calls the model (§6)."""
        return _cached_contributors(
            conn, CONTRIBUTOR_VERSION, self.source, cache_key,
            lambda: self.contributors.resolve(
                edition_title=edition_title, front_matter=front_matter,
                meta=meta, ladder=ladder))

    def resolve_person(
        self, conn, text: str, scheme: Optional[str] = None, *, offline: bool = False
    ) -> Optional["ResolverResult"]:
        # People only go through BDRC — 84000 indexes texts, not authors.
        return self._resolve(conn, "person", text, scheme,
                             skip_84000=True, offline=offline)

    def _resolve(self, conn, kind: str, text: str,
                 scheme: Optional[str],
                 *, skip_84000: bool = False,
                 offline: bool = False) -> Optional["ResolverResult"]:
        from catalogue.access_api import system_conn
        rc = system_conn(conn).resolver_cache
        qh = _query_hash(kind, text, scheme)
        cached = rc.get(qh, self.version)
        if cached is not None:
            data = json.loads(cached[0]) if cached[0] else None
            return ResolverResult(**data) if data else None

        # offline: cache-only — never touch the network, never write a marker.
        if offline:
            return None

        result = self._query_live(kind, text, scheme,
                                  skip_84000=skip_84000)
        rc.put(qh, self.version, result.source if result else self.source,
               json.dumps(result.__dict__) if result else None)
        return result

    def _query_live(self, kind: str, text: str, scheme: Optional[str],
                    *, skip_84000: bool) -> Optional["ResolverResult"]:
        text = text.strip()
        if not text:
            return None

        # 84000 first only when the input is *clearly* a Toh number. For
        # an English-title lookup the BDRC name search is broader, so we
        # try BDRC first and use 84000 as a fallback enrichment.
        if not skip_84000 and _looks_like_toh(text):
            hit = self.eighty4000.by_toh(_strip_toh_prefix(text))
            if hit:
                return _from_84000(hit)

        # BDRC: try the scheme-implied language first, then fall back
        # across the three families we care about.
        # BLMP matches a name across ALL resource types and returns a ranked
        # list, so the top hit is often the WRONG type — e.g. "Nagarjuna" ranks a
        # work (bdr:WA…) above any person, and the old `rows[0]` then got rejected
        # by the verifier's type guard. Filter to the type the caller asked for
        # BEFORE taking the top hit.
        want = "person" if kind == "person" else "work"
        order = bdrc_lang_order(scheme)
        for lang in order:
            rows = self.bdrc.lookup(text, lang=lang)
            typed = [(bid, label) for (bid, label) in rows
                     if entity_type(bid) == want]
            if typed:
                bid, label = typed[0]
                aliases = list(dict.fromkeys(
                    [label] + [lit for _, lit in typed[1:5]]
                ))
                return ResolverResult(
                    canonical_name=label,
                    canonical_system="bdrc",
                    canonical_number=bid,
                    aliases=aliases,
                    source="bdrc",
                )

        # 84000 English-title fallback (skipped for persons).
        if not skip_84000 and self.eighty4000.available():
            hit = self.eighty4000.by_english(text)
            if hit:
                return _from_84000(hit)
        return None


def _looks_like_toh(text: str) -> bool:
    """A bare 'Toh 182' or '182' that's almost certainly a Toh ref."""
    t = text.strip().lower()
    if t.startswith("toh"):
        return True
    return t.replace(".", "").isdigit() and 1 <= len(t) <= 5


def _strip_toh_prefix(text: str) -> str:
    t = text.strip().lower()
    if t.startswith("toh"):
        t = t[3:]
    return t.strip(" .:_-")


def _from_84000(hit: dict) -> "ResolverResult":
    aliases = [v for v in (hit.get("tibetan"), hit.get("sanskrit"))
               if v]
    return ResolverResult(
        canonical_name=hit.get("english") or "",
        canonical_system="toh",
        canonical_number=hit.get("toh"),
        aliases=aliases,
        source="84000",
    )


# ── CLI: fetch / refresh the 84000 TEI snapshot ───────────────────────────
def _fetch_84000(snapshot_dir: Path) -> int:
    """Clone or update github.com/84000/data-tei into `snapshot_dir`.
    Uses `git` — keeping the repo as a git checkout lets the user run
    `git pull` to refresh without a full re-download."""
    import subprocess
    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
    if (snapshot_dir / ".git").exists():
        cmd = ["git", "-C", str(snapshot_dir), "pull", "--ff-only"]
    else:
        cmd = ["git", "clone", "--depth", "1",
               "https://github.com/84000/data-tei.git",
               str(snapshot_dir)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, end="")
    return r.returncode


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Resolver utilities — manage the 84000 TEI snapshot."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-84000",
                       help="Clone/update github.com/84000/data-tei.")
    f.add_argument("--dir", type=Path, default=DEFAULT_84000_SNAPSHOT)
    f.add_argument("--no-rebuild", action="store_true",
                   help="Skip the post-fetch index rebuild.")
    args = ap.parse_args()

    if args.cmd == "fetch-84000":
        rc = _fetch_84000(args.dir)
        if rc != 0:
            raise SystemExit(rc)
        if not args.no_rebuild:
            idx = EightyFourThousandIndex(snapshot_dir=args.dir)
            n = len(idx.rebuild().get("by_toh", {}))
            print(f"84000 snapshot at {args.dir} — {n} Toh entries indexed.")
