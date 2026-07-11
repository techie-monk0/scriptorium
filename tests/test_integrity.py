"""Referential-integrity matrix: every link-mutating operation leaves the graph
sound, dangling references are detected + disallowed, and foreign keys are on.

The graph under test:
    person(author) ─< work_author >─ work ─< edition_work >─ edition ─< holding
    person(translator) ─< edition_translator >──────────────┘
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from catalogue.db_store import integrity as I
from catalogue.db_store import connect, init_db


def _graph(db):
    """A minimal but complete graph: author→work→edition(+holding), translator→edition."""
    p = db.execute("INSERT INTO person (primary_name) VALUES ('Author')").lastrowid
    p2 = db.execute("INSERT INTO person (primary_name) VALUES ('Translator')").lastrowid
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    e = db.execute("INSERT INTO edition (title) VALUES ('Ed')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, p))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e, w))
    db.execute("INSERT INTO edition_translator (edition_id, person_id, seq) VALUES (?,?,1)", (e, p2))
    db.execute("INSERT INTO holding (edition_id, form) VALUES (?, 'electronic')", (e,))
    db.commit()
    return {"p": p, "p2": p2, "w": w, "e": e}


# ── Foundations: FK enforcement + detection ─────────────────────────────────
def test_connect_enables_foreign_keys(tmp_path):
    db = init_db(tmp_path / "i.db")
    assert I.foreign_keys_on(db) is True
    db.close()
    assert I.foreign_keys_on(connect(tmp_path / "i.db")) is True


def test_clean_graph_has_no_errors(tmp_path):
    db = init_db(tmp_path / "i.db")
    _graph(db)
    rep = I.check_integrity(db)
    assert rep["ok"] and rep["errors"] == [] and rep["foreign_keys_on"] is True


def test_inserting_a_dangling_link_is_disallowed(tmp_path):
    """With FK on, the DB itself REFUSES a link to a non-existent record."""
    db = init_db(tmp_path / "i.db")
    g = _graph(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO work_author (work_id, person_id, role) "
                   "VALUES (?, 99999, 'author')", (g["w"],))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO edition_translator (edition_id, person_id, seq) "
                   "VALUES (99999, ?, 1)", (g["p2"],))


def test_warns_on_whole_book_work_duplicating_multiwork_edition(tmp_path):
    """General principle: a multi-text edition is represented by its CONSTITUENT works, not a
    single whole-book work standing for the whole edition. That container work is flagged — as
    an OVERRIDABLE warning, never a hard error."""
    from catalogue.db_store import add_alias
    db = init_db(tmp_path / "i.db")
    e = db.execute("INSERT INTO edition (title) VALUES ('Anthology of Three Texts')").lastrowid
    for i in (1, 2):                                   # ≥2 constituent works ⇒ multi-work edition
        w = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
        add_alias(db, "work", w, f"Constituent {i}", "english")
        db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,?)", (e, w, i))
    container = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid   # the mistake
    add_alias(db, "work", container, "Anthology of Three Texts", "english")   # title == edition title
    db.commit()

    rep = I.check_integrity(db)
    hit = [x for x in rep["warnings"] if "whole-book work duplicating" in x["check"]]
    assert hit and container in hit[0]["sample"]
    assert rep["ok"] and rep["errors"] == []           # a warning, not corruption — overridable


def test_structure_multiwork_flag_alone_triggers_container_warning(tmp_path):
    """Even before constituents are linked, an edition explicitly marked multi_work plus a
    same-title whole-book work is flagged."""
    from catalogue.db_store import add_alias
    db = init_db(tmp_path / "i.db")
    e = db.execute("INSERT INTO edition (title, structure) VALUES ('Collected Songs','multi_work')").lastrowid
    w = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", w, "Collected Songs", "english")
    db.commit()
    rep = I.check_integrity(db)
    assert any("whole-book work duplicating" in x["check"] for x in rep["warnings"])


def test_single_work_edition_with_matching_title_is_not_flagged(tmp_path):
    """A single-text edition whose one work shares its title is correct, not a container mistake."""
    from catalogue.db_store import add_alias
    db = init_db(tmp_path / "i.db")
    e = db.execute("INSERT INTO edition (title) VALUES ('A Single Text')").lastrowid
    w = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", w, "A Single Text", "english")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e, w))
    db.commit()
    rep = I.check_integrity(db)
    assert not any("whole-book work duplicating" in x["check"] for x in rep["warnings"])


def test_container_work_ids_read_surface(tmp_path):
    """The per-edition helper the add-flow calls returns the offending container work(s)."""
    from catalogue.db_store import add_alias
    from catalogue.access_api import system_conn
    db = init_db(tmp_path / "i.db")
    e = db.execute("INSERT INTO edition (title, structure) VALUES ('Two Texts','multi_work')").lastrowid
    container = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", container, "Two Texts", "english")
    db.commit()
    got = system_conn(db).editions.reads.container_work_ids(e)
    assert got == [container]


def test_detects_injected_orphan(tmp_path):
    """A delete through a raw FK-OFF connection (the sqlite3 CLI / a careless
    script) orphans a link — check_integrity catches it and assert_integrity raises."""
    p = tmp_path / "i.db"
    g = _graph(init_db(p))
    raw = sqlite3.connect(p)                       # FK OFF by default → no cascade
    raw.execute("DELETE FROM person WHERE id=?", (g["p"],))
    raw.commit(); raw.close()
    db = connect(p)
    rep = I.check_integrity(db)
    assert rep["ok"] is False
    assert any("orphan author" in e["check"] for e in rep["errors"])
    with pytest.raises(I.IntegrityError):
        I.assert_integrity(db)


# ── Post-condition: every link-mutating op keeps the graph sound ─────────────
def test_integrity_after_person_delete(tmp_path):
    from catalogue.services import contributor_edit as CE
    db = init_db(tmp_path / "i.db")
    g = _graph(db)
    CE.apply_delete(db, g["p"])                    # soft-delete the author (tombstone)
    I.assert_integrity(db)                         # no dangling refs (edges ride the tombstone)
    assert db.execute("SELECT 1 FROM work WHERE id=?", (g["w"],)).fetchone()      # work survives
    # The edge rides the tombstone, so the work has no LIVE author now (orphan flagged, not detached).
    assert db.execute("SELECT COUNT(*) FROM work_author wa JOIN v_live_person p "
                      "ON p.id = wa.person_id WHERE wa.work_id=?",
                      (g["w"],)).fetchone()[0] == 0
    # Deleting the translator likewise tombstones the person; the edge rides, graph stays sound.
    CE.apply_delete(db, g["p2"])
    I.assert_integrity(db)


def test_integrity_after_person_merge(tmp_path):
    from catalogue.services import contributor_edit as CE
    db = init_db(tmp_path / "i.db")
    g = _graph(db)
    dup = db.execute("INSERT INTO person (primary_name) VALUES ('Dup')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (g["w"], dup))
    db.execute("INSERT INTO edition_translator (edition_id, person_id, seq) VALUES (?,?,2)", (g["e"], dup))
    db.commit()
    CE.apply_merge(db, dup, g["p"])                # fold dup into the author
    I.assert_integrity(db)
    assert db.execute("SELECT 1 FROM person WHERE id=?", (dup,)).fetchone() is None  # dup gone
    # dup's edges collapsed onto the survivor (PK-dedup, none lost)
    assert db.execute("SELECT COUNT(*) FROM work_author WHERE work_id=? AND person_id=?",
                      (g["w"], g["p"]))


def test_integrity_after_person_split(tmp_path):
    from catalogue.services import contributor_edit as CE
    db = init_db(tmp_path / "i.db")
    blob = db.execute("INSERT INTO person (primary_name) VALUES ('A, B')").lastrowid
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    e = db.execute("INSERT INTO edition (title) VALUES ('Ed')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w, blob))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e, w))
    db.commit()
    CE.apply_split(db, blob, assignments=[{"name": "A", "role": "author"},
                                          {"name": "B", "role": "translator"}])
    I.assert_integrity(db)
    assert db.execute("SELECT 1 FROM person WHERE id=?", (blob,)).fetchone() is None  # blob gone


def test_integrity_after_work_merge(tmp_path):
    from catalogue.services import work_merge as WM
    from catalogue.db_store import add_alias
    db = init_db(tmp_path / "i.db")
    p = db.execute("INSERT INTO person (primary_name) VALUES ('Auth')").lastrowid
    w1 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    w2 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", w1, "Stages", "english")
    add_alias(db, "work", w2, "Stages", "english")
    e1 = db.execute("INSERT INTO edition (title) VALUES ('E1')").lastrowid
    e2 = db.execute("INSERT INTO edition (title) VALUES ('E2')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w1, p))
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')", (w2, p))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e1, w1))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e2, w2))
    db.commit()
    WM.apply_work_merge(db, w2, w1)               # fold w2 into w1
    I.assert_integrity(db)
    assert db.execute("SELECT 1 FROM work WHERE id=?", (w2,)).fetchone() is None
    # both editions now realize the surviving work
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE work_id=?", (w1,)).fetchone()[0] == 2


def test_integrity_after_edition_delete_with_translator(tmp_path):
    """The edition_delete fix: deleting an edition that has a translator leaves no
    orphan edition_translator (explicit detach, not only the cascade); the WORK
    survives."""
    from catalogue.webui.web import create_app
    app = create_app(tmp_path / "i.db", ingest_verify=False)
    db = connect(app.config["DB_PATH"])
    g = _graph(db); db.close()
    assert app.test_client().post(f"/edition/{g['e']}/delete").status_code in (200, 302)
    db = connect(app.config["DB_PATH"])
    I.assert_integrity(db)
    assert db.execute("SELECT COUNT(*) FROM edition_translator WHERE edition_id=?",
                      (g["e"],)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=?",
                      (g["e"],)).fetchone()[0] == 0
    assert db.execute("SELECT 1 FROM work WHERE id=?", (g["w"],)).fetchone()  # work preserved
    db.close()


# ── Server refuses an unsafe (FK-off) connection ────────────────────────────
def test_connect_self_verifies_fk(tmp_path):
    """connect() hands back a connection that provably enforces FKs."""
    db = init_db(tmp_path / "i.db")
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_web_refuses_request_on_fk_off_connection(tmp_path, monkeypatch):
    """If a future connection helper forgets the pragma, the server disallows the
    request with a clear 503 page instead of running unsafe."""
    import catalogue.webui.web as web
    app = web.create_app(tmp_path / "i.db", ingest_verify=False)   # built via real connect()
    app.testing = True

    def fk_off(path):                       # simulate the 'forgot to turn it on' bug
        return sqlite3.connect(str(path))   # raw → foreign_keys OFF by default

    monkeypatch.setattr(web, "connect", fk_off)
    r = app.test_client().get("/")
    assert r.status_code == 503
    assert b"not enforcing foreign keys" in r.data


# ── Convention guard: nothing in the package opens the DB raw ────────────────
def test_all_db_access_goes_through_connect():
    """Every DB connection must go through `connect()`/`connect_ro()` (which enforce
    foreign keys / read-only). Only `connection.py` — the chokepoint module — may call
    the raw sqlite connector. This guards the convention for current AND future code."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    # Post-reorg the code lives in the workspace packages, not repo_root/catalogue.
    roots = [root / "catalogue-packages", root / "catalogue-webui", root / "catalogue-cli"]
    pat = re.compile(r"\b(?:sqlite3|_sqlite)\.connect\s*\(")
    offenders = []
    for base in roots:
        for f in sorted(base.rglob("*.py")):
            if f.name == "connection.py":           # the one sanctioned chokepoint
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line.split("#", 1)[0]):
                    offenders.append(f"{f.relative_to(root)}:{n}: {line.strip()}")
    assert not offenders, (
        "raw sqlite connect bypasses connect()/connect_ro() (FK enforcement + the "
        "read/write split):\n  " + "\n  ".join(offenders))


# ── The live DB invariant (guard; skipped when the file isn't present) ───────
def test_live_db_has_no_dangling_references():
    path = "private/catalogue-db/catalogue.db"
    if not os.path.exists(path):
        pytest.skip("live DB not present")
    # Open IMMUTABLE read-only: the integrity scan must validate the real live DB
    # WITHOUT creating -wal/-shm sidecars, taking a lock, or mutating a byte (the
    # suite must never touch live — see conftest._guard_live_db).
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?immutable=1", uri=True)
    try:
        rep = I.check_integrity(conn)
    finally:
        conn.close()
    assert rep["ok"], rep["errors"]
