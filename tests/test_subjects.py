"""Subjects: hierarchical path derivation, folder→label maps, attach/dedupe,
cascade, the holding backfill, and the work/edition review UI."""
import pytest

from catalogue.db_store import connect, init_db
from catalogue.services import subjects as S
from catalogue.cli import subject_backfill
from catalogue.webui.web import create_app


def _edition(db, title="Ed"):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _holding(db, eid, path):
    return db.execute(
        "INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
        (eid, path)).lastrowid


@pytest.fixture(autouse=True)
def _autodetect_root(monkeypatch):
    # These tests use synthetic holding paths, so derive subjects from the auto-detected
    # holding root rather than the real configured _library_root. A test may override
    # this (a later monkeypatch.setattr wins) to exercise a specific config root.
    monkeypatch.setattr(S, "subject_root", lambda db: S.library_root(db))


# ── segment cleaning + path derivation ────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("01 Emptiness", "Emptiness"),        # leading number + space stripped
    ("02 - Two Truths", "Two Truths"),    # leading number/sep stripped
    ("3) Logic", "Logic"),
    ("123Pramana", "Pramana"),            # no space either
    ("Tantra", "Tantra"),
    ("  Madhyamaka  ", "Madhyamaka"),
    ("01 Books - Dharma", "Books - Dharma"),   # only LEADING non-alpha removed (mid '-' kept)
])
def test_clean_segment(raw, expected):
    assert S.clean_segment(raw) == expected


def test_inbox_is_never_added_as_a_subject(tmp_path):
    """'INBOX' is the intake area, not aboutness. add_subject must silently ignore it
    (no edition_subject row, no subject row) so it can't pollute the tree / filer again."""
    db = init_db(tmp_path / "c.db")
    eid = _edition(db, "Book")
    assert S.add_subject(db, "edition", eid, "INBOX") == -1          # reserved → no-op sentinel
    assert S.add_subject(db, "edition", eid, "inbox") == -1          # case-insensitive
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM subject WHERE name LIKE 'INBOX'").fetchone()[0] == 0
    assert [n for _, n in S.subjects_for(db, "edition", eid)] == []  # nothing tagged
    # a real subject still works
    S.add_subject(db, "edition", eid, "Tantra")
    assert "Tantra" in [n for _, n in S.subjects_for(db, "edition", eid)]


def test_segment_label_mapping_overrides_and_drops():
    m = {"01 books - dharma": "Buddhism", "misc": ""}
    assert S.segment_label("01 Books - Dharma", m) == "Buddhism"   # mapped wins
    assert S.segment_label("Misc", m) is None                       # empty label drops
    assert S.segment_label("Tantra", m) == "Tantra"                 # falls back to clean


def test_derive_subject_hierarchical():
    # root = the books folder; <root>/A/B yields subject "A/B"
    root = "/lib/01 Books - Dharma"
    assert S.derive_subject(root + "/Emptiness/A.pdf", root) == "Emptiness"
    assert S.derive_subject(root + "/Emptiness/Two Truths/A.pdf", root) == "Emptiness/Two Truths"
    assert S.derive_subject(root + "/03 Logic/A.pdf", root) == "Logic"     # leading number stripped
    assert S.derive_subject(root + "/Emptiness/A.pdf", root,
                            {"emptiness": "Śūnyatā"}) == "Śūnyatā"          # map override wins


def test_derive_subject_no_root_is_leaf():
    assert S.derive_subject("Emptiness/A.pdf") == "Emptiness"
    assert S.derive_subject(r"C:\lib\Pramana\d.pdf") == "Pramana"
    assert S.derive_subject("A.pdf") is None
    assert S.derive_subject("") is None
    assert S.derive_subject(None) is None


def test_library_root_keeps_subject_folder_that_holds_files(tmp_path):
    db = init_db(tmp_path / "c.db")
    e = _edition(db)
    _holding(db, e, "/lib/01 Books - Dharma/A.pdf")           # directly in top folder
    _holding(db, e, "/lib/01 Books - Dharma/Emptiness/B.pdf")
    # top folder holds files directly → it's a subject, so root is its PARENT
    assert S.library_root(db) == "/lib"


def test_library_root_multiple_top_folders(tmp_path):
    db = init_db(tmp_path / "c.db")
    e = _edition(db)
    _holding(db, e, "/lib/Dharma/A.pdf")
    _holding(db, e, "/lib/Science/B.pdf")
    assert S.library_root(db) == "/lib"
    root = S.library_root(db)
    assert S.derive_subject("/lib/Dharma/A.pdf", root) == "Dharma"
    assert S.derive_subject("/lib/Science/B.pdf", root) == "Science"


def test_suggest_edition_subject(tmp_path, monkeypatch):
    db = init_db(tmp_path / "c.db")
    e = _edition(db)
    _holding(db, e, "/lib/01 Books - Dharma/Emptiness/A.pdf")
    # config root = the books folder → the subject is just "Emptiness" (the subtree below it)
    monkeypatch.setattr(S, "subject_root", lambda db: "/lib/01 Books - Dharma")
    assert S.suggest_edition_subject(db, e) == "Emptiness"


# ── folder map persistence ────────────────────────────────────────────────────

def test_folder_map_upsert_and_apply(tmp_path):
    db = init_db(tmp_path / "c.db")
    S.set_folder_label(db, "01 Books - Dharma", "Buddhism")
    assert S.folder_map(db) == {"01 books - dharma": "Buddhism"}
    S.set_folder_label(db, "01 Books - Dharma", "Dharma")          # upsert
    assert S.folder_map(db) == {"01 books - dharma": "Dharma"}
    assert S.derive_subject("/r/01 Books - Dharma/A.pdf", "/r",
                            S.folder_map(db)) == "Dharma"


# ── attach / detach / read ────────────────────────────────────────────────────

def test_add_attach_idempotent_and_cascade(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    s1 = S.add_subject(db, "edition", eid, "Dharma/Emptiness")
    s2 = S.add_subject(db, "edition", eid, "dharma/emptiness")     # case-insensitive reuse
    s3 = S.add_subject(db, "edition", eid, "Dharma/Emptiness")     # re-attach no-op
    assert s1 == s2 == s3
    # A '/'-path materializes its container, so the leaf + its ancestor both exist:
    # `Dharma` (container, tags nothing) and `Dharma/Emptiness` (the attached leaf).
    assert {r[0] for r in db.execute("SELECT name FROM subject").fetchall()} == {
        "Dharma", "Dharma/Emptiness"}
    assert S.subjects_for(db, "edition", eid) == [(s1, "Dharma/Emptiness")]
    db.execute("DELETE FROM edition WHERE id = ?", (eid,))         # cascade drops the tag
    assert db.execute("SELECT COUNT(*) FROM edition_subject").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM subject").fetchone()[0] == 2   # subjects survive


def test_subject_shared_across_work_and_edition(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    assert S.add_subject(db, "edition", eid, "Dharma") == S.add_subject(db, "work", wid, "Dharma")
    assert db.execute("SELECT COUNT(*) FROM subject").fetchone()[0] == 1


def test_uncurated_count_ignores_protected_uncategorized(tmp_path):
    # The protected "Uncategorized" safety-net can't be renamed/deleted or marked
    # reviewed, so an EMPTY one (tagging nothing) must NOT be counted as uncurated
    # — otherwise the Review page nags forever with no way to clear it.
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)        # attaches the Uncategorized placeholder
    db.execute("DELETE FROM edition WHERE id = ?", (eid,))   # now Uncategorized tags nothing
    db.commit()
    assert db.execute("SELECT 1 FROM subject WHERE name = ?", (S.UNCATEGORIZED,)).fetchone()
    assert S.count_uncurated(db) == 0               # the empty protected subject is not a to-do

    # A genuinely orphan NON-protected subject is still counted.
    db.execute("INSERT INTO subject (name) VALUES ('Stray Topic')")
    db.commit()
    assert S.count_uncurated(db) == 1


# ── backfill CLI ──────────────────────────────────────────────────────────────

def _names(db, kind, pid):
    return {s for (_, s) in S.subjects_for(db, kind, pid)}


def test_backfill_hierarchical(tmp_path, monkeypatch):
    dbp = tmp_path / "c.db"
    db = init_db(dbp)
    e1 = _edition(db); _holding(db, e1, "/lib/01 Books - Dharma/Emptiness/A.pdf")
    e2 = _edition(db); _holding(db, e2, "/lib/01 Books - Dharma/Madhyamaka/B.pdf")
    db.commit()
    monkeypatch.setattr(S, "subject_root", lambda db: "/lib/01 Books - Dharma")

    subject_backfill.main([str(dbp)])                              # dry-run writes nothing
    assert db.execute("SELECT COUNT(*) FROM edition_subject").fetchone()[0] == 0

    subject_backfill.main([str(dbp), "--apply"])
    assert _names(db, "edition", e1) == {"Emptiness"}
    assert _names(db, "edition", e2) == {"Madhyamaka"}

    before = db.execute("SELECT COUNT(*) FROM edition_subject").fetchone()[0]
    subject_backfill.main([str(dbp), "--apply"])                  # idempotent
    assert db.execute("SELECT COUNT(*) FROM edition_subject").fetchone()[0] == before == 2


def test_backfill_map_persisted_and_applied(tmp_path):
    dbp = tmp_path / "c.db"
    db = init_db(dbp)
    e1 = _edition(db); _holding(db, e1, "/lib/01 Books - Dharma/Emptiness/B.pdf")
    e2 = _edition(db); _holding(db, e2, "/lib/01 Books - Dharma/C.pdf")   # anchors root
    db.commit()

    subject_backfill.main([str(dbp), "--map", "01 Books - Dharma=Buddhism", "--apply"])
    assert _names(db, "edition", e1) == {"Buddhism/Emptiness"}
    assert _names(db, "edition", e2) == {"Buddhism"}
    assert S.folder_map(db) == {"01 books - dharma": "Buddhism"}   # persisted


# ── web UI ────────────────────────────────────────────────────────────────────

@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    eid = _edition(db, "Book")
    _holding(db, eid, "/lib/Dharma/Emptiness/A.pdf")
    e2 = _edition(db, "Other")                                    # anchors root detection
    _holding(db, e2, "/lib/Dharma/Root.pdf")
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.commit()
    with app.test_client() as c:
        yield c, app, eid, wid


def test_edition_subjects_card_add_remove(web):
    c, app, eid, _ = web
    # card shows the folder-derived suggestion
    card = c.get(f"/subjects/edition/{eid}")
    assert card.status_code == 200
    assert b"Dharma/Emptiness" in card.data        # the "+ from folder" suggestion

    c.post(f"/subjects/edition/{eid}/add", data={"name": "Dharma/Emptiness"})
    db = connect(app.config["DB_PATH"])
    rows = S.subjects_for(db, "edition", eid)
    assert [n for _, n in rows] == ["Dharma/Emptiness"]
    sid = rows[0][0]
    # now attached → suggestion no longer offered
    assert b"from folder" not in c.get(f"/subjects/edition/{eid}").data

    # Removing the LAST real subject re-tags the Uncategorized placeholder — nothing
    # is ever subject-less (the review gate then blocks until it's recategorized).
    c.post(f"/subjects/edition/{eid}/remove", data={"subject_id": sid})
    assert [n for _, n in S.subjects_for(connect(app.config["DB_PATH"]), "edition", eid)] \
        == [S.UNCATEGORIZED]


def test_work_page_has_subjects_editor(web):
    c, app, _, wid = web
    page = c.get(f"/work/{wid}")
    assert page.status_code == 200
    assert b"Subjects" in page.data

    c.post(f"/subjects/work/{wid}/add", data={"name": "Dharma/Logic"})
    assert _names(connect(app.config["DB_PATH"]), "work", wid) == {"Dharma/Logic"}


def test_subjects_bad_kind_404(web):
    c, _, _, _ = web
    assert c.get("/subjects/bogus/1").status_code == 404
