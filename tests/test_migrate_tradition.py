"""Tests for catalogue/db_store/migrate_tradition.py — Phase 1 deterministic rules."""
from __future__ import annotations

from catalogue.db_store import add_alias, init_db
from catalogue.db_store import migrate_tradition as M


# ── helpers ──────────────────────────────────────────────────────────────────
def _person(db, name, external_id=None):
    return db.execute("INSERT INTO person (primary_name, external_id) VALUES (?,?)",
                      (name, external_id)).lastrowid


def _work(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", wid, title, "english")
    return wid


def _edition(db, title):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _subject(db, name):
    return db.execute("INSERT OR IGNORE INTO subject (name) VALUES (?)", (name,)) \
             and db.execute("SELECT id FROM subject WHERE name=?", (name,)).fetchone()[0]


def _tag_work_subject(db, wid, name):
    db.execute("INSERT OR IGNORE INTO work_subject (work_id, subject_id) VALUES (?,?)",
               (wid, _subject(db, name)))


def _tag_edition_subject(db, eid, name):
    db.execute("INSERT OR IGNORE INTO edition_subject (edition_id, subject_id) VALUES (?,?)",
               (eid, _subject(db, name)))


def _author(db, wid, person_id):
    db.execute("INSERT OR IGNORE INTO work_author (work_id, person_id) VALUES (?,?)",
               (wid, person_id))


def _link(db, eid, wid):
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
               (eid, wid))


def _labels(db, wid):
    """work_id → {tradition_name: (round(conf,3), source)} from work_tradition."""
    return {r[0]: (round(r[1], 3), r[2]) for r in db.execute(
        "SELECT t.name, wt.confidence, wt.source FROM work_tradition wt "
        "JOIN tradition t ON t.id=wt.tradition_id WHERE wt.work_id=?", (wid,)).fetchall()}


# ── subject rule ─────────────────────────────────────────────────────────────
def test_subject_rule_sakya(tmp_path):
    db = init_db(tmp_path / "t.db")
    w = _work(db, "A Sakya text")
    _tag_work_subject(db, w, "Buddhism/Sakya")
    db.commit()
    M.migrate(db)
    assert _labels(db, w) == {"Sakya": (0.9, "rule-subject")}


def test_subject_rule_multilabel_kagyu_mahamudra(tmp_path):
    db = init_db(tmp_path / "t.db")
    w = _work(db, "Gelug-Kagyu Mahāmudrā")
    _tag_work_subject(db, w, "Buddhism/Kagyu Mahamudra")
    db.commit()
    M.migrate(db)
    assert _labels(db, w) == {"Gelug": (0.85, "rule-subject"),
                              "Kagyu": (0.85, "rule-subject")}


def test_edition_subject_is_lifted_to_work(tmp_path):
    """A tradition-bearing subject on the EDITION (not the work) still classifies the work."""
    db = init_db(tmp_path / "t.db")
    w = _work(db, "Dzogchen text")
    e = _edition(db, "An edition")
    _link(db, e, w)
    _tag_edition_subject(db, e, "Buddhism/Dzogchen")
    db.commit()
    M.migrate(db)
    assert _labels(db, w) == {"Nyingma": (0.9, "rule-subject")}


# ── author rule ──────────────────────────────────────────────────────────────
def test_author_rule_gelug(tmp_path):
    db = init_db(tmp_path / "t.db")
    p = _person(db, "Tsongkhapa", "wikidata:Q323439")
    w = _work(db, "Lamrim Chenmo")
    _author(db, w, p)
    _tag_work_subject(db, w, "Buddhism/Lam Rim")   # topic subject: no tradition of its own
    db.commit()
    M.migrate(db)
    assert _labels(db, w) == {"Gelug": (0.85, "rule-author")}


def test_author_rule_common_indian_source(tmp_path):
    db = init_db(tmp_path / "t.db")
    p = _person(db, "Nāgārjuna", "bdr:P4954")
    w = _work(db, "Mūlamadhyamakakārikā")
    _author(db, w, p)
    _tag_work_subject(db, w, "Buddhism/Emptiness")
    db.commit()
    M.migrate(db)
    # Indian source ⇒ Common, and NOT presumed-Gelug despite being a Buddhist work.
    assert _labels(db, w) == {"Common": (0.85, "rule-author")}


# ── agreement boost ──────────────────────────────────────────────────────────
def test_agreement_boost(tmp_path):
    db = init_db(tmp_path / "t.db")
    p = _person(db, "Longchenpa", "wikidata:Q1708503")   # author ⇒ Nyingma
    w = _work(db, "Dzogchen treatise")
    _author(db, w, p)
    _tag_work_subject(db, w, "Buddhism/Dzogchen")        # subject ⇒ Nyingma (agree)
    db.commit()
    M.migrate(db)
    conf, _ = _labels(db, w)["Nyingma"]
    assert conf == 0.95   # max(0.9, 0.85) + 0.05 bonus


# ── presumed-Gelug fallback ──────────────────────────────────────────────────
def test_default_gelug_for_unclassified_buddhist_work(tmp_path):
    db = init_db(tmp_path / "t.db")
    w = _work(db, "A general Buddhist book")
    _tag_work_subject(db, w, "Buddhism/Meditation")   # topic only, unmapped author
    db.commit()
    M.migrate(db)
    assert _labels(db, w) == {"Gelug": (0.5, "rule-default")}


def test_no_default_gelug_flag(tmp_path):
    db = init_db(tmp_path / "t.db")
    w = _work(db, "A general Buddhist book")
    _tag_work_subject(db, w, "Buddhism/Meditation")
    db.commit()
    M.migrate(db, default_gelug=False)
    assert _labels(db, w) == {}


def test_non_buddhist_work_gets_no_default(tmp_path):
    db = init_db(tmp_path / "t.db")
    w = _work(db, "A cookbook")
    _tag_work_subject(db, w, "Cooking")
    db.commit()
    M.migrate(db)
    assert _labels(db, w) == {}


# ── manual rows preserved ────────────────────────────────────────────────────
def test_manual_rows_preserved_and_suppress_default(tmp_path):
    db = init_db(tmp_path / "t.db")
    w = _work(db, "Human-verdict work")
    _tag_work_subject(db, w, "Buddhism/Meditation")     # would otherwise default to Gelug
    kagyu = db.execute("SELECT id FROM tradition WHERE name='Kagyu'").fetchone()[0]
    db.execute("INSERT INTO work_tradition (work_id, tradition_id, confidence, source) "
               "VALUES (?,?,1.0,'human')", (w, kagyu))
    db.commit()
    M.migrate(db)
    # human verdict survives; the default rule did NOT add a competing Gelug row.
    assert _labels(db, w) == {"Kagyu": (1.0, "human")}


# ── idempotency ──────────────────────────────────────────────────────────────
def test_idempotent_rerun(tmp_path):
    db = init_db(tmp_path / "t.db")
    p = _person(db, "Tsongkhapa", "wikidata:Q323439")
    w = _work(db, "Lamrim")
    _author(db, w, p)
    _tag_work_subject(db, w, "Buddhism/Meditation")
    db.commit()
    first = M.migrate(db)
    second = M.migrate(db)
    assert second["rows_inserted"] == first["rows_inserted"]
    assert second["rule_rows_cleared"] == first["rows_inserted"]   # cleared what it wrote
    assert _labels(db, w) == {"Gelug": (0.85, "rule-author")}


# ── person lineage seeding ───────────────────────────────────────────────────
def test_seed_person_tradition_from_config(tmp_path):
    db = init_db(tmp_path / "t.db")
    p = _person(db, "Tsongkhapa", "wikidata:Q323439")   # Gelug in the author map
    q = _person(db, "Longchenpa", "wikidata:Q1708503")  # Nyingma
    db.commit()
    M.migrate(db)
    got = dict(db.execute("SELECT id, tradition FROM person").fetchall())
    assert got[p] == "Gelug" and got[q] == "Nyingma"


def test_seed_person_tradition_never_overwrites_human_edit(tmp_path):
    db = init_db(tmp_path / "t.db")
    p = _person(db, "Tsongkhapa", "wikidata:Q323439")
    db.execute("UPDATE person SET tradition='Kagyu' WHERE id=?", (p,))   # a human override
    db.commit()
    M.migrate(db)
    assert db.execute("SELECT tradition FROM person WHERE id=?", (p,)).fetchone()[0] == "Kagyu"


# ── verify gate ──────────────────────────────────────────────────────────────
def test_verify_gate_rejects_unseeded_tradition(tmp_path, monkeypatch):
    db = init_db(tmp_path / "t.db")
    w = _work(db, "x")
    _tag_work_subject(db, w, "Buddhism/Bogus")
    db.commit()
    monkeypatch.setitem(M.SUBJECT_TRADITION, "Buddhism/Bogus", [("Bön", 0.9)])
    try:
        M.migrate(db)
        assert False, "expected MigrationError"
    except M.MigrationError as e:
        assert "Bön" in str(e)
