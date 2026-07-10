"""File a reviewed book out of the inbox onto its subject shelf.

When a book is marked reviewed (`catalogue_review.set_review(status='ok')`, which
already refuses to complete unless a real subject is assigned), its file should leave
the inbox and land in the on-disk directory that matches its subject. The directory is
chosen *empirically*: the folder where existing books carrying that subject already
live — because the library's on-disk layout IS its primary classification (the directory
analysis showed ~89% of books sit in their subject's modal directory).

Two layers, mirroring `relink.py`'s `MoveResolver` (strategy) + `relink_to` (executor):

  • the DECISION is a client-supplied **protocol** — a `FilingProtocol` decides WHERE a
    book should go (`plan()` → `FilingPlan`). Each protocol is its own self-contained
    implementation; the default `EmpiricalFilingProtocol` ships here, others (manual,
    client-specific) drop in beside it via `PROTOCOLS`. Client code depends only on the
    interface and may pass a protocol by name or as its own instance.

  • the MOVE is a protocol-agnostic **executor** — `file_edition()` takes a concrete
    destination and moves the inbox copies there. It never consults a protocol.

ADDITIVE ONLY: a holding is moved *only if it currently sits in an inbox* (`is_in_inbox`).
Files already filed under a non-inbox directory are never moved or removed.
"""
from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field

from catalogue.db_store.db import VOCAB_PATH


def _acc(db):
    """A system Access over this connection — engine-routed subject/edition/holding reads + the
    filing holding-move write. The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(db)

# vocab key: the inbox folders (a list of paths). Inbox membership is by configured
# FOLDER — not a magic name — set from /settings.
INBOX_DIRS_KEY = "_inbox_dirs"

# Built-in default inbox folder (parallels mount.DEFAULT_TRASH_DIR): a real folder
# inside the synced library where new books land, used when `_inbox_dirs` is unset.
DEFAULT_INBOX_DIR = os.environ.get("CATALOGUE_INBOX_DIR", "")

# Subject top-level → library-root folder basename, where the two differ. Roots not
# listed map to a same-named root (History, Philosophy). See mount.library_roots().
_DOMAIN_ROOT_ALIASES = {"Buddhism": "01 Books - Dharma", "Music": "02_Music"}


# ── data units (frozen, like relink.MissingHolding / Match / Resolution) ───────
@dataclass(frozen=True)
class Holding:
    """One copy of a book, as a protocol/executor sees it."""
    id: int
    file_path: str | None
    in_inbox: bool


@dataclass(frozen=True)
class FilingContext:
    """The read-only unit a `FilingProtocol` decides on."""
    edition_id: int
    subjects: tuple[str, ...]          # every subject the edition carries (topic + series)
    series: tuple[str, ...]            # the kind='series' subset of `subjects`
    volume_set_id: int | None
    holdings: tuple[Holding, ...]


@dataclass(frozen=True)
class Destination:
    """One candidate folder a book could be filed into."""
    path: str
    source_subject: str | None         # the subject (or 'series') this folder came from
    n_books: int                       # existing books already filed there for that subject
    exists: bool                       # the folder exists on disk now
    is_series: bool = False


@dataclass
class FilingPlan:
    """A protocol's verdict: at most one `destination` safe to move silently, and/or
    `candidates` offered for one-click confirm (the `Resolution` shape)."""
    auto: bool = False
    destination: Destination | None = None
    candidates: list[Destination] = field(default_factory=list)


# ── inbox recognition (the additive guard) ─────────────────────────────────────
def _vocab() -> dict:
    try:
        return json.loads(VOCAB_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _norm_dir(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/")


def inbox_dirs() -> list[str]:
    """The inbox folders (vocab `_inbox_dirs`), normalised. New books land here, are
    scanned + walked FIRST, and are filed OUT onto their subject shelf on review. These
    are scanned in addition to the configured library roots, so an inbox that is a
    sibling of the roots (owned by none) is still picked up. Falls back to
    `DEFAULT_INBOX_DIR` when none are configured. Edited from /settings."""
    raw = _vocab().get(INBOX_DIRS_KEY)
    if isinstance(raw, list):                        # explicit config wins (even [] = none)
        return _configured_inbox_dirs()
    # Key absent (fresh install): fall back to the built-in default, but only if one
    # is set — an empty default means "no inbox configured", NOT a bogus "" folder.
    return [_norm_dir(DEFAULT_INBOX_DIR)] if DEFAULT_INBOX_DIR else []


def _configured_inbox_dirs() -> list[str]:
    """The EXPLICITLY configured inbox folders (no default fallback) — the list add/remove
    mutate, so a fresh install's default never gets baked into the stored config."""
    raw = _vocab().get(INBOX_DIRS_KEY)
    if not isinstance(raw, list):
        return []
    return [_norm_dir(p) for p in raw if isinstance(p, str) and p]


def set_inbox_dirs(dirs: list[str]) -> None:
    """Persist the inbox folders (vocab `_inbox_dirs`), normalised + de-duplicated.
    Writes through THIS module's VOCAB_PATH (the same binding `inbox_dirs` reads), so a
    single monkeypatch keeps reads + writes on one file under test."""
    from catalogue.services import mount
    out: list[str] = []
    for d in dirs:
        nd = _norm_dir(d)
        if nd and nd not in out:
            out.append(nd)
    mount._write_vocab_value(INBOX_DIRS_KEY, out, path=VOCAB_PATH)


def add_inbox_dir(path: str) -> None:
    """Add one inbox folder (idempotent). Operates on the explicitly configured list, so
    the first add doesn't bake in the default."""
    set_inbox_dirs(_configured_inbox_dirs() + [path])


def remove_inbox_dir(path: str) -> None:
    """Drop one inbox folder."""
    target = _norm_dir(path)
    set_inbox_dirs([d for d in _configured_inbox_dirs() if d != target])


def is_in_inbox(path: str | None) -> bool:
    """True when `path` lives in a configured inbox folder (`inbox_dirs()`) — the ONLY
    gate that lets a file move. Inbox membership is by FOLDER (set in /settings), not a
    magic name: a path matches when it is, or sits beneath, an inbox folder."""
    if not path:
        return False
    pk = path.replace("\\", "/").rstrip("/")
    return any(pk == d or pk.startswith(d + "/") for d in inbox_dirs())


# ── subject → edition / directory helpers (the canonical join) ─────────────────
def _subject_id(db, name: str) -> int | None:
    return _acc(db).subjects.graph.id_by_name(name)


def _holding_paths_for_subject(db, name: str, *, exclude_eid: int | None) -> list[str]:
    """Every recorded file_path whose edition carries `name`, via the canonical join
    (direct `edition_subject` ∪ contained-work `work_subject` — same rule as
    `subject_tree.editions_for_subject`). Excludes `exclude_eid` (the book being filed)."""
    sid = _subject_id(db, name)
    if sid is None:
        return []
    return _acc(db).editions.reads.holding_paths_for_subject(sid, exclude_eid)


def subject_directory(db, name: str, *, exclude_eid: int | None = None) -> Destination | None:
    """The directory where books carrying `name` already live — the MODAL
    `dirname(file_path)` across that subject's holdings, ignoring inbox copies. None
    when no filed book establishes a directory yet (a brand-new subject)."""
    dirs = Counter()
    for fp in _holding_paths_for_subject(db, name, exclude_eid=exclude_eid):
        if is_in_inbox(fp):
            continue
        d = os.path.dirname(fp.replace("\\", "/"))
        if d:
            dirs[d] += 1
    if not dirs:
        return None
    path, n = dirs.most_common(1)[0]
    return Destination(path=path, source_subject=name, n_books=n,
                       exists=os.path.isdir(path))


def _root_for_domain(top: str) -> str | None:
    """The configured library-root path whose folder matches subject top-level `top`
    (via `_DOMAIN_ROOT_ALIASES`, else same-name). None if no root matches."""
    from catalogue.services import mount
    want = _DOMAIN_ROOT_ALIASES.get(top, top).casefold()
    for r in mount.library_roots():
        if os.path.basename(r.path.rstrip("/")).casefold() == want:
            return r.path
    return None


def derive_directory(db, name: str) -> Destination | None:
    """Where a BRAND-NEW subject (no books yet) should be filed: its domain root +
    the subject's sub-path below the top level (`Buddhism/Tantra/Kalachakra` →
    `<01 Books - Dharma>/Tantra/Kalachakra`). None when no root maps the top level
    (e.g. a new top-level subject) — the operator then files it by hand."""
    from catalogue.services import subject_tree as st
    segs = st.segments(name)
    if not segs:
        return None
    root = _root_for_domain(segs[0])
    if not root:
        return None
    sub = "/".join(segs[1:]) if len(segs) > 1 else segs[0]
    path = f"{root.rstrip('/')}/{sub}"
    return Destination(path=path, source_subject=name, n_books=0,
                       exists=os.path.isdir(path))


# ── the abstract layer: the filing protocol the client provides ───────────────
class FilingProtocol(ABC):
    """Strategy that decides WHERE a reviewed edition's files should be filed.
    Concrete protocols own ALL subject→directory policy; client code depends only on
    this interface and may pass its own protocol (by name via `get_protocol`, or as an
    instance to `plan_filing`)."""

    @abstractmethod
    def plan(self, db, ctx: FilingContext) -> FilingPlan:
        """Decide the filing destination(s) for `ctx`."""


# ── Implementation (below the abstraction, like relink.FingerprintResolver) ────
class EmpiricalFilingProtocol(FilingProtocol):
    """Default policy: file a book where books with the same subject already live.

    One candidate per subject (empirical modal dir, else a derived `<root>/<leaf>`);
    plus, for a multi-volume set, the directory its sibling volumes already occupy.
    Auto-files only when every subject resolves to ONE existing directory; otherwise
    returns the candidates for the operator to confirm."""

    def plan(self, db, ctx: FilingContext) -> FilingPlan:
        cands: list[Destination] = []
        for name in ctx.subjects:
            d = subject_directory(db, name, exclude_eid=ctx.edition_id)
            if d is None:
                d = derive_directory(db, name)
            if d is not None:
                cands.append(Destination(d.path, name, d.n_books, d.exists,
                                         is_series=name in ctx.series))
        sib = self._volume_set_directory(db, ctx)
        if sib is not None:
            cands.append(sib)

        # De-dupe by path, keeping the richest signal (most books, series tag).
        by_path: dict[str, Destination] = {}
        for c in cands:
            cur = by_path.get(c.path)
            if cur is None or c.n_books > cur.n_books:
                by_path[c.path] = Destination(c.path, c.source_subject, c.n_books,
                                              c.exists, c.is_series or (cur.is_series if cur else False))
            elif c.is_series and not cur.is_series:
                by_path[c.path] = Destination(cur.path, cur.source_subject, cur.n_books,
                                              cur.exists, True)
        ordered = sorted(by_path.values(), key=lambda d: (-d.n_books, d.path))

        existing = [d for d in ordered if d.exists]
        if len(existing) == 1 and len(ordered) == 1:
            return FilingPlan(auto=True, destination=existing[0], candidates=ordered)
        return FilingPlan(auto=False, destination=None, candidates=ordered)

    def _volume_set_directory(self, db, ctx: FilingContext) -> Destination | None:
        """Modal directory of the OTHER volumes sharing this edition's volume_set_id —
        so a new volume of a set already on disk is offered its set's shelf."""
        if not ctx.volume_set_id:
            return None
        paths = _acc(db).editions.reads.volume_set_holding_paths(
            ctx.volume_set_id, ctx.edition_id)
        dirs = Counter()
        for fp in paths:
            if is_in_inbox(fp):
                continue
            d = os.path.dirname(fp.replace("\\", "/"))
            if d:
                dirs[d] += 1
        if not dirs:
            return None
        path, n = dirs.most_common(1)[0]
        return Destination(path=path, source_subject="(volume set)", n_books=n,
                           exists=os.path.isdir(path), is_series=True)


# ── protocol registry / selection (mirrors protocols.PROTOCOLS) ────────────────
PROTOCOLS: dict[str, FilingProtocol] = {"empirical": EmpiricalFilingProtocol()}
DEFAULT = "empirical"


def get_protocol(name: str | None = None) -> FilingProtocol:
    """The protocol a client gets by name; unknown/None → the default."""
    return PROTOCOLS.get(name or DEFAULT, PROTOCOLS[DEFAULT])


# ── module entry points (thin, protocol-driven) ────────────────────────────────
def build_context(db, eid: int) -> FilingContext:
    """Assemble the `FilingContext` for an edition: its subjects (topic + series via
    the canonical join), the series subset, its volume_set_id, and its holdings."""
    acc = _acc(db)
    sid_rows = acc.editions.reads.subject_names_kinds(eid)
    from catalogue.services.subjects import UNCATEGORIZED
    subjects = tuple(n for n, _ in sid_rows if n != UNCATEGORIZED)
    series = tuple(n for n, k in sid_rows if k == "series" and n != UNCATEGORIZED)

    volume_set_id = acc.editions.reads.volume_set_id(eid)

    holdings = tuple(
        Holding(id=h.id, file_path=h.file_path, in_inbox=is_in_inbox(h.file_path))
        for h in acc.holdings.reads.by_edition(eid))
    return FilingContext(edition_id=eid, subjects=subjects, series=series,
                         volume_set_id=volume_set_id, holdings=holdings)


def plan_filing(db, eid: int,
                protocol: "FilingProtocol | str | None" = None) -> FilingPlan:
    """Plan where edition `eid` should be filed, using a client-chosen protocol (an
    instance, a registered name, or None → default)."""
    proto = protocol if isinstance(protocol, FilingProtocol) else get_protocol(protocol)
    return proto.plan(db, build_context(db, eid))


# ── the executor (protocol-agnostic, additive-only) ────────────────────────────
def _unique_dest(directory: str, basename: str) -> str:
    """A path in `directory` for `basename` that doesn't clobber an existing file —
    ` (2)`, ` (3)`… suffix, the same rule as `mount.move_to_trash`."""
    dest = os.path.join(directory, basename)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(basename)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(directory, f"{stem} ({n}){ext}")
        n += 1
    return dest


def file_edition(db, eid: int, destination: str, *, create: bool = True,
                 commit: bool = True) -> dict:
    """Move edition `eid`'s INBOX copies into `destination` and repoint them.

    For each holding whose current file `is_in_inbox`: move it (collision-suffixed),
    then `UPDATE holding SET file_path = <new>, root_id = owning_root_id(<new>)` — file
    bytes are unchanged so `file_hash` is kept; `root_id` is re-derived because a move
    can cross roots (NULL when the dest sits under no configured root). Holdings already
    filed (not in an inbox) are skipped — the additive guarantee. Missing files (incl.
    an offline mount) are deferred for a later run. Move-then-update, so a move failure
    aborts before any DB write. Returns `{moved, skipped, deferred, destination}`."""
    from catalogue.services import mount, reconcile
    roots = mount.library_roots()
    report = {"destination": destination, "moved": [], "skipped": [], "deferred": []}

    holdings = [(h.id, h.file_path) for h in _acc(db).holdings.reads.by_edition(eid)]
    made_dir = False
    for hid, fp in holdings:
        if not is_in_inbox(fp):
            report["skipped"].append({"holding_id": hid, "reason": "not_in_inbox",
                                      "file_path": fp})
            continue
        if reconcile.file_state(fp) != "present":
            report["deferred"].append({"holding_id": hid, "reason": "missing_or_offline",
                                       "file_path": fp})
            continue
        if not os.path.isdir(destination):
            if not create:
                report["deferred"].append({"holding_id": hid, "reason": "no_dir",
                                           "file_path": fp})
                continue
            os.makedirs(destination, exist_ok=True)
            made_dir = True
        new_path = _unique_dest(destination, os.path.basename(fp.replace("\\", "/")))
        shutil.move(fp, new_path)
        _acc(db).holdings.writes.set_filed(hid, new_path, mount.owning_root_id(new_path, roots))
        report["moved"].append({"holding_id": hid, "from": fp, "to": new_path})

    if commit and report["moved"]:
        db.commit()
    report["created_dir"] = made_dir
    return report
