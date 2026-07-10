"""Auto-relink moved library files (broken-link healing).

When a recorded holding file goes missing it was usually just MOVED elsewhere under
the library root. Scan already catches moves by byte-hash, but a cloud client
(kDrive/iCloud) often rewrites container bytes on re-sync, so the byte-hash goes
stale and the move is missed. The robust signal is the TEXT content-fingerprint,
which survives byte-rewrites and annotations.

Layering (this is the point of the module):

    client code  ──>  service surface        : relink_moved(), relink_to()
                      (resolver-agnostic)
                         │ depends on
                         ▼
                      abstraction            : MoveResolver  (build_pool + resolve)
                         │ implemented by
                         ▼
                      implementation         : FingerprintResolver
                      (basename search, text fingerprint, byte-hash, fs walk)

Client code (the /reconcile route, the CLI) calls only the service surface and, if
it wants, passes a `MoveResolver`. HOW a move is recognised lives entirely in the
implementation below the abstraction, so the strategy can be swapped or stubbed
(tests inject a fake resolver) without touching callers.
"""
from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import reconcile as _rc
from catalogue.db_store import signature


def _acc(db):
    """A system Access over this connection — engine-routed holding reads + the relink writes."""
    from catalogue.access_api import system_conn
    return system_conn(db)


# ── Abstraction (what client code depends on) ─────────────────────────────────
@dataclass(frozen=True)
class MissingHolding:
    """A holding whose recorded file is gone on disk — the unit a resolver works on."""
    holding_id: int
    edition_id: int
    file_path: str
    file_hash: Optional[str]
    content_hash: Optional[str]


@dataclass(frozen=True)
class Match:
    """A located file a holding can be repointed to."""
    path: str
    byte_hash: Optional[str]


@dataclass
class Resolution:
    """A resolver's verdict for one missing holding: at most one `confirmed` match
    (safe to repoint silently) and/or `suggestions` (offered for one-click confirm)."""
    confirmed: Optional[Match] = None
    suggestions: list = field(default_factory=list)


class MoveResolver(ABC):
    """Strategy that locates where a missing holding's file moved, within the library
    root(s). Concrete resolvers own ALL matching details; client code depends only on
    this interface."""

    @abstractmethod
    def build_pool(self, db, roots) -> object:
        """Pre-compute shared search state once per pass (e.g. a filename index)."""

    @abstractmethod
    def resolve(self, holding: MissingHolding, pool) -> Resolution:
        """Decide where `holding` moved, given the pool from build_pool()."""

    def rehash(self, path: str) -> Optional[str]:
        """Byte-hash for an operator-chosen file (when confirming a suggestion).
        Default is a plain sha256; resolvers may override to reuse their own work."""
        try:
            return _sha256(path)
        except OSError:
            return None


# ── Implementation (below the abstraction) ────────────────────────────────────
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:                 # read-only — never mutate the mount
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _candidate_index(db, roots, *, suffixes=(".pdf", ".epub")) -> dict:
    """`basename.lower() -> [(path, size)]` for files under `roots` that are NOT
    already bound to a *present* holding (never steal a file serving another book).
    Stat-only — no reads — so kDrive/iCloud placeholders are still indexed."""
    from .sweep import _walk
    taken = set()
    for _hid, fp in _acc(db).holdings.reads.with_file_path():
        if _rc.file_state(fp) == "present":
            taken.add(os.path.normpath(fp))
    idx: dict[str, list] = {}
    for root in roots:
        for path in _walk(Path(root), suffixes):
            if os.path.normpath(str(path)) in taken:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            idx.setdefault(path.name.lower(), []).append((str(path), size))
    return idx


def _signature_of_file(path: str) -> "tuple[Optional[signature.Signature], Optional[str]]":
    """Recompute a file's `(Signature, byte_hash)` from disk, using the same basis as
    `reconcile.scan_dir` (annotation-stripped text for PDFs). The signature is text-based
    only when a trustworthy text layer was extracted, else byte-based / None — so it only
    confirms a move when the holding's stored signature is text-based too."""
    from .sweep import _hash_file
    from .extract import extract
    try:
        bh = _hash_file(Path(path), 1, 0.5)
    except OSError:
        bh = None
    try:
        ex = extract(Path(path))
    except Exception:
        ex = None
    if not ex or ex.is_image_only:
        return signature.of(None, None, bh), bh
    text = (_rc._pdf_text_sans_annots(path) or ex.text) \
        if str(path).lower().endswith(".pdf") else ex.text
    return signature.of(text, "ocr_good", bh), bh


class FingerprintResolver(MoveResolver):
    """Default strategy: search by basename, confirm by the TEXT content-fingerprint
    (survives cloud byte-rewrites + annotations). A unique fingerprint-confirmed
    candidate is `confirmed`; 0-or-many candidates are offered as `suggestions`."""

    def build_pool(self, db, roots) -> dict:
        return _candidate_index(db, roots)

    def resolve(self, holding: MissingHolding, pool) -> Resolution:
        cands = pool.get(Path(holding.file_path).name.lower()) or []
        if not cands:
            return Resolution()
        confirmed = []
        stored = signature.parse(holding.content_hash)
        if stored and stored.is_text:            # confirm a move by text signature only
            for cpath, _size in cands:
                cand_sig, cbh = _signature_of_file(cpath)
                if cand_sig and stored.matches(cand_sig):
                    confirmed.append(Match(cpath, cbh))
        if len(confirmed) == 1:                  # one trusted match → silent repoint
            return Resolution(confirmed=confirmed[0])
        seen, paths = set(), []                  # else offer the basename candidates
        for cpath, _size in cands:
            if cpath not in seen:
                seen.add(cpath)
                paths.append(cpath)
        return Resolution(suggestions=paths)

    def rehash(self, path: str) -> Optional[str]:
        return _signature_of_file(path)[1]


_DEFAULT_RESOLVER: MoveResolver = FingerprintResolver()


def default_resolver() -> MoveResolver:
    """The strategy client code gets when it doesn't pass its own."""
    return _DEFAULT_RESOLVER


# ── Service surface (client-facing; resolver-agnostic) ────────────────────────
def _gone(db) -> list:
    out = []
    for hid, eid, fp, fh, ch in _acc(db).holdings.reads.with_files():
        if _rc.file_state(fp) == "missing":
            out.append(MissingHolding(hid, eid, fp, fh, ch))
    return out


def relink_moved(db, roots=None, *, resolver: MoveResolver = None, commit: bool = True) -> dict:
    """Heal broken links by relinking files that moved under the library root. Each
    holding whose file is missing is handed to `resolver`:
      • a `confirmed` match → silently repoint (`file_path` + rehash `file_hash`,
        `content_hash` kept) and journal it for undo;
      • else its `suggestions` become one-click options for the operator.
    Returns {'relinked': [...], 'suggestions': {holding_id: [paths]}, 'undo_token'}."""
    resolver = resolver or default_resolver()
    if roots is None:
        roots = _rc.library_roots()
    gone = _gone(db)
    if not gone:
        return {"relinked": [], "suggestions": {}, "undo_token": None}

    pool = resolver.build_pool(db, roots)
    relinked: list = []
    suggestions: dict[int, list] = {}
    snaps: list = []
    for h in gone:
        res = resolver.resolve(h, pool)
        if res.confirmed:
            m = res.confirmed
            snaps.append({"holding_id": h.holding_id,
                          "before": {"file_path": h.file_path, "file_hash": h.file_hash},
                          "after": {"file_path": m.path, "file_hash": m.byte_hash or h.file_hash}})
            _acc(db).holdings.writes.set_location(
                h.holding_id, m.path, m.byte_hash or h.file_hash)
            relinked.append({"holding_id": h.holding_id, "edition_id": h.edition_id,
                             "old_path": h.file_path, "new_path": m.path})
        elif res.suggestions:
            suggestions[h.holding_id] = res.suggestions

    token = _journal(db, snaps) if snaps else None
    if commit:
        db.commit()
    return {"relinked": relinked, "suggestions": suggestions, "undo_token": token}


def relink_to(db, holding_id: int, path: str, *, resolver: MoveResolver = None,
              commit: bool = True) -> dict:
    """Operator-confirmed relink of one holding to a chosen `path` (the one-click
    suggestion action). Repoints `file_path`, rehashes `file_hash`, keeps
    `content_hash`, journals an undo token. Refuses a path that isn't a file."""
    resolver = resolver or default_resolver()
    if not os.path.isfile(path):
        raise ValueError("chosen file does not exist")
    row = _acc(db).holdings.reads.location_of(holding_id)
    if not row:
        raise ValueError(f"no holding {holding_id}")
    new_hash = resolver.rehash(path) or row[1]
    snap = {"holding_id": holding_id,
            "before": {"file_path": row[0], "file_hash": row[1]},
            "after": {"file_path": path, "file_hash": new_hash}}
    _acc(db).holdings.writes.set_location(holding_id, path, new_hash)
    token = _journal(db, [snap], summary="Re-linked moved file")
    if commit:
        db.commit()
    return {"holding_id": holding_id, "new_path": path, "undo_token": token}


# ── Undo (kind-dispatched journal — see contributor_undo) ─────────────────────
def _journal(db, snaps: list, summary: str = None) -> int:
    from .contributor_undo import log_undo
    n = len(snaps)
    summary = summary or f"Auto-relinked {n} moved file{'s' if n != 1 else ''}"
    return log_undo(db, "relink_moved", summary,
                    {"kind": "holding_relink",
                     "holding_ids": [s["holding_id"] for s in snaps], "items": snaps})


def _relink_fingerprint(db, snap) -> str:
    acc = _acc(db)
    parts = []
    for hid in snap["holding_ids"]:
        r = acc.holdings.reads.location_of(hid)
        parts.append(f"{hid}:{r[0] if r else None}:{r[1] if r else None}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _relink_missing(db, snap) -> list:
    acc = _acc(db)
    return [f"holding #{hid}" for hid in snap["holding_ids"]
            if acc.holdings.reads.get(hid) is None]


def _relink_restore(db, snap) -> None:
    acc = _acc(db)
    for it in snap["items"]:
        acc.holdings.writes.set_location(
            it["holding_id"], it["before"]["file_path"], it["before"]["file_hash"])


try:
    from .contributor_undo import register_kind as _register_kind
    _register_kind("holding_relink", fingerprint=_relink_fingerprint,
                   missing=_relink_missing, restore=_relink_restore, ids_key="holding_ids")
except Exception:                                  # registry optional in minimal contexts
    pass
