"""Subject keywords for works and editions.

A *subject* is a keyword (e.g. "Dharma/Emptiness") that can be attached to any
number of works and/or editions, many-to-many. Subjects live in the shared
`subject` lookup table (UNIQUE name); the `work_subject` and `edition_subject`
join tables carry the attachments (ON DELETE CASCADE both ways, so deleting a
work/edition drops its tags but never the shared subject row).

Subjects are HIERARCHICAL by convention: a '/' in the name nests it, so
`Dharma/Emptiness` is "Emptiness" under "Dharma". Nothing in the schema enforces
this — it's just a slash-delimited path string — but `derive_subject` builds one
from a holding's directory path, and the UI/facets can roll up on the prefix.

The first source of subjects is the folder a holding's file lives in. The full
directory path between the library root and the file becomes the subject path,
each segment cleaned (`clean_segment`: drop "01 ", "Books - " noise) or remapped
via the `subject_folder_map` table (so `01 Books - Dharma` → `Dharma`, or
whatever label you assign). Example:

    <root>/01 Books - Dharma/Emptiness/A.pdf   →   subject "Dharma/Emptiness"

The backfill pass that walks holdings and applies this lives in
`catalogue/cli/subject_backfill.py`; the per-edition suggestion shown in the
review UI is `suggest_edition_subject`.
"""
import os
_KINDS = ("work", "edition")


def _g(db):
    """The subject-GRAPH store bound over this connection (engine-routed primitives)."""
    from catalogue.access_api import system_conn
    return system_conn(db).subjects.graph

# Every work/edition must carry at least one subject. When none is supplied at
# creation (or the last one is removed) this placeholder is attached so nothing is
# ever subject-less; the review gate refuses to mark such a record reviewed until a
# real subject replaces it. See `ensure_categorized` / `has_uncategorized`.
UNCATEGORIZED = "Uncategorized"

# Reserved names that must NEVER become subject tags. 'INBOX' is the intake area (the
# 04_INBOX folder / a workflow state), not aboutness — tagging records with it pollutes
# the subject tree and skews the filer's destination ranking (catalogue/domain/filing).
# add_subject silently ignores these (a stray bulk-assign of "INBOX" is a harmless no-op,
# not an error), so the tag can't be reintroduced after cleanup.
RESERVED_SUBJECTS = frozenset({"inbox"})


class UncategorizedError(Exception):
    """An operation that needs a real subject was attempted on a work/edition that
    still carries only the `Uncategorized` placeholder (e.g. marking it reviewed).
    Web routes turn this into a popup; CLI commands print it and exit non-zero."""


class ProtectedSubjectError(Exception):
    """A predefined subject (e.g. `UNCATEGORIZED`) cannot be renamed, merged away, or
    deleted — it's part of the schema's contract that nothing is ever subject-less."""


# Predefined subjects the operator may attach/detach but never rename or delete.
PROTECTED_SUBJECTS = (UNCATEGORIZED,)


def is_protected_subject(db, sid: int) -> bool:
    """True iff subject `sid` is a predefined, undeletable/unrenamable subject."""
    name = _g(db).name_of(sid)
    return bool(name) and name.casefold() in {p.casefold() for p in PROTECTED_SUBJECTS}


# ── deriving subjects from folder paths ───────────────────────────────────────

def clean_segment(raw: str) -> str:
    """Subject label for a raw folder name: strip ALL leading non-alphabetic
    characters (ordering numbers / separators / punctuation), keeping the rest.
    `01 Emptiness` → `Emptiness`; `02 - Two Truths` → `Two Truths`; `3) Logic` →
    `Logic`; `Tantra` → `Tantra`. The `subject_folder_map` table overrides per folder."""
    s = (raw or "").strip()
    i = 0
    while i < len(s) and not s[i].isalpha():     # unicode-aware (keeps accented letters)
        i += 1
    return s[i:].strip()


def segment_label(raw: str, mapping: dict | None = None) -> str | None:
    """Subject label for one raw folder segment: the mapped label if the folder
    is in `mapping` (authoritative; an empty mapped label DROPS the segment),
    else the auto-cleaned form. None when nothing meaningful remains."""
    mapping = mapping or {}
    key = (raw or "").strip().casefold()
    if key in mapping:
        return mapping[key].strip() or None
    return clean_segment(raw) or None


def derive_subject(file_path: str | None, root: str = "",
                   mapping: dict | None = None) -> str | None:
    """Hierarchical subject for a file: the cleaned/mapped directory segments
    between `root` and the file, joined by '/'. With no `root`, the file's
    immediate parent folder is used. Returns None when no directory remains.
    Backslashes are tolerated so Windows-style stored paths still resolve."""
    if not file_path:
        return None
    path = file_path.replace("\\", "/")
    root = (root or "").replace("\\", "/")
    if root:
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = path
        parent = os.path.dirname(rel.replace("\\", "/"))
    else:
        parent = os.path.basename(os.path.dirname(path.rstrip("/")))
    segs = [s for s in parent.split("/") if s not in ("", ".", "..")]
    labels = [lbl for s in segs if (lbl := segment_label(s, mapping))]
    return "/".join(labels) or None


def library_root(db) -> str:
    """The directory boundary above which folders are infrastructure and below
    which they're subjects. Auto-detected from the common prefix of holding
    paths, with one refinement: a folder that *directly* holds book files is
    itself a subject (e.g. `01 Books - Dharma` → `Dharma`), so its parent is the
    root — otherwise that top level would be silently stripped. Empty when there
    are no paths or they share no common root."""
    paths = [p.replace("\\", "/") for p in _g(db).holding_file_paths()]
    # Relative outliers would make commonpath raise (mixing abs/rel); ignore them
    # for root detection when we have absolute paths to anchor on.
    pool = [p for p in paths if p.startswith("/")] or paths
    if not pool:
        return ""
    if len(pool) == 1:
        return os.path.dirname(pool[0])
    try:
        cp = os.path.commonpath(pool)
    except ValueError:
        return ""
    has_direct_files = any(os.path.dirname(p) == cp for p in pool)
    return os.path.dirname(cp) if has_direct_files else cp


def subject_root(db) -> str:
    """The PRIMARY library root for subject derivation — the books folder under which a
    path `<root>/A/B` yields the subject `A/B`. Prefers the CONFIGURED primary root
    (vocab.json, via features.library_root) so the operator controls it; falls back to
    auto-detecting from holding paths when unset. With multiple roots, prefer
    `derive_subject_for_path`, which honours each root's own derive_subject flag."""
    from catalogue.services import features
    return features.library_root() or library_root(db)


def derive_subject_for_path(db, file_path: str | None, *, mapping: dict | None = None) -> str | None:
    """Folder-derived subject for one file, honouring its OWNING root's
    `derive_subject` flag (multi-root aware): None when that root has derivation OFF
    (the caller then leaves the record to the `Uncategorized` safety net) or when no
    configured root contains the file. With no roots configured at all, falls back to
    the legacy single auto-detected `subject_root`."""
    if not file_path:
        return None
    from catalogue.services.mount import owning_root, library_roots
    mapping = mapping if mapping is not None else folder_map(db)
    r = owning_root(file_path, library_roots())
    if r is not None:
        if not r.derive_subject:                     # derivation OFF → Uncategorized net
            return None
        return derive_subject(file_path, r.path, mapping)
    # Under no configured root → legacy single auto-detected/primary-root derivation.
    return derive_subject(file_path, subject_root(db), mapping)


def suggest_edition_subject(db, edition_id: int, *, root: str | None = None,
                            mapping: dict | None = None) -> str | None:
    """Folder-derived subject for an edition (from its first holding's path), or
    None. Used by the review UI's "+ from folder" affordance."""
    if mapping is None:
        mapping = folder_map(db)
    path = _g(db).first_holding_path(edition_id)
    if not path:
        return None
    if root is not None:                             # explicit override (back-compat/tests)
        return derive_subject(path, root, mapping)
    return derive_subject_for_path(db, path, mapping=mapping)   # per-owning-root


# ── folder → label mapping (subject_folder_map) ───────────────────────────────

def folder_map(db) -> dict:
    """`{casefold(raw folder) → label}` overrides for `clean_segment`."""
    return _g(db).folder_map()


def set_folder_label(db, raw: str, label: str) -> None:
    """Map a raw folder segment to a subject label (upsert). An empty `label`
    means "drop this segment from the path" (e.g. an infrastructure folder)."""
    _g(db).set_folder_label((raw or "").strip().casefold(), (label or "").strip())


# ── attach / detach / read ────────────────────────────────────────────────────

def populate_subject_vocab(db, root, *, mapping=None) -> dict:
    """Seed the `subject` table with the predefined subjects derived from the on-disk
    library tree under `root`: every folder's cleaned/mapped hierarchical label becomes
    a subject (so the autocomplete offers them even before any book is filed there).

    Subtrees matching the config exclusions (vocab.json `_exclusions`, via
    skip.is_excluded — e.g. ANNOTATED) are PRUNED: neither the excluded folder nor
    anything beneath it contributes a subject. Idempotent; returns
    `{'added': [labels…], 'scanned': n}`. The caller commits."""
    import os
    from catalogue.services.skip import is_excluded
    mapping = mapping if mapping is not None else folder_map(db)
    root = (root or "").replace("\\", "/").rstrip("/")
    if not root or not os.path.isdir(root):
        return {"added": [], "scanned": 0}
    have = {n.casefold() for n in _g(db).all_names()}
    added, scanned = [], 0
    for dirpath, dirnames, _files in os.walk(root):
        # prune excluded subtrees IN PLACE so os.walk never descends into them
        dirnames[:] = [d for d in dirnames
                       if not is_excluded(d, os.path.join(dirpath, d))]
        norm = dirpath.replace("\\", "/")
        if norm == root:
            continue
        rel = os.path.relpath(norm, root).replace("\\", "/")
        segs = [s for s in rel.split("/") if s not in ("", ".", "..")]
        labels = [lbl for s in segs if (lbl := segment_label(s, mapping))]
        label = "/".join(labels)
        if not label:
            continue
        scanned += 1
        if label.casefold() not in have:
            get_or_create_subject(db, label)
            have.add(label.casefold())
            added.append(label)
    return {"added": added, "scanned": scanned}


def attach_dir_subjects(db, root, *, mapping=None) -> dict:
    """Attach each book's folder-derived subject to the right home, from holding paths:
    to the edition's WORK(S) when it has any (classical — the edition then INHERITS it),
    else directly to the EDITION (modern editions have no work, so the subject comes
    straight from the directory). Config-excluded files (skip.is_excluded) are skipped.
    Idempotent; returns `{'work': n_new, 'edition': n_new}`. The caller commits."""
    from catalogue.services.skip import is_excluded
    mapping = mapping if mapping is not None else folder_map(db)
    rows = _g(db).edition_holding_paths()
    added = {"work": 0, "edition": 0}
    done = set()
    for eid, path in rows:
        if is_excluded(None, path):
            continue
        # Honour the explicit `root` the caller passed (the CLI's --root is a manual
        # operator override). The per-root derive_subject flag governs the automatic /
        # UI suggestion path (derive_subject_for_path), not this explicit backfill.
        subj = derive_subject(path, root, mapping)
        if not subj:
            continue
        wids = _g(db).work_ids_of_edition(eid)
        targets = [("work", w) for w in wids] or [("edition", eid)]
        for kind, pid in targets:
            key = (kind, pid, subj.casefold())
            if key in done:
                continue
            done.add(key)
            sid = get_or_create_subject(db, subj)
            already = _g(db).is_attached(kind, pid, sid)
            add_subject(db, kind, pid, subj)
            if not already:
                added[kind] += 1
    return added


def get_or_create_subject(db, name: str, *, kind: str = "topic") -> int:
    """Subject id for `name`, creating the row on first sight. Matching is
    case-insensitive so "Emptiness" and "emptiness" never fork; the casing of
    the first-created row is kept.

    `kind` is 'topic' (aboutness — the default) or 'series' (a Series/Collection
    grouping). A row that already exists keeps its stored kind — we never silently
    re-flag a topic as a series or vice-versa.

    Hierarchical topics MATERIALIZE their ancestors: creating `Buddhism/Emptiness`
    also creates the container `Buddhism` (if absent) so every '/'-path has a real
    parent row for the tree/browse pages to link to. Series are flat (no '/'), so
    this is a no-op for them. See catalogue/domain/subject_tree.py."""
    name = (name or "").strip().strip("/")
    if not name:
        raise ValueError("subject name must be non-empty")
    sid = _g(db).id_by_name(name)
    if sid is not None:
        return sid
    if kind == "topic" and "/" in name:                  # ensure containers exist first
        segs = name.split("/")
        for i in range(1, len(segs)):
            get_or_create_subject(db, "/".join(segs[:i]), kind="topic")
    return _g(db).insert_subject(name, kind)


def add_subject(db, kind: str, parent_id: int, name: str,
                *, subject_kind: str = "topic") -> int:
    """Attach subject `name` to a work/edition; returns the subject id.
    Idempotent — re-attaching the same subject is a no-op.

    `subject_kind` ('topic' | 'series') is the kind of the SUBJECT being attached —
    distinct from `kind` ('work' | 'edition'), the record it attaches TO.

    Attaching any real TOPICAL subject lifts the `UNCATEGORIZED` placeholder here,
    centrally — so every assignment path (single + bulk routes, apply, backfill,
    work-identity) keeps the invariant 'a book is never both categorized and
    Uncategorized', without each caller having to remember. Adding `UNCATEGORIZED`
    itself (ensure_categorized) is exempt; and a SERIES tag never lifts it (a series
    is not a topic, so the record still 'needs a real subject')."""
    assert kind in _KINDS, kind
    if (name or "").strip().casefold() in RESERVED_SUBJECTS:
        return -1                            # reserved (e.g. 'INBOX') — never a real tag
    sid = get_or_create_subject(db, name, kind=subject_kind)
    _g(db).attach(kind, parent_id, sid)
    if subject_kind == "topic" and (name or "").strip().casefold() != UNCATEGORIZED.casefold():
        clear_uncategorized(db, kind, parent_id)
    return sid


def remove_subject(db, kind: str, parent_id: int, subject_id: int) -> None:
    """Detach one subject from a work/edition. Leaves the shared subject row
    intact (it may be tagging other entities)."""
    assert kind in _KINDS, kind
    _g(db).detach(kind, parent_id, subject_id)


def subjects_for(db, kind: str, parent_id: int,
                 *, subject_kind: str | None = None) -> list[tuple[int, str]]:
    """`(subject_id, name)` pairs attached to one work/edition, name-sorted. With
    `subject_kind` ('topic' | 'series'), only subjects of that kind — so a caller can
    show topical tags and series memberships in separate sections."""
    assert kind in _KINDS, kind
    return _g(db).tags_for(kind, parent_id, subject_kind=subject_kind)


def ensure_categorized(db, kind: str, parent_id: int) -> bool:
    """Guarantee a work/edition is never subject-less: if it carries no subject yet,
    attach the `UNCATEGORIZED` placeholder. Returns True iff it had to be added (the
    record arrived with no subject), so callers can warn the operator that it must be
    reviewed/recategorized later. Idempotent; does NOT commit (caller owns the txn)."""
    assert kind in _KINDS, kind
    # Only a TOPICAL subject counts as "categorized": a record carrying ONLY a
    # series tag (kind='series') still needs a real topic, so it gets the placeholder.
    if _g(db).has_topic(kind, parent_id):
        return False
    add_subject(db, kind, parent_id, UNCATEGORIZED)
    return True


def clear_uncategorized(db, kind: str, parent_id: int) -> None:
    """Drop the `UNCATEGORIZED` placeholder iff the work/edition also carries a real
    TOPICAL subject — called when an operator attaches a genuine topic, so the
    placeholder means 'no real topic yet' and the review gate lifts. A series tag is
    NOT a topic and never lifts it. Never strips the last tag."""
    assert kind in _KINDS, kind
    if _g(db).real_topic_count(kind, parent_id, UNCATEGORIZED):
        _g(db).clear_named(kind, parent_id, UNCATEGORIZED)


def has_uncategorized(db, kind: str, parent_id: int) -> bool:
    """True iff this work/edition still carries the `UNCATEGORIZED` placeholder. The
    review gate uses this to refuse a 'reviewed' verdict until a real subject is set."""
    assert kind in _KINDS, kind
    return _g(db).has_named_tag(kind, parent_id, UNCATEGORIZED)


# ── Subject vocabulary curation (Review → Subjects tab + unified Search) ──────
def list_subjects(db, *, q: str | None = None, limit: int | None = None,
                  kind: str | None = None) -> list[dict]:
    """Every subject as `{id, name, kind, n_works, n_editions}`, name-sorted. With
    `q`, only subjects whose name CONTAINS it (case-insensitive). With `kind`, only
    that kind ('topic' | 'series'). Subject names are human/slash-path labels, not
    folded keys, so this is a plain NOCASE LIKE."""
    return [{"id": r[0], "name": r[1], "kind": r[2], "n_works": r[3], "n_editions": r[4]}
            for r in _g(db).list_with_counts(q, kind, limit)]


def subjects_matching(db, q: str, *, limit: int = 25) -> list[dict]:
    """Search helper for the unified-search registry: subjects matching `q` (any kind —
    a series name is findable too; the hit carries its `kind`)."""
    return list_subjects(db, q=q, limit=limit) if (q or "").strip() else []


def count_uncurated(db) -> int:
    """Topical subjects that need a human pass: a genuine ORPHAN — a leaf topic that
    tags nothing AND has no sub-topics (left over after merges/deletes/typos). Drives
    the Review card badge + the Subjects tab's worklist.

    A '/' in the name is NO LONGER a defect — the hierarchy is intentional, so a
    container parent (tags nothing directly but has children) is healthy, not a to-do.
    Series (kind='series') are a separate namespace and never counted here. Protected
    predefined subjects (e.g. `UNCATEGORIZED`) are never counted either."""
    return _g(db).uncurated_count(PROTECTED_SUBJECTS)


def subject_tagged(db, sid: int) -> dict:
    """The works + editions a subject tags (for the curation detail pane)."""
    works = [{"id": r[0], "title": r[1] or f"work#{r[0]}"} for r in _g(db).tagged_works(sid)]
    eds = [{"id": r[0], "title": r[1] or "(untitled)"} for r in _g(db).tagged_editions(sid)]
    return {"works": works, "editions": eds}


def merge_subjects(db, src_id: int, dst_id: int) -> int:
    """Fold subject `src_id` into `dst_id`: re-point every work/edition tag, then
    drop the now-empty src row. Returns the surviving (dst) id. No-op if equal."""
    if src_id == dst_id:
        return dst_id
    if is_protected_subject(db, src_id):
        raise ProtectedSubjectError(
            f"“{UNCATEGORIZED}” is a predefined subject and cannot be merged away.")
    for kind in ("work", "edition"):
        _g(db).repoint_tags(kind, src_id, dst_id)
    _g(db).delete_subject(src_id)
    return dst_id


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards (`%` `_`) and the escape char itself, for use with
    `LIKE ? ESCAPE '\\'`. Subject names are free text, so a name like `A_B` must
    not match `AxB` when we prefix-search its descendants."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def rename_subject(db, sid: int, new_name: str) -> int:
    """Rename a subject. If another subject already owns `new_name` (NOCASE), this
    is really a merge — fold `sid` into it. Returns the surviving id.

    Renaming a CONTAINER cascades the prefix to its descendants: renaming
    `Buddhism` → `Dharma` rewrites `Buddhism/Emptiness` → `Dharma/Emptiness` and so
    on down the tree, so the hierarchy stays intact. Where a rewritten descendant
    name collides with an existing subject, the two are merged. The new name's own
    ancestors are materialized if it introduces new path levels."""
    new_name = (new_name or "").strip().strip("/")
    if not new_name:
        raise ValueError("subject name must be non-empty")
    if is_protected_subject(db, sid):
        raise ProtectedSubjectError(
            f"“{UNCATEGORIZED}” is a predefined subject and cannot be renamed.")
    cur = _g(db).name_kind_of(sid)
    if not cur:
        raise ValueError(f"no subject #{sid}")
    old_name, kind = cur
    if new_name.casefold() == old_name.casefold():       # pure case change
        _g(db).update_name(sid, new_name)
        return sid
    other = _g(db).name_taken(new_name, sid)
    if other:
        return merge_subjects(db, sid, other)
    # Snapshot descendants BEFORE moving the parent (so the prefix still matches).
    descendants = _g(db).descendants(_like_escape(old_name) + "/%")
    _g(db).update_name(sid, new_name)
    if kind == "topic" and "/" in new_name:              # materialize any new ancestors
        segs = new_name.split("/")
        for i in range(1, len(segs)):
            get_or_create_subject(db, "/".join(segs[:i]), kind="topic")
    for did, dname in descendants:
        moved = new_name + dname[len(old_name):]          # swap the leading prefix
        clash = _g(db).name_taken(moved, did)
        if clash:
            merge_subjects(db, did, clash)
        else:
            _g(db).update_name(did, moved)
    return sid


def delete_subject(db, sid: int) -> None:
    """Delete a subject; its work_subject/edition_subject tags cascade away. The
    predefined `UNCATEGORIZED` subject is protected and cannot be deleted."""
    if is_protected_subject(db, sid):
        raise ProtectedSubjectError(
            f"“{UNCATEGORIZED}” is a predefined subject and cannot be deleted.")
    _g(db).delete_subject(sid)
