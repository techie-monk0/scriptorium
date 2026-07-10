"""Work identity at creation — the work twin of `get_or_create_person`.

A *Work* (FRBR sense) is one abstract text; an English translation, a Sanskrit
edition and a Tibetan edition of it should resolve to ONE work row carrying all
three titles, not fork into three. Persons already dedup at creation
(`get_or_create_person` by `fold_key`); works historically did not — `promote.py`
minted a fresh work per proposal, so two English editions of the
Mūlamadhyamakakārikā forked (live e310 + e312). This module closes that gap.

`get_or_create_work` finds an existing work to attach to, in order:
  1. canonical number (Toh / BDRC / Wikidata) — language-independent, confident;
  2. a folded original-language alias (Sanskrit / Tibetan) — language-independent;
  3. a folded English alias, GUARDED by author overlap — title alone is too weak.
Otherwise it creates a fresh work. It is NON-DESTRUCTIVE: it never merges two
pre-existing works; when an English title collides but authors don't agree it
creates a new work and flags `merge_candidate=True` for human review (the
homonym-safety rule from `wylie_resolve` / the dgongs-pa-rab-gsal lesson).

It owns the work row's identity columns (canonical#, sanskrit/tibetan titles) and
all its title aliases; the caller owns author/edition links. Canonical# and
original-language titles are wired in by a later slice (`sanskrit_resolve`); today
`promote.py` calls it with an English title + the work's resolved author ids,
which already collapses the English forks.
"""
from __future__ import annotations

from catalogue.db_store import add_alias, fold_key

# Alias scheme for each original-language title key, and the work column it fills.
_TITLE_SCHEME = {"sanskrit": "iast", "tibetan": "wylie"}
_TITLE_COLUMN = {"sanskrit": "sanskrit_title", "tibetan": "tibetan_title"}


def _acc(db):
    """A system Access over this connection (engine-routed work identity reads + writes)."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def find_work_by_canonical(db, system, number):
    """Work id carrying this canonical (system, number), or None."""
    return _acc(db).works.reads.find_by_canonical(system, number)


def find_works_by_title_key(db, key):
    """Work ids carrying any alias whose `normalized_key` == `key`."""
    return _acc(db).works.reads.ids_by_alias_key(key)


def _authors_of(db, wid):
    return set(_acc(db).works.reads.author_ids(wid))


def _ensure_alias(db, wid, text, scheme):
    text = (text or "").strip()
    if text and not _acc(db).works.reads.has_alias_key(wid, fold_key(text)):
        add_alias(db, "work", wid, text, scheme)


def _fill_titles_and_aliases(db, wid, original_titles):
    """Fill EMPTY sanskrit/tibetan_title columns (never overwrite) and add a
    title alias per available original-language title."""
    for lang, text in (original_titles or {}).items():
        text = (text or "").strip()
        if not text:
            continue
        col = _TITLE_COLUMN.get(lang)
        if col:
            _acc(db).works.writes.fill_scalars(wid, {col: text})
        _ensure_alias(db, wid, text, _TITLE_SCHEME.get(lang, "other"))


def resync_native_titles(db, wid) -> None:
    """Re-derive the denormalized work.sanskrit_title / tibetan_title COLUMNS from the
    work_alias rows — the title aliases are the source of truth. Call this after any edit
    that adds or removes a native-title alias (e.g. the work-page alias CRUD) so the
    columns, which `search.py` and `work_review.py` read, never go stale against the
    aliases. The earliest alias of the mapped scheme wins; none ⇒ the column is cleared."""
    acc = _acc(db)
    for lang, scheme in _TITLE_SCHEME.items():
        col = _TITLE_COLUMN[lang]
        acc.works.writes.set_native_title(wid, col, acc.works.reads.first_alias_text(wid, scheme))


def _create(db, *, canonical, original_language, notes, english_title, original_titles):
    system, number = canonical or (None, None)
    wid = _acc(db).works.writes.insert_work(
        {"original_language": original_language, "canonical_system": system,
         "canonical_number": number, "notes": notes})
    _ensure_alias(db, wid, english_title, "english")
    _fill_titles_and_aliases(db, wid, original_titles)
    return wid


def set_work_type(db, wid, work_type) -> None:
    """Set a work's root/commentary type (or any open-vocab type). Registers an unseen
    code first so the FK to work_type holds."""
    _acc(db).works.writes.set_work_type(wid, work_type)


def relate_commentary(db, commentary_wid, root_wid) -> None:
    """Record that `commentary_wid` is a commentary on `root_wid` (idempotent). Marks
    both works' types accordingly. No-op for a missing/self pair."""
    if not (commentary_wid and root_wid) or commentary_wid == root_wid:
        return
    _acc(db).works.writes.relate_commentary(commentary_wid, root_wid)


def commentary_root_id(db, commentary_wid) -> "int | None":
    """The root work `commentary_wid` comments on, if recorded."""
    return _acc(db).works.reads.commentary_root_id(commentary_wid)


def create_work(db, *, english_title=None, sanskrit_title=None, tibetan_title=None,
                canonical_system=None, canonical_number=None, work_type=None,
                original_language=None, era=None, notes=None, author_pids=(), subjects=()):
    """Manually add a work with all its details (the operator's 'Add a new work' form).

    Goes through `get_or_create_work` for the identity/alias/canonical logic (so a manual
    add still de-dupes onto an existing canonical#/title match rather than silently
    duplicating), then fills the extra descriptive columns it doesn't manage (`work_type`,
    `era`) and attaches its `subjects` (a work should carry its own subject keywords;
    editions inherit them). Returns `(work_id, created, merge_candidate)`."""
    canonical = (canonical_system, canonical_number) if (canonical_system and canonical_number) else None
    wid, created, mc = get_or_create_work(
        db, canonical=canonical, english_title=english_title,
        original_titles={"sanskrit": sanskrit_title, "tibetan": tibetan_title},
        author_pids=author_pids, notes=notes, original_language=original_language)
    from catalogue.db_store import contributor_store as cs
    from catalogue.services import subjects as S
    for pid in author_pids or ():
        cs.add_work_author(db, wid, pid)
    for name in subjects or ():
        if (name or "").strip():
            S.add_subject(db, "work", wid, name.strip())
    wt = (work_type or "").strip() or None
    if wt:
        # work_type is an open vocabulary behind an FK — set_work_type registers an unseen code
        # so a free-typed type ('root', 'commentary', …) doesn't trip the constraint.
        set_work_type(db, wid, wt)
    era = (era or "").strip() or None
    if era:
        _acc(db).works.writes.set_scalars(wid, {"era": era})
    # No work is ever subject-less: if the operator supplied no subject, fall back to
    # the Uncategorized placeholder (review will block until a real one replaces it).
    S.ensure_categorized(db, "work", wid)
    return wid, created, mc


def get_or_create_work(db, *, canonical=None, english_title=None, original_titles=None,
                       author_pids=(), notes=None, original_language=None):
    """Return `(work_id, created, merge_candidate)`.

    `canonical` is `(system, number)`; `original_titles` is
    `{'sanskrit': iast, 'tibetan': wylie}`; `author_pids` anchors the
    English-title guard. See the module docstring for the lookup order. Never
    merges two pre-existing works; an English-title collision without author
    agreement yields a NEW work with `merge_candidate=True`.
    """
    original_titles = {k: v for k, v in (original_titles or {}).items() if (v or "").strip()}
    author_pids = set(author_pids or ())

    # 1. canonical# — language-independent identity.
    wid = find_work_by_canonical(db, *(canonical or (None, None)))
    if wid:
        _fill_titles_and_aliases(db, wid, original_titles)
        return wid, False, False

    # 2. folded original-language alias — language-independent identity.
    for _lang, text in original_titles.items():
        hits = find_works_by_title_key(db, fold_key(text))
        if hits:
            _fill_titles_and_aliases(db, hits[0], original_titles)
            return hits[0], False, False

    # 3. folded English alias, guarded by author overlap.
    if english_title and english_title.strip():
        cands = find_works_by_title_key(db, fold_key(english_title))
        for wid in cands:
            if author_pids and (_authors_of(db, wid) & author_pids):
                _ensure_alias(db, wid, english_title, "english")
                _fill_titles_and_aliases(db, wid, original_titles)
                return wid, False, False
        if cands:
            # Title collides but no author agreement — create, flag for review.
            wid = _create(db, canonical=canonical, original_language=original_language,
                          notes=notes, english_title=english_title,
                          original_titles=original_titles)
            return wid, True, True

    # 4. fresh work.
    wid = _create(db, canonical=canonical, original_language=original_language,
                  notes=notes, english_title=english_title, original_titles=original_titles)
    return wid, True, False
