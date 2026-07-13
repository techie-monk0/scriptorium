"""Layer 2 — an EDITION is a modern commentary on classical work(s)
(`edition_commentary_on`, edition→work, many-to-many). Covers the add/remove routes, the
read-only Browse banner + per-work ⬑ back-ref, internal vs external targets, and the
`library.edition_commentaries` / surfacing builder. Hermetic (Flask test client).

See docs/design/commentary_relationships_model.md."""
import pytest

from catalogue.db_store import add_alias, connect
from catalogue.services import library
from catalogue.webui.web import create_app


@pytest.fixture
def ctx(tmp_path):
    """A multi-work edition holding w1 + w2 (both surface in "Works In This Edition"),
    plus an EXTERNAL work w3 not contained in the edition."""
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    eid = db.execute(
        "INSERT INTO edition (title, structure) VALUES ('Modern Teaching', 'multi_work')").lastrowid
    w1 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    w2 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    w3 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", w1, "Root Text One", "english")
    add_alias(db, "work", w2, "Root Text Two", "english")
    add_alias(db, "work", w3, "External Source", "english")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, w1))
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 2)", (eid, w2))
    db.commit()
    return app, eid, w1, w2, w3


def _add(c, eid, wid):
    return c.post(f"/edition/{eid}/modern-commentary/add", data={"work_id": wid})


def test_add_internal_target_shows_banner_and_backref(ctx):
    app, eid, w1, w2, w3 = ctx
    with app.test_client() as c:
        _add(c, eid, w1)
        html = c.get(f"/edition/{eid}/works-summary").data.decode()
    # The "Commentary on" section names the target and links to its work page.
    assert "📘 Commentary on" in html
    assert f'/work/{w1}' in html and "Root Text One" in html
    # The contained target work carries the ⬑ back-ref; the other contained work does not.
    assert "this edition's modern commentary is on this work" in html
    assert html.count("this edition's modern commentary is on this work") == 1


def test_many_to_many_lists_all_targets(ctx):
    app, eid, w1, w2, w3 = ctx
    with app.test_client() as c:
        _add(c, eid, w1)
        _add(c, eid, w2)
        html = c.get(f"/edition/{eid}/works-summary").data.decode()
    assert "Root Text One" in html and "Root Text Two" in html
    # Both contained works are now targets → two back-refs.
    assert html.count("this edition's modern commentary is on this work") == 2
    assert len(library.edition_commentaries(connect(app.config["DB_PATH"]), eid)) == 2


def test_external_target_banner_only_no_backref(ctx):
    app, eid, w1, w2, w3 = ctx
    with app.test_client() as c:
        _add(c, eid, w3)                                   # w3 is NOT contained in the edition
        html = c.get(f"/edition/{eid}/works-summary").data.decode()
    assert "External Source" in html and f'/work/{w3}' in html      # appears in the banner
    assert "this edition's modern commentary is on this work" not in html  # no contained block


def test_remove_drops_the_edge(ctx):
    app, eid, w1, w2, w3 = ctx
    with app.test_client() as c:
        _add(c, eid, w1)
        c.post(f"/edition/{eid}/modern-commentary/{w1}/remove")
        html = c.get(f"/edition/{eid}/works-summary").data.decode()
    assert "📘 Commentary on" not in html                      # section hidden when no edges
    assert "this edition's modern commentary is on this work" not in html
    assert library.edition_commentaries(connect(app.config["DB_PATH"]), eid) == []


def test_add_is_idempotent(ctx):
    app, eid, w1, w2, w3 = ctx
    with app.test_client() as c:
        _add(c, eid, w1)
        _add(c, eid, w1)                                   # INSERT OR IGNORE — no duplicate
    db = connect(app.config["DB_PATH"])
    assert db.execute(
        "SELECT COUNT(*) FROM edition_commentary_on WHERE edition_id = ? AND to_work_id = ?",
        (eid, w1)).fetchone()[0] == 1


def test_browse_is_readonly_with_review_link_picker_only_in_review(ctx):
    """Browse stays look-only: the read-only summary shows NO inline picker, just a link to
    this edition's Review card (deep-linked by row id). The add picker lives in Review."""
    app, eid, w1, w2, w3 = ctx
    with app.test_client() as c:
        browse = c.get(f"/edition/{eid}/works-summary").data.decode()
        review = c.get(f"/works/detect/{eid}/edit").data.decode()
    assert f"/works/detect/{eid}/review" in browse             # link to the Review card (seeds + deep-links)
    assert "Edit this edition on the Review page" in browse
    assert "this edition comments on" not in browse            # no inline picker on Browse
    assert "this edition comments on" in review                # picker lives in Review


def test_layout_commentary_above_collapsed_basics_with_contributors_inside(ctx):
    """Commentary-on sits at the top, directly above the Edition Basics section; the
    contributors (Authors/Translators) now live INSIDE Edition Basics, which is collapsed
    by default and whose summary is an <h3> matching the sibling section headers."""
    app, eid, w1, w2, w3 = ctx
    with app.test_client() as c:
        _add(c, eid, w1)
        h = c.get(f"/edition/{eid}/works-summary").data.decode()
    iC = h.find("📘 Commentary on")
    iB = h.find("<summary><h3>Edition Basics</h3></summary>")
    iA = h.find("Authors")
    assert 0 <= iC < iB < iA                                   # commentary → basics summary → contributors (inside)
    assert 'class="ed-basics"' in h and '<details class="ed-basics" open' not in h  # collapsed


def test_builder_returns_id_and_title_and_marks_targets(ctx):
    app, eid, w1, w2, w3 = ctx
    db = connect(app.config["DB_PATH"])
    db.execute("INSERT INTO edition_commentary_on (edition_id, to_work_id) VALUES (?, ?)", (eid, w1))
    db.commit()
    assert library.edition_commentaries(db, eid) == [{"id": w1, "title": "Root Text One"}]
    summaries = {s["id"]: s for s in library.edition_work_summaries(db, eid)}
    assert summaries[w1]["is_modern_commentary_target"] is True
    assert summaries[w2]["is_modern_commentary_target"] is False
