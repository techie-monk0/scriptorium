"""Canonical contributor data-access (FRBR Phase C).

Single home for the two contributor concerns, so every call site routes through
here instead of hand-writing SQL against work_contributor / edition_work:

  • AUTHOR  → lives on the WORK   → `work_author(work_id, person_id, role)`
  • TRANSLATOR → lives on the EDITION → `edition_translator(edition_id, person_id, seq)`,
    with a nullable per-work OVERRIDE `edition_work.translator_person_id`
    (NULL ⇒ inherit the edition's translator set; used only for mixed-translator
    anthologies, of which the corpus currently has none).

Because all contributor storage logic is here, Phase D (dropping `work_contributor`)
only has to touch this module + the schema, not the ~50 former call sites.

NOTE: distinct from `contributors.py`, which is the ingest-time LLM resolver that
reads a title page; this module is the persistence layer for the resolved result.

Person-identity ops (merge/split/delete/GC) re-point or detach a person across
BOTH homes; `repoint_person`, `detach_person`, and `person_referenced` centralize
that so names.py / contributor_edit.py / promote.py share one definition.
"""
from __future__ import annotations

# ── AUTHORS (work_author) ─────────────────────────────────────────────────────
def work_author_rows(db, wid: int) -> list[tuple[int, str]]:
    """[(person_id, role)] for a work, ordered for stable display."""
    return db.execute(
        "SELECT person_id, role FROM work_author WHERE work_id = ? ORDER BY role, person_id",
        (wid,)).fetchall()


def work_author_ids(db, wid: int) -> list[int]:
    return [r[0] for r in db.execute(
        "SELECT person_id FROM work_author WHERE work_id = ? ORDER BY person_id",
        (wid,)).fetchall()]


def add_work_author(db, wid: int, pid: int, role: str = "author") -> None:
    db.execute("INSERT OR IGNORE INTO work_author (work_id, person_id, role) "
               "VALUES (?, ?, ?)", (wid, pid, role))


def set_work_authors(db, wid: int, desired: set) -> set:
    """Reconcile a work's authors to `desired` = {(person_id, role)}. Returns the
    person_ids whose author edge on this work was removed (caller may GC)."""
    current = {(p, r) for p, r in work_author_rows(db, wid)}
    removed = set()
    for pid, role in current - desired:
        db.execute("DELETE FROM work_author WHERE work_id = ? AND person_id = ? AND role = ?",
                   (wid, pid, role))
        removed.add(pid)
    for pid, role in desired - current:
        add_work_author(db, wid, pid, role)
    return removed


# ── TRANSLATORS (edition_translator + per-work override) ──────────────────────
def edition_translator_ids(db, eid: int) -> list[int]:
    """The edition's translator person-ids, ordered by seq (book-level set)."""
    return [r[0] for r in db.execute(
        "SELECT person_id FROM edition_translator WHERE edition_id = ? ORDER BY seq, person_id",
        (eid,)).fetchall()]


def add_edition_translator(db, eid: int, pid: int) -> None:
    """Append a translator to an edition (seq = next position). Idempotent."""
    if pid is None:
        return
    nxt = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM edition_translator WHERE edition_id = ?",
        (eid,)).fetchone()[0]
    db.execute("INSERT OR IGNORE INTO edition_translator (edition_id, person_id, seq) "
               "VALUES (?, ?, ?)", (eid, pid, nxt))


# ── EDITION authors (book-level authorship; the home for a degenerate book's
#    author, so it can drop its work without losing authorship) ────────────────
def edition_author_ids(db, eid: int) -> list[int]:
    """The edition's author person-ids, ordered by seq."""
    return [r[0] for r in db.execute(
        "SELECT person_id FROM edition_author WHERE edition_id = ? ORDER BY seq, person_id",
        (eid,)).fetchall()]


def add_edition_author(db, eid: int, pid: int, role: str = "author") -> None:
    """Append an author to an edition (seq = next position). Idempotent."""
    if pid is None:
        return
    nxt = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM edition_author WHERE edition_id = ?",
        (eid,)).fetchone()[0]
    db.execute("INSERT OR IGNORE INTO edition_author (edition_id, person_id, role, seq) "
               "VALUES (?, ?, ?, ?)", (eid, pid, role, nxt))


def set_edition_authors(db, eid: int, pids, *, role: str = "author") -> set:
    """Reconcile an edition's author set to `pids` (ordered iterable). Returns the
    person_ids removed (caller may GC). De-dupes while preserving order."""
    seen, ordered = set(), []
    for p in pids:
        if p is not None and p not in seen:
            seen.add(p); ordered.append(p)
    current = set(edition_author_ids(db, eid))
    removed = current - seen
    db.execute("DELETE FROM edition_author WHERE edition_id = ?", (eid,))
    for seq, pid in enumerate(ordered, 1):
        db.execute("INSERT OR IGNORE INTO edition_author (edition_id, person_id, role, seq) "
                   "VALUES (?, ?, ?, ?)", (eid, pid, role, seq))
    return removed


def set_edition_translators(db, eid: int, pids) -> set:
    """Reconcile an edition's translator set to `pids` (ordered iterable). Returns
    the person_ids removed (caller may GC). De-dupes while preserving order."""
    seen, ordered = set(), []
    for p in pids:
        if p is not None and p not in seen:
            seen.add(p); ordered.append(p)
    current = set(edition_translator_ids(db, eid))
    removed = current - seen
    db.execute("DELETE FROM edition_translator WHERE edition_id = ?", (eid,))
    for seq, pid in enumerate(ordered, 1):
        db.execute("INSERT OR IGNORE INTO edition_translator (edition_id, person_id, seq) "
                   "VALUES (?, ?, ?)", (eid, pid, seq))
    return removed


def link_work(db, eid: int, wid: int, sequence: int | None = None) -> int:
    """Link contained work `wid` to edition `eid` (manifestation edge). Idempotent —
    no-op if already linked. Auto-assigns the next sequence when none is given.
    Returns the sequence used. Centralises edition_work writes for works_apply."""
    row = db.execute("SELECT sequence FROM edition_work WHERE edition_id = ? AND work_id = ?",
                     (eid, wid)).fetchone()
    if row:
        return row[0]
    if sequence is None:
        sequence = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM edition_work "
                              "WHERE edition_id = ?", (eid,)).fetchone()[0]
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, ?)",
               (eid, wid, sequence))
    return sequence


def unlink_work(db, eid: int, wid: int) -> None:
    """Remove the edition_work edge between `eid` and `wid` (does not touch the work
    row — caller decides whether the now-possibly-orphaned work is dropped)."""
    db.execute("DELETE FROM edition_work WHERE edition_id = ? AND work_id = ?", (eid, wid))


def work_translator(db, eid: int, wid: int) -> int | None:
    """Effective translator for contained work `wid` in edition `eid`:
    the per-work override if set, else the edition's first translator."""
    row = db.execute(
        "SELECT translator_person_id FROM edition_work WHERE edition_id = ? AND work_id = ?",
        (eid, wid)).fetchone()
    if row and row[0] is not None:
        return row[0]
    ids = edition_translator_ids(db, eid)
    return ids[0] if ids else None


# ── PERSON identity ops (merge / split / delete / GC) ─────────────────────────
def repoint_person(db, dup: int, canon: int) -> None:
    """Re-point every contributor edge from person `dup` onto `canon`, across both
    homes (work_author, edition_translator) and the edition_work override. Used by
    the person-merge / split paths. OR IGNORE + delete handles PK collisions."""
    if dup == canon:
        return
    db.execute("UPDATE OR IGNORE work_author SET person_id = ? WHERE person_id = ?",
               (canon, dup))
    db.execute("DELETE FROM work_author WHERE person_id = ?", (dup,))
    db.execute("UPDATE OR IGNORE edition_translator SET person_id = ? WHERE person_id = ?",
               (canon, dup))
    db.execute("DELETE FROM edition_translator WHERE person_id = ?", (dup,))
    db.execute("UPDATE OR IGNORE edition_author SET person_id = ? WHERE person_id = ?",
               (canon, dup))
    db.execute("DELETE FROM edition_author WHERE person_id = ?", (dup,))
    db.execute("UPDATE edition_work SET translator_person_id = ? "
               "WHERE translator_person_id = ?", (canon, dup))


def detach_person(db, pid: int) -> None:
    """Remove a person from every contributor edge (both homes + override). Used by
    the person-delete path before deleting the person row."""
    db.execute("DELETE FROM work_author WHERE person_id = ?", (pid,))
    db.execute("DELETE FROM edition_translator WHERE person_id = ?", (pid,))
    db.execute("DELETE FROM edition_author WHERE person_id = ?", (pid,))
    db.execute("UPDATE edition_work SET translator_person_id = NULL "
               "WHERE translator_person_id = ?", (pid,))


def person_referenced(db, pid: int) -> bool:
    """True if any contributor edge still references the person (GC guard)."""
    return db.execute(
        "SELECT 1 FROM work_author WHERE person_id = ? "
        "UNION ALL SELECT 1 FROM edition_author WHERE person_id = ? "
        "UNION ALL SELECT 1 FROM edition_translator WHERE person_id = ? "
        "UNION ALL SELECT 1 FROM edition_work WHERE translator_person_id = ? LIMIT 1",
        (pid, pid, pid, pid)).fetchone() is not None


def person_edition_ids_as_author(db, pid: int) -> list[int]:
    """Editions a person authored at the book level (edition_author)."""
    return [r[0] for r in db.execute(
        "SELECT edition_id FROM edition_author WHERE person_id = ? ORDER BY edition_id",
        (pid,)).fetchall()]


# ── PERSON-centric reads ──────────────────────────────────────────────────────
def person_work_ids(db, pid: int) -> list[int]:
    """Distinct works a person is attached to: authored (work_author) OR translated
    (edition_translator → the editions' works, plus any per-work override)."""
    return [r[0] for r in db.execute(
        "SELECT DISTINCT wid FROM ("
        "  SELECT work_id AS wid FROM work_author WHERE person_id = ?"
        "  UNION SELECT ew.work_id FROM edition_translator et "
        "        JOIN edition_work ew ON ew.edition_id = et.edition_id WHERE et.person_id = ?"
        "  UNION SELECT work_id FROM edition_work WHERE translator_person_id = ?"
        ") ORDER BY wid", (pid, pid, pid)).fetchall()]


def person_edition_ids_as_translator(db, pid: int) -> list[int]:
    """Editions a person translated (book-level set ∪ per-work override)."""
    return [r[0] for r in db.execute(
        "SELECT DISTINCT eid FROM ("
        "  SELECT edition_id AS eid FROM edition_translator WHERE person_id = ?"
        "  UNION SELECT edition_id FROM edition_work WHERE translator_person_id = ?"
        ") ORDER BY eid", (pid, pid)).fetchall()]
