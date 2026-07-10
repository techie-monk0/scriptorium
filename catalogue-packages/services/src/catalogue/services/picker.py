"""Modular candidate picker — the entity-agnostic core behind both the CLI and the
web "resolve" UI.

The job is always the same shape: a catalogue row (a person, a work, …) is
unresolved; several authorities each offer ranked *candidates*; a human reads the
labels and says which one is correct; we *bind* it. Only two things are
kind-specific — where the candidates come from and how a chosen one is written —
so a KIND registers:

  * `providers()`      → a list of CandidateProviders (one per authority)
  * `list_unresolved`  → the rows still needing a decision  (id, label, current, aliases)
  * `bind`             → apply a chosen Candidate to the row

Everything else — gathering/merging candidates, the interactive prompt, the web
fragment — is generic over `kind`. Add a kind (or an authority) by registering it;
no front-end changes. `person` and `work` ship here; `verify.bind_person` is reused
so a hand-picked person match takes the identical path (cross-link harvest included)
as the automatic one.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from . import bdrc, verify
from catalogue.db_store import fold_key, init_db
from catalogue.db_store.integrity import IntegrityError, verified_commit
from .honorifics import strip_honorifics
from .http_util import AuthorityUnavailable
from catalogue.db_store import default_db_path


def _acc(db):
    """A system Access over this connection — engine-routed work/person reads + writes."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _reads(db):
    """The person READ surface bound over this caller's connection (engine-routed, live-only)."""
    return _acc(db).persons.reads


# ── Candidate ─────────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    """One option an authority offers for a row. `id` is namespaced (bdr:P84 /
    wikidata:Q123 / toh:182); `label`/`detail` are what the human reads to decide."""
    id: str
    source: str                  # bdrc | wikidata | viaf | toh | …
    label: str
    detail: str = ""
    url: str = ""
    score: float = 0.0

    def as_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "label": self.label,
                "detail": self.detail, "url": self.url, "score": self.score}


def authority_url(ext_id: str) -> str:
    """Public web page for a namespaced authority id (for click-through)."""
    if not ext_id:
        return ""
    pre, _, tail = ext_id.partition(":")
    return {
        "bdr": f"https://purl.bdrc.io/resource/{tail}",
        "wikidata": f"https://www.wikidata.org/wiki/{tail}",
        "viaf": f"https://viaf.org/viaf/{tail}",
        "toh": f"https://read.84000.co/translation/toh{tail}.html",
        "dila": f"https://authority.dila.edu.tw/person/?fromInner={tail}",
    }.get(pre, "")


def _queries(text: str, aliases) -> list:
    """Distinct query forms for a row: the name/title, its extended-stripped form,
    then each alias — deduped on fold_key so a re-spelling isn't a second query."""
    out, seen = [], set()
    for q in (text, strip_honorifics(text or "", extended=True), *(aliases or ())):
        q = (q or "").strip()
        k = fold_key(q)
        if q and k not in seen:
            seen.add(k)
            out.append(q)
    return out


# ── Provider protocol ───────────────────────────────────────────────────────────
class CandidateProvider(Protocol):
    name: str
    def candidates(self, db, text: str, aliases=()) -> "list[Candidate]": ...


# ── PERSON providers ──────────────────────────────────────────────────────────
class BdrcPersonProvider:
    """BDRC ElasticSearch persons. size>=20 so homonyms stay visible (see verify)."""
    name = "bdrc"

    def __init__(self, es=None, size: int = 20):
        self.es = es if es is not None else bdrc.BdrcElasticSearch(size=size)

    def candidates(self, db, text, aliases=()):
        best: dict = {}
        for q in _queries(text, aliases):
            try:
                hits = self.es.person_search(q)
            except Exception:
                continue
            for h in hits:
                if not bdrc.is_person_id(h.get("id")):
                    continue
                sc = h.get("score", 0) or 0
                cur = best.get(h["id"])
                if cur is None or sc > cur.score:
                    best[h["id"]] = Candidate(
                        h["id"], "bdrc", " | ".join((h.get("labels") or [])[:3]),
                        url=authority_url(h["id"]), score=sc)
        return sorted(best.values(), key=lambda c: -c.score)


class WikidataPersonProvider:
    name = "wikidata"

    def __init__(self, client=None, n: int = 6):
        self.client, self.n = client, n

    def candidates(self, db, text, aliases=()):
        cl = self.client if self.client not in (None, False) else verify._wikidata_client()
        try:
            hits = cl.search(text)[:self.n]
        except Exception:
            return []
        out, seen = [], set()
        for qid, label, desc in hits:
            if qid in seen:
                continue
            seen.add(qid)
            out.append(Candidate(f"wikidata:{qid}", "wikidata", label,
                                 detail=desc or "", url=authority_url(f"wikidata:{qid}")))
        return out


class ViafPersonProvider:
    name = "viaf"

    def __init__(self, client=None, n: int = 6):
        self.client, self.n = client, n

    def candidates(self, db, text, aliases=()):
        cl = self.client if self.client not in (None, False) else verify._viaf_client()
        try:
            hits = cl.suggest(text)[:self.n]
        except Exception:
            return []
        return [Candidate(f"viaf:{vid}", "viaf", term, url=authority_url(f"viaf:{vid}"))
                for vid, term in hits]


# ── WORK providers ────────────────────────────────────────────────────────────
class WorkCanonicalProvider:
    """84000/BDRC canonical resolver — surfaces its single best match as a candidate
    (Toh number is high-precision; BDRC work id is a softer suggestion)."""
    name = "canonical"

    def __init__(self, resolver=None):
        self.resolver = resolver

    def candidates(self, db, text, aliases=()):
        r = self.resolver
        if r is None:
            from .work_canonical_resolver import LiveResolver
            r = LiveResolver()
        try:
            res = r.resolve_work(db, text)
        except Exception:
            return []
        num = getattr(res, "canonical_number", None) if res else None
        if not num:
            return []
        sys_ = getattr(res, "canonical_system", None) or "toh"
        cid = f"toh:{num}" if sys_ == "toh" else num     # bdrc number is already bdr:…
        return [Candidate(cid, sys_, getattr(res, "canonical_name", None) or text,
                          url=authority_url(cid))]


# ── gather (generic) ────────────────────────────────────────────────────────────
def gather(db, kind: str, text: str, aliases=(), providers=None) -> "list[Candidate]":
    """Ranked candidates for a row from every provider of `kind`, provider order
    preserved (precision authorities first)."""
    provs = providers if providers is not None else KINDS[kind].providers()
    out = []
    for p in provs:
        try:
            out.extend(p.candidates(db, text, aliases))
        except Exception:
            pass
    return out


# Scheme aliases the operator may type → the canonical namespaced prefix.
_ID_SCHEME_ALIASES = {"bdr": "bdr", "bdrc": "bdr", "wikidata": "wikidata",
                      "wd": "wikidata", "viaf": "viaf", "toh": "toh", "dila": "dila"}
# Which schemes are valid for which kind (so a work id pasted under People is a
# clean "no match", not a bogus person binding).
_ID_KIND_SCHEMES = {"person": {"bdr", "wikidata", "viaf", "dila"},
                    "work": {"bdr", "toh"}}


def looks_like_authority_id(raw: str) -> bool:
    """True if `raw` is `<scheme>:<tail>` with a scheme we know how to resolve."""
    pre, sep, tail = (raw or "").strip().partition(":")
    return bool(sep and tail.strip() and pre.strip().lower() in _ID_SCHEME_ALIASES)


def lookup_by_id(db, kind: str, raw: str):
    """Resolve a typed authority id (e.g. `bdr:P123`, `wikidata:Q42`, `viaf:99`) to
    its single Candidate so the operator can bind a known id directly instead of
    searching by name.

    Returns:
      * `None`     — `raw` isn't id-shaped; caller should fall back to a name search.
      * `[]`       — id-shaped but no match (unknown scheme for this kind, wrong
                     entity type, or the id doesn't exist) → "no match found".
      * `[Candidate]` — the exact authority candidate.
    """
    pre, sep, tail = (raw or "").strip().partition(":")
    pre, tail = pre.strip().lower(), tail.strip()
    if not (sep and tail and pre in _ID_SCHEME_ALIASES):
        return None                                   # not an id query
    scheme = _ID_SCHEME_ALIASES[pre]
    if scheme not in _ID_KIND_SCHEMES.get(kind, set()):
        return []                                     # right shape, wrong kind
    ext = f"{scheme}:{tail}"
    if scheme == "bdr":
        if bdrc.entity_type(ext) != kind:             # person id under People, work under Works
            return []                                 # wrong BDRC entity type
        # BDRC has no cheap id→label lookup; show the id and let ↗ confirm the name.
        return [Candidate(ext, "bdrc", ext, url=authority_url(ext))]
    if scheme == "wikidata":
        try:
            ent = verify._wikidata_client().entity(tail)
        except Exception:
            ent = None
        if not ent:
            return []                                 # QID doesn't exist
        from . import wikidata as W
        name, _aliases = W.labels_and_aliases(ent)
        desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value", "")
        return [Candidate(ext, "wikidata", name or ext,
                          detail=desc, url=authority_url(ext))]
    # viaf / toh / dila: no cheap by-id existence check — accept a well-formed id and
    # let the operator confirm via the ↗ authority link.
    return [Candidate(ext, scheme, ext, url=authority_url(ext))]


# ── binders (per kind) ──────────────────────────────────────────────────────────
def _person_scheme(ext_id: str) -> str:
    """Namespaced id → its person_external_id scheme code ('bdr:P…' → 'bdrc')."""
    return ("bdrc" if ext_id.startswith("bdr:")
            else "viaf" if ext_id.startswith("viaf:")
            else (ext_id.partition(":")[0] or "external"))


def _harvest_extra(ext_id: str):
    """(name, aliases, extra_ids) for a chosen id. Wikidata is the identity hub, so
    a NON-wikidata pick (bdr:/viaf:/dila:) is first reverse-resolved to its Wikidata
    QID — that way the bind keys on the hub and dedups cross-scheme (a BDRC pick
    collapses onto an existing wikidata-bound record). A wikidata pick (resolved or
    direct) harvests its cross-links (bdrc/viaf/dila) so they land in
    person_external_id too.

    A NETWORK failure is distinguished from a genuine empty: on AuthorityUnavailable
    the result carries `_incomplete=True` so the bind is flagged for re-harvest rather
    than silently looking fully resolved (only the hub id, no cross-links) — see
    authority_dedup_plan.md §6.17. `_incomplete` is a sentinel, not a stored id;
    verify.bind_person pops it before writing person_external_id. A genuine miss
    (Wikidata has no item carrying the picked id, or an unsupported scheme) binds on
    the raw id as before — the 'match on raw, then create' fallback."""
    if ext_id.startswith("wikidata:Q"):
        try:
            from . import wikidata as W
            ent = verify._wikidata_client().entity(ext_id.split(":", 1)[1])
            if ent:
                name, aliases = W.labels_and_aliases(ent)
                return name, aliases, {"wikidata": ext_id, **W.cross_ids(ent)}
        except AuthorityUnavailable:
            return None, None, {"wikidata": ext_id, "_incomplete": True}
        except Exception:
            pass
        return None, None, {"wikidata": ext_id}

    # Non-wikidata pick: try to climb to the Wikidata hub before harvesting.
    if not ext_id.startswith("wikidata:"):
        try:
            hub = verify._wikidata_client().resolve_by_external_id(ext_id)
        except AuthorityUnavailable:
            # Offline: keep the raw id, flag for re-harvest (never mistake offline for
            # 'no hub' — that would let a duplicate of a hub-bound record slip through).
            return None, None, {_person_scheme(ext_id): ext_id, "_incomplete": True}
        if hub:
            name, aliases, extra = _harvest_extra(hub)        # harvest the hub's full set
            # Keep the picked id even if Wikidata's entity happens to omit that property.
            extra.setdefault(_person_scheme(ext_id), ext_id)
            return name, aliases, extra
        # genuine miss → bind on the raw id (still deduped on it, just not cross-scheme).

    return None, None, {_person_scheme(ext_id): ext_id}


def bind_person(db, pid: int, choice, *, commit: bool = True, force: bool = False) -> bool:
    ext_id = choice.id if isinstance(choice, Candidate) else choice
    name, aliases, extra = _harvest_extra(ext_id)
    # Normalize the hub to Wikidata when the pick resolved there, so person.external_id
    # is the dedup hub regardless of which authority the operator clicked.
    hub = extra.get("wikidata", ext_id)
    if name is None and isinstance(choice, Candidate):
        name = choice.label
    return verify.bind_person(db, pid, hub, name, aliases, extra,
                              commit=commit, force=force)


def bind_work(db, wid: int, choice, *, commit: bool = True, force: bool = False) -> bool:
    """Set a work's canonical_system/number from a chosen Candidate. No-op if the
    work already has one, unless `force` (a deliberate rebind) overwrites it."""
    system = choice.source if isinstance(choice, Candidate) else "toh"
    ext_id = choice.id if isinstance(choice, Candidate) else choice
    number = ext_id.split(":", 1)[1] if ext_id.startswith("toh:") else ext_id
    work = _acc(db).works.reads.get(wid)
    if not work or (work.canonical_number and not force):
        return False
    _acc(db).works.writes.set_scalars(wid, {"canonical_system": system, "canonical_number": number})
    label = choice.label if isinstance(choice, Candidate) else None
    if label:
        verify._add_canonical_aliases(db, "work", wid,
                                      verify.Match(number, system, label, []))
    if commit:
        db.commit()
    return True


# ── unresolved-row listers (per kind) ────────────────────────────────────────────
def _person_unresolved(db, *, limit=None, ids=None):
    return _reads(db).unresolved(limit=limit, ids=ids)


def _work_unresolved(db, *, limit=None, ids=None):
    acc = _acc(db)
    wids = acc.works.reads.canonical_unresolved_ids(limit=limit, ids=ids)
    out = []
    for wid in wids:
        names = [t for t, _scheme in acc.works.reads.aliases(wid) if t]
        cur = acc.works.reads.get(wid).canonical_number
        if names:
            out.append((wid, names[0], cur, tuple(names[1:])))
    return out


# ── KIND registry ────────────────────────────────────────────────────────────────
# Total count of unresolved rows per kind — mirrors each list_unresolved's WHERE
# (works also require ≥1 alias, matching the list which skips name-less works) so
# the "N of M" header can't drift from the actual list.
def _person_unresolved_count(db) -> int:
    return _reads(db).unresolved_count()


def _work_unresolved_count(db) -> int:
    return _acc(db).works.reads.canonical_unresolved_count()


@dataclass
class KindSpec:
    kind: str
    label: str                                   # human plural ("People", "Works")
    providers: Callable[[], list]
    list_unresolved: Callable
    bind: Callable
    count_unresolved: Callable                    # total unresolved (no limit)


KINDS = {
    "person": KindSpec(
        "person", "People",
        lambda: [WikidataPersonProvider(), ViafPersonProvider(), BdrcPersonProvider()],
        _person_unresolved, bind_person, _person_unresolved_count),
    "work": KindSpec(
        "work", "Works",
        lambda: [WorkCanonicalProvider()],
        _work_unresolved, bind_work, _work_unresolved_count),
}


def unresolved(db, kind: str, *, limit=None, ids=None):
    return KINDS[kind].list_unresolved(db, limit=limit, ids=ids)


def count_unresolved(db, kind: str) -> int:
    """Total unresolved rows for `kind`, ignoring the display limit (the M in the
    'N of M' header). Generic over kind → persons and works both get it."""
    return KINDS[kind].count_unresolved(db)


def bind(db, kind: str, entity_id: int, choice, *, commit: bool = True,
         force: bool = False) -> bool:
    return KINDS[kind].bind(db, entity_id, choice, commit=commit, force=force)


def bind_with_dedup(db, kind: str, entity_id: int, choice, *, commit: bool = True,
                    force: bool = False) -> dict:
    """Bind, then — for persons — run the on-bind dedup (auto-merge a same-identity
    duplicate, or surface a suggestion), in ONE transaction. Returns
    {'ok': bool, 'dedup': <dedup_on_bind result>|None}. The single bind entry point
    shared by the web /picker route AND the interactive CLI, so both surfaces dedupe
    identically (web-UI behaviour is never ahead of the CLI)."""
    ok = bind(db, kind, entity_id, choice, commit=False, force=force)
    dedup = None
    if ok and kind == "person":
        from . import person_dedup as PD
        dedup = PD.dedup_on_bind(db, entity_id, commit=False)
    if commit:
        # An on-bind dedup that actually MERGED moved link edges → verify before commit
        # (roll back if any edge is left dangling); a plain bind moved no links.
        if dedup and dedup.get("merged_into"):
            verified_commit(db)
        else:
            db.commit()
    return {"ok": ok, "dedup": dedup}


# ── bulk operations (multi-select) ───────────────────────────────────────────────
@dataclass
class BulkOp:
    """One operation an operator can apply to MANY selected rows at once. `target`
    declares what extra input the op needs:
      'none'     — each selected row is handled independently;
      'survivor' — one of the selected rows is the merge target every other row
                   folds INTO (so the operator must pick which one survives).
    The SAME registry drives the web action bar and the CLI multi-select menu, so a
    kind's bulk options never differ between surfaces."""
    key: str
    label: str
    danger: bool = False
    target: str = "none"                 # 'none' | 'survivor'

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label,
                "danger": self.danger, "target": self.target}


# Which bulk operations each kind offers — the context-dependence the operator sees:
# persons can be confirmed-local / marked-org / merged / deleted; works (no local
# delete path) only merge. Add a kind's row here, no surface changes needed.
BULK_OPS = {
    "person": [
        BulkOp("create_new", "✓ Mark reviewed (keep as local, no authority)"),
        BulkOp("mark_org",   "Mark as organization"),
        BulkOp("merge",      "Merge all into one…", target="survivor"),
        BulkOp("delete",     "Delete records", danger=True),
    ],
    "work": [
        BulkOp("merge",      "Merge all into one…", target="survivor"),
    ],
}


def bulk_ops(kind: str) -> "list[BulkOp]":
    """The bulk operations available for `kind` (empty for an unknown kind)."""
    return BULK_OPS.get(kind, [])


def _merge_into(db, kind: str, eid: int, target: int):
    """Fold one row INTO `target` (commit deferred). Returns the apply report, which
    may carry an 'error' key (e.g. the cross-authority safety rail) instead of raising."""
    if kind == "person":
        from . import contributor_edit as CE
        return CE.apply_merge(db, eid, target, commit=False)
    if kind == "work":
        from . import work_merge as WM
        return WM.apply_work_merge(db, eid, target, commit=False)
    raise ValueError(f"merge is not supported for kind {kind!r}")


def _bulk_one(db, kind: str, op: str, eid: int):
    """Apply a single-target (target='none') bulk op to one row (commit deferred).
    Returns (status, detail) with status ∈ {'ok','skipped'}; raises on a hard failure
    so the driver records it under 'failed'."""
    if kind == "person" and op == "create_new":
        ok = verify.confirm_local(db, eid, commit=False)
        return ("ok", "confirmed local") if ok else ("skipped", "already bound")
    if kind == "person" and op == "mark_org":
        from .names import set_person_kind
        ok = set_person_kind(db, eid, organization=True, commit=False)
        return ("ok", "marked organization") if ok else ("skipped", "bound / ineligible")
    if kind == "person" and op == "delete":
        from . import contributor_edit as CE
        rep = CE.apply_delete(db, eid, commit=False)
        if rep.get("error"):
            raise ValueError(rep["error"])
        return ("ok", f"deleted ({len(rep['works_detached'])} work(s) detached)")
    raise ValueError(f"bulk op {op!r} is not supported for kind {kind!r}")


def bulk_apply(db, kind: str, op: str, ids, *, target=None, commit: bool = True) -> dict:
    """Apply ONE bulk operation to many entity ids in a single transaction (best
    effort: a row that errors is recorded, the rest still apply). The shared entry
    point for the web /picker/<kind>/bulk route AND the CLI multi-select, so both
    surfaces behave identically. Returns
      {'op', 'kind', 'ok':[{'id','detail'}], 'skipped':[…], 'failed':[{'id','error'}],
       'target'? }
    Raises ValueError for a malformed request (unknown op, empty selection, a merge
    with no/foreign target) — the caller turns that into a 400."""
    spec = next((o for o in bulk_ops(kind) if o.key == op), None)
    if spec is None:
        raise ValueError(f"unknown bulk op {op!r} for kind {kind!r}")
    ids = [int(x) for x in ids]
    if not ids:
        raise ValueError("no rows selected")
    res = {"op": op, "kind": kind, "ok": [], "skipped": [], "failed": []}
    if spec.target == "survivor":
        if target is None or int(target) not in ids:
            raise ValueError("merge needs a target chosen from the selected rows")
        res["target"] = target = int(target)
        for eid in ids:
            if eid == target:
                continue
            try:
                rep = _merge_into(db, kind, eid, target)
                if isinstance(rep, dict) and rep.get("error"):
                    res["failed"].append({"id": eid, "error": rep["error"]})
                else:
                    res["ok"].append({"id": eid, "detail": f"merged into #{target}"})
            except IntegrityError:                       # links lost → whole txn already
                raise                                    # rolled back; abort the bulk op
            except Exception as e:                       # noqa: BLE001 — report, don't abort
                res["failed"].append({"id": eid, "error": str(e)})
    else:
        for eid in ids:
            try:
                status, detail = _bulk_one(db, kind, op, eid)
                res[status].append({"id": eid, "detail": detail})
            except IntegrityError:                       # integrity broke → abort atomically
                raise
            except Exception as e:                       # noqa: BLE001
                res["failed"].append({"id": eid, "error": str(e)})
    if commit:
        # merge/delete move or detach link edges → verify before commit (roll back the
        # whole bulk op on a dangling ref); create_new/mark_org touch no links.
        if op in ("merge", "delete"):
            verified_commit(db)
        else:
            db.commit()
    return res


# ── interactive CLI ───────────────────────────────────────────────────────────────
def parse_choice(raw: str, n: int):
    """Map a prompt reply to an action. Pure (no I/O) so it's unit-testable.
    Returns (action, index|None): bind|skip|local|quit|invalid."""
    r = (raw or "").strip().lower()
    if r in ("q", "quit"):
        return ("quit", None)
    if r in ("", "s", "skip"):
        return ("skip", None)
    if r in ("l", "local"):
        return ("local", None)
    if r.isdigit() and 1 <= int(r) <= n:
        return ("bind", int(r) - 1)
    return ("invalid", None)


def _parse_selection(raw: str, ids) -> list:
    """Map a multi-select reply over a 1-based display list to the chosen ids. Accepts
    ranges and lists ('1-5,8 11'), and 'all'/'*'/'a' for everything. Out-of-range and
    junk tokens are ignored; duplicates are folded (order preserved). Pure → testable."""
    r = (raw or "").strip().lower()
    if r in ("all", "*", "a"):
        return list(ids)
    n, picked = len(ids), []
    for tok in r.replace(",", " ").split():
        if "-" in tok:
            lo, _, hi = tok.partition("-")
            if lo.isdigit() and hi.isdigit():
                picked += [k for k in range(int(lo), int(hi) + 1) if 1 <= k <= n]
        elif tok.isdigit():
            if 1 <= int(tok) <= n:
                picked.append(int(tok))
    seen, out = set(), []
    for k in picked:
        if ids[k - 1] not in seen:
            seen.add(ids[k - 1])
            out.append(ids[k - 1])
    return out


def _show_cands(cands, out) -> None:
    for j, c in enumerate(cands, 1):
        line = f"    {j:>2}) {c.id:18} {c.source:9} {c.label[:58]:58}"
        if c.detail:
            line += f"  — {c.detail[:40]}"
        out(line + (f"   {c.url}" if c.url else ""))


def _search_persons(db, q: str, exclude: int):
    """Persons whose name/alias fold-key contains `q` (excluding `exclude`)."""
    return _reads(db).search(q, exclude=exclude)


def _edit_blob(db, kind, eid, which, input_fn, out) -> bool:
    """Handle an `x` (split-by-comma), `d` (delete), `m` (merge into another person)
    or `a` (add alias) request: show the plan, confirm, apply, and report. Returns True
    if the row was mutated/removed. Persons only."""
    if kind != "person":
        out("    (split/delete/merge/alias apply to persons only)")
        return False
    from . import contributor_edit as CE
    if which == "o":
        from .names import set_person_kind, is_organization_name
        p = _reads(db).get(eid)
        hint = " (looks like an org)" if p and is_organization_name(p.primary_name) else ""
        if (input_fn(f"    mark as organization{hint}? [y/N]: ") or "").strip().lower() != "y":
            out("    (cancelled)")
            return False
        set_person_kind(db, eid, organization=True)
        out("    → marked as organization (off the person worklist)\n")
        return True
    if which == "a":
        text = (input_fn("    new alias text (blank = cancel): ") or "").strip()
        if not text:
            out("    (cancelled)")
            return False
        scheme = (input_fn("    scheme [english]: ") or "english").strip() or "english"
        from catalogue.db_store import add_alias
        add_alias(db, "person", eid, text, scheme)
        db.commit()
        out(f"    → added alias {text!r} ({scheme})\n")
        return True
    if which == "m":
        q = (input_fn("    search for the canonical person to merge INTO: ") or "").strip()
        if not q:
            out("    (cancelled)")
            return False
        hits = _search_persons(db, q, eid)
        if not hits:
            out("    (no matching person — create/bind it first)")
            return False
        for j, (hid, name, dates, ext) in enumerate(hits, 1):
            out(f"      {j:>2}) #{hid} {name}{' ('+dates+')' if dates else ''}"
                f"{'  ['+ext+']' if ext else ''}")
        sel = (input_fn(f"    merge #{eid} into which [1-{len(hits)}, blank=cancel]: ")
               or "").strip()
        if not (sel.isdigit() and 1 <= int(sel) <= len(hits)):
            out("    (cancelled)")
            return False
        target = hits[int(sel) - 1][0]
        plan = CE.plan_merge(db, eid, target)
        if plan.get("error"):
            out(f"    {plan['error']}")
            return False
        out(f"    merge {plan['dup']['name']!r} → {plan['canon']['name']!r}: "
            f"{len(plan['works'])} work(s), {len(plan['editions'])} translator slot(s), "
            f"+{len(plan['aliases_gained'])} alias(es)")
        if (input_fn("    confirm merge? [y/N]: ") or "").strip().lower() != "y":
            out("    (cancelled)")
            return False
        rep = CE.apply_merge(db, eid, target)
        out(f"    → merged #{eid} into #{target} ({rep['into_name']}); "
            f"{len(rep['works_repointed'])} work(s) repointed\n")
        return True
    if which == "x":
        plan = CE.plan_split(db, eid)
        if plan.get("error"):
            out(f"    {plan['error']}")
            return False
        # Name the works AND the books the edges belong to.
        out(f"    split {plan['name']!r}; affected work(s):")
        for w in plan["works"]:
            books = ", ".join(b["book"] for b in w["books"]) or "—"
            out(f"      • {w['role']} of {w['label']!r}   [book: {books}]")
        if plan["editions"]:
            out(f"      ({len(plan['editions'])} translator slot(s) → the part you "
                "mark translator)")
        # Per-part role assignment (blank = suggested default).
        assignments = []
        for p in plan["parts"]:
            tag = f"existing #{p['existing_id']}" if p["existing_id"] else "new"
            ans = (input_fn(f"      role for {p['name']!r} ({tag}) "
                            f"[a=author/t=translator, default {p['role']}]: ") or "").strip().lower()
            role = ("translator" if ans in ("t", "translator")
                    else "author" if ans in ("a", "author") else p["role"])
            assignments.append({"name": p["name"], "role": role})
        summary = ", ".join(f"{a['name']}={a['role']}" for a in assignments)
        if (input_fn(f"    confirm split ({summary})? [y/N]: ") or "").strip().lower() != "y":
            out("    (cancelled)")
            return False
        rep = CE.apply_split(db, eid, assignments=assignments)
        out("    → split into " + "; ".join(f"{i['name']} ({i['role']})" for i in rep["into"])
            + f"; created {len(rep['created'])}; {len(rep['works_repointed'])} work(s)\n")
        return True
    # delete
    plan = CE.plan_delete(db, eid)
    if plan.get("error"):
        out(f"    {plan['error']}")
        return False
    out(f"    delete {plan['name']!r} — detaches {len(plan['works'])} work(s)"
        + (f", clears {len(plan['editions'])} translator slot(s)" if plan["editions"] else ""))
    for w in plan["works"]:
        out(f"      • {w['role']} of {w['label']}")
    if (input_fn("    confirm delete? [y/N]: ") or "").strip().lower() != "y":
        out("    (cancelled)")
        return False
    rep = CE.apply_delete(db, eid)
    out(f"    → deleted #{eid}; detached {len(rep['works_detached'])} work(s)\n")
    return True


def run_cli(db, kind: str, *, limit=None, ids=None, providers=None,
            input_fn=input, out=print) -> dict:
    """Walk unresolved rows; for each, show the ranked candidates and bind the one
    the operator picks. Typing `/TEXT` re-searches; for persons `x` splits a
    comma-blob into its real contributors and `d` deletes the row (both re-point
    every work/edition edge and report). `input_fn`/`out` are injectable for tests."""
    spec = KINDS[kind]
    items = spec.list_unresolved(db, limit=limit, ids=ids)
    extra = ("  x=split-comma  d=delete  m=merge  a=add-alias  o=mark-org"
             if kind == "person" else "")
    out(f"{len(items)} unresolved {kind}(s). [number]=bind  s=skip  "
        f"l=confirm-local  /TEXT=re-search{extra}  q=quit\n")
    tally = {"bound": 0, "skipped": 0, "local": 0, "edited": 0,
             "merged": 0, "dup_suggested": 0}
    for i, (eid, label, cur, aliases) in enumerate(items, 1):
        cands = gather(db, kind, label, aliases, providers=providers)
        out(f"[{i}/{len(items)}] {kind} #{eid}  {label!r}   (current: {cur or '—'})")
        _show_cands(cands, out)
        if not cands:
            out("    (no candidates — type /TEXT to search a different term, or s)")
        action = None
        while action is None:
            raw = input_fn(f"    choose [#/s/l/q, /TEXT{'/x/d/m/a/o' if kind=='person' else ''}]: ")
            s = (raw or "").strip().lower()
            if s.startswith("/"):
                newq = raw.strip()[1:].strip()
                if newq:
                    cands = gather(db, kind, newq, providers=providers)
                    out(f"    re-search {newq!r} → {len(cands)} candidate(s)")
                    _show_cands(cands, out)
                continue
            if kind == "person" and s in ("x", "d", "m", "a", "o"):
                if _edit_blob(db, kind, eid, s, input_fn, out):
                    action = "edited"
                continue
            act, idx = parse_choice(raw, len(cands))
            if act == "invalid":
                out("    ? enter a number, s / l / q, /TEXT"
                    + (", x, d, m, a, o" if kind == "person" else ""))
                continue
            action = act
            if act == "bind":
                action = ("bind", idx)
        if action == "quit":
            break
        if action == "edited":
            tally["edited"] += 1
            continue
        if action == "skip":
            tally["skipped"] += 1
            out("")
            continue
        if action == "local":
            if kind == "person":
                verify.confirm_local(db, eid)
            tally["local"] += 1
            out("    → confirmed local (no external authority)\n")
            continue
        idx = action[1]
        res = bind_with_dedup(db, kind, eid, cands[idx])
        ok = res["ok"]
        msg = ("bound " + cands[idx].id) if ok else "no-op (already bound?)"
        dd = res["dedup"]
        if dd and dd.get("merged_into"):
            msg += (f"  →  MERGED into #{dd['merged_into']} "
                    f"(same authority identity, via {dd.get('via')})")
            tally["merged"] += 1
        elif dd and dd.get("suggest"):
            msg += (f"  →  possible duplicate of {dd['suggest']} "
                    f"({dd.get('reason', '')}) — review")
            tally["dup_suggested"] += 1
        out(f"    → {msg}\n")
        tally["bound"] += int(ok)
    out(f"done: {tally}")
    return tally


def _cancelled() -> dict:
    return {"op": None, "ok": [], "skipped": [], "failed": []}


def run_bulk_cli(db, kind: str, *, limit=None, ids=None,
                 input_fn=input, out=print) -> dict:
    """Interactive multi-select: list the unresolved rows, let the operator pick a
    SET (e.g. '1-5,8' or 'all'), choose ONE bulk operation, confirm, and apply it to
    every selected row at once via `bulk_apply` — the same code path the web action
    bar uses. `input_fn`/`out` are injectable for tests."""
    items = KINDS[kind].list_unresolved(db, limit=limit, ids=ids)
    if not items:
        out(f"no unresolved {kind}(s).")
        return _cancelled()
    labels = {eid: label for (eid, label, *_r) in items}
    row_ids = [eid for (eid, *_r) in items]
    for i, (eid, label, cur, _aliases) in enumerate(items, 1):
        out(f"  {i:>3}) #{eid}  {label!r}   (current: {cur or '—'})")
    sel = _parse_selection(input_fn("select rows [e.g. 1-5,8  or  all]: "), row_ids)
    if not sel:
        out("(nothing selected)")
        return _cancelled()
    ops = bulk_ops(kind)
    out(f"\n{len(sel)} selected. Operation:")
    for j, o in enumerate(ops, 1):
        out(f"  {j}) {o.label}" + ("   [!]" if o.danger else ""))
    raw = (input_fn("operation [number, blank=cancel]: ") or "").strip()
    if not (raw.isdigit() and 1 <= int(raw) <= len(ops)):
        out("(cancelled)")
        return _cancelled()
    spec = ops[int(raw) - 1]
    target = None
    if spec.target == "survivor":
        out("merge target — the record everything else folds INTO:")
        for k, eid in enumerate(sel, 1):
            out(f"  {k}) #{eid}  {labels.get(eid, '')!r}")
        traw = (input_fn(f"target [1-{len(sel)}, blank=cancel]: ") or "").strip()
        if not (traw.isdigit() and 1 <= int(traw) <= len(sel)):
            out("(cancelled)")
            return _cancelled()
        target = sel[int(traw) - 1]
    verb = spec.label.rstrip("…")
    if (input_fn(f"apply {verb!r} to {len(sel)} {kind}(s)? [y/N]: ")
            or "").strip().lower() != "y":
        out("(cancelled)")
        return _cancelled()
    res = bulk_apply(db, kind, spec.key, sel, target=target)
    out(f"→ {len(res['ok'])} done, {len(res['skipped'])} skipped, "
        f"{len(res['failed'])} failed")
    for f in res["failed"]:
        out(f"    ✗ #{f['id']}: {f['error']}")
    return res


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Interactive authority picker: list candidates per unresolved "
                    "row and pick the correct one. Works for any registered kind.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--kind", choices=sorted(KINDS), default="person")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pids", help="comma/space-separated row ids to inspect")
    ap.add_argument("--bulk", action="store_true",
                    help="multi-select mode: pick a SET of rows (e.g. 1-5,8 or 'all') "
                         "and apply one operation (merge/delete/create-new/mark-org) "
                         "to all at once, instead of resolving them one by one")
    ap.add_argument("--dry-run", action="store_true",
                    help="experiment without writing: every bind/split/delete is "
                         "swallowed and rolled back, the real DB is untouched")
    args = ap.parse_args(argv)
    from catalogue.db_store import DryRunConnection
    raw = init_db(args.db)
    raw.execute("PRAGMA busy_timeout = 30000")
    db = DryRunConnection(raw) if args.dry_run else raw
    if args.dry_run:
        print("*** DRY-RUN — no changes will be saved ***", file=sys.stderr, flush=True)
    ids = ([int(x) for x in args.pids.replace(",", " ").split()] if args.pids else None)
    try:
        if args.bulk:
            run_bulk_cli(db, args.kind, limit=args.limit, ids=ids)
        else:
            run_cli(db, args.kind, limit=args.limit, ids=ids)
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
    finally:
        if args.dry_run:
            raw.rollback()             # discard everything the session "wrote"


if __name__ == "__main__":
    main()
