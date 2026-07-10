"""Verification pass over promoted canonical rows (§4.3, whats_next B).

A SEPARATE, idempotent pass — deliberately not wired into the review accept, so
accepting a proposal stays instant. It walks the `person` / `work` rows promotion
created and, for each not-yet-verified row, asks a chain of pluggable *verifiers*
to identify it, attaching the external identifier of the first accepted match:

  person → `external_id`           (e.g. 'bdr:P1KG10193')
  work   → `canonical_system` + `canonical_number`  (e.g. 'toh' / '182')

Pluggability: a verifier is anything implementing the `Verifier` protocol
(`name` + `verify(db, kind, text) -> Match | None`). `verify_all` tries them in
order; the first to return a Match wins. The default chain is `[BdrcVerifier()]`
(BDRC + 84000 via the cached resolver); add e.g. a Wikidata or manual-CSV verifier
by passing your own list — no change to the walk/attach logic.

A verifier OWNS its precision: `BdrcVerifier` rejects wrong-entity-type ids (a
person must be a `bdr:P…`, a work a work/Toh id) and hits whose canonical name
shares no token with the query, because BDRC's name search returns the top fuzzy
hit of any type (see the code review in catalogue/bdrc.py).

Run AFTER promotion. `offline=True` reads only the warm `resolver_cache` (no
network — safe alongside OCR); the default falls back to a live BDRC lookup on a
cache miss (network only, never the LLM).
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from functools import partial
from typing import Optional, Protocol

from . import bdrc
from catalogue.db_store import add_alias, fold_key, init_db, search_normalize


def _acc(db):
    """A system Access over this connection — engine-routed person/work reads + writes (authority
    bind + external ids), the review queue, and the verify-cleanup maintenance. Caller commits."""
    from catalogue.access_api import system_conn
    return system_conn(db)
from .honorifics import (
    honorific_keys, is_ordinal_token, ordinal_value, strip_honorifics,
    translit_variants,
)
from .http_util import AuthorityUnavailable
from .work_canonical_resolver import LiveResolver, cached_rows
from catalogue.db_store import default_db_path


# ── Match + Verifier protocol ─────────────────────────────────────────────────
@dataclass
class Match:
    """An identification. `system`/`number` map to work.canonical_system/number;
    for a person `number` is the hub external_id (→ person.external_id).

    `extra_ids` carries regional-authority cross-links harvested in one shot
    ({scheme: full_id}, e.g. {'bdrc':'bdr:P4954','viaf':'viaf:8711937'}) → written
    to person_external_id. `provisional=True` means a FUZZY hit (BDRC BLMP) that
    must be QUEUED for human review, never auto-applied — see verify_person."""
    number: str
    system: Optional[str] = None
    canonical_name: Optional[str] = None
    aliases: list = field(default_factory=list)
    verifier: str = ""
    extra_ids: dict = field(default_factory=dict)
    provisional: bool = False


class Verifier(Protocol):
    name: str
    def verify(self, db, kind: str, text: str) -> Optional[Match]: ...


# ── Shared precision helper ───────────────────────────────────────────────────
# `_TOKEN` (4+ chars) is for the FUZZY overlap check — long tokens only, so a short
# noise word can't trigger a fuzzy match. `_NAME_TOKEN` (1+ char) is for the EXACT
# whole-name set: it must keep EVERY syllable, incl. short ones. Dropping <4-char
# tokens was a real bug — "Lama Yeshe Chö Pel" lost "Chö"/"Pel" and collapsed to
# {lama,yeshe}, falsely matching "Lama Yeshe".
_TOKEN = re.compile(r"[a-z0-9]{4,}")
_NAME_TOKEN = re.compile(r"[a-z0-9]+")

def _tokens(text: str) -> set:
    """4+ char fold-key tokens, MINUS honorific titles. Dropping titles stops a
    shared courtesy word ("lama" in "Lama Zopa" vs "Lama Yeshe") from registering
    as a name overlap. Office words (dalai/panchen/karmapa…) are NOT honorifics,
    so they survive and still distinguish/match offices."""
    return set(_TOKEN.findall(fold_key(text or ""))) - honorific_keys()


def name_overlaps(query: str, *candidates: str) -> bool:
    """True if the query shares a 4+ char (non-title) token with any candidate
    name. Short/tokenless queries can't be judged → allowed (don't over-block).
    FUZZY — fine for BDRC hits (which are provisional → human-reviewed), too weak
    for an auto-applied match (see `name_matches_exactly`)."""
    qt = _tokens(query)
    if not qt:
        return True
    return any(qt & _tokens(c) for c in candidates)


def _name_token_set(text: str) -> frozenset:
    """The whole-name token set used by the EXACT auto-bind gate. Built on
    `search_normalize` (NFKD diacritic-strip + lowercase) — NOT `fold_key` — so it
    keeps the SAFE normalization (Śāntideva=Santideva, Müller=Muller) but DROPS
    fold_key's aspirate-digraph collapse (sh/ch/ph/th/kh…), which is a Sanskrit/Tibetan
    transliteration device that over-merges plain English names (Smith=Smit, Booth=Boot,
    Stephen=Stepen). The exact gate auto-binds, so it must not rely on that lossy bridge.

    EVERY syllable counts (so "Lama Yeshe Chö Pel" ≠ "Lama Yeshe"); honorifics stay
    ("Lama Yeshe" ≠ "Yeshe"); ordinal tokens are dropped (compared separately by value
    via ordinal_value, so '14th'/'Fourteenth'/'XIV' forms agree on the rest)."""
    toks = _NAME_TOKEN.findall(search_normalize(text or ""))
    return frozenset(t for t in toks if not is_ordinal_token(t))


def name_matches_exactly(query: str, *candidates: str) -> bool:
    """True if `query` matches a candidate label/alias as a WHOLE name. Accepts on
    EITHER (a) the full token SET being equal (order/punctuation-insensitive, so
    "Surname, Given" inversion works), OR (b) the honorific-stripped fold-keys being
    literally equal (so a titled query "Geshe Lhundub Sopa" matches a bare "Lhundub
    Sopa"). Strict acceptance for AUTO-APPLIED matches (Wikidata/VIAF): a lone given
    name only matches the same lone name, never a longer name that merely contains
    it — the Lama-Yeshe-Losal / obscure-Yeshe false positives are rejected because
    {lama,yeshe} ≠ {yeshe} and ≠ {lama,yeshe,losal,rinpoche}.

    ORDINAL GATE: an office incumbent is identified by its number, but the digit/
    roman ordinal ('14th'/'XIV') isn't a 4-char token, so token-set equality alone
    would match "14th Dalai Lama" to "7th Dalai Lama". So: if EITHER side carries an
    ordinal, the candidate is rejected unless both ordinals are present and EQUAL.

    A wrong miss is recoverable (re-run / BLMP review); a wrong auto-bind is silent
    corruption."""
    q_set = _name_token_set(query)
    q_strip = search_normalize(strip_honorifics(query))   # NOT fold_key — no digraph bridge
    q_ord = ordinal_value(query)
    for c in candidates:
        if not c:
            continue
        c_ord = ordinal_value(c)
        # If either names an ordinal, require both to agree (7th ≠ 14th; a numbered
        # office must not match an unnumbered/different-numbered one).
        if (q_ord is not None or c_ord is not None) and q_ord != c_ord:
            continue
        if q_set and _name_token_set(c) == q_set:
            return True                               # same whole name (any order)
        ck = search_normalize(strip_honorifics(c))
        if ck and ck == q_strip:
            return True                               # equal after stripping titles
    return False


# ── BDRC / 84000 verifier ─────────────────────────────────────────────────────
@dataclass
class BdrcVerifier:
    """Identify a person/work via the cached BDRC + 84000 resolver, with guards
    against BDRC's fuzzy-top-hit behaviour: wrong entity type and non-overlapping
    names are rejected."""
    name: str = "bdrc"
    resolver: object = field(default_factory=LiveResolver)
    offline: bool = False
    kinds: tuple = ("person", "work")     # restrict to ("work",) when ES owns persons

    def verify(self, db, kind: str, text: str) -> Optional[Match]:
        if kind not in self.kinds:
            return None
        if kind == "person":
            res = self.resolver.resolve_person(db, text, offline=self.offline)
            if (res and bdrc.is_person_id(res.canonical_number)
                    and name_overlaps(text, res.canonical_name or "", *(res.aliases or []))):
                # BLMP is a fuzzy name search whose top hit is unreliable (it ranks
                # works above persons, surfaces collaborators — see the Sajjana/
                # Nagarjuna probe). So a person hit from here is PROVISIONAL: queued
                # for human confirmation, never auto-applied. The precision BDRC id
                # now comes from the Wikidata cross-link instead.
                return Match(res.canonical_number, "bdrc", res.canonical_name,
                             list(res.aliases or []), self.name, provisional=True)
            return None
        if kind == "work":
            res = self.resolver.resolve_work(db, text, offline=self.offline)
            if not (res and res.canonical_number):
                return None
            # Toh ids are matched by number (reliable → auto-apply). A BDRC work id
            # comes from BLMP's fuzzy search → provisional, queued for review.
            if res.canonical_system == "toh":
                return Match(res.canonical_number, "toh", res.canonical_name,
                             list(res.aliases or []), self.name)
            if (bdrc.is_work_id(res.canonical_number)
                    and name_overlaps(text, res.canonical_name or "", *(res.aliases or []))):
                return Match(res.canonical_number, res.canonical_system,
                             res.canonical_name, list(res.aliases or []), self.name,
                             provisional=True)
        return None


@dataclass
class BdrcESVerifier:
    """Identify a PERSON via library.bdrc.io's ElasticSearch (`bdrc_prod`) instead of
    the BLMP template. AUTO-APPLIED only when the query matches a returned label
    EXACTLY (name_matches_exactly): the `type:Person` filter rules out wrong-type ids,
    and the exact-name gate rules out BDRC's fuzzy top-hit noise. No exact match, or
    ≥2 exact matches (homonyms) → None — we deliberately do NOT queue fuzzy
    non-matches (the BLMP-noise lesson). Persons only; works keep going through
    BdrcVerifier (84000/BLMP). Opt-in via default_verifiers(bdrc_over_blmp=True).

    LIMITATION: BDRC stores many Tibetan names only in Wylie, so a phonetic-English
    name won't match those (the site's phonetic→Wylie query conversion is not done
    here) — those persons fall to the manual matching tool. See person_resolution.md.

    HOMONYM GUARD: a bare Tibetan personal name (e.g. "dge 'dun rgya mtsho") is shared
    by dozens of BDRC persons. The auto-bind condition is therefore "exactly ONE
    DISTINCT person exact-matches", and the search `size` must be large enough for the
    homonyms to actually appear — with size=4 they were truncated away, so a lone
    survivor looked unambiguous and the wrong namesake bound (the P1GS147791 / 2nd Dalai
    Lama case). size=20 makes the collision visible so the guard refuses."""
    name: str = "bdrc-es"
    es: object = field(default_factory=lambda: bdrc.BdrcElasticSearch(size=20))
    offline: bool = False

    def verify(self, db, kind: str, text: str) -> Optional[Match]:
        if kind != "person" or self.offline:
            return None
        try:
            hits = self.es.person_search(text)
        except Exception:
            return None                       # network/parse failure → no match (not a miss)
        # Dedup by id: a single person can surface in several hits, but ≥2 DISTINCT
        # persons exact-matching the name is a homonym collision → refuse to bind.
        matches = {h["id"]: h for h in hits
                   if bdrc.is_person_id(h.get("id"))
                   and name_matches_exactly(text, *(h.get("labels") or []))}
        if len(matches) != 1:
            return None                       # no exact match, or ambiguous homonyms
        m = next(iter(matches.values()))
        return Match(m["id"], "bdrc", (m.get("labels") or [None])[0], [], self.name)


# ── Wikidata / VIAF person verifiers ────────────────────────────────────────────
# Persons only. Works keep going through BDRC/84000 here (canonical id), while the
# richer work→author identification lives in the separate WorkAuthorityResolver.
# Both cache their per-name result in `resolver_cache` (so re-runs don't re-query)
# and respect `offline` by returning None before any network/cache touch.
@dataclass
class WikidataPersonVerifier:
    """Identify a person via Wikidata: search the name, accept the first
    human-typed (P31=Q5) hit whose label/alias EXACTLY matches the query (or its
    honorific-stripped form), and attach `wikidata:Q…` + its multilingual aliases.
    Exact (not fuzzy) because this auto-applies — see `name_matches_exactly` and
    the Lama Yeshe case. `max_candidates` is scanned in rank order; the search is
    on the ORIGINAL name, and strip_honorifics' single-name guard keeps "Lama
    Yeshe" intact rather than searching bare "Yeshe"."""
    name: str = "wikidata"
    client: object = field(default_factory=lambda: _wikidata_client())
    offline: bool = False
    max_candidates: int = 5

    def verify(self, db, kind: str, text: str) -> Optional[Match]:
        if kind != "person" or self.offline:
            return None
        rows = cached_rows(
            # v4: name-matcher fixed (search_normalize basis — no digraph over-merge,
            # no short-syllable collapse). v3 cached results were chosen under the buggy
            # matcher (e.g. "Lama Yeshe"→Q106793860); the bump invalidates them.
            db, namespace="person_verify", source=self.name, query=text,
            version=4, compute=lambda: self._lookup(text), cache_empty=False)
        if not rows:
            return None
        r = rows[0]
        # `ambiguous` = ≥2 distinct humans exactly matched this name → don't auto-bind
        # the first (could be the wrong homonym); mark provisional so verify_person
        # QUEUES it for a human instead of guessing.
        return Match(r["number"], "wikidata", r.get("canonical_name"),
                     list(r.get("aliases") or []), self.name,
                     extra_ids=dict(r.get("extra_ids") or {}),
                     provisional=bool(r.get("ambiguous")))

    def _lookup(self, text: str) -> list:
        from . import wikidata as W
        # Search the original name (strip_honorifics' guard avoids a bare given
        # name); collect ALL human candidates whose label/alias EXACTLY matches.
        hits = []
        for qid, _label, _desc in self.client.search(text)[:self.max_candidates]:
            ent = self.client.entity(qid)
            if not ent or not W.is_human(ent):
                continue
            name, aliases = W.labels_and_aliases(ent)
            if name_matches_exactly(text, name, *aliases):
                # Hub win: one resolved item yields the BDRC/DILA/VIAF ids for free.
                extra = {"wikidata": f"wikidata:{qid}", **W.cross_ids(ent)}
                hits.append({"number": f"wikidata:{qid}", "canonical_name": name,
                             "aliases": aliases, "extra_ids": extra})
        if not hits:
            return []
        distinct = {h["number"] for h in hits}
        head = dict(hits[0])
        head["ambiguous"] = len(distinct) > 1     # 2+ exact-name humans → queue, don't bind
        return [head]


@dataclass
class ViafPersonVerifier:
    """Identify a (usually modern) person via VIAF AutoSuggest: accept the first
    `personal` hit whose term EXACTLY matches the query (or its stripped form);
    attach `viaf:<id>`. Exact (not fuzzy) because this auto-applies."""
    name: str = "viaf"
    client: object = field(default_factory=lambda: _viaf_client())
    offline: bool = False

    def verify(self, db, kind: str, text: str) -> Optional[Match]:
        if kind != "person" or self.offline:
            return None
        rows = cached_rows(
            db, namespace="person_verify", source=self.name, query=text,
            version=3, compute=lambda: self._lookup(text), cache_empty=False)  # v3: matcher fix
        if not rows:
            return None
        r = rows[0]
        return Match(r["number"], "viaf", r.get("canonical_name"), [], self.name,
                     provisional=bool(r.get("ambiguous")))

    def _lookup(self, text: str) -> list:
        hits = [{"number": f"viaf:{vid}", "canonical_name": term}
                for vid, term in self.client.suggest(text)
                if name_matches_exactly(text, term)]
        if not hits:
            return []
        distinct = {h["number"] for h in hits}
        head = dict(hits[0])
        head["ambiguous"] = len(distinct) > 1     # 2+ exact-name hits → queue, don't bind
        return [head]


def _wikidata_client():
    from .wikidata import WikidataClient
    return WikidataClient()


def _viaf_client():
    from .viaf import VIAFClient
    return VIAFClient()


# Allowed person external-id prefixes. `purge_suspect_matches` nulls anything
# outside this set (a wrong-type id written before the guards existed).
PERSON_ID_PREFIXES = ("bdr:P", "wikidata:Q", "viaf:")


def default_verifiers(*, offline: bool = False, bdrc_over_blmp: bool = False) -> list:
    """Chain order = precision before recall (first match wins per `_first_match`):
      1. Wikidata — typed (is_human) + harvests BDRC/DILA/VIAF cross-links in one
         hit; the precision-first authority path (raw BDRC SPARQL is disabled and
         BLMP-by-name is noise — see probe notes). Auto-applied.
      2. VIAF — modern/Western personal-name authority, name-guarded. Auto-applied.
      3. BDRC BLMP — fuzzy, last-resort recall for persons absent from Wikidata;
         returns PROVISIONAL matches → queued for review, never auto-applied.
    For works only BdrcVerifier responds (84000 Toh auto-applies; BDRC work id is
    provisional). Each verifier no-ops cleanly when offline.

    `bdrc_over_blmp` (default OFF — the default flow keeps BLMP): for PERSONS, use the
    ElasticSearch `BdrcESVerifier` (exact-name auto-bind, library.bdrc.io backend)
    INSTEAD of the BLMP person path; works still go through `BdrcVerifier` (84000/BLMP)."""
    chain = [WikidataPersonVerifier(offline=offline),
             ViafPersonVerifier(offline=offline)]
    if bdrc_over_blmp:
        chain.append(BdrcESVerifier(offline=offline))            # persons via ElasticSearch
        chain.append(BdrcVerifier(offline=offline, kinds=("work",)))  # works only
    else:
        chain.append(BdrcVerifier(offline=offline))              # BLMP for person + work
    return chain


# ── Attaching a match ─────────────────────────────────────────────────────────
def _has_alias(db, kind: str, parent_id: int, text: str) -> bool:
    key = fold_key(text)
    reads = _acc(db).persons.reads if kind == "person" else _acc(db).works.reads
    return reads.has_alias_key(parent_id, key)


def _add_canonical_aliases(db, kind: str, pid: int, m: Match) -> None:
    for text in [m.canonical_name, *(m.aliases or [])]:
        text = (text or "").strip()
        if text and not _has_alias(db, kind, pid, text):
            add_alias(db, kind, pid, text, "other")


def _store_external_ids(db, pid: int, extra_ids: dict) -> None:
    """Record every harvested authority id on the person (one row per scheme).
    Idempotent: re-resolving overwrites the same (person_id, scheme) row."""
    for scheme, value in (extra_ids or {}).items():
        if scheme and value:
            _acc(db).persons.writes.store_external_id(pid, scheme, value)


def _candidate_queued(db, kind: str, rid: int) -> bool:
    """True if a provisional match for this row is already pending review, so a
    re-run doesn't enqueue duplicates."""
    # person → person_authority (bind external_id); work → work_canonical (assign
    # canonical_system/number). work_authority.py owns the separate 'work_authorship'
    # item (author/translator assignment) — different decision, different payload.
    item_type = "person_authority" if kind == "person" else "work_canonical"
    return _acc(db).review.reads.exists_pending(item_type, f'%"{kind}_id": {rid}%')


def _queue_candidate(db, kind: str, rid: int, m: Match) -> None:
    """Enqueue a provisional (fuzzy BDRC BLMP) match for human confirmation
    instead of auto-applying it."""
    # person → person_authority (bind external_id); work → work_canonical (assign
    # canonical_system/number). work_authority.py owns the separate 'work_authorship'
    # item (author/translator assignment) — different decision, different payload.
    item_type = "person_authority" if kind == "person" else "work_canonical"
    # Record the person's CURRENT name as an id-reuse anchor: accept_person_authority
    # re-checks it (person_identity_ok) so a recycled id can't bind this candidate onto a
    # different, later person. (Works have no single title column — their canonical accept
    # is not identity-binding in the same way, so no anchor there.)
    extra = {}
    if kind == "person":
        p = _acc(db).persons.reads.get(rid)
        extra["person_name"] = p.primary_name if p else None
    _acc(db).review.writes.enqueue(item_type, {
        f"{kind}_id": rid,
        **extra,
        "candidate_id": m.number,
        "system": m.system,
        "canonical_name": m.canonical_name,
        "aliases": list(m.aliases or []),
        "verifier": m.verifier,
        "reason": "bdrc_blmp_fuzzy",
    })


def _first_match(db, verifiers, kind: str, text: str) -> Optional[Match]:
    for v in verifiers:
        m = v.verify(db, kind, text)
        if m:
            return m
    return None


def _person_has_work(db, pid: int) -> bool:
    """True if the person is attached to any work (author/translator contributor, or
    edition translator). Such people belong to the WORK-DRIVEN joint pass
    (catalogue/person_work.py) — the name-only pass must NOT bind them, because a
    name alone can't disambiguate homonyms the work would (the Lama Yeshe case)."""
    from catalogue.db_store import contributor_store as cs
    return cs.person_referenced(db, pid)


# ── Acting on queued provisional candidates (the /review accept/reject UI) ──────
def _resolve_item(db, item_id: int, status: str) -> None:
    _acc(db).review.writes.set_status(item_id, status)


def person_identity_ok(db, pid: int, expected_name) -> bool:
    """Guard a queued person item against id-reuse: confirm person `pid` STILL matches
    the name the item recorded at queue time. The cache/queue store ids the database
    can't see as references, and SQLite recycles primary keys — so a pending item can
    outlive its person and point at whoever later inherits the id. Blank `expected_name`
    (older items predating this field) → can't check, allow. Otherwise the name must
    fold-key-match the person's current primary_name or one of its aliases; a total
    mismatch means the id was recycled onto someone else → refuse the bind."""
    if not expected_name:
        return True
    from catalogue.db_store import fold_key
    want = fold_key(expected_name)
    if not want:
        return True
    acc = _acc(db)
    p = acc.persons.reads.get(pid)
    if not p:
        return False
    names = [p.primary_name] + [text for _aid, text, _scheme in acc.persons.reads.aliases(pid)]
    return any(n and fold_key(n) == want for n in names)


def bind_person(db, pid: int, ext_id: str, name=None, aliases=None,
                extra_ids=None, *, commit: bool = True, force: bool = False) -> bool:
    """THE single definition of "bind a person to an authority id": set
    person.external_id + verification_status='verified', add the canonical name +
    aliases (scheme 'other'), and store any cross-linked authority ids
    (person_external_id). Shared by `accept_person_authority` and the person+work
    joint accept so the two can't drift. No-op (returns False) if the person is
    missing, or already bound — UNLESS `force` (a deliberate operator rebind):
    then the previous id's harvested cross-links are dropped and replaced. The
    default keeps the auto-matcher from ever clobbering an existing binding."""
    # `_incomplete` is a harvest sentinel (the cross-link fetch failed, §6.17), not
    # an authority id — pop it so it never reaches person_external_id, and record it
    # on the row so the bind is flagged for re-harvest. A complete bind clears it.
    extra_ids = dict(extra_ids or {})
    incomplete = 1 if extra_ids.pop("_incomplete", False) else 0
    p = _acc(db).persons.reads.get(pid)       # LIVE-only (a tombstoned person can't be bound)
    if not p:
        return False                       # missing or tombstoned
    if p.external_id and not force:
        return False                       # already bound; a rebind must pass force
    if p.external_id and p.external_id != ext_id:
        # Rebinding to a DIFFERENT authority → the old hub's harvested cross-links
        # (BDRC/VIAF/DILA) are now wrong; drop them before storing the new ones.
        _acc(db).persons.writes.clear_external_ids(pid)
    # Routed through the guarded write API (store.Store): rows=1 asserts the bind
    # actually landed on the row. This is the write that used to fail silently when
    # harvest_incomplete was missing from the DB — now it can't pass for a success.
    from catalogue.db_store import as_store
    # Clear any pending suggested_external_id: a real binding supersedes the suggestion
    # (whether or not it's the one that was suggested), and the row leaves the worklist.
    as_store(db).write(
        "UPDATE person SET external_id = ?, verification_status = 'verified', "
        "harvest_incomplete = ?, suggested_external_id = NULL WHERE id = ?",
        (ext_id, incomplete, pid), rows=1)
    _add_canonical_aliases(db, "person", pid,
                           Match(ext_id, None, name, list(aliases or [])))
    _store_external_ids(db, pid, extra_ids)
    if commit:
        db.commit()
    return True


def accept_person_authority(db, item_id: int, *, commit: bool = True) -> bool:
    """Apply a queued `person_authority` candidate: bind the person's external_id,
    flip to 'verified', add the candidate's aliases + any cross-links. Marks the
    item resolved. Returns False if missing/not pending or the person already bound."""
    row = _acc(db).review.reads.get_typed(item_id, "person_authority")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    pid, cid = p.get("person_id"), p.get("candidate_id")
    if not pid or not cid:
        return False
    if not person_identity_ok(db, pid, p.get("person_name")):
        return False                       # person id was recycled — don't bind a stranger
    if not bind_person(db, pid, cid, p.get("canonical_name"), p.get("aliases"),
                       p.get("extra_ids"), commit=False):
        return False                       # already bound by another path
    _resolve_item(db, item_id, "resolved")
    if commit:
        db.commit()
    return True


def accept_work_canonical(db, item_id: int, *, commit: bool = True) -> bool:
    """Apply a queued `work_canonical` candidate: set the work's canonical_system/
    number + aliases. Marks the item resolved. False if missing/not pending or the
    work already has a canonical id."""
    row = _acc(db).review.reads.get_typed(item_id, "work_canonical")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    wid, cid = p.get("work_id"), p.get("candidate_id")
    if not wid or not cid:
        return False
    wf = _acc(db).works.reads.review_fields(wid)
    if wf and wf["canonical_number"]:
        return False
    _acc(db).works.writes.set_scalars(wid, {"canonical_system": p.get("system"), "canonical_number": cid})
    m = Match(cid, p.get("system"), p.get("canonical_name"),
              list(p.get("aliases") or []), p.get("verifier") or "")
    _add_canonical_aliases(db, "work", wid, m)
    _resolve_item(db, item_id, "resolved")
    if commit:
        db.commit()
    return True


def reject_candidate(db, item_id: int, *, commit: bool = True) -> bool:
    """Reject a queued authority candidate (person_authority / work_canonical):
    mark it 'rejected' without binding anything. The row stays provisional and can
    be re-checked or confirmed-local later."""
    status = _acc(db).review.reads.status_of(item_id, ("person_authority", "work_canonical"))
    if status != "pending":
        return False
    _resolve_item(db, item_id, "rejected")
    if commit:
        db.commit()
    return True


def _person_query_forms(db, pid: int, primary_name: str, *, extensions: bool) -> list:
    """Name forms to try against the verifier chain, in priority order.

    Default: just the honorific-stripped `primary_name`. Under
    --person-resolution-extensions, ALSO try every `person_alias` (extended-stripped)
    — so an office/ordinal primary ("Dalai Lama XIV") still reaches its personal-name
    alias ("Tenzin Gyatso"), and transliteration variants get a second chance.
    Primary first, then aliases by id; deduped on fold_key (a re-spelling of the same
    form isn't a second query)."""
    forms, seen = [], set()

    def _add(text: str) -> None:
        text = (text or "").strip()
        key = fold_key(text)
        if text and key not in seen:
            seen.add(key)
            forms.append(text)

    _add(strip_honorifics(primary_name, extended=extensions))
    if extensions:
        # Guarantee extensions ⊇ baseline: also try the BASIC-stripped form baseline
        # would have used. Extended stripping can over-strip a title that is really
        # part of the figure's conventional identity ("Sakya Pandita" → "Sakya",
        # losing the exact authority match Q982008), so keep the gentler form as a
        # fallback query. Deduped on fold_key, so it's a no-op when the two agree.
        _add(strip_honorifics(primary_name, extended=False))
        for _aid, txt, _scheme in _acc(db).persons.reads.aliases(pid):
            _add(strip_honorifics(txt or "", extended=True))
        # Transliteration variants (Lozang↔Lobsang) of every form gathered so far —
        # appended last, so exact/alias forms are still tried first.
        for base in list(forms):
            for v in translit_variants(base):
                _add(v)
    return forms


def verify_person(db, verifiers, pid: int, *, commit: bool = True,
                  extensions: bool = False, defer_to_joint: bool = False) -> str:
    """matched | candidate | deferred | unmatched | already. Attaches
    person.external_id + flips to 'verified' on an unambiguous exact match. Already
    'verified'/'confirmed_local' → 'already'.

    A HARD (non-provisional) exact-name hit is auto-bound regardless of work edges —
    the verifier's name guard makes it safe. A FUZZY hit is NEVER auto-bound (the
    Sajjana trap); it is QUEUED as a candidate for human pick in /picker.

    `defer_to_joint` (default False) is legacy: when True, a work-attached person with
    only a fuzzy/absent hit returns 'deferred' for the work-driven joint pass
    (catalogue/person_work.py) to own. That pass is now DEPRECATED (low value on this
    modern-container corpus — see whats_next §B.1), so the DEFAULT now queues the fuzzy
    hit as a candidate even for work-attached persons, instead of dead-ending them.

    `extensions` (--person-resolution-extensions) widens the query to the person's
    aliases and uses extended honorific/office stripping (see _person_query_forms);
    the first HARD hit across any form wins, else the first fuzzy hit is the fallback."""
    pr = _acc(db).persons.reads.get(pid)
    if not pr:
        return "unmatched"
    row = (pr.primary_name, pr.external_id, pr.verification_status)
    if row[1] or row[2] in ("verified", "confirmed_local"):
        return "already"
    # Search/match on the title-free form ("Kyabje Trijang Rinpoche" → "Trijang"),
    # but keep the stored primary_name untouched. strip_honorifics leaves an
    # office+ordinal ("14th Dalai Lama") intact — that IS the identity. A HARD hit
    # from ANY form binds; otherwise keep the first fuzzy/ambiguous hit as fallback.
    m = None
    for q in _person_query_forms(db, pid, row[0], extensions=extensions):
        cand = _first_match(db, verifiers, "person", q)
        if cand and not cand.provisional:
            m = cand
            break
        if cand and m is None:
            m = cand
    has_work = _person_has_work(db, pid)
    # A HARD (non-provisional) hit is an exact-name authority match — the verifier's
    # own name guard already fired, so it is NOT the homonym risk the deferral guards
    # against. Bind it even for a work-attached person (e.g. Atisha → Q320150, whose
    # only catalogue works are modern containers the joint pass can't resolve). Only
    # a FUZZY or absent match on a work-attached person is deferred to the joint pass.
    if m and not m.provisional:
        _acc(db).persons.writes.bind_external(pid, m.number, "verified")
        _add_canonical_aliases(db, "person", pid, m)
        # Cross-linked authority ids (BDRC/DILA/VIAF) harvested from the same hit.
        _store_external_ids(db, pid, m.extra_ids)
        if commit:
            db.commit()
        return "matched"
    if defer_to_joint and has_work:
        return "deferred"                  # legacy: work-driven joint pass owns it
    if not m:
        return "unmatched"
    # A fuzzy hit — confirm by a human, don't auto-bind (the Sajjana trap). Now the
    # joint pass is deprecated, work-attached fuzzy hits are queued here too (not
    # deferred to nowhere). Queue once; leave the person 'provisional'.
    if not _candidate_queued(db, "person", pid):
        _queue_candidate(db, "person", pid, m)
        if commit:
            db.commit()
    return "candidate"


def confirm_local(db, pid: int, *, commit: bool = True) -> bool:
    """Mark a person 'confirmed_local': a human reviewed it and no external
    authority record exists (e.g. a self-published modern author), so THIS row is
    canonical. Removes it from the verify worklist permanently — which the bare
    `external_id IS NULL` signal could never express. No-op (returns False) if the
    person already has an external_id (don't downgrade a real authority match)."""
    changed = _acc(db).persons.writes.confirm_local(pid)
    if commit:
        db.commit()
    return changed


def verify_work(db, verifiers, wid: int, *, commit: bool = True) -> str:
    """matched | unmatched | already. Attaches work.canonical_system/number."""
    wf = _acc(db).works.reads.review_fields(wid)
    if not wf:
        return "unmatched"
    if wf["canonical_number"]:
        return "already"
    title = _acc(db).works.reads.representative_title(wid)
    if not title:
        return "unmatched"
    m = _first_match(db, verifiers, "work", title)
    if not m:
        return "unmatched"
    if m.provisional:
        # BDRC work id from BLMP's fuzzy search — queue, don't auto-apply (a Toh
        # match is reliable and lands in the auto-apply path below instead).
        if not _candidate_queued(db, "work", wid):
            _queue_candidate(db, "work", wid, m)
            if commit:
                db.commit()
        return "candidate"
    _acc(db).works.writes.set_scalars(wid, {"canonical_system": m.system, "canonical_number": m.number})
    _add_canonical_aliases(db, "work", wid, m)
    if commit:
        db.commit()
    return "matched"


# ── Walk ──────────────────────────────────────────────────────────────────────
def verify_all(db, verifiers=None, *, kinds=("person", "work"),
               offline: bool = False, bdrc_over_blmp: bool = False,
               extensions: bool = False, defer_to_joint: bool = False,
               limit: int | None = None, verbose: bool = False) -> dict:
    """Verify every not-yet-verified person/work via the verifier chain. Commits
    per row (short locks, resumable). Returns per-kind status tallies.
    `bdrc_over_blmp` swaps the ElasticSearch person verifier in for BLMP (works
    unaffected) — see default_verifiers. `extensions` enables alias-based matching +
    extended honorific stripping on the person pass (see verify_person)."""
    verifiers = (verifiers if verifiers is not None
                 else default_verifiers(offline=offline, bdrc_over_blmp=bdrc_over_blmp))
    if verbose:
        print(f"Verifying via [{', '.join(v.name for v in verifiers)}]"
              f"{' (offline)' if offline else ''}"
              f"{' +extensions' if extensions else ''}…", flush=True)
    summary: dict = {}
    if "person" in kinds:
        ids = _acc(db).persons.reads.provisional_ids(limit)
        person_fn = partial(verify_person, extensions=extensions,
                            defer_to_joint=defer_to_joint)
        summary["person"] = _run(db, verifiers, ids, person_fn, verbose, "person")
    if "work" in kinds:
        ids = _acc(db).works.reads.canonical_unresolved_ids(limit=limit)
        summary["work"] = _run(db, verifiers, ids, verify_work, verbose, "work")
    return summary


def verify_persons(db, person_ids, verifiers=None, *, offline: bool = False,
                   bdrc_over_blmp: bool = False, extensions: bool = False,
                   defer_to_joint: bool = False, verbose: bool = False) -> dict:
    """Run the person verify pass over a SPECIFIC id list (not the whole worklist).
    Same per-row contract as verify_all (commits per row, skips already-verified);
    used for ingest-time matching of the persons a freshly promoted book created."""
    verifiers = (verifiers if verifiers is not None
                 else default_verifiers(offline=offline, bdrc_over_blmp=bdrc_over_blmp))
    return _run(db, verifiers, list(person_ids),
                partial(verify_person, extensions=extensions,
                        defer_to_joint=defer_to_joint), verbose, "person")


def verify_works(db, work_ids, verifiers=None, *, offline: bool = False,
                 bdrc_over_blmp: bool = False, verbose: bool = False) -> dict:
    """Run the work verify pass over a SPECIFIC id list (ingest-time twin of
    verify_persons)."""
    verifiers = (verifiers if verifiers is not None
                 else default_verifiers(offline=offline, bdrc_over_blmp=bdrc_over_blmp))
    return _run(db, verifiers, list(work_ids), verify_work, verbose, "work")


def verify_promotion(db, result, verifiers=None, *, offline: bool = False,
                     bdrc_over_blmp: bool = False, extensions: bool = False,
                     verbose: bool = False) -> dict:
    """INGEST-TIME authority match: verify the persons + works a just-promoted book
    created, so garbled author names entering at processing time are resolved (or
    surfaced as unmatched in /picker) immediately — not only by a periodic pass.
    Auto-binds HARD exact hits; the rest stay provisional. `result` is any object
    carrying `.created_person_ids` and `.work_ids` (a promote.PromotionResult). Share
    one verifier chain across the call so a batch reuses warm caches/clients."""
    verifiers = (verifiers if verifiers is not None
                 else default_verifiers(offline=offline, bdrc_over_blmp=bdrc_over_blmp))
    out: dict = {}
    pids = list(getattr(result, "created_person_ids", None) or [])
    wids = list(getattr(result, "work_ids", None) or [])
    if pids:
        out["person"] = verify_persons(db, pids, verifiers, extensions=extensions,
                                       verbose=verbose)
    if wids:
        out["work"] = verify_works(db, wids, verifiers, verbose=verbose)
    return out


_MARK = {"matched": "✓", "candidate": "?", "unmatched": "·", "already": "»",
         "deferred": "→"}    # → work-driven joint pass owns it (has a work edge)


def _row_label(db, kind: str, rid: int):
    if kind == "person":
        p = _acc(db).persons.reads.get(rid)
        return (p.primary_name if p else f"#{rid}"), (p.external_id if p else None)
    wf = _acc(db).works.reads.review_fields(rid)
    title = _acc(db).works.reads.representative_title(rid)
    return (title or f"#{rid}"), (wf["canonical_number"] if wf else None)


def _run(db, verifiers, ids, fn, verbose=False, kind="") -> dict:
    import time
    tally = {"matched": 0, "candidate": 0, "unmatched": 0, "already": 0, "deferred": 0}
    total = len(ids)
    if verbose:
        print(f"\n{kind}: {total} to check", flush=True)
    t0 = time.time()
    for i, rid in enumerate(ids, 1):
        try:
            status = fn(db, verifiers, rid, commit=True)
        except AuthorityUnavailable as e:
            # An authority is throttling/unreachable AFTER retries. Stop the pass
            # cleanly rather than recording the rest as false "unmatched" (the
            # bug behind the ~10% rate). Nothing is cached as a miss, so a re-run
            # resumes exactly where this left off.
            tally["stopped_early"] = True
            if verbose:
                print(f"\n  ⚠ stopping: {e}\n  (re-run to resume — nothing was "
                      f"recorded as a miss)", flush=True)
            break
        tally[status] += 1
        if verbose:
            name, ident = _row_label(db, kind, rid)
            print(f"  [{kind} {i}/{total}] {_MARK[status]} {status:9} "
                  f"{(ident or ''):20} {name}", flush=True)
    if verbose:
        print(f"{kind}: {tally['matched']} matched, {tally['unmatched']} unmatched, "
              f"{tally['already']} already — {time.time() - t0:.1f}s", flush=True)
    return tally


# ── Cleanup of pre-guard garbage ──────────────────────────────────────────────
def purge_suspect_matches(db, *, commit: bool = True) -> dict:
    """Clear WRONG-TYPE external ids written before the type guards existed (a
    non-person id on a person, a person id on a work). Leaves good matches intact;
    re-run verify afterwards.

    NOTE: a *shared* external id is **no longer purged**. Under the old BDRC fuzzy
    matcher a shared id signalled a false positive (two distinct rows wrongly glued
    to one id). With precise/manual binding the opposite is true: a shared id means
    the rows are the **same person** → that's a MERGE signal, handled by
    `catalogue/person_dedup.py`, not corruption to null out here. Nulling shared ids
    would actively undo the very links dedup relies on (see authority_dedup_model.md
    §1)."""
    # Clearing a wrong-type bind drops the row back to 'provisional' so the next verify re-checks it;
    # the scheme='other' alias pollution from the bad run is dropped (never a parent's last alias).
    out = _acc(db).health.purge_wrong_type_authority(PERSON_ID_PREFIXES)
    if commit:
        db.commit()
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="External-authority checks over promoted rows. Default: verify "
                    "person/work canonical ids against BDRC/84000. The two opt-in "
                    "external passes run INSTEAD of the default when their flag is set.")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--offline", action="store_true",
                    help="cache-only; never hit the network (safe during OCR)")
    ap.add_argument("--kind", choices=["person", "work", "both"], default="both")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-row progress (just the final summary)")
    ap.add_argument("--purge", action="store_true",
                    help="first clear wrong-type / colliding external ids written "
                         "before the match guards existed, then re-verify")
    ap.add_argument("--bdrc-over-blmp", dest="bdrc_over_blmp", action="store_true",
                    help="resolve PERSONS via the library.bdrc.io ElasticSearch "
                         "(exact-name auto-bind) instead of the BLMP template; works "
                         "still use BLMP/84000. Default OFF (BLMP for both).")
    ap.add_argument("--person-resolution-extensions", dest="extensions",
                    action="store_true",
                    help="name-matching extensions on the person pass: extended "
                         "honorific/office stripping for classical mononyms "
                         "(Acarya Nagarjuna → Nagarjuna; bare Panchen prefix) + "
                         "matching against ALL of a person's aliases, not just "
                         "primary_name. Default OFF.")
    ap.add_argument("--person-work-joint-pass", dest="person_work_joint",
                    action="store_true",
                    help="after the person-only pass, run the work-driven joint "
                         "resolution (catalogue.person_work) over still-provisional "
                         "work-attached persons. Default OFF.")
    # Opt-in external-authority passes (run INSTEAD of the default id verification).
    ap.add_argument("--check_external_work", action="store_true",
                    help="resolve classical work authors/translators (84000 + "
                         "Wikidata consensus) via catalogue.work_authority")
    ap.add_argument("--check_external_edition", action="store_true",
                    help="diff each edition's metadata against Open Library / Google "
                         "Books via catalogue.edition_verify")
    ap.add_argument("--dry-run", action="store_true",
                    help="(--check_external_work) resolve + print, write nothing")
    ap.add_argument("--refresh", action="store_true",
                    help="(--check_external_work) clear the cached authority lookups "
                         "first so every work is re-queried from scratch")
    args = ap.parse_args(argv)
    # init_db (not bare connect) so an older live DB gets the additive migrations
    # this pass depends on — notably person.verification_status. Idempotent.
    db = init_db(args.db)
    db.execute("PRAGMA busy_timeout = 30000")

    # The external passes own different decisions (authorship / edition metadata) and
    # write their own review-item types, so they run as their own jobs — not the
    # default person/work id verification. If either flag is given, run ONLY those.
    if args.check_external_work or args.check_external_edition:
        verbose = not args.quiet
        if args.check_external_work:
            from . import work_authority as work_authority_mod
            if args.refresh:
                n = work_authority_mod.clear_cache(db)
                print(f"refresh: cleared {n} cached lookup(s)", flush=True)
            tally = work_authority_mod.resolve_all_works(
                db, offline=args.offline, limit=args.limit, verbose=verbose,
                dry_run=args.dry_run)
            print("work-authority:", tally)
        if args.check_external_edition:
            from . import edition_verify as edition_verify_mod
            tally = edition_verify_mod.verify_all_editions(
                db, limit=args.limit, verbose=verbose)
            print("edition-verify:", tally)
        return

    kinds = ("person", "work") if args.kind == "both" else (args.kind,)
    if args.purge:
        print("purged suspect matches:", purge_suspect_matches(db), flush=True)
    summary = verify_all(db, kinds=kinds, offline=args.offline,
                         bdrc_over_blmp=args.bdrc_over_blmp,
                         extensions=args.extensions,
                         limit=args.limit, verbose=not args.quiet)
    print("summary:", summary)

    # The work-driven joint pass runs AFTER the name-only person pass (so anything the
    # name pass could bind on its own is already done, and the joint pass only sees the
    # residual provisional, work-attached persons). Opt-in; person-scoped.
    if args.person_work_joint and "person" in kinds:
        from . import person_work
        joint = person_work.resolve_all_person_works(
            db, offline=args.offline, limit=args.limit, verbose=not args.quiet)
        print("person-work-joint:", joint)


if __name__ == "__main__":
    main()
