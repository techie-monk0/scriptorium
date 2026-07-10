"""Seed the subject table from the on-disk library tree, honouring config subtree
exclusions; plus the FK-input datalists (subject / work_type autocomplete)."""
import pytest

from catalogue.db_store import connect, init_db
from catalogue.services import subjects as S
from catalogue.webui.web import create_app


def _tree(root):
    # `root` IS the books folder; its subtree yields the subjects (<root>/A/B → A/B).
    (root / "Emptiness").mkdir(parents=True)
    (root / "Madhyamaka" / "Two Truths").mkdir(parents=True)
    (root / "03 Logic").mkdir(parents=True)                       # leading number stripped
    (root / "Vinaya ANNOTATED" / "Hidden").mkdir(parents=True)    # excluded subtree
    (root / "Emptiness" / "A.pdf").write_text("x")


def test_populate_subject_vocab_with_exclusions(tmp_path):
    db = init_db(tmp_path / "c.db")
    lib = tmp_path / "lib"; lib.mkdir(); _tree(lib)
    S.populate_subject_vocab(db, str(lib))
    db.commit()
    names = {r[0] for r in db.execute("SELECT name FROM subject")}
    assert {"Emptiness", "Madhyamaka", "Madhyamaka/Two Truths", "Logic"} <= names
    assert not any("ANNOTATED" in n or "Hidden" in n or "Vinaya" in n for n in names)  # pruned
    assert "03 Logic" not in names                                # leading number stripped
    # idempotent — a second run adds nothing
    assert S.populate_subject_vocab(db, str(lib))["added"] == []


def test_populate_respects_folder_map(tmp_path):
    db = init_db(tmp_path / "c.db")
    lib = tmp_path / "lib"; (lib / "Misc" / "Emptiness").mkdir(parents=True)
    S.set_folder_label(db, "Misc", "")                             # map drops the segment
    res = S.populate_subject_vocab(db, str(lib))
    db.commit()
    names = {r[0] for r in db.execute("SELECT name FROM subject")}
    assert "Emptiness" in names and not any(n.startswith("Misc") for n in names)


def test_cli_seed_vocab(tmp_path):
    from catalogue.cli import subject_backfill
    db = init_db(tmp_path / "c.db"); db.close()
    lib = tmp_path / "lib"; lib.mkdir(); _tree(lib)
    rc = subject_backfill.main([str(tmp_path / "c.db"), "--read-subject-from-dir-structure",
                                "--root", str(lib), "--apply"])
    assert rc == 0
    db = connect(tmp_path / "c.db")
    assert db.execute("SELECT COUNT(*) FROM subject WHERE name='Madhyamaka/Two Truths'").fetchone()[0] == 1


def test_scan_attaches_to_works_else_modern_edition(tmp_path):
    """--read-subject-from-dir-structure --apply tags each book's WORK (so the edition
    inherits); a work-less (modern) edition is tagged directly from its folder."""
    from catalogue.cli import subject_backfill
    db = init_db(tmp_path / "c.db")
    lib = tmp_path / "lib"
    (lib / "Emptiness").mkdir(parents=True)
    (lib / "History").mkdir(parents=True)
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid          # classical
    e1 = db.execute("INSERT INTO edition (title) VALUES ('Classical')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (e1, w))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (e1, str(lib / "Emptiness" / "A.pdf")))
    e2 = db.execute("INSERT INTO edition (title) VALUES ('Modern')").lastrowid       # no work
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
               (e2, str(lib / "History" / "B.pdf")))
    db.commit(); db.close()

    subject_backfill.main([str(tmp_path / "c.db"), "--read-subject-from-dir-structure",
                           "--root", str(lib), "--apply"])
    db = connect(tmp_path / "c.db")
    assert {n for _, n in S.subjects_for(db, "work", w)} == {"Emptiness"}            # on the work
    assert S.subjects_for(db, "edition", e1) == []                                   # edition inherits → no own
    assert {n for _, n in S.subjects_for(db, "edition", e2)} == {"History"}          # modern → on the edition


def test_fk_inputs_use_datalist(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    S.get_or_create_subject(db, "Madhyamaka")
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.commit()
    with app.test_client() as c:
        wp = c.get(f"/work/{wid}").data.decode()
    # work_type is now two mutually-exclusive checkboxes (Root text / Commentary), not a datalist
    assert 'action="/work/%d/set-type"' % wid in wp and "Root text" in wp and "Commentary" in wp
    assert 'list="work-type-opts"' not in wp
    assert "<datalist" in wp and '<option value="Madhyamaka">' in wp     # subject datalist still populated
