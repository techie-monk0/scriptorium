"""Referential + completeness integrity checks for the catalogue graph.

The catalogue is a small directed graph of records joined by link tables::

    person ──< work_author >── work ──< edition_work >── edition ──< holding
       └──────< edition_translator >─────────────────────┘
                         (edition_work.translator_person_id = per-work override)

Every link must reference a live row. SQLite ENFORCES this through the schema's
``ON DELETE CASCADE`` / ``ON DELETE SET NULL`` clauses — **but only when
``PRAGMA foreign_keys = ON``**, which `db.connect()` sets and which SQLite
otherwise defaults OFF. So every in-app write is safe, yet a write through a raw
connection (the ``sqlite3`` CLI, or a script that forgot the pragma) could leave
a dangling reference. This module is the safety net: it DETECTS those.

Two severities:
  * **errors**   — dangling references (a link whose target row is gone). These
    must always be zero; they are real corruption.
  * **warnings** — completeness gaps that are valid referentially but worth
    surfacing (an author-less work, an edition with no file, a person recorded as
    both author of a work and translator of an edition of it).

`check_integrity(db)` returns a structured report; `assert_integrity(db)` raises
on any error (handy as a post-condition in tests / after a bulk mutation). The
CLI (`python -m catalogue.db_store.integrity [db]`) prints the report and exits non-zero
on any error.
"""

from __future__ import annotations

import sqlite3
from .paths import default_db_path


class IntegrityError(AssertionError):
    """Raised by `assert_integrity` when dangling references are found."""


# Each entry: (human label, SQL selecting one column of offending row ids).
# A non-empty result = a problem of that class. These cover every link that
# references person / work / edition / holding — the spanning set of the graph.
DANGLING_CHECKS = [
    # ── links → person ──────────────────────────────────────────────────────
    ("work_author.person_id → person (orphan author)",
     "SELECT wa.rowid FROM work_author wa "
     "LEFT JOIN person p ON p.id = wa.person_id WHERE p.id IS NULL"),
    ("edition_translator.person_id → person (orphan translator)",
     "SELECT et.rowid FROM edition_translator et "
     "LEFT JOIN person p ON p.id = et.person_id WHERE p.id IS NULL"),
    ("edition_work.translator_person_id → person (orphan override translator)",
     "SELECT ew.rowid FROM edition_work ew LEFT JOIN person p ON p.id = ew.translator_person_id "
     "WHERE ew.translator_person_id IS NOT NULL AND p.id IS NULL"),
    ("person_alias.person_id → person",
     "SELECT a.id FROM person_alias a "
     "LEFT JOIN person p ON p.id = a.person_id WHERE p.id IS NULL"),
    ("person_external_id.person_id → person",
     "SELECT x.rowid FROM person_external_id x "
     "LEFT JOIN person p ON p.id = x.person_id WHERE p.id IS NULL"),
    # ── links → work ────────────────────────────────────────────────────────
    ("work_author.work_id → work",
     "SELECT wa.rowid FROM work_author wa "
     "LEFT JOIN work w ON w.id = wa.work_id WHERE w.id IS NULL"),
    ("edition_work.work_id → work",
     "SELECT ew.rowid FROM edition_work ew "
     "LEFT JOIN work w ON w.id = ew.work_id WHERE w.id IS NULL"),
    ("work_alias.work_id → work",
     "SELECT a.id FROM work_alias a "
     "LEFT JOIN work w ON w.id = a.work_id WHERE w.id IS NULL"),
    # ── links → edition ─────────────────────────────────────────────────────
    ("edition_work.edition_id → edition",
     "SELECT ew.rowid FROM edition_work ew "
     "LEFT JOIN edition e ON e.id = ew.edition_id WHERE e.id IS NULL"),
    ("edition_translator.edition_id → edition",
     "SELECT et.rowid FROM edition_translator et "
     "LEFT JOIN edition e ON e.id = et.edition_id WHERE e.id IS NULL"),
    # ── links → edition (holding) ───────────────────────────────────────────
    ("holding.edition_id → edition",
     "SELECT h.id FROM holding h "
     "LEFT JOIN edition e ON e.id = h.edition_id WHERE e.id IS NULL"),
]


# ── edition-identity stability (S1/S2) — the promise external tools rely on ─────────
# Static invariants a DB must satisfy at rest; the temporal guarantees (pub_id write-once,
# never reused) are enforced by the edition_pub_id_immutable/mint triggers + the unique index,
# not scannable here. On a not-yet-migrated DB these skip via `_run`'s no-such-column tolerance.
# See docs/access/external_tool_dependency_contract.md (S1–S3) and citation_edition_contract_plan.md §3.
STABILITY_CHECKS = [
    ("edition.pub_id missing (S1: every edition carries a stable token)",
     "SELECT id FROM edition WHERE pub_id IS NULL"),
    ("edition.pub_id duplicated (S1: a token is never shared or reused)",
     "SELECT pub_id FROM edition WHERE pub_id IS NOT NULL "
     "GROUP BY pub_id HAVING COUNT(*) > 1"),
    ("edition.superseded_by → edition (S2: a forwarding pointer must resolve)",
     "SELECT e.id FROM edition e LEFT JOIN edition w ON w.id = e.superseded_by "
     "WHERE e.superseded_by IS NOT NULL AND w.id IS NULL"),
    ("edition.superseded_by = self (S2: no self-forward)",
     "SELECT id FROM edition WHERE superseded_by = id"),
    ("edition.superseded_by cycle (S2: forwarding chains must terminate)",
     "WITH RECURSIVE chain(start, cur, depth) AS ("
     " SELECT id, superseded_by, 1 FROM edition WHERE superseded_by IS NOT NULL "
     " UNION ALL "
     " SELECT c.start, e.superseded_by, c.depth + 1 "
     " FROM chain c JOIN edition e ON e.id = c.cur "
     " WHERE e.superseded_by IS NOT NULL AND c.depth < 100"
     ") SELECT DISTINCT start FROM chain WHERE cur = start"),
]

COMPLETENESS_CHECKS = [
    ("work with no author (author unassigned)",
     "SELECT w.id FROM work w "
     "WHERE NOT EXISTS (SELECT 1 FROM work_author wa WHERE wa.work_id = w.id)"),
    ("work with no edition (unreachable composition)",
     "SELECT w.id FROM work w "
     "WHERE NOT EXISTS (SELECT 1 FROM edition_work ew WHERE ew.work_id = w.id)"),
    ("edition with no work (no contained composition)",
     "SELECT e.id FROM edition e "
     "WHERE NOT EXISTS (SELECT 1 FROM edition_work ew WHERE ew.edition_id = e.id)"),
    # A multi-text edition must be represented by its CONSTITUENT works, never by a single
    # "whole-book" work standing for the entire edition — that container work is a
    # cataloguing mistake. Flags a live work whose title equals a live edition's title where
    # that edition is NOT a single work (structure='multi_work' OR ≥2 other live constituent
    # works). A WARNING, not an error: the operator may keep it deliberately (override).
    ("whole-book work duplicating a multi-work edition (should be edition-only)",
     "SELECT DISTINCT w.id FROM work w "
     "JOIN work_alias a ON a.work_id = w.id AND a.scheme = 'english' "
     "JOIN edition e ON e.title = a.text AND e.deleted_at IS NULL "
     "WHERE w.deleted_at IS NULL AND ("
     "  e.structure = 'multi_work' OR "
     "  (SELECT COUNT(*) FROM edition_work ew JOIN work w2 ON w2.id = ew.work_id "
     "   WHERE ew.edition_id = e.id AND w2.deleted_at IS NULL AND w2.id <> w.id) >= 2)"),
    ("edition with no holding (no file/copy)",
     "SELECT e.id FROM edition e "
     "WHERE NOT EXISTS (SELECT 1 FROM holding h WHERE h.edition_id = e.id)"),
    ("person both authors a work AND translates an edition of it (resolver mirror)",
     "SELECT DISTINCT wa.person_id FROM work_author wa "
     "JOIN edition_work ew ON ew.work_id = wa.work_id "
     "JOIN edition_translator et ON et.edition_id = ew.edition_id "
     "AND et.person_id = wa.person_id"),
    ("person with no name",
     "SELECT id FROM person WHERE primary_name IS NULL OR TRIM(primary_name) = ''"),
    # Duplicates that authority-dedup should collapse: rows DIRECTLY sharing a hub
    # id or a cross-link value. (Only direct sharing — the transitive closure is
    # the person_dedup batch's job; this warning just nudges you to run it.)
    ("persons sharing an authority identity (run person-dedup batch)",
     "SELECT p.id FROM person p WHERE p.external_id IS NOT NULL AND EXISTS "
     "(SELECT 1 FROM person q WHERE q.external_id = p.external_id AND q.id <> p.id) "
     "UNION "
     "SELECT pe.person_id FROM person_external_id pe WHERE EXISTS "
     "(SELECT 1 FROM person_external_id qe WHERE qe.value = pe.value "
     "AND qe.person_id <> pe.person_id)"),
    # Bound but its cross-link harvest failed (network down) → partial key-set that
    # dedup can't fully see. Re-harvest these (authority_dedup_plan.md §6.17).
    ("person bound but harvest incomplete (re-harvest pending)",
     "SELECT id FROM person WHERE harvest_incomplete = 1"),
]


def foreign_keys_on(db) -> bool:
    """Whether THIS connection enforces foreign keys (cascades fire only if so)."""
    return bool(db.execute("PRAGMA foreign_keys").fetchone()[0])


def _run(db, checks, sample):
    out = []
    for label, sql in checks:
        try:
            rows = db.execute(sql).fetchall()
        except sqlite3.OperationalError as e:
            # A check referencing a column added by a later migration must not crash
            # the whole report on a not-yet-migrated DB (e.g. the live file opened via
            # connect(), which doesn't migrate). No column → no offenders → skip it.
            if "no such column" in str(e):
                continue
            raise
        if rows:
            out.append({"check": label, "count": len(rows),
                        "sample": [r[0] for r in rows[:sample]]})
    return out


def check_integrity(db, *, sample: int = 5) -> dict:
    """Full report: {ok, foreign_keys_on, errors[], warnings[]}.

    `errors` = dangling references (PRAGMA foreign_key_check — the generic,
    authoritative scan over every DECLARED foreign key — plus the explicit
    labelled scans above, which also catch a legacy table that lost its FK
    declaration). `warnings` = completeness gaps. `sample` bounds the example ids
    listed per finding."""
    errors = []
    # Schema drift first: a DB missing a column the code writes to is a more
    # fundamental error than a dangling row (and is WHY _run below has to tolerate
    # `no such column`). Surfacing it here means /integrity and the CLI catch the
    # silent-write-loss condition instead of skipping past it.
    from .db import schema_drift
    drift = schema_drift(db)
    if any(drift.values()):
        bits = []
        if drift["missing_tables"]:
            bits.append(f"tables {drift['missing_tables']}")
        for t, c in drift["missing_columns"].items():
            bits.append(f"{t}.{c}")
        for kind in ("indexes", "triggers", "views"):
            if drift[f"missing_{kind}"]:
                bits.append(f"{kind} {drift[f'missing_{kind}']}")
        errors.append({"check": "schema drift (DB behind code — writes may be lost)",
                       "count": len(bits), "sample": bits[:sample]})
    fk_violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        errors.append({"check": "PRAGMA foreign_key_check (declared FK violations)",
                       "count": len(fk_violations),
                       "sample": [tuple(r) for r in fk_violations[:sample]]})
    errors.extend(_run(db, DANGLING_CHECKS, sample))
    errors.extend(_run(db, STABILITY_CHECKS, sample))   # edition-identity S1/S2 (→ verified_commit)
    warnings = _run(db, COMPLETENESS_CHECKS, sample)
    return {"ok": not errors, "foreign_keys_on": foreign_keys_on(db),
            "errors": errors, "warnings": warnings}


def assert_integrity(db) -> None:
    """Raise `IntegrityError` if any dangling reference exists (no-op otherwise).
    A drop-in post-condition for tests and bulk mutations."""
    rep = check_integrity(db)
    if rep["errors"]:
        lines = "\n".join(f"  {e['count']:>6}  {e['check']}  e.g. {e['sample']}"
                          for e in rep["errors"])
        raise IntegrityError("dangling references found:\n" + lines)


def verified_commit(db) -> None:
    """The atomic post-condition for a link-moving op (merge / dedupe / delete /
    split): assert the DB is referentially intact, THEN commit. If a re-point was
    missed — a work_author / edition_translator / … row left pointing at a now-deleted
    person — `assert_integrity` raises, and we ROLL BACK the whole transaction and
    re-raise, so the operation fails as a unit and the DB stays at its last consistent
    committed state (never half-merged). Use in place of `db.commit()` anywhere a
    merge/delete/split is being committed."""
    try:
        assert_integrity(db)
    except IntegrityError:
        db.rollback()              # undo the entire in-flight op — leave nothing dangling
        raise
    db.commit()


def main(argv=None) -> None:
    import argparse
    import sys
    from .db import connect
    ap = argparse.ArgumentParser(
        description="Catalogue referential + completeness integrity check.")
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--warnings", action="store_true",
                    help="also list completeness warnings even when integrity is OK")
    args = ap.parse_args(argv)
    db = connect(args.db)
    rep = check_integrity(db)
    print(f"foreign_keys: {'ON' if rep['foreign_keys_on'] else 'OFF'}")
    if rep["ok"]:
        print("✓ referential integrity OK — no dangling references")
    else:
        print(f"✗ {len(rep['errors'])} integrity ERROR class(es) — DANGLING REFERENCES:")
        for e in rep["errors"]:
            print(f"   {e['count']:>6}  {e['check']}\n             e.g. {e['sample']}")
    if args.warnings or not rep["ok"]:
        if rep["warnings"]:
            print("completeness warnings (not corruption):")
            for w in rep["warnings"]:
                print(f"   {w['count']:>6}  {w['check']}  e.g. {w['sample']}")
    sys.exit(1 if rep["errors"] else 0)


if __name__ == "__main__":
    main()
