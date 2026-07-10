"""Work editing page (/work/<id>): subject autocomplete (datalist + add-new),
filename aliases hidden, and the delete confirm listing linked editions."""
import pytest

from catalogue.db_store import connect, add_alias
from catalogue.webui.web import create_app


@pytest.fixture
def ctx(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wid, "Ocean of Reasoning", "english")
    add_alias(db, "work", wid, "Ocean of Reasoning.pdf", "filename")     # should be hidden
    eid = db.execute("INSERT INTO edition (title) VALUES ('Tsongkhapa — Ocean')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    # an existing subject elsewhere → should appear as a datalist option
    from catalogue.services import subjects as S
    S.add_subject(db, "work", wid, "Madhyamaka")
    other = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    S.add_subject(db, "work", other, "Pramana")
    db.commit()
    return app, wid, eid


def test_subject_autocomplete_datalist(ctx):
    app, wid, eid = ctx
    with app.test_client() as c:
        page = c.get(f"/work/{wid}").data.decode()
    assert "<datalist" in page and 'list="subj-opts' in page
    assert "<option value=\"Pramana\">" in page and "<option value=\"Madhyamaka\">" in page   # existing subjects offered
    assert "type a new one to create it" in page                          # add-new affordance


def test_filename_alias_hidden(ctx):
    app, wid, eid = ctx
    with app.test_client() as c:
        page = c.get(f"/work/{wid}").data.decode()
    assert "Ocean of Reasoning" in page
    assert "Ocean of Reasoning.pdf" not in page          # the filename-derived alias row is hidden


def test_native_title_alias_edit_syncs_work_columns(ctx):
    """The work.sanskrit_title / tibetan_title COLUMNS (read by search + work-review) are
    derived from the title aliases: adding a wylie/iast alias fills the column, deleting it
    clears the column — so editing the name in one place never strands a stale copy."""
    app, wid, eid = ctx
    with app.test_client() as c:
        # add a Tibetan (wylie) + Sanskrit (iast) title via the alias CRUD
        c.post(f"/work/{wid}/alias/add", data={"text": "rigs pa'i rgya mtsho", "scheme": "wylie"})
        c.post(f"/work/{wid}/alias/add", data={"text": "Yuktiṣaṣṭikā", "scheme": "iast"})
        db = connect(app.config["DB_PATH"])
        row = db.execute("SELECT tibetan_title, sanskrit_title FROM work WHERE id=?", (wid,)).fetchone()
        assert row == ("rigs pa'i rgya mtsho", "Yuktiṣaṣṭikā")           # columns filled from aliases

        # now delete the wylie alias → its column must clear (no stale title)
        aid = db.execute("SELECT id FROM work_alias WHERE work_id=? AND scheme='wylie'", (wid,)).fetchone()[0]
        c.post(f"/work/{wid}/alias/{aid}/delete")
    db = connect(app.config["DB_PATH"])
    row = db.execute("SELECT tibetan_title, sanskrit_title FROM work WHERE id=?", (wid,)).fetchone()
    assert row == (None, "Yuktiṣaṣṭikā")                                  # tibetan cleared, sanskrit kept


def _title(db, wid):
    """The display/primary title = the lowest-id alias (every read does ORDER BY id LIMIT 1)."""
    return db.execute("SELECT text FROM work_alias WHERE work_id=? ORDER BY id LIMIT 1",
                      (wid,)).fetchone()[0]


def test_alias_rename_in_place(ctx):
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    aid = db.execute("SELECT id FROM work_alias WHERE work_id=? AND scheme='english'",
                     (wid,)).fetchone()[0]
    with app.test_client() as c:
        c.post(f"/work/{wid}/alias/{aid}/rename", data={"text": "Ocean of Reasoning (rev.)"})
    db = connect(app.config["DB_PATH"])
    text, key = db.execute("SELECT text, normalized_key FROM work_alias WHERE id=?", (aid,)).fetchone()
    assert text == "Ocean of Reasoning (rev.)"
    from catalogue.db_store import fold_key
    assert key == fold_key(text)                         # normalized_key re-folded (§4.2)


def test_set_alias_as_primary_title(ctx):
    """Make primary promotes a non-primary alias to the display title (lowest-id slot),
    so it becomes the title everywhere the work is listed."""
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    add_alias(db, "work", wid, "Rigs pa'i rgya mtsho", "english")   # a later (non-primary) alias
    db.commit()
    second = db.execute("SELECT id FROM work_alias WHERE work_id=? AND text=?",
                        (wid, "Rigs pa'i rgya mtsho")).fetchone()[0]
    assert _title(db, wid) == "Ocean of Reasoning"       # before: the first alias is the title
    with app.test_client() as c:
        c.post(f"/work/{wid}/alias/{second}/primary")
    db = connect(app.config["DB_PATH"])
    assert _title(db, wid) == "Rigs pa'i rgya mtsho"     # after: the chosen text is the display title
    # and the page marks exactly one row primary (the badge span, not the intro paragraph)
    with app.test_client() as c:
        page = c.get(f"/work/{wid}").data.decode()
    assert page.count("★ primary</span>") == 1


def test_work_page_shows_rename_and_make_primary_controls(ctx):
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    add_alias(db, "work", wid, "Second title", "english")   # a non-primary alias to promote
    db.commit()
    with app.test_client() as c:
        page = c.get(f"/work/{wid}").data.decode()
    assert f"/work/{wid}/alias/" in page and "/rename" in page and "/primary" in page
    assert "★ primary" in page and "Make primary" in page


def test_work_card_shows_root_commentary_checkboxes(ctx):
    """The Root/commentary section offers two mutually-exclusive checkboxes (no standalone
    'Type' field); the root-text picker only appears once 'Commentary' is checked."""
    app, wid, eid = ctx
    with app.test_client() as c:
        page = c.get(f"/work/{wid}").data.decode()
    assert "Root / commentary" in page
    assert 'action="/work/%d/set-type"' % wid in page and "wkToggleType" in page
    assert "Root text" in page and "Commentary" in page
    assert 'name="work_type" list=' not in page                  # the old datalist Type field is gone
    assert 'action="/work/%d/commentary-of"' % wid not in page   # root-text picker hidden until 'Commentary'


def test_set_type_checkboxes_root_commentary_or_neither(ctx):
    app, wid, eid = ctx
    with app.test_client() as c:
        c.post(f"/work/{wid}/set-type", data={"work_type": "root"})
        db = connect(app.config["DB_PATH"])
        assert db.execute("SELECT work_type FROM work WHERE id=?", (wid,)).fetchone()[0] == "root"
        assert 'action="/work/%d/commentary-of"' % wid not in c.get(f"/work/{wid}").data.decode()  # root → no root box
        # switch to commentary → the root-text picker now appears
        c.post(f"/work/{wid}/set-type", data={"work_type": "commentary"})
        page = c.get(f"/work/{wid}").data.decode()
        assert 'action="/work/%d/commentary-of"' % wid in page
        # uncheck both → neither (work_type cleared)
        c.post(f"/work/{wid}/set-type", data={"work_type": ""})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT work_type FROM work WHERE id=?", (wid,)).fetchone()[0] is None


def test_unchecking_commentary_drops_root_link(ctx):
    """Re-classifying a commentary as a root (or neither) drops its commentary→root link."""
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    root = db.execute("INSERT INTO work (work_type) VALUES ('root')").lastrowid
    add_alias(db, "work", root, "Root", "english")
    db.commit()
    with app.test_client() as c:
        c.post(f"/work/{wid}/commentary-of", data={"work_id": root})    # commentary + link
        c.post(f"/work/{wid}/set-type", data={"work_type": "root"})     # now a root
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT work_type FROM work WHERE id=?", (wid,)).fetchone()[0] == "root"
    assert db.execute("SELECT COUNT(*) FROM relationship WHERE from_work_id=?", (wid,)).fetchone()[0] == 0


def test_set_and_clear_commentary_root(ctx):
    """Picking a root marks this work a commentary and records commentary_on; clearing
    drops the relationship (both works kept). Per-work, so it's independent of editions."""
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    root = db.execute("INSERT INTO work (work_type) VALUES ('root')").lastrowid
    add_alias(db, "work", root, "The Root Verses", "english")
    db.commit()
    with app.test_client() as c:
        c.post(f"/work/{wid}/commentary-of", data={"work_id": root})
        db = connect(app.config["DB_PATH"])
        assert db.execute(
            "SELECT 1 FROM relationship WHERE from_work_id=? AND to_work_id=? "
            "AND relation='commentary_on'", (wid, root)).fetchone()
        assert db.execute("SELECT work_type FROM work WHERE id=?", (wid,)).fetchone()[0] == "commentary"
        # the commentary's card now names its root; the root's card lists its commentary
        assert f"#{root}" in c.get(f"/work/{wid}").data.decode()
        assert f"#{wid}" in c.get(f"/work/{root}").data.decode()
        # clearing removes the relationship; both works remain
        c.post(f"/work/{wid}/commentary-of/clear")
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM relationship WHERE from_work_id=?", (wid,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM work WHERE id IN (?, ?)", (wid, root)).fetchone()[0] == 2


def test_commentary_root_ignores_self_link(ctx):
    app, wid, eid = ctx
    with app.test_client() as c:
        c.post(f"/work/{wid}/commentary-of", data={"work_id": wid})    # can't comment on itself
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM relationship WHERE from_work_id=?", (wid,)).fetchone()[0] == 0


def test_add_author_via_picker(ctx):
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Tsongkhapa')").lastrowid
    db.commit()
    with app.test_client() as c:
        # the page offers the picker + add form
        page = c.get(f"/work/{wid}").data.decode()
        assert "Add an author" in page and "mountAuthorPicker" in page
        assert 'action="/work/%d/author/add"' % wid in page
        # picking a person submits the add form
        c.post(f"/work/{wid}/author/add", data={"pid": pid, "role": "author"})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT role FROM work_author WHERE work_id=? AND person_id=?",
                      (wid, pid)).fetchone()[0] == "author"


def test_add_author_create_new_opens_editable_person_page(ctx):
    """A NEW author (no match): the picker sends new_author_name; the server creates the
    person, links them as author, and redirects to their EDITABLE page (with a back link)."""
    app, wid, eid = ctx
    with app.test_client() as c:
        r = c.post(f"/work/{wid}/author/add",
                   data={"new_author_name": "Newly Typed Author", "role": "author"})
        assert r.status_code in (302, 303)
        loc = r.headers["Location"]
        assert "/person/" in loc and f"from_work={wid}" in loc        # → the new author's page
        db = connect(app.config["DB_PATH"])
        pid = db.execute("SELECT p.id FROM person p JOIN work_author wa ON wa.person_id=p.id "
                         "WHERE wa.work_id=? AND p.primary_name='Newly Typed Author'", (wid,)).fetchone()[0]
        page = c.get(loc).data.decode()
    assert f'action="/person/{pid}/edit"' in page and "Save" in page   # the page is editable
    assert "back to the work" in page                                  # from_work banner


def test_person_page_edit_fields(ctx):
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Initial')").lastrowid
    db.commit()
    with app.test_client() as c:
        c.post(f"/person/{pid}/edit", data={
            "primary_name": "Tsongkhapa", "dates": "1357–1419",
            "role_hint": "author", "verification_status": "verified"})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT primary_name, dates, role_hint, verification_status "
                      "FROM person WHERE id=?", (pid,)).fetchone() == \
        ("Tsongkhapa", "1357–1419", "author", "verified")
    assert db.execute("SELECT 1 FROM person_alias WHERE person_id=? AND text='Tsongkhapa'",
                      (pid,)).fetchone()                              # new name seeded as alias


def test_person_page_alias_add_and_delete(ctx):
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('P')").lastrowid
    db.commit()
    with app.test_client() as c:
        c.post(f"/person/{pid}/alias/add", data={"text": "Je Rinpoche", "scheme": "english"})
        db = connect(app.config["DB_PATH"])
        aid = db.execute("SELECT id FROM person_alias WHERE person_id=? AND text='Je Rinpoche'",
                         (pid,)).fetchone()[0]
        c.post(f"/person/{pid}/alias/{aid}/delete")
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT 1 FROM person_alias WHERE id=?", (aid,)).fetchone() is None


def test_person_make_alias_primary(ctx):
    """Promote an alias to the display name; the old primary name is kept as an alias."""
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Lobzang Drakpa')").lastrowid
    add_alias(db, "person", pid, "Lobzang Drakpa", "english")     # seed the current name
    add_alias(db, "person", pid, "Tsongkhapa", "english")         # an alias to promote
    db.commit()
    aid = db.execute("SELECT id FROM person_alias WHERE person_id=? AND text='Tsongkhapa'",
                     (pid,)).fetchone()[0]
    with app.test_client() as c:
        c.post(f"/person/{pid}/alias/{aid}/primary")
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT primary_name FROM person WHERE id=?", (pid,)).fetchone()[0] == "Tsongkhapa"
    assert db.execute("SELECT 1 FROM person_alias WHERE person_id=? AND text='Lobzang Drakpa'",
                      (pid,)).fetchone()                          # old name kept as an alias
    # the page marks the promoted alias primary and offers Make primary on the other
    with app.test_client() as c:
        page = c.get(f"/person/{pid}").data.decode()
    assert "★ primary" in page and "Make primary" in page


def test_remove_author(ctx):
    app, wid, eid = ctx
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Candrakīrti')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?, ?, 'author')", (wid, pid))
    db.commit()
    with app.test_client() as c:
        c.post(f"/work/{wid}/author/remove", data={"pid": pid, "role": "author"})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT 1 FROM work_author WHERE work_id=? AND person_id=?",
                      (wid, pid)).fetchone() is None


def test_add_author_rejects_unknown_person_and_role(ctx):
    app, wid, eid = ctx
    with app.test_client() as c:
        assert c.post(f"/work/{wid}/author/add", data={"pid": 99999, "role": "author"}).status_code == 404
        db = connect(app.config["DB_PATH"])
        pid = db.execute("INSERT INTO person (primary_name) VALUES ('X')").lastrowid
        db.commit()
        assert c.post(f"/work/{wid}/author/add", data={"pid": pid, "role": "bogus"}).status_code == 400


def test_delete_confirm_lists_linked_editions(ctx):
    app, wid, eid = ctx
    with app.test_client() as c:
        page = c.get(f"/work/{wid}").data.decode()
    # the linked edition is embedded on the delete button (data-editions); the confirm
    # warning lives in the work-card JS included on the page.
    assert "data-editions" in page
    assert "Tsongkhapa \\u2014 Ocean" in page or "Tsongkhapa — Ocean" in page
    assert "NO LONGER have it" in page
