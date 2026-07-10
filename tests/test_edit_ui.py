"""Regression tests for in-UI editing of book details (§ edit-from-review/holdings).

Pins:
  - the open `holding_type` vocabulary loads from JSON (a new code is data, not code),
  - the additive migration adds holding_type/notes and backfills from form+extension,
  - the per-holding fields editor persists through /holding/<id>/edit,
  - a book shows its list of available formats on the edition card.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import (
    connect, init_db, load_vocab, _migrate, derive_holding_type,
)
from catalogue.webui.web import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "edit.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app


def _conn(app):
    return connect(app.config["DB_PATH"])


def _edition(db, title="A Book"):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _holding(db, eid, *, form="electronic", file_path=None, holding_type=None):
    return db.execute(
        "INSERT INTO holding (edition_id, form, file_path, holding_type) "
        "VALUES (?, ?, ?, ?)", (eid, form, file_path, holding_type)
    ).lastrowid


# ── Open vocabulary ──────────────────────────────────────────────────────────
def test_holding_type_vocab_seeded_from_json(env):
    _, app = env
    db = _conn(app)
    codes = {r[0] for r in db.execute("SELECT code FROM holding_type")}
    assert {"pdf", "epub", "physical"} <= codes
    db.close()


def test_locator_type_vocab_seeded_from_json(env):
    _, app = env
    db = _conn(app)
    codes = {r[0] for r in db.execute("SELECT code FROM locator_type")}
    assert {"page", "chapter", "section"} <= codes
    db.close()


def test_load_vocab_adds_new_code_without_migration(tmp_path):
    """A new type is an INSERT from config, not a schema change (§12.4)."""
    conn = init_db(tmp_path / "v.db")
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps(
        {"holding_type": [{"code": "audio", "label": "Audiobook"}]}
    ))
    load_vocab(conn, extra)
    conn.commit()
    labels = dict(conn.execute("SELECT code, label FROM holding_type").fetchall())
    assert labels["audio"] == "Audiobook"
    # And a holding may now reference it (FK satisfied).
    eid = _edition(conn)
    _holding(conn, eid, holding_type="audio")
    conn.commit()
    conn.close()


# ── Migration + backfill ───────────────────────────────────────────────────
def test_derive_holding_type():
    assert derive_holding_type("electronic", "/x/Y.PDF", None) == "pdf"
    assert derive_holding_type("electronic", None, "/a.epub") == "epub"
    assert derive_holding_type("physical", None, None) == "physical"
    assert derive_holding_type("electronic", None, None) is None


def test_migration_adds_columns_and_backfills(tmp_path):
    conn = init_db(tmp_path / "old.db")
    # Simulate a pre-migration holding table (no holding_type / notes columns).
    conn.executescript("""
        DROP TABLE holding;
        CREATE TABLE holding (
          id INTEGER PRIMARY KEY,
          edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
          form TEXT, file_path TEXT, file_hash TEXT, shelf_location TEXT,
          ocr_quality_score REAL, text_status TEXT,
          digitizer_used TEXT, archival_pdf_path TEXT,
          date_added TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    eid = _edition(conn)
    conn.execute("INSERT INTO holding (id, edition_id, form, file_path) "
                 "VALUES (1, ?, 'electronic', '/x/book.pdf')", (eid,))
    conn.execute("INSERT INTO holding (id, edition_id, form) "
                 "VALUES (2, ?, 'physical')", (eid,))
    conn.commit()
    assert "holding_type" not in {r[1] for r in conn.execute("PRAGMA table_info(holding)")}

    load_vocab(conn)
    _migrate(conn)
    conn.commit()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(holding)")}
    assert {"holding_type", "notes"} <= cols
    got = dict(conn.execute("SELECT id, holding_type FROM holding").fetchall())
    assert got == {1: "pdf", 2: "physical"}
    conn.close()


# ── Per-holding fields editor ────────────────────────────────────────────────
def test_holding_card_renders_editable_fields(env):
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    hid = _holding(db, eid, holding_type="pdf")
    db.commit(); db.close()

    r = c.get(f"/holding/{hid}/card")
    assert r.status_code == 200
    for field in (b'name="holding_type"', b'name="text_status"', b'name="form"',
                  b'name="shelf_location"', b'name="ocr_quality_score"',
                  b'name="notes"'):
        assert field in r.data, field
    # The open-vocab options are present.
    assert b">physical<" in r.data and b">epub<" in r.data


def test_holding_edit_persists(env):
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    hid = _holding(db, eid, holding_type="pdf")
    db.commit(); db.close()

    r = c.post(f"/holding/{hid}/edit", data={
        "holding_type": "epub", "form": "electronic", "text_status": "ocr_poor",
        "shelf_location": "Shelf 3", "ocr_quality_score": "0.42",
        "notes": "water-damaged spine",
    })
    assert r.status_code in (302, 303)

    db = _conn(app)
    row = db.execute(
        "SELECT holding_type, text_status, shelf_location, ocr_quality_score, notes "
        "FROM holding WHERE id = ?", (hid,)
    ).fetchone()
    db.close()
    assert row == ("epub", "ocr_poor", "Shelf 3", 0.42, "water-damaged spine")


def test_holding_edit_rejects_unknown_type_via_fk(env):
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    hid = _holding(db, eid, holding_type="pdf")
    db.commit(); db.close()
    # 'vinyl' isn't in the vocabulary → FK violation surfaces as a 500, not a
    # silently-stored bad code.
    with pytest.raises(Exception):
        c.post(f"/holding/{hid}/edit", data={"holding_type": "vinyl"})


# ── Available-formats list on the edition card ───────────────────────────────
def test_edition_card_lists_available_formats(env):
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    _holding(db, eid, holding_type="pdf", file_path="/a.pdf")
    _holding(db, eid, form="physical", holding_type="physical")
    db.commit(); db.close()

    r = c.get(f"/edition/{eid}/card")
    assert r.status_code == 200
    assert b"Available as" in r.data
    assert b">pdf<" in r.data and b">physical<" in r.data


# ── Contained-works: edit an existing link in place ──────────────────────────
def test_edition_card_contained_rows_are_editable(env):
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Root', 'english', 'root')", (wid,))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
               "VALUES (?, ?, 1)", (eid, wid))
    db.commit(); db.close()

    r = c.get(f"/edition/{eid}/card")
    assert r.status_code == 200
    # The contained row exposes a Save button + editable locator type + value.
    assert b'action="/edition/%d/work/update"' % eid in r.data
    assert b'name="section_locator"' in r.data
    assert b'name="locator_type"' in r.data
    assert b">chapter<" in r.data and b">page<" in r.data
    assert b">Save<" in r.data
    # Sequence is no longer exposed as an editable field.
    assert b"<th>seq</th>" not in r.data


def test_edition_work_update_persists(env):
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Tr.')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
               "VALUES (?, ?, 1)", (eid, wid))
    db.commit(); db.close()

    # The UI sends no `sequence` (it's auto-managed) — only the editable fields.
    r = c.post(f"/edition/{eid}/work/update", data={
        "work_id": wid, "old_sequence": "1", "translator_id": pid,
        "locator_type": "chapter", "section_locator": "3",
    })
    assert r.status_code in (302, 303)

    db = _conn(app)
    row = db.execute(
        "SELECT sequence, translator_person_id, section_locator, locator_type "
        "FROM edition_work WHERE edition_id = ? AND work_id = ?", (eid, wid)
    ).fetchone()
    db.close()
    assert row == (1, pid, "3", "chapter")   # sequence untouched (not in the form)


def test_edition_add_work_auto_assigns_sequence(env):
    """No sequence field in the UI — Add appends each work to the end."""
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    w1 = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    w2 = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.commit(); db.close()

    assert c.post(f"/edition/{eid}/work/add", data={"work_id": w1}).status_code in (302, 303)
    assert c.post(f"/edition/{eid}/work/add", data={"work_id": w2}).status_code in (302, 303)

    db = _conn(app)
    seqs = dict(db.execute(
        "SELECT work_id, sequence FROM edition_work WHERE edition_id = ?", (eid,)
    ).fetchall())
    db.close()
    assert seqs == {w1: 1, w2: 2}


def test_edition_card_uses_pickers_not_whole_table_selects(env):
    """Work + Translator are Typeahead pickers, not <select>s that enumerate the whole
    works/people tables. Picker hosts are present; the raw selects are gone."""
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Root', 'english', 'root')", (wid,))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    db.commit(); db.close()
    page = c.get(f"/edition/{eid}/card").data
    assert b'class="edc-work"' in page and b'edcPickWork(' in page          # work picker
    assert b'edcPickPerson(' in page                                        # person picker
    assert page.count(b'class="edc-person"') >= 2                           # both the row + add-form translator
    assert b'name="new_work_title"' in page                                 # inline-create field
    # the whole-table <select>s are gone (the small locator_type vocab select legitimately stays)
    assert b'<select name="work_id"' not in page
    assert b'<select name="translator_id"' not in page
    # the edition page itself loads the picker module + its handlers
    full = c.get(f"/edition/{eid}").data
    assert b"window.Typeahead" in full and b"edcPickWork" in full


def test_edition_work_note_round_trips_through_add_and_update(env):
    """A per-appearance note (e.g. 'only chs. 1-3 of the root text') lives on the
    edition_work join, survives add, can be edited via update, and is cleared when
    blanked. It is scoped to THIS edition — never written to work/edition tables."""
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.commit(); db.close()

    # add carries the note
    assert c.post(f"/edition/{eid}/work/add", data={
        "work_id": wid, "note": "only chs. 1-3 of the root text",
    }).status_code in (302, 303)
    db = _conn(app)
    seq, note = db.execute(
        "SELECT sequence, note FROM edition_work WHERE edition_id=? AND work_id=?",
        (eid, wid)).fetchone()
    db.close()
    assert note == "only chs. 1-3 of the root text"

    # update edits the note in place (other fields still work)
    assert c.post(f"/edition/{eid}/work/update", data={
        "work_id": wid, "old_sequence": str(seq), "note": "verse portions only",
    }).status_code in (302, 303)
    db = _conn(app)
    assert db.execute(
        "SELECT note FROM edition_work WHERE edition_id=? AND work_id=?",
        (eid, wid)).fetchone()[0] == "verse portions only"
    db.close()

    # blanking the note clears it (stored NULL, not empty string)
    assert c.post(f"/edition/{eid}/work/update", data={
        "work_id": wid, "old_sequence": str(seq), "note": "   ",
    }).status_code in (302, 303)
    db = _conn(app)
    assert db.execute(
        "SELECT note FROM edition_work WHERE edition_id=? AND work_id=?",
        (eid, wid)).fetchone()[0] is None
    db.close()


def test_edition_card_exposes_note_input(env):
    """The contained-works editor offers a per-appearance note field pre-filled
    with the stored value."""
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Root', 'english', 'root')", (wid,))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence, note) "
               "VALUES (?, ?, 1, 'only chs. 1-3')", (eid, wid))
    db.commit(); db.close()
    page = c.get(f"/edition/{eid}/card").data
    assert b'name="note"' in page
    assert b'only chs. 1-3' in page


def test_works_in_edition_shows_per_appearance_note(env):
    """edition_work_summaries injects the join note as `edition_note`, surfaced as a
    caption in the read-only Works-In-This-Edition view."""
    from catalogue.services.library import edition_work_summaries
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    # two works so the surfacing predicate keeps them in the summary
    w1 = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    w2 = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    for w in (w1, w2):
        db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
                   "VALUES (?, ?, 'english', ?)", (w, f"W{w}", f"w{w}"))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence, note) "
               "VALUES (?, ?, 1, 'only chs. 1-3 of the root text')", (eid, w1))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) "
               "VALUES (?, ?, 2)", (eid, w2))
    db.commit()
    summaries = edition_work_summaries(db, eid)
    db.close()
    by_id = {s["id"]: s for s in summaries}
    assert by_id[w1]["edition_note"] == "only chs. 1-3 of the root text"
    assert by_id[w2]["edition_note"] is None


def test_add_work_inline_create_from_typed_title(env):
    """Picking '+ create' on the work picker posts new_work_title (no work_id); the route
    creates the work (with that english title alias) and links it to the edition."""
    c, app = env
    db = _conn(app)
    eid = _edition(db)
    db.commit(); db.close()
    r = c.post(f"/edition/{eid}/work/add", data={"new_work_title": "A Freshly Typed Work"})
    assert r.status_code in (302, 303)
    db = _conn(app)
    row = db.execute(
        "SELECT w.id, (SELECT text FROM work_alias WHERE work_id=w.id ORDER BY id LIMIT 1) "
        "FROM work w JOIN edition_work ew ON ew.work_id=w.id WHERE ew.edition_id=?", (eid,)
    ).fetchone()
    db.close()
    assert row is not None and row[1] == "A Freshly Typed Work"   # created + linked


# ── Opening a holding's file (viewer) ────────────────────────────────────────
def test_holding_file_serves_repo_relative_path(tmp_path, monkeypatch):
    """A holding whose file_path is stored relative to the repo root must serve,
    not 500. send_file resolves relative paths against the app root, so the route
    has to make the path absolute first."""
    monkeypatch.chdir(tmp_path)            # CWD = where relative paths anchor
    (tmp_path / "archival").mkdir()
    (tmp_path / "archival" / "book.pdf").write_bytes(b"%PDF-1.4 fake")
    app = create_app(tmp_path / "f.db"); app.testing = True
    db = connect(app.config["DB_PATH"])
    eid = _edition(db)
    hid = db.execute(
        "INSERT INTO holding (edition_id, form, file_path) "
        "VALUES (?, 'electronic', 'archival/book.pdf')", (eid,)
    ).lastrowid
    db.commit(); db.close()

    with app.test_client() as c:
        r = c.get(f"/holding/{hid}/file")
        assert r.status_code == 200
        assert r.data == b"%PDF-1.4 fake"


def test_holding_file_missing_is_404_not_500(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(tmp_path / "f.db"); app.testing = True
    db = connect(app.config["DB_PATH"])
    eid = _edition(db)
    hid = db.execute(
        "INSERT INTO holding (edition_id, form, file_path) "
        "VALUES (?, 'electronic', 'archival/nope.pdf')", (eid,)
    ).lastrowid
    db.commit(); db.close()
    with app.test_client() as c:
        assert c.get(f"/holding/{hid}/file").status_code == 404
