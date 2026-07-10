"""Tests for catalogue/migrate_frbr.py — additive Phase-B population of the FRBR homes."""
from __future__ import annotations

from catalogue.db_store import add_alias, init_db
from catalogue.db_store import migrate_frbr as M


def _person(db, name):
    return db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid


def _work(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


def _edition(db, title):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _legacy(db):
    """Re-create the pre-FRBR `work_contributor` table (Phase D dropped it from the
    schema). The migration's job is to drain it, so the test must supply it."""
    db.execute("CREATE TABLE IF NOT EXISTS work_contributor ("
               "work_id INTEGER NOT NULL, person_id INTEGER NOT NULL, role TEXT, "
               "PRIMARY KEY (work_id, person_id, role))")


def _seed(db):
    """One edition with one work; author via work_contributor; translator via BOTH
    the edition_work override AND a work_contributor translator row (the two legacy
    homes the migration unions)."""
    _legacy(db)
    auth = _person(db, "Śāntideva")
    tr1 = _person(db, "Translator A")           # via edition_work override
    tr2 = _person(db, "Translator B")           # via work_contributor role='translator'
    w = _work(db, "Bodhicaryāvatāra")
    e = _edition(db, "The Way of the Bodhisattva")
    db.execute("INSERT INTO work_contributor (work_id, person_id, role) VALUES (?,?,'author')",
               (w, auth))
    db.execute("INSERT INTO work_contributor (work_id, person_id, role) VALUES (?,?,'translator')",
               (w, tr2))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence, translator_person_id) "
               "VALUES (?,?,1,?)", (e, w, tr1))
    db.commit()
    return auth, tr1, tr2, w, e


def test_populate_work_author_from_contributor(tmp_path):
    db = init_db(tmp_path / "wa.db")
    auth, tr1, tr2, w, e = _seed(db)
    M.populate_work_author(db)
    rows = db.execute("SELECT work_id, person_id, role FROM work_author").fetchall()
    assert rows == [(w, auth, "author")]        # translator NOT copied to work_author


def test_populate_edition_translator_unions_both_homes(tmp_path):
    db = init_db(tmp_path / "et.db")
    auth, tr1, tr2, w, e = _seed(db)
    M.populate_edition_translator(db)
    pids = {r[0] for r in db.execute(
        "SELECT person_id FROM edition_translator WHERE edition_id=?", (e,)).fetchall()}
    assert pids == {tr1, tr2}                    # override ∪ work_contributor translator


def test_migrate_is_idempotent(tmp_path):
    db = init_db(tmp_path / "idem.db")
    _seed(db)
    r1 = M.migrate(db)
    counts1 = (db.execute("SELECT COUNT(*) FROM work_author").fetchone()[0],
               db.execute("SELECT COUNT(*) FROM edition_translator").fetchone()[0])
    r2 = M.migrate(db)
    counts2 = (db.execute("SELECT COUNT(*) FROM work_author").fetchone()[0],
               db.execute("SELECT COUNT(*) FROM edition_translator").fetchone()[0])
    assert counts1 == counts2                    # no growth on re-run
    assert r2["work_author_inserted"] == 0 and r2["edition_translator_inserted"] == 0


def test_backfill_work_less_editions(tmp_path):
    db = init_db(tmp_path / "bf.db")
    _seed(db)
    orphan = _edition(db, "Orphan Book With No Work")
    db.commit()
    res = M.migrate(db, backfill=True)
    assert res["backfilled_editions"] == 1
    assert res["editions_without_work"] == 0
    # the orphan edition now links to a work whose title came from the edition
    wid = db.execute("SELECT work_id FROM edition_work WHERE edition_id=?",
                     (orphan,)).fetchone()[0]
    title = db.execute("SELECT text FROM work_alias WHERE work_id=?", (wid,)).fetchone()[0]
    assert title == "Orphan Book With No Work"


def test_no_backfill_leaves_orphan(tmp_path):
    db = init_db(tmp_path / "nbf.db")
    _seed(db)
    _edition(db, "Orphan")
    db.commit()
    res = M.migrate(db, backfill=False)
    assert res["backfilled_editions"] == 0
    assert res["editions_without_work"] == 1


def test_does_not_touch_legacy_tables(tmp_path):
    db = init_db(tmp_path / "legacy.db")
    auth, tr1, tr2, w, e = _seed(db)
    wc_before = db.execute("SELECT COUNT(*) FROM work_contributor").fetchone()[0]
    ew_tr = db.execute("SELECT translator_person_id FROM edition_work WHERE edition_id=?",
                       (e,)).fetchone()[0]
    M.migrate(db)
    # work_contributor untouched; edition_work override intact (app still reads them)
    assert db.execute("SELECT COUNT(*) FROM work_contributor").fetchone()[0] == wc_before
    assert db.execute("SELECT translator_person_id FROM edition_work WHERE edition_id=?",
                      (e,)).fetchone()[0] == ew_tr
