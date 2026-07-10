"""FRBR migration — Phase B (ADDITIVE population of the new homes).

Populates `work_author` and `edition_translator` from the existing
`work_contributor` / `edition_work.translator_person_id` data, and (optionally)
backfills work-less editions with a single whole-book work. It deliberately does
NOT modify or drop `work_contributor` or `edition_work.translator_person_id` — the
running app still reads those until Phase C repoints reads. So this is safe to run
against live: it only adds rows to tables nothing reads yet.

Idempotent (INSERT OR IGNORE throughout). Verify gates abort the run if the moved
counts don't reconcile. The new tables themselves are created by schema.sql
(CREATE TABLE IF NOT EXISTS on every init) — this module assumes they exist.

CLI:
    python -m catalogue.db_store.migrate_frbr report   [db]
    python -m catalogue.db_store.migrate_frbr migrate  [db] [--no-backfill]
"""
from __future__ import annotations

import argparse

from .db import add_alias, init_db
from .paths import default_db_path


class MigrationError(RuntimeError):
    """A verify gate failed — the migration is aborted before commit."""


def _has_legacy(db) -> bool:
    """True while the pre-FRBR `work_contributor` table still exists (Phase D drops
    it). The populate steps no-op once it's gone, so this module is safe to run
    against a fully-migrated DB."""
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_contributor'"
    ).fetchone() is not None


# ── population ────────────────────────────────────────────────────────────────
def populate_work_author(db) -> int:
    """work_author ← work_contributor WHERE role='author'. Returns rows inserted."""
    if not _has_legacy(db):
        return 0
    before = db.execute("SELECT COUNT(*) FROM work_author").fetchone()[0]
    db.execute(
        "INSERT OR IGNORE INTO work_author (work_id, person_id, role) "
        "SELECT work_id, person_id, 'author' FROM work_contributor WHERE role = 'author'")
    return db.execute("SELECT COUNT(*) FROM work_author").fetchone()[0] - before


def _edition_translator_sources(db):
    """(edition_id, person_id) pairs for every translator of an edition, from the
    per-work override column plus (while it exists) the legacy work_contributor."""
    sql = ("SELECT edition_id, translator_person_id FROM edition_work "
           "WHERE translator_person_id IS NOT NULL")
    if _has_legacy(db):
        sql += (" UNION SELECT ew.edition_id, wc.person_id FROM work_contributor wc "
                "JOIN edition_work ew ON ew.work_id = wc.work_id "
                "WHERE wc.role = 'translator'")
    rows = db.execute(sql).fetchall()
    by_edition: dict[int, set] = {}
    for eid, pid in rows:
        by_edition.setdefault(eid, set()).add(pid)
    return by_edition


def populate_edition_translator(db) -> int:
    """edition_translator ← DISTINCT translators per edition (override ∪ work_contributor),
    seq ordered by person_id. Returns rows inserted."""
    before = db.execute("SELECT COUNT(*) FROM edition_translator").fetchone()[0]
    for eid, pids in _edition_translator_sources(db).items():
        for seq, pid in enumerate(sorted(pids), 1):
            db.execute(
                "INSERT OR IGNORE INTO edition_translator (edition_id, person_id, seq) "
                "VALUES (?, ?, ?)", (eid, pid, seq))
    return db.execute("SELECT COUNT(*) FROM edition_translator").fetchone()[0] - before


def backfill_work_less_editions(db) -> int:
    """Every edition must be navigable to a work. For each edition with no
    edition_work, mint a whole-book single work titled from the edition and link it.
    Returns the number of editions backfilled."""
    eids = [r[0] for r in db.execute(
        "SELECT e.id FROM edition e WHERE NOT EXISTS "
        "(SELECT 1 FROM edition_work ew WHERE ew.edition_id = e.id)").fetchall()]
    for eid in eids:
        title = db.execute("SELECT title FROM edition WHERE id = ?", (eid,)).fetchone()[0]
        wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
        add_alias(db, "work", wid, title or f"edition#{eid}", "english")
        db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
                   (eid, wid))
    return len(eids)


# ── verify gates ────────────────────────────────────────────────────────────────
def _verify(db) -> dict:
    """Reconcile the moved data; raise MigrationError on any mismatch."""
    # 1. Every (work, author) edge in work_contributor is present in work_author.
    wc_authors = db.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT work_id, person_id "
        "FROM work_contributor WHERE role='author')").fetchone()[0] if _has_legacy(db) else 0
    wa = db.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT work_id, person_id FROM work_author)"
    ).fetchone()[0]
    if wa < wc_authors:
        raise MigrationError(
            f"work_author has {wa} (work,person) pairs < {wc_authors} author edges")

    # 2. Every edition that has a translator source has ≥1 edition_translator row.
    src = set(_edition_translator_sources(db).keys())
    covered = {r[0] for r in db.execute(
        "SELECT DISTINCT edition_id FROM edition_translator").fetchall()}
    missing = src - covered
    if missing:
        raise MigrationError(
            f"{len(missing)} edition(s) with a translator have no edition_translator row: "
            f"{sorted(missing)[:10]}")

    # 3. Every edition is now work-navigable (after backfill).
    orphan_eds = db.execute(
        "SELECT COUNT(*) FROM edition e WHERE NOT EXISTS "
        "(SELECT 1 FROM edition_work ew WHERE ew.edition_id = e.id)").fetchone()[0]
    return {"author_edges": wc_authors, "work_author_pairs": wa,
            "editions_with_translator": len(src), "editions_without_work": orphan_eds}


def migrate(db, *, backfill: bool = True, commit: bool = True) -> dict:
    """Run the additive Phase-B population with verify gates. Returns a summary.
    Aborts (no commit) if a gate fails."""
    if backfill:
        backfilled = backfill_work_less_editions(db)
    else:
        backfilled = 0
    wa = populate_work_author(db)
    et = populate_edition_translator(db)
    report = _verify(db)
    if commit:
        db.commit()
    return {"backfilled_editions": backfilled, "work_author_inserted": wa,
            "edition_translator_inserted": et, **report}


def absorb_legacy(db) -> bool:
    """Phase D, run automatically from db._migrate: if the legacy `work_contributor`
    table still exists, move its data into work_author / edition_translator (so no
    author/translator edge is lost) and DROP it. Idempotent — no-op once gone.
    Returns True if it dropped the table this call. Does NOT backfill work-less
    editions (that's a deliberate Phase-B step, not a plain-init side effect)."""
    if not _has_legacy(db):
        return False
    populate_work_author(db)
    populate_edition_translator(db)
    db.execute("DROP TABLE work_contributor")
    return True


def report(db) -> dict:
    """Dry preview of what migrate would touch (no writes)."""
    wc_authors = db.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT work_id, person_id "
        "FROM work_contributor WHERE role='author')").fetchone()[0] if _has_legacy(db) else 0
    src = _edition_translator_sources(db)
    work_less = db.execute(
        "SELECT COUNT(*) FROM edition e WHERE NOT EXISTS "
        "(SELECT 1 FROM edition_work ew WHERE ew.edition_id = e.id)").fetchone()[0]
    return {"author_edges_to_move": wc_authors,
            "editions_with_translator": len(src),
            "translator_pairs": sum(len(v) for v in src.values()),
            "work_less_editions_to_backfill": work_less,
            "already_in_work_author": db.execute(
                "SELECT COUNT(*) FROM work_author").fetchone()[0],
            "already_in_edition_translator": db.execute(
                "SELECT COUNT(*) FROM edition_translator").fetchone()[0]}


# ── CLI ─────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("report", "migrate"):
        p = sub.add_parser(name)
        p.add_argument("db", nargs="?", default=default_db_path())
        if name == "migrate":
            p.add_argument("--no-backfill", action="store_true",
                           help="skip minting works for work-less editions")
    args = ap.parse_args(argv)

    db = init_db(args.db)
    if args.cmd == "report":
        for k, v in report(db).items():
            print(f"  {k}: {v}")
    else:
        try:
            res = migrate(db, backfill=not args.no_backfill)
        except MigrationError as e:
            print(f"ABORTED (verify gate failed): {e}")
            return 1
        print("migration complete:")
        for k, v in res.items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
