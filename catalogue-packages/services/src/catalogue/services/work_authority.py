"""WorkAuthorityResolver — definitive authors/translators of classical works.

Distinct from catalogue/verify.py's *work* verifier: that pass attaches a catalog
identifier (Toh / BDRC id) to a work. THIS engine answers the different question
**who wrote / translated the work** — which for a classical Indian or Tibetan text
is a scholarly-canonical fact that belongs to the *work*, not to whatever a
publisher printed on a translated edition's title page. So a work's author should
come from a canon catalog, never from the book.

Modularity (the whole point):
  - A *source* implements `WorkAuthoritySource.lookup(title, …) -> [WorkAuthorityRecord]`.
  - Register more (BDRC, Pandit, Wikidata, …) with `@register_source` or just pass
    your own `sources=[…]` to the resolver. The consensus engine is source-agnostic.
  - Each source OWNS its precision and degrades to `[]` (no snapshot / no network /
    parse drift) rather than raising — the resolver stays callable with nothing
    configured.

Confidence policy (heeds the M4 finding that naive single-authority name search is
unreliable — see memory `external-matching-strategy`):
  - **verified**  — ≥2 sources agree on an author (fold-key), OR a single source
                    returns a strong-title canon catalog hit (Toh/number) with an
                    author. Safe to auto-apply.
  - **candidate** — exactly one source, decent title match. Queue for one-click review.
  - **none**      — nothing matched the title well enough.

Nothing here writes to the DB except the explicit `apply_to_work` helper.
"""
from __future__ import annotations

import abc
import difflib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Optional

from catalogue.db_store import fold_key, nfc, search_normalize
from .work_canonical_resolver import cached_rows
from catalogue.db_store import default_db_path


def _acc(db):
    """A system Access over this connection (engine-routed work/edition reads + writes).
    The review_queue plumbing and the resolver_cache stay raw — separate concerns."""
    from catalogue.access_api import system_conn
    return system_conn(db)

# Title-match gates. A record whose title can't be matched to the query above
# TITLE_MATCH_MIN is dropped (it's about a different text). STRONG_TITLE is the
# bar at which a SINGLE catalog hit is trustworthy enough to auto-verify.
TITLE_MATCH_MIN = 0.80
STRONG_TITLE = 0.92


def _similar(a: str, b: str) -> float:
    """Fold-key Ratcliff/Obershelp similarity in [0,1]. Folding first makes the
    score diacritic- and digraph-insensitive (so 'Bodhicaryavatara' scores 1.0
    against 'Bodhicaryāvatāra'). Empty vs anything → 0."""
    ka, kb = fold_key(a or ""), fold_key(b or "")
    if not ka or not kb:
        return 0.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


# ── Distinctive-word containment (the "native title buried in a verbose title" gate)─
# Whole-string `_similar` is length-sensitive: a stored title like "Nagarjuna's Middle
# Way: The Mulamadhyamakakarika" scores only ~0.6 against the catalogue's bare
# "Mūlamadhyamakakārikā", even though the work's real (Sanskrit) title sits right
# inside it. This containment check rescues those — but ONLY on a DISTINCTIVE shared
# word, so it never fires on a generic-title collision ("Buddhist Ethics" matching two
# unrelated books). It is a CANDIDATE-tier signal: it lets a record clear the gate, but
# never reaches STRONG_TITLE, so it can't auto-verify (see `resolve`).
_DISTINCTIVE_LEN = 12      # a shared word this long is almost surely a real title word

# Long-ish words that nonetheless recur across unrelated Buddhist titles — a shared one
# is NOT proof of "same work", so they are excluded from the distinctive set.
_GENERIC_TITLE_WORDS = frozenset({
    "introduction", "enlightenment", "consciousness", "instructions", "commentaries",
    "philosophy", "compassion", "bodhisattva", "meditation", "realization",
    "translation", "explanation", "reflections", "teachings", "buddhism", "buddhist",
})


def _title_words(t: str) -> set:
    """Significant words of a title, diacritics stripped FIRST so a native title isn't
    shattered (`search_normalize`: NFKD + lowercase, keeps word boundaries). Drops
    short tokens; keeps the surface words (genericness is judged separately)."""
    return {w for w in re.findall(r"[a-z0-9]+", search_normalize(t or ""))
            if len(w) >= 3}


def _has_distinctive(shared: set) -> bool:
    """A shared word counts as distinctive (enough to assert 'same work' on its own)
    if it is long AND not a recurring generic Buddhist term."""
    return any(len(w) >= _DISTINCTIVE_LEN and w not in _GENERIC_TITLE_WORDS
               for w in shared)


def _titles_contained(a: str, b: str) -> bool:
    """True if two titles share a DISTINCTIVE word — symmetric, so it doesn't matter
    which side is the longer/verbose one (our long English title vs the catalogue's
    bare native title, OR our bare title vs the catalogue's long formal one). Generic
    overlaps alone ("buddhist", "ethics", "middle", "way") never qualify."""
    return _has_distinctive(_title_words(a) & _title_words(b))


def native_title_terms(title: str) -> list:
    """Extra SEARCH terms that isolate a title's distinctive (likely native: Sanskrit/
    Tibetan) words — "strip the English", applied at search time. An authority can't
    return a work whose stored title buries the native title in English framing
    ("Nagarjuna's Middle Way: The Mulamadhyamakakarika") unless we also search the bare
    distinctive token ("mulamadhyamakakarika"). Returns [] when nothing distinctive
    remains (a purely English/generic title) — a no-op on modern trade titles."""
    words = [w for w in re.findall(r"[a-z0-9]+", search_normalize(title or ""))
             if len(w) >= _DISTINCTIVE_LEN and w not in _GENERIC_TITLE_WORDS]
    return _dedup(words)


# Romanized Tibetan is written in syllables; authorities index it either spaced
# ("lam rim chen mo", Wylie) or run-together ("Lamrim Chenmo", phonetic). We can't
# detect language or syllabify reliably, so we don't try — we just send BOTH a
# space-collapsed and a (crude) re-spaced variant as EXTRA searches and let the
# authority arbitrate. Guards keep this cheap and safe:
#   • a title with PUNCTUATION (, : ; — / ' etc. or sentence structure) is English
#     prose → no variant (your rule: punctuation ⇒ English, no spacing);
#   • only applied to SHORT titles (≤4 word-ish tokens) — long titles are English.
_PUNCT = re.compile(r"[.,:;!?/()\[\]\"'’“”—–]|（|）")
_WS = re.compile(r"\s+")


def _maybe_tibetan(title: str) -> bool:
    """A weak 'might be romanized Tibetan' signal: no punctuation, ≤4 tokens, all
    ASCII letters (no diacritics → not IAST Sanskrit), no obvious English function
    words. Deliberately conservative — false negatives are fine (we just skip the
    extra search), false positives only cost a wasted lookup."""
    t = (title or "").strip()
    if not t or _PUNCT.search(t):
        return False
    toks = _WS.split(t)
    if not (1 <= len(toks) <= 4):
        return False
    if any(not tok.isascii() for tok in toks):     # diacritics → IAST Sanskrit/other
        return False
    english = {"the", "of", "a", "an", "to", "and", "on", "in", "for", "how",
               "stages", "path", "mind", "guide", "history", "wisdom"}
    return not any(tok.lower() in english for tok in toks)


def tibetan_spacing_variants(title: str) -> list:
    """Extra search strings for a possibly-Tibetan `title`: the space-collapsed
    form and a re-spaced form. Returns [] for anything that looks English (so it's
    a no-op on normal titles). The re-spaced form is crude (split a run-together
    token on a fixed syllable cue list) — it won't always be right, but Wikidata
    fuzzy-matches, and a wrong variant simply finds nothing."""
    if not _maybe_tibetan(title):
        return []
    out, seen = [], {fold_key(title)}
    variants = [title.replace(" ", "")]        # space-collapsed whole title
    # Re-spaced WHOLE title: split each run-together token (≥6 chars) on common
    # Tibetan syllable onsets, then rejoin — so "Lamrim Chenmo" → "lam rim chen mo",
    # not fragments. Crude/best-effort; a wrong split just finds nothing.
    cue = re.compile(r"(?<=.)(rim|chen|chos|pa|po|ba|mo|rab|gsal|sgrub|grub|dpal|"
                     r"rnam|bzang|ling|tsang|dorje|tshe)")
    respaced_tokens = []
    changed = False
    for tok in _WS.split(title):
        s = cue.sub(r" \1", tok.lower()) if len(tok) >= 6 else tok.lower()
        if s != tok.lower():
            changed = True
        respaced_tokens.append(s)
    if changed:
        variants.append(_WS.sub(" ", " ".join(respaced_tokens)).strip())
    for v in variants:
        k = fold_key(v)
        if v and k and k not in seen:
            seen.add(k)
            out.append(v)
    return out


# ── Records ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WorkAuthorityRecord:
    """One source's view of a work. `authors`/`translators` are canonical names
    as that authority spells them. `score` is the title-match confidence the
    resolver fills in (0 from the source itself).

    `author_ids` is the SOURCE-NEUTRAL identity carrier the person+work joint
    resolver reads: a tuple of `{"name","external_id","extra_ids"}` dicts, one per
    author the source could pin to a stable id (e.g. Wikidata keeps the P50 author's
    Q-id + its BDRC/DILA/VIAF cross_ids). Sources that only know names (84000, BDRC)
    leave it empty — those persons then resolve by name via the normal path. Kept
    PARALLEL to `authors` (names) so existing callers/apply_to_work are unaffected."""
    source: str
    title: str = ""
    external_id: Optional[str] = None
    canonical_system: Optional[str] = None   # 'toh' | 'bdrc' | 'pandit' | …
    canonical_number: Optional[str] = None
    authors: tuple = ()
    translators: tuple = ()
    score: float = 0.0
    dates: dict = field(default_factory=dict)   # name -> "1357-1419" (optional)
    extra: dict = field(default_factory=dict)
    author_ids: tuple = ()                      # [{"name","external_id","extra_ids"}]
    # The matched item's OWN label/alias set (native script + transliterations) as
    # the source spells them. The resolver's title-gate scores the query against
    # `title` AND these — so a cross-script/variant hit the authority's search
    # already validated (e.g. found "lam rim chen mo" → an item whose English label
    # is "The Great Treatise…") isn't dropped just because the English label differs.
    aliases: tuple = ()

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["authors"] = list(self.authors)
        d["translators"] = list(self.translators)
        d["author_ids"] = [dict(a) for a in self.author_ids]
        d["aliases"] = list(self.aliases)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WorkAuthorityRecord":
        d = dict(d)
        d["authors"] = tuple(d.get("authors") or ())
        d["translators"] = tuple(d.get("translators") or ())
        d["author_ids"] = tuple(d.get("author_ids") or ())
        d["aliases"] = tuple(d.get("aliases") or ())
        return cls(**d)


@dataclass
class WorkAuthorityConsensus:
    verdict: str                       # 'verified' | 'candidate' | 'none'
    authors: list = field(default_factory=list)
    translators: list = field(default_factory=list)
    canonical_system: Optional[str] = None
    canonical_number: Optional[str] = None
    external_ids: dict = field(default_factory=dict)   # source -> WORK id
    dates: dict = field(default_factory=dict)          # name -> dates
    agreement: int = 0                 # #sources backing the chosen author set
    records: list = field(default_factory=list)        # raw matched records (audit)
    # Source-neutral per-author identities ({"name","external_id","extra_ids"}),
    # unioned across the matched records — what the person+work joint resolver uses
    # to bind a person to an authority id. Empty when no source pinned an id.
    author_ids: list = field(default_factory=list)


# ── Source plug-in surface ──────────────────────────────────────────────────────
class WorkAuthoritySource(abc.ABC):
    name: str = "source"
    version: int = 1                   # bump to invalidate this source's cache

    @abc.abstractmethod
    def lookup(self, title: str, *, language: Optional[str] = None,
               aliases: tuple = ()) -> list[WorkAuthorityRecord]:
        """Return candidate works for `title`. MUST NOT raise on network/parse
        failure — return [] (the resolver treats sources as best-effort)."""
        raise NotImplementedError

    def authors_for(self, number: str, *, title: Optional[str] = None,
                    language: Optional[str] = None) -> list[dict]:
        """The AUTHORS of the work THIS source identifies by `number` (its external id /
        toh# / Q-id). Returns the source-neutral identity carrier
        `[{"name", "external_id"?, "extra_ids"?}]` — the same shape as `author_ids` — so
        a caller can link them to a `work` (resolving each name/id to a `person`). `title`
        is an optional hint for sources that look up by title. MUST NOT raise: best-effort,
        return [] on failure OR when the source carries no author data (the default)."""
        return []


_SOURCES: dict = {}


def register_source(cls):
    """Class decorator: make a source constructible by name via `build_sources`."""
    _SOURCES[cls.name] = cls
    return cls


def build_sources(names=None, **kwargs) -> list:
    """Instantiate registered sources by name (default: all registered)."""
    names = list(_SOURCES) if names is None else names
    return [_SOURCES[n](**kwargs) for n in names]


# canonical_system (as stored on a work / carried by the picker) → authority source name.
_SYSTEM_SOURCE = {"toh": "84000", "84000": "84000", "84k": "84000",
                  "bdrc": "bdrc", "bdr": "bdrc", "wikidata": "wikidata"}


def authors_for(system: str, number: str, *, title: "str | None" = None,
                sources: "dict | None" = None) -> list[dict]:
    """Authors of the work identified by (`system`, `number`) — dispatched to that
    authority's OWN author lookup (each source implements `authors_for`). Returns the
    source-neutral `[{"name", "external_id"?, "extra_ids"?}]`. Best-effort: unknown
    system or any failure → []. Pass `sources={name: instance}` to inject (tests)."""
    name = _SYSTEM_SOURCE.get((system or "").strip().lower())
    if not name:
        return []
    try:
        src = (sources or {}).get(name)
        if src is None:
            src = build_sources([name])[0]
        return src.authors_for(number, title=title) or []
    except Exception:
        return []


# ── The engine ──────────────────────────────────────────────────────────────────
class WorkAuthorityResolver:
    """Query every source for a work title, score each result's title against the
    query+aliases, and fuse the survivors into one consensus author/translator set.

    `db` is used only for the per-source result cache (`resolver_cache`); pass
    `db=None` for a pure, network-only run (tests, offline)."""

    def __init__(self, sources=None, *, db=None, title_min: float = TITLE_MATCH_MIN,
                 offline: bool = False):
        self.sources = list(sources) if sources is not None else default_sources(
            offline=offline)
        self.db = db
        self.title_min = title_min
        self.offline = offline      # cache-only: read warm cache, never network

    def _cached_lookup(self, src: WorkAuthoritySource, title: str,
                       language, aliases) -> list[WorkAuthorityRecord]:
        key = f"{language or ''}|{title}|{'|'.join(aliases)}"
        rows = cached_rows(
            self.db, namespace="work_authority", source=src.name,
            query=key, version=src.version, write=not self.offline,
            compute=lambda: [r.to_dict() for r in
                             src.lookup(title, language=language, aliases=tuple(aliases))],
        )
        return [WorkAuthorityRecord.from_dict(r) for r in rows]

    def resolve(self, title: str, *, language: Optional[str] = None,
                aliases=()) -> WorkAuthorityConsensus:
        title = (title or "").strip()
        aliases = tuple(a.strip() for a in (aliases or ()) if a and a.strip())
        if not title and not aliases:
            return WorkAuthorityConsensus("none")
        # Isolated native-title terms ("strip the English") let an authority FIND a
        # work whose stored title buries the native title in English framing; they ride
        # along as extra search aliases AND as scoring queries.
        native = native_title_terms(title)
        search_aliases = tuple(_dedup([*aliases, *native]))
        # Score against the same term set the sources searched — the query, its
        # aliases, the native terms, AND the Tibetan spacing variants — so a hit found
        # via a variant (e.g. "lam rim chen mo") can still clear the title-gate.
        queries = _dedup([title, *aliases, *native, *tibetan_spacing_variants(title)])

        scored: list[WorkAuthorityRecord] = []
        for src in self.sources:
            for r in self._cached_lookup(src, title, language, search_aliases):
                # The record's own label/alias set is also fair game: the authority
                # returned it because one of those matched our query.
                cands = [r.title, *r.aliases]
                s = max((_similar(c, q) for c in cands for q in queries),
                        default=0.0)
                # Containment fallback: a verbose title that *contains* the work's
                # distinctive native title (e.g. "…Mulamadhyamakakarika" vs the bare
                # "Mūlamadhyamakakārikā") scores low full-string but is the same work.
                # Lift it to exactly the gate (NOT to STRONG_TITLE) so it survives as a
                # CANDIDATE for review and can never silently auto-verify.
                if s < self.title_min and any(
                        _titles_contained(c, q) for c in cands for q in queries):
                    s = self.title_min
                scored.append(replace(r, score=max(r.score, s)))
        matched = [r for r in scored if r.score >= self.title_min]
        return _consensus(matched)


def _dedup(items) -> list:
    out, seen = [], set()
    for it in items:
        k = fold_key(it)
        if it and k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _consensus(matched: list) -> WorkAuthorityConsensus:
    if not matched:
        return WorkAuthorityConsensus("none")

    # Group author names across sources by fold-key; remember a display spelling
    # and any dates the sources carried.
    def group(field_name):
        by_src = defaultdict(set)
        disp, dates = {}, {}
        for r in matched:
            for name in getattr(r, field_name):
                k = fold_key(name)
                if not k:
                    continue
                by_src[k].add(r.source)
                disp.setdefault(k, name)
                if r.dates.get(name):
                    dates.setdefault(k, r.dates[name])
        return by_src, disp, dates

    a_by_src, a_disp, a_dates = group("authors")
    t_by_src, t_disp, t_dates = group("translators")

    agreed_authors = [k for k, s in a_by_src.items() if len(s) >= 2]
    agreed_translators = [k for k, s in t_by_src.items() if len(s) >= 2]

    external_ids = {r.source: r.external_id for r in matched if r.external_id}
    csys, cnum = _pick_canonical(matched)

    # Union the source-neutral author identities across matched records, deduped by
    # external_id (the joint resolver reads these to bind a person to an authority).
    author_ids, _seen_aid = [], set()
    for r in matched:
        for a in (r.author_ids or ()):
            key = a.get("external_id")
            if key and key not in _seen_aid:
                _seen_aid.add(key)
                author_ids.append(dict(a))

    # A canon catalog hit whose title is a strong match is itself authoritative even
    # from ONE source — but ONLY a true scholarly authorship catalogue counts, i.e.
    # 84000/Toh. A Wikidata (or BDRC) hit also carries an id in `canonical_number`,
    # but that is an identity hub, not an authorship catalogue: a lone same-title
    # Wikidata hit on a generic English title ("Buddhist Ethics", "Appearance and
    # Reality", "Mirror of Wisdom") collides with an unrelated work and must NOT
    # auto-verify. So a single Wikidata hit stays a *candidate* (the docstring's rule,
    # now enforced); verification needs Toh, or a second source agreeing on the author.
    strong_catalog = any(
        r.canonical_system == "toh" and r.canonical_number and r.score >= STRONG_TITLE
        for r in matched
    )

    if agreed_authors:
        authors = [a_disp[k] for k in agreed_authors]
        dates = {a_disp[k]: a_dates[k] for k in agreed_authors if k in a_dates}
        verdict, agreement = "verified", max(len(a_by_src[k]) for k in agreed_authors)
    else:
        best = max((r for r in matched if r.authors),
                   key=lambda r: r.score, default=None)
        authors = list(best.authors) if best else []
        dates = dict(best.dates) if best else {}
        if authors and strong_catalog:
            verdict, agreement = "verified", 1
        elif authors or matched:
            verdict, agreement = "candidate", (1 if authors else 0)
        else:
            verdict, agreement = "none", 0

    # Translators: take the cross-source agreed set; otherwise the chosen/best
    # record's translators (as candidates riding along with the author verdict).
    if agreed_translators:
        translators = [t_disp[k] for k in agreed_translators]
        dates.update({t_disp[k]: t_dates[k] for k in agreed_translators if k in t_dates})
    else:
        tbest = max((r for r in matched if r.translators),
                    key=lambda r: r.score, default=None)
        translators = list(tbest.translators) if tbest else []

    return WorkAuthorityConsensus(
        verdict=verdict, authors=authors, translators=translators,
        canonical_system=csys, canonical_number=cnum,
        external_ids=external_ids, dates=dates, agreement=agreement,
        records=matched, author_ids=author_ids,
    )


def _pick_canonical(matched: list):
    """The canonical id written to `work.canonical_number` must be a real canon
    catalogue number — a Toh (84000) number, or a BDRC work id — NOT a Wikidata
    Q-id (that is an identity hub, kept in `external_ids` for cross-linking, never
    promoted to canonical_number). Prefer Toh, then BDRC, by highest title score."""
    pool = [r for r in matched
            if r.canonical_number and r.canonical_system in ("toh", "bdrc")]
    toh = [r for r in pool if r.canonical_system == "toh"]
    pool = toh or pool
    if not pool:
        return None, None
    best = max(pool, key=lambda r: r.score)
    return best.canonical_system, best.canonical_number


# ── Applying a consensus to the catalogue ───────────────────────────────────────
def apply_to_work(db, work_id: int, consensus: WorkAuthorityConsensus, *,
                  only_if_verified: bool = True, commit: bool = True) -> dict:
    """Write a consensus onto an existing work: fill canonical_system/number when
    missing, and link author/translator persons via `work_contributor` (dedup +
    person creation reuse promote.get_or_create_person, so spelling variants
    collapse onto existing people). Person external-id reconciliation stays the
    job of catalogue/verify.py's person pass — kept separate on purpose.

    Idempotent: existing contributor rows are INSERT OR IGNORE'd; an already-set
    canonical id is left untouched. Returns a small summary dict."""
    from .promote import get_or_create_person   # local import avoids a cycle
    from catalogue.db_store import add_alias

    out = {"work_id": work_id, "verdict": consensus.verdict,
           "authors_linked": 0, "translators_linked": 0,
           "canonical_set": False, "skipped": False}
    if only_if_verified and consensus.verdict != "verified":
        out["skipped"] = True
        return out
    fields = _acc(db).works.reads.review_fields(work_id)
    if fields is None:
        out["skipped"] = True
        return out

    # Canonical id (only if the work doesn't already have one).
    if consensus.canonical_number and not fields["canonical_number"]:
        _acc(db).works.writes.set_scalars(work_id, {
            "canonical_system": consensus.canonical_system,
            "canonical_number": consensus.canonical_number})
        out["canonical_set"] = True

    def link(name, role):
        from catalogue.db_store import contributor_store as cs
        clean = nfc(name).strip()
        if not clean:
            return False
        # Carry dates into the name so get_or_create_person splits them out.
        d = consensus.dates.get(name)
        pid, _created = get_or_create_person(
            db, f"{clean} ({d})" if d else clean, role)
        if role == "translator":
            # Translator lives on the edition(s) this work appears in.
            for ed in _acc(db).editions.reads.by_work(work_id):
                cs.add_edition_translator(db, ed.id, pid)
        else:
            cs.add_work_author(db, work_id, pid, role)
        return True

    for a in consensus.authors:
        if link(a, "author"):
            out["authors_linked"] += 1
    for t in consensus.translators:
        if link(t, "translator"):
            out["translators_linked"] += 1

    if commit:
        db.commit()
    return out


# ── Walk over existing works (the batch form of the promotion hook) ──────────────
# Mirrors catalogue/verify.py:verify_all. Worklist = works with NO author
# contributor yet (the question this engine answers is "who wrote it"). For each:
# resolve title+aliases → 'verified' is auto-applied, 'candidate' is queued for a
# one-click review, 'none' is skipped. Idempotent: applying an author removes the
# work from the worklist; candidates are not re-queued while one is pending.
def _work_query(db, wid: int) -> tuple:
    """(title, aliases, language) for a work — title is the first alias, aliases
    are the rest (other scripts/spellings improve the title match)."""
    acc = _acc(db)
    names = [t for t, _scheme in acc.works.reads.aliases(wid) if t]
    lang = (acc.works.reads.review_fields(wid) or {}).get("original_language")
    return (names[0] if names else ""), tuple(names[1:]), lang


def _work_filename(db, wid: int) -> str:
    """Basename of a file backing this work (via its edition's holding) — for CLI
    output, so a proposed match can be traced back to the physical book."""
    return _acc(db).works.reads.backing_filename(wid)


def _has_author(db, wid: int) -> bool:
    return _acc(db).works.reads.has_author_role(wid)


def _candidate_already_queued(db, wid: int) -> bool:
    return _acc(db).review.reads.exists_pending(
        "work_authorship", f'%"work_id": {wid}%')


def _queue_candidate(db, wid: int, c: WorkAuthorityConsensus) -> None:
    _acc(db).review.writes.enqueue("work_authorship", {
        "work_id": wid,
        "verdict": c.verdict,
        "authors": list(c.authors),
        "translators": list(c.translators),
        "canonical_system": c.canonical_system,
        "canonical_number": c.canonical_number,
        "external_ids": c.external_ids,
        "agreement": c.agreement,
        "sources": sorted({r.source for r in c.records}),
    })


def accept_work_authorship(db, item_id: int, *, commit: bool = True) -> bool:
    """Apply a queued `work_authorship` candidate (the /review accept action):
    link its authors/translators (and canonical id) onto the work via
    `apply_to_work`, bypassing the verified-only gate since a human is confirming.
    Marks the item resolved. False if missing/not pending."""
    row = _acc(db).review.reads.get_typed(item_id, "work_authorship")
    if not row or row[1] != "pending":
        return False
    p = json.loads(row[0])
    wid = p.get("work_id")
    if not wid:
        return False
    consensus = WorkAuthorityConsensus(
        verdict="verified",                       # human-confirmed → force-apply
        authors=list(p.get("authors") or []),
        translators=list(p.get("translators") or []),
        canonical_system=p.get("canonical_system"),
        canonical_number=p.get("canonical_number"),
        external_ids=dict(p.get("external_ids") or {}))
    apply_to_work(db, wid, consensus, only_if_verified=False, commit=False)
    _acc(db).review.writes.resolve(item_id)
    if commit:
        db.commit()
    return True


def reject_work_authorship(db, item_id: int, *, commit: bool = True) -> bool:
    """Reject a queued `work_authorship` candidate without applying it."""
    if _acc(db).review.reads.status_of(item_id, "work_authorship") != "pending":
        return False
    _acc(db).review.writes.reject(item_id)
    if commit:
        db.commit()
    return True


def resolve_work_authorship(db, resolver, wid: int, *, commit: bool = True) -> str:
    """matched | candidate | unmatched | already. 'verified' consensus → applied;
    'candidate' → queued (once); 'none' → unmatched. Works that already have an
    author are 'already'."""
    if _has_author(db, wid):
        return "already"
    title, aliases, language = _work_query(db, wid)
    if not title and not aliases:
        return "unmatched"
    c = resolver.resolve(title, language=language, aliases=aliases)
    if c.verdict == "verified":
        apply_to_work(db, wid, c, only_if_verified=True, commit=commit)
        return "matched"
    if c.verdict == "candidate":
        if not _candidate_already_queued(db, wid):
            _queue_candidate(db, wid, c)
            if commit:
                db.commit()
        return "candidate"
    return "unmatched"


def _dry_run_work(db, resolver, wid: int):
    """Resolve a work WITHOUT writing anything — returns (status, consensus|None).
    'already' for works that already have an author (skipped, like the live walk)."""
    if _has_author(db, wid):
        return "already", None
    title, aliases, language = _work_query(db, wid)
    if not title and not aliases:
        return "unmatched", None
    c = resolver.resolve(title, language=language, aliases=aliases)
    status = {"verified": "matched", "candidate": "candidate",
              "none": "unmatched"}[c.verdict]
    return status, c


def clear_cache(db, sources=None, *, commit: bool = True) -> int:
    """Drop this resolver's memoized authority lookups from `resolver_cache` so the
    next run RE-QUERIES from scratch instead of replaying cached results. Scoped to
    the work-authority source names (default 84000 + wikidata) so it does NOT clear
    the person/work verify chain's BDRC cache. Returns rows deleted."""
    names = sources if sources is not None else ["84000", "wikidata"]
    n = _acc(db).resolver_cache.delete_sources(names)
    if commit:
        db.commit()
    return n


def resolve_all_works(db, resolver=None, *, offline: bool = False,
                      limit: int | None = None, verbose: bool = False,
                      dry_run: bool = False) -> dict:
    """Walk every author-less work through the resolver. Commits per row
    (resumable). Returns a status tally.

    `dry_run=True` resolves and PRINTS each match (verdict + proposed
    authors/translators/canonical/sources) but writes NOTHING to the catalogue —
    no contributors linked, no canonical set, no candidate queued (the resolver's
    authority cache may still warm, which is harmless)."""
    resolver = resolver or WorkAuthorityResolver(db=db, offline=offline)
    ids = _acc(db).works.reads.author_less_ids(limit)
    tally = {"matched": 0, "candidate": 0, "unmatched": 0, "already": 0}
    if verbose:
        srcs = ", ".join(s.name for s in resolver.sources)
        print(f"Work-authorship via [{srcs}]{' (offline)' if offline else ''}"
              f"{' [DRY-RUN — no writes]' if dry_run else ''} — "
              f"{len(ids)} author-less work(s)…", flush=True)
    mark = {"matched": "✓", "candidate": "?", "unmatched": "·", "already": "»"}
    for i, wid in enumerate(ids, 1):
        if dry_run:
            status, c = _dry_run_work(db, resolver, wid)
        else:
            status = resolve_work_authorship(db, resolver, wid, commit=True)
            c = None
        tally[status] += 1
        title, _, _ = _work_query(db, wid)
        fname = _work_filename(db, wid)
        if dry_run and c is not None and status in ("matched", "candidate"):
            # Always surface a proposed match in a dry-run, with its evidence.
            srcs = sorted({r.source for r in c.records})
            print(f"  [{i}/{len(ids)}] {mark[status]} {status:9} {title[:64]!r}",
                  flush=True)
            print(f"        file: {fname or '(no file)'}", flush=True)
            print(f"        verdict={c.verdict} agreement={c.agreement} "
                  f"sources={srcs} canonical={c.canonical_system}/{c.canonical_number}",
                  flush=True)
            print(f"        authors={c.authors}  translators={c.translators}",
                  flush=True)
            # The matched catalogue record(s) — the authority's own title + its
            # native-script / transliteration aliases (the Sanskrit/Tibetan text).
            for r in c.records:
                alis = "  aliases=" + str(list(r.aliases)[:4]) if r.aliases else ""
                print(f"        ↳ matched {r.source}: {r.title!r}"
                      f"{alis}", flush=True)
        elif verbose:
            print(f"  [{i}/{len(ids)}] {mark[status]} {status:9} {title[:50]}  "
                  f"⟵ {fname}", flush=True)
    if verbose or dry_run:
        print(f"done: {tally}", flush=True)
    return tally


def main(argv=None) -> None:
    import argparse
    from catalogue.db_store import init_db
    ap = argparse.ArgumentParser(
        description="Resolve classical work authors/translators against 84000 + "
                    "Wikidata (consensus). Auto-applies verified; queues candidates.")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--offline", action="store_true",
                    help="cache-only; never hit the network (safe during OCR)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve + print what matched; write NOTHING to the catalogue")
    ap.add_argument("--refresh", action="store_true",
                    help="clear the cached authority lookups first, so every work is "
                         "RE-QUERIED from scratch (use after changing match logic)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-row progress (just the final tally)")
    args = ap.parse_args(argv)
    db = init_db(args.db)        # additive migrations; idempotent
    db.execute("PRAGMA busy_timeout = 30000")
    if args.refresh:
        n = clear_cache(db)
        print(f"refresh: cleared {n} cached lookup(s) — re-querying from scratch",
              flush=True)
    tally = resolve_all_works(db, offline=args.offline, limit=args.limit,
                              verbose=not args.quiet, dry_run=args.dry_run)
    print("summary:", tally)


# ── Built-in sources ────────────────────────────────────────────────────────────
@register_source
class EightyFourThousandSource(WorkAuthoritySource):
    """84000 (Translating the Words of the Buddha) — reads the *local* TEI
    snapshot (catalogue.work_canonical_resolver.EightyFourThousandIndex). Best authority for
    Kangyur/Tengyur works: gives the Toh number plus the catalogued author and
    translators. No-ops to [] when the opt-in snapshot is absent."""
    name = "84000"
    version = 1

    def __init__(self, index=None):
        from .work_canonical_resolver import EightyFourThousandIndex
        self.index = index or EightyFourThousandIndex()

    def lookup(self, title, *, language=None, aliases=()):
        try:
            if not self.index.available():
                return []
            hit = self.index.by_english(title)
            for alias in aliases:
                if hit:
                    break
                hit = self.index.by_english(alias)
            if not hit:
                return []
            authors, translators = _read_84000_contributors(self.index, hit)
            return [WorkAuthorityRecord(
                source=self.name,
                title=hit.get("english") or title,
                canonical_system="toh",
                canonical_number=hit.get("toh"),
                authors=tuple(authors),
                translators=tuple(translators),
                extra={"file": hit.get("file")},
            )]
        except Exception:
            return []

    def authors_for(self, number, *, title=None, language=None):
        """84000 catalogues the author per toh#: resolve the TEI hit by toh (then by the
        English title as a fallback) and read its `<author>` headings."""
        try:
            if not self.index.available():
                return []
            hit = (self.index.by_toh(number) if number else None) \
                or (self.index.by_english(title) if title else None)
            if not hit:
                return []
            authors, _tr = _read_84000_contributors(self.index, hit)
            return [{"name": a} for a in authors if a]
        except Exception:
            return []


def _read_84000_contributors(index, hit) -> tuple:
    """Parse `<author>` / translator `<respStmt>` from the TEI file a 84000 index
    hit points at. Defensive: any shape drift → ([], []). Pure-ish (only reads
    the snapshot file), so it's unit-testable with a crafted TEI."""
    import xml.etree.ElementTree as ET
    from .work_canonical_resolver import _TEI_NS
    rel = (hit or {}).get("file")
    if not rel:
        return [], []
    path = index.snapshot_dir / rel
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return [], []
    authors, translators = [], []
    for a in root.iterfind(".//tei:titleStmt/tei:author", _TEI_NS):
        t = "".join(a.itertext()).strip()
        if t:
            authors.append(t)
    for rs in root.iterfind(".//tei:titleStmt/tei:respStmt", _TEI_NS):
        resp = "".join(
            (r.text or "") for r in rs.iterfind("tei:resp", _TEI_NS)).lower()
        names = [" ".join("".join(n.itertext()).split())
                 for n in rs.iterfind("tei:name", _TEI_NS)]
        names = [n for n in names if n]
        if "translat" in resp:
            translators.extend(names)
        elif "author" in resp and not authors:
            authors.extend(names)
    return _dedup(authors), _dedup(translators)


@register_source
class BdrcWorkSource(WorkAuthoritySource):
    """BDRC work lookup via the `BLMP` name client (catalogue/bdrc.py). Contributes
    the canon **catalog id + title** for a matched work; author extraction from
    BDRC's graph (the work→creator relation) is a follow-up query and TODO, so on
    its own this source yields a catalog-id candidate and relies on the
    cross-source agreement rule to supply a verified author. Type-filters to work
    ids (BLMP returns hits of any resource type). Network-guarded → [] on failure."""
    name = "bdrc"
    version = 1

    def __init__(self, client=None):
        from .bdrc import BDRCClient
        self.client = client or BDRCClient()

    def lookup(self, title, *, language=None, aliases=()):
        from .bdrc import bdrc_lang_order, is_work_id
        try:
            for lang in bdrc_lang_order(language):
                recs = []
                for bid, label in self.client.lookup(title, lang=lang):
                    if is_work_id(bid):
                        recs.append(WorkAuthorityRecord(
                            source=self.name, title=label, external_id=bid,
                            canonical_system="bdrc", canonical_number=bid))
                if recs:
                    return recs
            return []
        except Exception:
            return []

    def authors_for(self, number, *, title=None, language=None):
        """BDRC carries the work→creator relation: `live_work_matches` already resolves a
        title to disambiguated WA records each bearing `authors` (Wylie name strings), so
        find the picked `number` among them and return its authors. Network-guarded → []."""
        from .bdrc import live_work_matches
        try:
            if not title:
                return []
            for m in live_work_matches(title, limit=12):
                if m.get("number") == number:
                    return [{"name": n} for n in (m.get("authors") or []) if n]
            return []
        except Exception:
            return []


@register_source
class WikidataWorkSource(WorkAuthoritySource):
    """Wikidata — search the title, keep items that are a written work (or carry a
    P50 author), and resolve each author Q-id to its multilingual label (native
    Tibetan/Sanskrit + transliterations come along as aliases). The single most
    useful author source for both classical and modern works, and it cross-links
    BDRC/VIAF ids for free. Network-guarded → [] on failure; `offline=True` skips
    the network entirely (safe alongside OCR)."""
    name = "wikidata"
    version = 2          # v2: record carries the matched item's aliases (title-gate)

    def __init__(self, client=None, *, offline: bool = False, max_candidates: int = 3):
        from .wikidata import WikidataClient
        self.client = client or WikidataClient()
        self.offline = offline
        self.max_candidates = max_candidates

    def lookup(self, title, *, language=None, aliases=()):
        from . import wikidata as W
        if self.offline:
            return []
        try:
            out = []
            # Search the title + its aliases, plus syllable-spacing variants for a
            # possibly-Tibetan title (a no-op for English titles). Dedupe candidate
            # Q-ids across all the searches so one work isn't processed twice.
            terms, seen_terms = [], set()
            for t in [title, *aliases, *tibetan_spacing_variants(title)]:
                k = fold_key(t or "")
                if t and k and k not in seen_terms:
                    seen_terms.add(k)
                    terms.append(t)
            candidates, seen_qid = [], set()
            for term in terms:
                for qid, label, desc in self.client.search(term)[:self.max_candidates]:
                    if qid not in seen_qid:
                        seen_qid.add(qid)
                        candidates.append((qid, label, desc))
            for qid, _label, _desc in candidates:
                ent = self.client.entity(qid)
                if not ent or not W.is_work(ent):
                    continue
                wname, waliases = W.labels_and_aliases(ent)
                author_ids = self._author_ids(ent)
                if not author_ids:
                    continue        # a work with no resolvable author adds nothing
                out.append(WorkAuthorityRecord(
                    source=self.name, title=wname or title, external_id=qid,
                    canonical_system="wikidata", canonical_number=qid,
                    authors=tuple(a["name"] for a in author_ids),
                    author_ids=tuple(author_ids), aliases=tuple(waliases)))
            return out
        except Exception:
            return []

    def _author_ids(self, ent) -> list[dict]:
        """The P50 authors of a Wikidata work entity, each resolved to its label + the
        SOURCE-NEUTRAL identity carrier {name, external_id, extra_ids} — keeping the
        author's Q-id and BDRC/VIAF/DILA cross-links so the person can be bound to THIS
        identity, not just matched by spelling."""
        from . import wikidata as W
        out = []
        for aid in W.claim_ids(ent, W.P_AUTHOR):
            aent = self.client.entity(aid)
            if not aent:
                continue
            aname, _ = W.labels_and_aliases(aent)
            if aname:
                out.append({"name": aname, "external_id": f"wikidata:{aid}",
                            "extra_ids": {"wikidata": f"wikidata:{aid}",
                                          **W.cross_ids(aent)}})
        return out

    def authors_for(self, number, *, title=None, language=None):
        """Authors of the Wikidata item `number` (a Q-id) — fetch the entity and resolve
        its P50 authors directly (no title search needed). Network-guarded → []."""
        from . import wikidata as W
        if self.offline:
            return []
        try:
            ent = self.client.entity(number)
            if not ent:
                return []
            return self._author_ids(ent)
        except Exception:
            return []


def default_sources(*, offline: bool = False) -> list:
    """Conservative default: 84000 (local, no network) first, then Wikidata
    (author + cross-links). BDRC contributes a catalog id; Pandit slots in here
    (or via `build_sources`) once added. A single Wikidata hit is a *candidate*;
    it only auto-verifies an author when a second source (e.g. 84000) agrees.

    VIAF is intentionally absent: it is a person/name authority with no work→author
    relation, so it cannot answer "who wrote this work". Modern authors get their
    VIAF id later, when the person verify pass reconciles the author-persons this
    resolver creates (catalogue/verify.py chain: BDRC → Wikidata → VIAF)."""
    return [EightyFourThousandSource(), WikidataWorkSource(offline=offline)]


if __name__ == "__main__":
    main()
