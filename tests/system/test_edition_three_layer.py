"""Black-box tests for the three-layer edition/work display shared by Browse and
Review: Edition Basics (read-only on Browse, editable in Review), "Works In This
Edition" (read-only Work Basics + collapsed Work Details), and the
"✎ edit this work →" link / add-picker placement."""
from catalogue.db_store import connect, add_alias
from catalogue.services import work_detect as WD
from catalogue.webui.web import create_app


def _app(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    return app, connect(app.config["DB_PATH"])


def _classical_edition(db, *, title="A Classical Text", toh="3824"):
    """An edition whose single contained work is a canonical (surfaced) work, with an
    edition-level author and a work-level author."""
    ed_author = db.execute("INSERT INTO person (primary_name) VALUES ('General Editor')").lastrowid
    wk_author = db.execute("INSERT INTO person (primary_name) VALUES ('Nāgārjuna')").lastrowid
    w = db.execute("INSERT INTO work (work_type, canonical_system, canonical_number) "
                   "VALUES ('root', 'toh', ?)", (toh,)).lastrowid
    add_alias(db, "work", w, "Root Stanzas", "english")
    add_alias(db, "work", w, "Mūlamadhyamakakārikā", "iast")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?, 'author')",
               (w, wk_author))
    eid = db.execute("INSERT INTO edition (title, structure) VALUES (?, 'single_work')",
                     (title,)).lastrowid
    db.execute("INSERT INTO edition_author (edition_id, person_id) VALUES (?,?)", (eid, ed_author))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (eid, w))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/c.pdf')",
               (eid,))
    return eid, w, ed_author, wk_author


def test_browse_summary_readonly_three_layers(tmp_path):
    app, db = _app(tmp_path)
    eid, w, ed_author, wk_author = _classical_edition(db)
    db.commit()
    with app.test_client() as c:
        s = c.get(f"/edition/{eid}/works-summary").data.decode()
    # Edition Basics — read-only, edition author linked, NO editors.
    assert "Edition Basics" in s
    assert f'/person/{ed_author}' in s
    assert "bbMountPersonSearch" not in s and "<input" not in s     # read-only on Browse
    # Works In This Edition — Work Basics with the work title + work author link.
    assert "Works In This Edition" in s and "wie-work" in s
    assert "Root Stanzas" in s and f'/person/{wk_author}' in s
    # Work Details — the Toh authority link to 84000.
    assert "read.84000.co/translation/toh3824" in s
    # Browse has no inline editable work card and no edit link.
    assert f'data-card-url="/work/{w}/card"' not in s


def test_browse_summary_modern_single_hides_works(tmp_path):
    app, db = _app(tmp_path)
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Modern Book', 'single_work')"
                     ).lastrowid
    w = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid   # degenerate placeholder
    add_alias(db, "work", w, "Modern Book", "english")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (eid, w))
    db.commit()
    with app.test_client() as c:
        s = c.get(f"/edition/{eid}/works-summary").data.decode()
    assert "Edition Basics" in s
    assert "Works In This Edition" not in s        # edition-only — placeholder not surfaced


def test_review_pane_editable_basics_readonly_works_with_edit_link(tmp_path):
    app, db = _app(tmp_path)
    eid, w, ed_author, wk_author = _classical_edition(db)
    WD.store_detection(db, eid, "single", WD.detect_single(
        db, eid, classical=lambda c: {"system": "toh", "number": "3824", "english": "Root Stanzas"}))
    db.commit()
    with app.test_client() as c:
        pane = c.get("/works/detect/single").data.decode()
        card = c.get(f"/works/detect/{eid}/edit").data.decode()
    # Edition Basics is EDITABLE in Review (the card fragment carries the chip editors).
    assert "bbMountPersonSearch" in card and f"saveEditionTitle({eid}, this)" in card
    # The works are READ-ONLY in the pane, each with an "✎ edit this work →" link.
    assert "Works In This Edition" in pane and f'href="/work/{w}"' in pane
    assert f'data-card-url="/work/{w}/card"' not in pane     # not edited inline any more
    # The dead pre-refactor markup is gone.
    assert "mw-work" not in pane and "wie-work" in pane


def test_edition_basics_shows_file_location_browse_and_review(tmp_path):
    """REGRESSION: the holding's file LOCATION must appear in Edition Basics on BOTH
    the read-only Browse summary and the editable Review pane, each paired with the
    📖↗ open-in-viewer control that opens that holding's file."""
    app, db = _app(tmp_path)
    eid, w, _ea, _wa = _classical_edition(db)
    real = tmp_path / "book.pdf"; real.write_bytes(b"%PDF-1.4")   # a present file → live icon
    db.execute("UPDATE holding SET file_path = ? WHERE edition_id = ?", (str(real), eid))
    WD.store_detection(db, eid, "single", WD.detect_single(
        db, eid, classical=lambda c: {"system": "toh", "number": "3824", "english": "Root Stanzas"}))
    db.commit()
    hid = db.execute("SELECT id FROM holding WHERE edition_id = ?", (eid,)).fetchone()[0]
    with app.test_client() as c:
        browse = c.get(f"/edition/{eid}/works-summary").data.decode()
        review = c.get(f"/works/detect/{eid}/edit").data.decode()
    for pane in (browse, review):
        assert str(real) in pane                        # the actual file location is shown
        assert "📖↗" in pane                             # paired open-in-viewer control
        assert f"/holding/{hid}/file" in pane           # icon opens THIS holding's file


def test_review_add_picker_renders_above_the_works(tmp_path):
    app, db = _app(tmp_path)
    eid, w, _ea, _wa = _classical_edition(db)
    WD.store_detection(db, eid, "single", WD.detect_single(
        db, eid, classical=lambda c: {"system": "toh", "number": "3824", "english": "Root Stanzas"}))
    db.commit()
    with app.test_client() as c:
        pane = c.get("/works/detect/single").data.decode()
    add_at = pane.find("Add or link a work")
    works_at = pane.find("Works In This Edition")
    assert 0 <= add_at < works_at        # the add/link picker precedes the works section
