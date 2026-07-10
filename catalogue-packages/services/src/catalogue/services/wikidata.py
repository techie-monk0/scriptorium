"""Wikidata client — the highest-leverage authority because a single item links
out to BDRC, VIAF, GND, … and carries multilingual labels (native Tibetan/
Devanagari + transliterations). Used by:
  - work_authority.WikidataWorkSource  (work → author via P50)
  - verify.WikidataPersonVerifier       (person → Q-id)

Single reviewable place for everything touching `wikidata.org`. Transport is
`(url) -> dict` so tests inject canned JSON and a swap (alternate mirror, async)
doesn't touch callers. The default transport throttles + retries 429s and RAISES
`AuthorityUnavailable` on transport failure — a throttle is NOT a miss (see
catalogue/http_util.py). A genuine empty result still returns []/None.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from .http_util import AuthorityUnavailable, ThrottledTransport

WikidataTransport = Callable[[str], dict]

# Item ids we care about for typing a hit.
Q_HUMAN = "Q5"
# "is a written work" family — pragmatic set; `is_work` ALSO accepts items with an
# author or a title property, so this list need not be exhaustive. Includes the
# Buddhist-text classes that the old narrow set missed (Mahayana sutra etc.) —
# these matter for the WORK-cataloguing pass (recognizing a canonical text), even
# though authorless sutras can't drive person identification.
WORK_CLASSES = frozenset({
    "Q47461344",   # written work
    "Q7725634",    # literary work
    "Q571",        # book
    "Q234460",     # text
    "Q1980247",    # chapter
    "Q49848",      # document
    "Q3331189",    # version, edition, or translation
    "Q1191035",    # Mahayana sutra (the Lankāvatāra case)
    "Q179461",     # religious text
    "Q2122442",    # sutra
    "Q17329259",   # encyclopedic work / treatise family
})

# A P31 class whose English label contains one of these is treated as a work — a
# robust catch-all for the many Buddhist text subtypes (sūtra, tantra, śāstra,
# commentary, …) we can't enumerate by Q-id. Checked against the class's label,
# which `is_work` is given via `class_labels`.
WORK_CLASS_LABEL_HINTS = (
    "sutra", "sūtra", "tantra", "shastra", "śāstra", "scripture", "treatise",
    "text", "commentary", "writing", "work", "book",
)

P_INSTANCE_OF = "P31"
P_AUTHOR = "P50"
P_TITLE = "P1476"        # "title" property — strong signal an item is a work
P_EDITION_OF = "P629"    # "edition or translation of" → the abstract WORK an edition holds

# Authority cross-link properties — the whole point of using Wikidata as the hub:
# one resolved item hands us the regional authority ids for free, each verified
# against the live API (see probe notes). Values are STRING external ids, not
# item refs, so they need `claim_strings`, not `claim_ids`.
#   P2477 — BDRC person id (bare 'P4954' → we prefix 'bdr:')
#   P1187 — Dharma Drum / DILA person id (East Asian Buddhist authority; 'A001583')
#   P214  — VIAF id (modern/Western)
P_BDRC = "P2477"
P_DILA = "P1187"
P_VIAF = "P214"

# Reverse of cross_ids: our namespaced scheme → (Wikidata property, namespace prefix
# to strip so the value matches how Wikidata stores it). Wikidata holds the BARE id
# (BDRC 'P4954', VIAF '264715620', DILA 'A001583'), so resolve_by_external_id strips
# our 'bdr:'/'viaf:'/'dila:' before querying haswbstatement.
EXTERNAL_ID_PROPS = {
    "bdrc": (P_BDRC, "bdr:"),
    "bdr":  (P_BDRC, "bdr:"),    # accept either spelling of the namespace
    "viaf": (P_VIAF, "viaf:"),
    "dila": (P_DILA, "dila:"),
}


def _split_ns(ext_id: str) -> tuple[str, str]:
    """('bdr:P4954') -> ('bdr', 'P4954'). No colon -> ('', ext_id)."""
    scheme, _, tail = (ext_id or "").partition(":")
    return (scheme, tail) if tail else ("", ext_id or "")


@dataclass
class WikidataClient:
    base_url: str = "https://www.wikidata.org"
    limit: int = 7
    transport: WikidataTransport = field(default_factory=ThrottledTransport)

    # ── endpoints ──────────────────────────────────────────────────────────
    def _search_url(self, text: str, language: str) -> str:
        params = {"action": "wbsearchentities", "format": "json",
                  "language": language, "uselang": language, "type": "item",
                  "limit": str(self.limit), "search": text}
        return f"{self.base_url.rstrip('/')}/w/api.php?" + urllib.parse.urlencode(params)

    def _haswbstatement_url(self, prop: str, value: str) -> str:
        # list=search with a haswbstatement: filter is the cheap reverse lookup:
        # "which item has P2477 = P4954?". Returns query.search[].title = the QID.
        params = {"action": "query", "list": "search", "format": "json",
                  "srlimit": "5", "srsearch": f"haswbstatement:{prop}={value}"}
        return f"{self.base_url.rstrip('/')}/w/api.php?" + urllib.parse.urlencode(params)

    def resolve_by_external_id(self, ext_id: str) -> Optional[str]:
        """Reverse lookup: a regional-authority id (`bdr:P…` / `viaf:…` / `dila:…`)
        → the `wikidata:Q…` hub id that carries it, or None for a genuine miss
        (no such item, or an unsupported scheme). A transport failure RAISES
        `AuthorityUnavailable` so the caller can keep the raw id and flag the bind
        incomplete rather than mistaking offline for 'Wikidata has no item'.

        Already a Wikidata id → returned as-is (no network)."""
        scheme, tail = _split_ns(ext_id)
        if scheme in ("wikidata", "wd"):
            return f"wikidata:{tail}"
        mapping = EXTERNAL_ID_PROPS.get(scheme)
        if not mapping:
            return None
        prop, prefix = mapping
        bare = tail if not ext_id.startswith(prefix) else ext_id[len(prefix):]
        try:
            data = self.transport(self._haswbstatement_url(prop, bare))
        except AuthorityUnavailable:
            raise
        except Exception:
            return None
        for hit in (data or {}).get("query", {}).get("search", []) or []:
            qid = hit.get("title")
            if qid and qid.startswith("Q"):
                return f"wikidata:{qid}"
        return None

    def _entity_url(self, qid: str) -> str:
        # wbgetentities, NOT Special:EntityData/{qid}.json — the latter's payload
        # has NO `claims` key, so is_human/is_work were always False and every
        # Wikidata hit got silently rejected. wbgetentities returns the same
        # {"entities": {qid: {...}}} shape WITH claims.
        params = {"action": "wbgetentities", "ids": qid,
                  "props": "labels|aliases|claims", "format": "json"}
        return f"{self.base_url.rstrip('/')}/w/api.php?" + urllib.parse.urlencode(params)

    def search(self, text: str, *, language: str = "en") -> list[tuple[str, str, str]]:
        """Return `[(qid, label, description), …]`. A genuine empty result is [];
        a transport failure RAISES `AuthorityUnavailable` (don't cache as a miss)."""
        try:
            data = self.transport(self._search_url(text, language))
        except AuthorityUnavailable:
            raise
        except Exception:
            return []
        out = []
        for hit in (data or {}).get("search", []) or []:
            qid = hit.get("id")
            if qid:
                out.append((qid, hit.get("label") or "",
                            hit.get("description") or ""))
        return out

    def entity(self, qid: str) -> Optional[dict]:
        """Return the entity dict for `qid` (the value under entities[qid]), or
        None for a genuine miss. A transport failure RAISES `AuthorityUnavailable`."""
        try:
            data = self.transport(self._entity_url(qid))
        except AuthorityUnavailable:
            raise
        except Exception:
            return None
        ent = (data or {}).get("entities", {})
        return ent.get(qid) if isinstance(ent, dict) else None


# ── entity readers (pure; unit-testable) ────────────────────────────────────────
def claim_ids(entity: dict, prop: str) -> list[str]:
    """All item-id values of statements for `prop` (e.g. P50 author → [Qids])."""
    out = []
    for st in (entity or {}).get("claims", {}).get(prop, []) or []:
        try:
            v = st["mainsnak"]["datavalue"]["value"]
            qid = v.get("id") if isinstance(v, dict) else None
            if qid:
                out.append(qid)
        except (KeyError, TypeError):
            continue
    return out


def claim_strings(entity: dict, prop: str) -> list[str]:
    """All STRING values of statements for `prop` — for external-id properties
    (P2477/P1187/P214) whose datavalue is a bare string, not an item ref."""
    out = []
    for st in (entity or {}).get("claims", {}).get(prop, []) or []:
        try:
            v = st["mainsnak"]["datavalue"]["value"]
            if isinstance(v, str) and v:
                out.append(v)
        except (KeyError, TypeError):
            continue
    return out


def cross_ids(entity: dict) -> dict:
    """Harvest the regional-authority cross-links off a resolved Wikidata person.
    Returns a {scheme: full_id} dict for whichever are present, namespaced to
    match `PERSON_ID_PREFIXES`: BDRC → 'bdr:P…', DILA → 'dila:…', VIAF → 'viaf:…'.
    The first value of each property wins (these are functional ids)."""
    out = {}
    bdrc = claim_strings(entity, P_BDRC)
    if bdrc:
        # BDRC ids are stored bare in Wikidata ('P4954'); our convention is 'bdr:'.
        v = bdrc[0]
        out["bdrc"] = v if v.startswith("bdr:") else f"bdr:{v}"
    dila = claim_strings(entity, P_DILA)
    if dila:
        out["dila"] = f"dila:{dila[0]}"
    viaf = claim_strings(entity, P_VIAF)
    if viaf:
        out["viaf"] = f"viaf:{viaf[0]}"
    return out


def instance_of(entity: dict) -> set:
    return set(claim_ids(entity, P_INSTANCE_OF))


def is_human(entity: dict) -> bool:
    return Q_HUMAN in instance_of(entity)


def live_work_matches(title: str, *, limit: int = 5, client=None) -> "list[dict]":
    """Best-effort LIVE Wikidata WORK search for the works authority picker:
    `[{'system':'wikidata', 'number':'Q…', 'title':str, 'desc':str}]`. Searches labels,
    fetches the top candidates and keeps only those that are works (is_work). Bounded
    (a handful of entity fetches) and swallows every failure (returns [])."""
    if not (title or "").strip():
        return []
    try:
        client = client or WikidataClient()
        out, seen = [], set()
        for qid, label, desc in client.search(title)[:max(2 * limit, 6)]:
            if qid in seen:
                continue
            seen.add(qid)
            ent = client.entity(qid)
            if not ent or not is_work(ent):
                continue
            name, _aliases = labels_and_aliases(ent)
            out.append({"system": "wikidata", "number": qid,
                        "title": name or label, "desc": desc,
                        # cache the abstract-work link so disambiguate() needn't refetch
                        "_edition_of": edition_of(ent)})
            if len(out) >= limit:
                break
        # Collapse edition/version hits to the work they're an edition of (P629).
        return disambiguate(out, client=client)[:limit]
    except Exception:
        return []


def edition_of(entity: dict) -> list:
    """The abstract work(s) this item is an 'edition or translation of' (P629). A Wikidata
    edition/version item points at its WORK via P629, so following it yields the work qid."""
    return claim_ids(entity, P_EDITION_OF)


def disambiguate(matches: "list[dict]", *, client=None) -> "list[dict]":
    """Collapse Wikidata EDITION/VERSION hits to the WORK they're an edition or translation
    of (P629), and drop duplicate Q-ids — so a title search surfaces the work, not its many
    printed editions/translations. Each match is `{system, number:'Q…', title, …}`; a match
    may carry a cached `_edition_of` (work qids, set by `live_work_matches`) to avoid a
    refetch, otherwise the link is read live via `client`. Order-preserving, idempotent, and
    best-effort: an item whose work can't be resolved is kept as-is. Internal `_`-keys are
    stripped from the output."""
    out: list = []
    seen: set = set()
    for m in matches or []:
        qid = m.get("number")
        if not qid:
            continue
        title = m.get("title")
        works = m.get("_edition_of")
        if works is None and client is not None:
            try:
                ent = client.entity(qid)
                works = edition_of(ent) if ent else []
            except Exception:
                works = []
        if works:                                   # an edition → swap to its work
            wqid = works[0]
            if wqid != qid:
                if client is not None:
                    try:
                        went = client.entity(wqid)
                        name, _al = labels_and_aliases(went) if went else (None, None)
                        title = name or title
                    except Exception:
                        pass
                qid = wqid
        if qid in seen:
            continue
        seen.add(qid)
        row = {k: v for k, v in m.items() if not k.startswith("_")}
        row["number"], row["title"] = qid, title
        out.append(row)
    return out


def is_work(entity: dict, *, class_labels: "dict | None" = None) -> bool:
    """True if the item is a written work. Accepts on ANY of, in cheap-first order
    (all read from the already-fetched entity — no extra network):
      • it names an author (P50) — an authored thing is a work;
      • it carries a title (P1476) — strong work signal;
      • its instance-of class is in WORK_CLASSES (incl. Mahayana sutra etc.);
      • OPTIONALLY: a class whose label matches WORK_CLASS_LABEL_HINTS — only when
        the caller supplies `class_labels` ({class_qid: english_label}), since
        resolving class labels needs extra lookups. This is the catch-all for the
        many Buddhist text subtypes we can't list by Q-id (the Lankāvatāra was
        rejected because 'Mahayana sutra' wasn't in the old set).

    Note: authorless works (most sutras) pass now, which matters for the WORK
    pass; they still can't identify a PERSON (no author to match)."""
    if claim_ids(entity, P_AUTHOR):
        return True
    if claim_strings(entity, P_TITLE):
        return True
    classes = instance_of(entity)
    if classes & WORK_CLASSES:
        return True
    if class_labels:
        for qid in classes:
            lab = (class_labels.get(qid) or "").lower()
            if any(h in lab for h in WORK_CLASS_LABEL_HINTS):
                return True
    return False


def labels_and_aliases(entity: dict, *, langs=("en", "bo", "sa", "sa-x-iast",
                                               "bo-x-ewts")) -> tuple[str, list]:
    """Return (primary_label, aliases). primary = English label if present, else
    the first available. aliases = the other-language labels + declared aliases —
    this is where the native Tibetan/Sanskrit + transliterations come from, so a
    later lookup in ANY script matches."""
    labels = (entity or {}).get("labels", {}) or {}
    primary = (labels.get("en") or {}).get("value") or ""
    if not primary:
        for lng in langs:
            v = (labels.get(lng) or {}).get("value")
            if v:
                primary = v
                break
    extra = []
    for lng in langs:
        v = (labels.get(lng) or {}).get("value")
        if v and v != primary:
            extra.append(v)
    for lng, items in ((entity or {}).get("aliases", {}) or {}).items():
        for a in items or []:
            val = a.get("value") if isinstance(a, dict) else None
            if val:
                extra.append(val)
    # de-dup preserving order
    seen, out = set(), []
    for v in extra:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return primary, out
