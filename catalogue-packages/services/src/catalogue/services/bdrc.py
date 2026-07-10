"""BDRC (Buddhist Digital Resource Center) name-lookup client.

Extracted into its own module so it's a single, reviewable place for everything
that talks to `purl.bdrc.io`, and so verification backends (see catalogue/verify.py)
can be swapped without touching the resolver. `LiveResolver` composes this client
behind the `resolver_cache`; verify.py's `BdrcVerifier` reaches it via that cache.

Known sharp edges (see the verify.py guards that defend against them):
  - `BLMP` matches name literals across ALL resource types, so a person query can
    return a Work id (bdr:WA…) and vice-versa — callers MUST type-check the id.
  - it returns a ranked fuzzy list; the top hit is not necessarily a real match,
    so callers should verify the label against the query.
  - the transport swallows network errors into an empty result, which a caller
    can mistake for (and cache as) a definitive miss — see `LookupError`-free
    `lookup` vs `lookup_strict`.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

# Transport is `(url) -> dict` so tests inject canned JSON and a future swap
# (async, cached HTTP, alternate base URL) doesn't touch caller code.
BDRCTransport = Callable[[str], dict]


def _default_bdrc_transport(url: str) -> dict:
    """One-shot GET against BDRC. UA identifies this tool (§13) and a short
    timeout keeps a flaky network from pinning the caller."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "library_cataloging/1.0 (BDRC name lookup)"},
    )
    with urllib.request.urlopen(req, timeout=8.0) as resp:        # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


# BDRC resource-id prefixes (the letter after `bdr:`). Persons are `P`; works are
# `W`/`WA`/`MW`/`UT`; places `G`; topics `T`; lineages `R`. We expose enough to
# let callers reject a wrong-type hit.
def entity_type(bdrc_id: Optional[str]) -> Optional[str]:
    """'person' | 'work' | 'other' | None for a `bdr:…` id (or bare local id)."""
    if not bdrc_id:
        return None
    local = bdrc_id.split(":", 1)[-1]
    if local.startswith("P") and not local.startswith("PR"):
        return "person"
    if local.startswith(("W", "MW", "UT")):
        return "work"
    return "other"


def is_person_id(bdrc_id: Optional[str]) -> bool:
    return entity_type(bdrc_id) == "person"


def is_work_id(bdrc_id: Optional[str]) -> bool:
    return entity_type(bdrc_id) == "work"


# ── ElasticSearch person search (library.bdrc.io backend) ─────────────────────
# The public `bdrc_prod` index behind library.bdrc.io, reached with the read-only
# Basic-auth key shipped in the site's JS. A precision alternative to the BLMP
# template (which matches literals across ALL resource types and never surfaced the
# person, even from exact Wylie). NOTE: BDRC stores person names in Wylie + English;
# the site's phonetic→Wylie query conversion is NOT replicated here, so a phonetic
# query only matches where BDRC also holds an English/phonetic label.
_ES_URL = "https://autocomplete.bdrc.io/msearch"
_ES_INDEX = "bdrc_prod"
_ES_AUTH = "Basic " + base64.b64encode(b"publicquery:0Vsg1QvjLkTCzvtl").decode()
_ES_FIELDS = ["prefLabel_en^3", "prefLabel_bo_x_ewts^3", "prefLabel_iast^3",
              "altLabel_en", "altLabel_bo_x_ewts", "altLabel_iast"]
_ES_LABEL_KEYS = ("prefLabel_en", "prefLabel_bo_x_ewts", "prefLabel_iast",
                  "altLabel_en", "altLabel_bo_x_ewts", "altLabel_iast")

ESTransport = Callable[[str], dict]


def _default_es_transport(body: str) -> dict:
    """POST an ElasticSearch `_msearch` NDJSON body to the public BDRC search."""
    req = urllib.request.Request(
        _ES_URL, data=body.encode("utf-8"),
        headers={"Authorization": _ES_AUTH, "Content-Type": "application/x-ndjson",
                 "User-Agent": "library_cataloging/1.0 (BDRC search)"})
    with urllib.request.urlopen(req, timeout=8.0) as resp:        # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class BdrcElasticSearch:
    """library.bdrc.io's fuzzy person search over the `bdrc_prod` index. Returns
    `[{"id": "bdr:P…", "score": float, "labels": [str, …]}, …]` (top hits with every
    pref/alt label string) for a name-guarded match by the caller. The `type:Person`
    filter means every id is a person, so there's no wrong-type hit. Transport is
    injectable so tests pass canned ES JSON."""
    transport: ESTransport = _default_es_transport
    size: int = 4

    def person_search(self, name: str) -> "list[dict]":
        query = {"query": {"bool": {
                    "must": [{"bool": {"should": [
                        {"multi_match": {"query": name, "fields": _ES_FIELDS,
                                         "fuzziness": "AUTO:4,8"}},
                        {"multi_match": {"query": name, "fields": _ES_FIELDS,
                                         "type": "phrase", "boost": 3}}]}}],
                    "filter": [{"term": {"type": "Person"}}]}},
                 "size": self.size}
        body = json.dumps({"index": _ES_INDEX}) + "\n" + json.dumps(query) + "\n"
        data = self.transport(body)
        hits = ((data.get("responses") or [{}])[0]
                .get("hits", {}).get("hits", []))
        out = []
        for h in hits:
            src = h.get("_source", {})
            labels: list = []
            for k in _ES_LABEL_KEYS:
                v = src.get(k)
                if isinstance(v, list):
                    labels.extend(x for x in v if x)
                elif v:
                    labels.append(v)
            local = h.get("_id", "")
            out.append({"id": f"bdr:{local}" if local else "",
                        "score": h.get("_score", 0.0), "labels": labels})
        return out


# Work-search fields: a work's titles live in the Wylie/IAST/English label fields; the
# author lives in `authorName_bo_x_ewts`. Mirrors `BdrcElasticSearch` but is NOT
# type-filtered to Person — and deliberately NOT filtered to a work `type` either: an
# earlier `type:Work` filter returned 0 (BDRC types works as `Instance`/`AbstractWork`),
# so we let the long Wylie title phrase itself exclude persons/places and have the caller
# (wylie_resolve) confirm by author. Returns hits with every title label + author label.
_ES_WORK_FIELDS = ["prefLabel_bo_x_ewts^3", "altLabel_bo_x_ewts^2",
                   "prefLabel_iast^2", "prefLabel_en"]
_ES_WORK_TITLE_KEYS = ("prefLabel_bo_x_ewts", "altLabel_bo_x_ewts",
                       "prefLabel_iast", "prefLabel_en")


def _collect(src: dict, keys) -> "list[str]":
    out: list = []
    for k in keys:
        v = src.get(k)
        if isinstance(v, list):
            out.extend(x for x in v if x)
        elif v:
            out.append(v)
    return out


@dataclass
class BdrcWorkSearch:
    """library.bdrc.io fuzzy WORK search over `bdrc_prod`. `work_search(title, author)`
    returns `[{"id": "bdr:…", "score": float, "titles": [ewts/iast/en, …],
    "authors": [ewts, …]}, …]`. A phrase match on the Wylie title is OR-boosted with a
    fuzzy match and, when given, an author match — so the right work ranks above the many
    title-formula homonyms. Transport injectable for hermetic tests."""
    transport: ESTransport = _default_es_transport
    size: int = 8

    def work_search(self, title: str, author: "str | None" = None) -> "list[dict]":
        should = [
            {"match_phrase": {"prefLabel_bo_x_ewts": {"query": title, "boost": 5}}},
            {"match_phrase": {"altLabel_bo_x_ewts": {"query": title, "boost": 4}}},
            {"multi_match": {"query": title, "fields": _ES_WORK_FIELDS,
                             "fuzziness": "AUTO:4,8"}},
        ]
        if author:
            should.append({"match": {"authorName_bo_x_ewts":
                                     {"query": author, "boost": 2}}})
        query = {"query": {"bool": {"should": should, "minimum_should_match": 1}},
                 "size": self.size}
        body = json.dumps({"index": _ES_INDEX}) + "\n" + json.dumps(query) + "\n"
        data = self.transport(body)
        hits = ((data.get("responses") or [{}])[0]
                .get("hits", {}).get("hits", []))
        out = []
        for h in hits:
            src = h.get("_source", {})
            local = h.get("_id", "")
            out.append({"id": f"bdr:{local}" if local else "",
                        "score": h.get("_score", 0.0),
                        "titles": _collect(src, _ES_WORK_TITLE_KEYS),
                        "authors": _collect(src, ("authorName_bo_x_ewts", "author")),
                        # FRBR plumbing for disambiguate(): the doc type ('Instance' /
                        # 'PartTypeText' / …) and its associated resources, which name the
                        # abstract WORK (WA…) a manifestation/part belongs to.
                        "type": src.get("type") or [],
                        "associated_res": src.get("associated_res") or []})
        return out


def parse_bdrc_ttl_titles(body: str) -> dict:
    """Pull titles out of a BDRC resource Turtle doc, by language tag:
    `{'en':[…], 'bo-x-ewts':[…], 'sa-x-iast':[…], 'all':[…]}` (prefLabel first)."""
    import re
    out = {"en": [], "bo-x-ewts": [], "sa-x-iast": [], "all": []}
    for pred in ("skos:prefLabel", "rdfs:label", "skos:altLabel"):
        m = re.search(re.escape(pred) + r"(.*?)(?:\s;|\s\.)\s*\n", body or "", re.DOTALL)
        if not m:
            continue
        for text, lang in re.findall(r'"([^"]+)"@(\S+)', m.group(1)):
            out["all"].append(text)
            if lang in out:
                out[lang].append(text)
    return out


def _fetch_bdrc_ttl(local: str, timeout: float) -> "str | None":
    req = urllib.request.Request(
        f"https://purl.bdrc.io/resource/{local}.ttl",
        headers={"User-Agent": "library_cataloging/1.0 (BDRC resource)",
                 "Accept": "text/turtle"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:       # noqa: S310
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def _work_from_resource(local: str, timeout: float, _depth: int = 0) -> "dict | None":
    """Fallback when a BDRC id isn't in the autocomplete ES index: fetch its Turtle from
    purl.bdrc.io and read the titles. A SCAN INSTANCE (bdo:Instance, e.g. W8LS77524) has
    no titles of its own — they live on the abstract work it is `bdo:instanceOf` — so we
    follow that link (one hop) to the work record and take the title there."""
    import re
    body = _fetch_bdrc_ttl(local, timeout)
    if not body:
        return None
    t = parse_bdrc_ttl_titles(body)
    if t["all"]:
        title = (t["en"] or t["bo-x-ewts"] or t["all"])[0]
        return {"system": "bdrc", "number": f"bdr:{local}", "title": title, "titles": t["all"],
                "english": (t["en"] or [None])[0], "tibetan": (t["bo-x-ewts"] or [None])[0],
                "sanskrit": (t["sa-x-iast"] or [None])[0]}
    if _depth < 2:                                  # an instance → follow to its work
        for pred in ("bdo:instanceOf", "bdo:instanceReproductionOf"):
            m = re.search(re.escape(pred) + r"\s+bdr:(\w+)", body)
            if m:
                linked = _work_from_resource(m.group(1), timeout, _depth + 1)
                if linked:
                    return linked
    return None


def work_by_id(work_id: str, *, timeout: float = 3.0,
               transport: "ESTransport | None" = None) -> "dict | None":
    """Fetch ONE BDRC work by its id ('bdr:WA…' or bare 'WA…') → its titles. Returns
    `{'system':'bdrc', 'number':'bdr:…', 'title':str, 'titles':[…]}` or None. Best-effort.
    Tries the fast autocomplete index (manifestation MW… ids) first, then falls back to
    the purl.bdrc.io resource record (works for abstract WA…/W… ids too)."""
    local = (work_id or "").split(":", 1)[-1].strip()
    if not local:
        return None
    query = {"query": {"ids": {"values": [local]}}, "size": 1}
    body = json.dumps({"index": _ES_INDEX}) + "\n" + json.dumps(query) + "\n"
    if transport is None:
        def transport(b):
            req = urllib.request.Request(
                _ES_URL, data=b.encode("utf-8"),
                headers={"Authorization": _ES_AUTH, "Content-Type": "application/x-ndjson",
                         "User-Agent": "library_cataloging/1.0 (BDRC work by id)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
    try:
        data = transport(body)
        hits = ((data.get("responses") or [{}])[0].get("hits", {}).get("hits", []))
        if hits:
            h = hits[0]
            src = h.get("_source", {})
            titles = _collect(src, _ES_WORK_TITLE_KEYS)
            local_id = h.get("_id", local)
            # BDRC titles are Wylie (bo-x-ewts) → Tibetan, not English.
            bo = _collect(src, ("prefLabel_bo_x_ewts", "altLabel_bo_x_ewts"))
            en = _collect(src, ("prefLabel_en", "altLabel_en"))
            iast = _collect(src, ("prefLabel_iast", "altLabel_iast"))
            return {"system": "bdrc", "number": f"bdr:{local_id}",
                    "title": titles[0] if titles else f"bdr:{local_id}", "titles": titles,
                    "english": en[0] if en else None,
                    "tibetan": bo[0] if bo else (titles[0] if titles else None),
                    "sanskrit": iast[0] if iast else None}
    except Exception:
        pass
    return _work_from_resource(local, timeout)   # fallback for WA…/W… abstract works


def describe_unresolved(work_id: str, *, timeout: float = 3.0) -> str:
    """Explain why a pasted BDRC id didn't resolve to a work — its actual record type
    (Person / Place / scan Instance / Topic …) read from purl.bdrc.io, so the operator
    knows what they pasted instead of a generic 'could not resolve'."""
    import re
    local = (work_id or "").split(":", 1)[-1].strip()
    if not local:
        return "empty id"
    body = _fetch_bdrc_ttl(local, timeout)
    if not body:
        return (f"no BDRC record found for bdr:{local} — check the id "
                f"(or BDRC is unreachable right now).")
    m = re.search(r"bdr:" + re.escape(local) + r"\s+a\s+([^;.]+)", body)
    types = set(re.findall(r"bdo:(\w+)", m.group(1))) if m else set()
    if "Person" in types:
        return f"bdr:{local} is a BDRC PERSON record, not a work — use it in the person picker."
    if "Place" in types:
        return f"bdr:{local} is a BDRC PLACE, not a work."
    if types & {"Instance", "ImageInstance", "DigitalInstance"}:
        return (f"bdr:{local} is a BDRC scan/image INSTANCE with no linked work title — "
                f"try the abstract-work id (WA…) for this text.")
    for t in ("Topic", "Lineage", "Role", "Corporation", "Collection"):
        if t in types:
            return f"bdr:{local} is a BDRC {t}, not a work."
    return f"bdr:{local} has no work title on record at BDRC."


def _abstract_work_id(match: dict) -> "str | None":
    """The BDRC ABSTRACT WORK (WA…) a search hit belongs to — the WA among its associated
    resources. None if the hit isn't linked to one (some scans only name their print)."""
    for r in (match.get("associated_res") or []):
        if isinstance(r, str) and r.startswith("WA"):
            return r
    return None


def _author_ids(match: dict) -> "list[str]":
    """Author/agent person ids (P…, not PR… prints) among a hit's associated resources —
    a CHEAP 'this work names an author' signal straight from the search doc, no fetch."""
    return [r for r in (match.get("associated_res") or [])
            if isinstance(r, str) and r.startswith("P") and not r.startswith("PR")]


_WORK_META_CACHE: dict = {}


def _parse_work_meta(local: str, body: "str | None") -> dict:
    """Classify a BDRC work record from its Turtle: `{'kind': 'work'|'serial'|'other'|None,
    'has_author': bool}`. kind='serial' (bdo:SerialWork) marks a SERIES/collection container
    — a series title, NOT a text — so authority selection demotes it."""
    import re
    if not body:
        return {"kind": None, "has_author": False}
    m = re.search(r"bdr:" + re.escape(local) + r"\s+a\s+([^.;]+)", body)
    types = set(re.findall(r"bdo:(\w+)", m.group(1))) if m else set()
    if "SerialWork" in types:
        kind = "serial"
    elif "Work" in types:
        kind = "work"
    else:
        kind = "other" if types else None
    has_author = bool(re.search(r"bdo:creator\b", body)
                      or re.search(r"bdo:agent\s+bdr:P", body))
    return {"kind": kind, "has_author": has_author}


def work_meta(number: str, *, timeout: float = 3.0, fetch=None) -> dict:
    """(memoised) Classify a BDRC work id for authority selection — its record TYPE and
    whether it names an author — read from the resource TTL. `{'kind', 'has_author'}`.
    Best-effort: a network/parse failure → `{'kind': None, 'has_author': False}` (treated as
    a plain authorless work, never demoted as a serial). `fetch(local)->ttl|None` injectable
    for hermetic tests."""
    local = (number or "").split(":", 1)[-1].strip()
    if not local:
        return {"kind": None, "has_author": False}
    if local in _WORK_META_CACHE:
        return _WORK_META_CACHE[local]
    body = (fetch or (lambda l: _fetch_bdrc_ttl(l, timeout)))(local)
    meta = _parse_work_meta(local, body)
    _WORK_META_CACHE[local] = meta
    return meta


def _no_meta(_number: str) -> dict:
    return {"kind": None, "has_author": False}


def _cached_meta(number: str) -> dict:
    """Cache-only work classification — NO network. Used on the hot autocomplete path so a
    live search never blocks on per-work TTL fetches; the cache is warmed in the background."""
    local = (number or "").split(":", 1)[-1]
    return _WORK_META_CACHE.get(local, {"kind": None, "has_author": False})


def _title_key(title: "str | None") -> str:
    import re
    from catalogue.db_store import fold_key
    return re.sub(r"[^a-z0-9]+", "", fold_key(title or "")) if title else ""


def select_authority(works: "list[dict]", *, meta=None, limit=None) -> "list[dict]":
    """Pick the best BDRC AUTHORITY record per text from `disambiguate()` output, applying:
      • GRANULARITY — an abstract Work (WA…) beats a manifestation-only group (MW…), which
        is kept last and flagged `provisional`;
      • REJECT CONTAINERS — a `bdo:SerialWork` (a series/collection whose title is the series
        name, not the text) is demoted to the bottom and flagged `serial`;
      • PREFER AUTHOR-BEARING — among same-title records, a Work that names an author floats
        above one that doesn't;
      • FOLD DUPLICATES — records sharing a title collapse onto the best one, the rest listed
        in its `same_as` (so a later hit on any resolves to the chosen work, not a new one).
    `meta(number) -> {'kind','has_author'}` is injectable (default: the live cached BDRC TTL
    classifier `work_meta`). Stable within a tier. Returns the kept works."""
    meta = meta or work_meta
    ranked = []
    for i, w in enumerate(works or []):
        local = (w.get("number") or "").split(":", 1)[-1]
        is_wa = local.startswith("WA")
        info = meta(w["number"]) if is_wa else {"kind": "manifestation", "has_author": False}
        kind = info.get("kind")
        has_author = (bool(info.get("has_author")) or bool(w.get("author_ids"))
                      or bool(w.get("authors")))
        if kind == "serial":
            tier = 4                       # series/collection container — not the text
        elif is_wa and kind in ("work", None) and has_author:
            tier = 0                       # abstract Work that names its author — best
        elif is_wa and kind in ("work", None):
            tier = 1                       # abstract Work, no author
        elif is_wa:
            tier = 2                       # WA of some other kind
        else:
            tier = 3                       # manifestation only (no abstract work)
        ranked.append((tier, i, w, kind))
    ranked.sort(key=lambda t: (t[0], t[1]))
    out, by_title = [], {}
    for tier, _i, w, kind in ranked:
        key = _title_key(w.get("title"))
        primary = by_title.get(key) if key else None
        if primary is not None:                            # a better record for this text won
            primary.setdefault("same_as", []).append(w["number"])
            continue
        row = {k: v for k, v in w.items() if k != "author_ids"}
        row.setdefault("same_as", [])
        if tier == 3:
            row["provisional"] = True                      # MW-only: no abstract work at BDRC
        if kind == "serial":
            row["serial"] = True
        out.append(row)
        if key:
            by_title[key] = row
    return out[:limit] if limit else out


def _instance_root(local: str) -> str:
    """The parent manifestation of an outline/part node: a BDRC id like
    'MW1NLM1472_O1NLM1472_005' or 'MW30443_W6e04f' → its manifestation 'MW1NLM1472' /
    'MW30443' (everything before the first underscore)."""
    return (local or "").split("_", 1)[0]


def disambiguate(matches: "list[dict]") -> "list[dict]":
    """Collapse BDRC manifestation / part hits to the one ABSTRACT WORK they belong to.

    BDRC's FRBR model returns a separate row for every scan, reprint, and outline node of a
    text (e.g. searching one title surfaces MW1ALS19080E, MW1ALS19027, MW1NLM1136, … — all
    the same work), but the catalogue wants the single Work. Group key, best→worst:
      1. the WA… in the hit's `associated_res` (the FRBR abstract work);
      2. else the parent manifestation id (an outline node `MW…_…` → its `MW…`);
      3. else the id itself.
    The first (highest-scoring) hit in each group supplies the title/authors; every collapsed
    source id is recorded in `members`. Incoming order (ES score) is preserved. Inputs may
    carry `associated_res`/`type`/`score` (from `work_search`); outputs are clean picker rows
    `{system, number:'bdr:WA…', title, titles, authors, members}`. Idempotent."""
    groups: dict = {}
    order: list = []
    for m in matches or []:
        local = (m.get("number") or "").split(":", 1)[-1]
        if not local:
            continue
        key = _abstract_work_id(m) or _instance_root(local)
        g = groups.get(key)
        if g is None:
            titles = m.get("titles") or ([m["title"]] if m.get("title") else [])
            groups[key] = {"system": "bdrc", "number": f"bdr:{key}",
                           "title": titles[0] if titles else f"bdr:{key}",
                           "titles": titles, "authors": list(m.get("authors") or []),
                           "author_ids": _author_ids(m), "members": [local]}
            order.append(key)
        else:
            if local not in g["members"]:
                g["members"].append(local)
            if not g["authors"] and m.get("authors"):
                g["authors"] = list(m["authors"])
            for p in _author_ids(m):
                if p not in g["author_ids"]:
                    g["author_ids"].append(p)
    return [groups[k] for k in order]


def live_work_matches(title: str, *, limit: int = 8, timeout: float = 3.0,
                      transport: "ESTransport | None" = None, meta=None) -> "list[dict]":
    """Best-effort LIVE BDRC work search shaped for the works authority picker:
    `[{'system':'bdrc', 'number':'bdr:WA…', 'title':str, 'titles':[…], 'authors':[…],
    'members':[…], 'same_as':[…]}]`. Hits are `disambiguate`d (collapsed to one row per
    abstract work) then ranked by `select_authority` (best authority record per text:
    author-bearing Work first, series containers dropped, duplicates folded into `same_as`).
    Short timeout, swallows every error (returns []), so the box degrades to the offline
    sources when BDRC is slow. `meta` (the work classifier) defaults to the live cached TTL
    fetcher; an injected `transport` (tests) defaults it to the no-network stub."""
    if not (title or "").strip():
        return []
    live = transport is None
    if transport is None:
        def transport(body):                                     # short-timeout transport
            req = urllib.request.Request(
                _ES_URL, data=body.encode("utf-8"),
                headers={"Authorization": _ES_AUTH, "Content-Type": "application/x-ndjson",
                         "User-Agent": "library_cataloging/1.0 (BDRC works)"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
    try:
        # Fetch extra raw hits since several collapse into one work, so we still surface
        # roughly `limit` distinct works after disambiguation.
        hits = BdrcWorkSearch(transport=transport,
                              size=min(max(limit * 3, limit), 24)).work_search(title)
    except Exception:
        return []
    out = []
    for h in hits:
        if not h.get("id"):
            continue
        titles = h.get("titles") or []
        out.append({"system": "bdrc", "number": h["id"],
                    "title": titles[0] if titles else h["id"],
                    "titles": titles, "authors": h.get("authors") or [],
                    "associated_res": h.get("associated_res") or [],
                    "type": h.get("type") or []})
    works = disambiguate(out)
    # Ranking metadata. An explicitly-injected `meta` wins (tests). On the LIVE path rank with
    # CACHED metadata ONLY (never block the autocomplete on a per-candidate TTL fetch — that
    # was the 'lam rim chen mo took 120s' bug) and warm the cache in a daemon thread so the
    # NEXT search for a common title is fully ranked. Off-live (injected transport) → no-net stub.
    if meta is None:
        if live:
            cold = [w["number"] for w in works
                    if w["number"].split(":", 1)[-1].startswith("WA")
                    and w["number"].split(":", 1)[-1] not in _WORK_META_CACHE]
            if cold:
                import threading
                def _warm(ids=cold[:16]):
                    for n in ids:
                        try:
                            work_meta(n, timeout=timeout)
                        except Exception:
                            pass
                threading.Thread(target=_warm, daemon=True).start()
            meta = _cached_meta
        else:
            meta = _no_meta
    return select_authority(works, meta=meta)[:limit]


@dataclass
class BDRCClient:
    """`purl.bdrc.io` `lds-pdi` JSON name lookup via the `BLMP` template.

    `BLMP` ("a table containing the Id and matching literal for the given query
    and language tag") is the live template ID. Response is SPARQL-JSON; we read
    only `.value` so a shape tweak doesn't silently empty hits."""

    base_url: str = "https://purl.bdrc.io"
    template: str = "BLMP"
    limit: int = 20
    transport: BDRCTransport = field(default=_default_bdrc_transport)

    def _url(self, text: str, lang: str) -> str:
        params = {"L_NAME": text, "LG_NAME": lang,
                  "I_LIM": str(self.limit), "format": "json"}
        return (f"{self.base_url.rstrip('/')}/query/table/{self.template}?"
                + urllib.parse.urlencode(params, quote_via=urllib.parse.quote))

    def lookup(self, text: str, *, lang: str) -> list[tuple[str, str]]:
        """Return `[(bdrc_id, label), …]` (short ids like `bdr:P1KG10193`).
        Network/parse errors are swallowed to `[]` — convenient, but a caller
        that caches the result cannot tell a real miss from a transient failure;
        use `lookup_strict` when that distinction matters."""
        try:
            data = self.transport(self._url(text, lang))
        except Exception:
            return []
        return _bindings_to_pairs(data)

    def lookup_strict(self, text: str, *, lang: str) -> list[tuple[str, str]]:
        """Like `lookup` but raises on transport failure (so the caller can avoid
        caching a failure as a definitive miss). Addresses code-review item #4."""
        return _bindings_to_pairs(self.transport(self._url(text, lang)))


def _bindings_to_pairs(data: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for s_val, label in _iter_bindings(data):
        bid = bdrc_short(s_val)
        if bid and label:
            out.append((bid, label))
    return out


def _iter_bindings(data: dict) -> Iterable[tuple[str, str]]:
    """Yield `(s_uri, lit_value)` from a SPARQL-JSON `results.bindings` list.
    Defensive against shape drift: tolerates a missing `lit`, string-vs-dict cell
    forms, or the older lds-pdi `rows`/`dataRow` wrapper some templates emit."""
    if not isinstance(data, dict):
        return
    bindings = (data.get("results") or {}).get("bindings")
    if isinstance(bindings, list):
        for b in bindings:
            if not isinstance(b, dict):
                continue
            s = (b.get("s") or {}).get("value", "") if isinstance(b.get("s"), dict) else ""
            lit = (b.get("lit") or {}).get("value", "") if isinstance(b.get("lit"), dict) else ""
            yield s, lit
        return
    rows = data.get("rows")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get("dataRow") if isinstance(row.get("dataRow"), list) else [row]
        for cell in cells:
            if isinstance(cell, dict):
                yield cell.get("s", ""), cell.get("lit", "")


def bdrc_short(s: str) -> str:
    """`http://purl.bdrc.io/resource/P1KG10193` → `bdr:P1KG10193`."""
    if not isinstance(s, str) or not s:
        return ""
    tail = s.rsplit("/", 1)[-1]
    return f"bdr:{tail}" if tail else ""


def bdrc_lang_order(scheme: Optional[str]) -> list[str]:
    """Map our alias scheme codes (§5) to BDRC `LG_NAME` order."""
    s = (scheme or "").lower()
    if s in ("iast",):
        return ["sa-x-iast", "en", "bo-x-ewts"]
    if s in ("wylie", "acip"):
        return ["bo-x-ewts", "en", "sa-x-iast"]
    if s in ("english", "phonetic", "thl"):
        return ["en", "bo-x-ewts", "sa-x-iast"]
    return ["sa-x-iast", "bo-x-ewts", "en"]
