"""Tests for step-4 canonical promotion (catalogue/cip_promote.py): idempotent writes,
BDRC-id dedup of multiple editions onto one work, and dry-run leaving no trace."""
from __future__ import annotations

from catalogue.cli.cip_promote import promote_verified
from catalogue.db_store import init_db


def _edition(db, title):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


_KW = dict(
    bdrc_id="bdr:MW3KG147",
    ewts_title="dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal",
    bdrc_title_label="dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal/",
    bdrc_author_label="tsong kha pa blo bzang grags pa/",
    ewts_author="tsong kha pa blo bzang grags pa",
    author_display="Tsoṅ-kha-pa Blo-bzaṅ-grags-pa", dates="1357-1419")


def test_apply_creates_canonical_rows(tmp_path):
    db = init_db(str(tmp_path / "t.db"))
    eid = _edition(db, "Illuminating the Intent")
    r = promote_verified(db, eid, english_title="Illuminating the Intent", apply=True, **_KW)
    assert r.status == "promoted" and r.work_id
    w = db.execute("SELECT tibetan_title, canonical_system, canonical_number FROM work "
                   "WHERE id=?", (r.work_id,)).fetchone()
    # tibetan_title is the SHAD-STRIPPED BDRC authority label
    assert w[0] == "dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal"
    assert w[1] == "bdrc" and w[2] == "bdr:MW3KG147"
    # wylie + english aliases present
    schemes = {s for (s,) in db.execute(
        "SELECT scheme FROM work_alias WHERE work_id=?", (r.work_id,))}
    assert "wylie" in schemes and "english" in schemes
    # author linked with wylie alias + dates
    assert db.execute("SELECT role FROM work_author WHERE work_id=? AND person_id=?",
                      (r.work_id, r.person_id)).fetchone()[0] == "author"
    assert db.execute("SELECT dates FROM person WHERE id=?", (r.person_id,)).fetchone()[0] \
        == "1357-1419"
    assert db.execute("SELECT 1 FROM edition_work WHERE edition_id=? AND work_id=?",
                      (eid, r.work_id)).fetchone()


def test_idempotent_second_run_is_noop(tmp_path):
    db = init_db(str(tmp_path / "t.db"))
    eid = _edition(db, "Illuminating the Intent")
    promote_verified(db, eid, english_title="Illuminating the Intent", apply=True, **_KW)
    r2 = promote_verified(db, eid, english_title="Illuminating the Intent", apply=True, **_KW)
    assert r2.status == "already" and not [a for a in r2.actions if a[0] == "+"]
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM work_alias WHERE scheme='wylie'").fetchone()[0] == 1


def test_two_editions_same_bdrc_id_dedup_to_one_work(tmp_path):
    db = init_db(str(tmp_path / "t.db"))
    e1 = _edition(db, "Illuminating the Intent")            # Jinpa
    e2 = _edition(db, "Illumination of the Thought")        # Hopkins — same work
    r1 = promote_verified(db, e1, english_title="Illuminating the Intent", apply=True, **_KW)
    r2 = promote_verified(db, e2, english_title="Illumination of the Thought", apply=True, **_KW)
    assert r1.work_id == r2.work_id                          # one canonical work
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE work_id=?",
                      (r1.work_id,)).fetchone()[0] == 2
    # both English titles captured as aliases
    eng = {t for (t,) in db.execute(
        "SELECT text FROM work_alias WHERE work_id=? AND scheme='english'", (r1.work_id,))}
    assert eng == {"Illuminating the Intent", "Illumination of the Thought"}


def test_dry_run_leaves_no_trace(tmp_path):
    db = init_db(str(tmp_path / "t.db"))
    eid = _edition(db, "Illuminating the Intent")
    r = promote_verified(db, eid, english_title="Illuminating the Intent", apply=False, **_KW)
    assert r.status == "promoted" and r.actions          # it PLANNED writes
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 0   # but wrote nothing
    assert db.execute("SELECT COUNT(*) FROM work_alias").fetchone()[0] == 0
