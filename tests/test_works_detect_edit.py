"""In-pane editing of a single-work edition before apply: title, authors/translators,
multi-work reclassify, and link/add works. Hermetic (Flask test client)."""
import pytest

from catalogue.db_store import connect, contributor_store as cs
from catalogue.services import work_detect as WD, works_apply as WA
from catalogue.webui.web import create_app


@pytest.fixture
def ctx(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Jampa Tegchok')").lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, pid))
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Old Title', 'single_work')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    db.commit()
    return app, eid, pid


def test_edit_card_renders_and_has_pickers(ctx):
    app, eid, pid = ctx
    with app.test_client() as c:
        html = c.get(f"/works/detect/{eid}/edit").data.decode()
    assert "Old Title" in html and "Stored title" in html
    assert 'bbMountPersonSearch' in html and 'bbMountWorkPick' in html
    assert 'add a new work' in html and '*' in html                 # required marker present
    # The old degenerate-work "this edition is a commentary" checkbox is gone, replaced by
    # the edition-level Layer-2 "Commentary on" row + work picker (works for single + multi).
    assert 'this edition is a commentary' not in html
    assert 'Commentary on' in html
    assert 'this edition comments on' in html                       # the modern-commentary picker


def test_detail_pane_has_card_and_shortcuts(ctx):
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
    assert f'data-card-url="/works/detect/{eid}/edit"' in page
    assert 'data-key="s"' in page                                   # accept & apply
    assert 'bbMountNewWorkAuthors' in page


def test_work_picker_search_is_local_live_is_opt_in(ctx):
    """Perf regression: the work picker searches the LOCAL catalogue + offline 84000 as
    you type (no network), so it doesn't re-search after every letter. The slow live
    BDRC/Wikidata lookup is behind an explicit opt-in button, never fired per keystroke."""
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()        # page-level picker JS
        card = c.get(f"/works/detect/{eid}/edit").data.decode()   # the picker markup
    # As-you-type URL is LOCAL (authority=1) and carries NO live=1 …
    assert "&editions=1&authority=1`" in page
    assert "&editions=1&authority=1&live=1" not in page
    # … while the opt-in live search still exists (its own handler + URL + button).
    assert "function bbWorkPickLive" in page and "&authority=1&live=1`" in page
    assert "also search live authorities" in card
    # the in-form authority search (also network) is debounced longer, not per-keystroke
    assert "debounce: 450" in page


def test_card_lists_holdings_with_open_links(ctx):
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    # the helper added one '/x.pdf' holding; add an EPUB so the edition has two files
    db.execute("INSERT INTO holding (edition_id, form, holding_type, file_path) "
               "VALUES (?, 'electronic', 'epub', '/lib/A.epub')", (eid,))
    db.commit()
    with app.test_client() as c:
        # Holdings + preview moved OUT of the /edit card into _edition_extras.html, rendered
        # in the detail pane (below the works), not in the editable Edition Basics card.
        card = c.get(f"/works/detect/{eid}/edit").data.decode()
        page = c.get("/works/detect/single").data.decode()
    assert "Holdings" not in card                                   # no longer in the edit card
    assert "Holdings" in page                                       # now in the detail pane
    assert "A.epub" in page and ".pdf" in page                      # both files listed
    assert page.count("openNative(") >= 2                            # an open link per file
    app, eid, pid = ctx
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/set-title", data={"title": "New Title"})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT title FROM edition WHERE id=?", (eid,)).fetchone()[0] == "New Title"


def test_save_title_reads_from_clicked_button_not_global_id(ctx):
    """Regression (title-won't-save-after-linking-a-work): once a card form is submitted the
    book-browser syncs the FILLED card into the row's hidden .detail-src snapshot, so two
    inputs share id ed-title-<eid>. Saving must read the LIVE pane's input via the clicked
    button — a bare document.getElementById would grab the stale hidden duplicate."""
    app, eid, pid = ctx
    with app.test_client() as c:
        card = c.get(f"/works/detect/{eid}/edit").data.decode()
        page = c.get("/works/detect/single").data.decode()
    assert f"saveEditionTitle({eid}, this)" in card                   # button passes its own element
    assert "btn.closest('td')" in page and ".detail #ed-title-" in page  # scoped to the live pane


def test_set_title_renames_placeholder_work(ctx):
    """Fixing a garbled edition title via set-title renames the linked placeholder work's
    English alias too, so it doesn't keep the old (garbled) name and apply still drops it."""
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    wid = db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0]
    from catalogue.db_store import add_alias
    add_alias(db, "work", wid, "Old Title", "english")          # placeholder mirrors the book title
    db.commit()
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/set-title", data={"title": "Fixed Title"})
    db = connect(app.config["DB_PATH"])
    assert [t for (t,) in db.execute(
        "SELECT text FROM work_alias WHERE work_id=? AND scheme='english'", (wid,))] == ["Fixed Title"]
    WA.apply_single(db, eid)                                     # still recognised → dropped
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (wid,)).fetchone()[0] == 0


def test_edit_card_isbn_is_editable_in_review(ctx):
    """The Review edit card exposes an editable ISBN input + Save button wired to the
    in-place saveEditionIsbn helper (Browse keeps it read-only)."""
    app, eid, pid = ctx
    with app.test_client() as c:
        card = c.get(f"/works/detect/{eid}/edit").data.decode()
    assert f'id="ed-isbn-{eid}"' in card                          # editable input present
    assert f"saveEditionIsbn({eid}, this)" in card                # wired to the in-place save


def test_set_isbn_persists_and_blank_clears(ctx):
    app, eid, pid = ctx
    with app.test_client() as c:
        r = c.post(f"/works/detect/{eid}/set-isbn", data={"isbn": " 9780861714865 "})
        assert r.status_code == 200
        db = connect(app.config["DB_PATH"])
        assert db.execute("SELECT isbn FROM edition WHERE id=?", (eid,)).fetchone()[0] == "9780861714865"
        db.close()
        c.post(f"/works/detect/{eid}/set-isbn", data={"isbn": "   "})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT isbn FROM edition WHERE id=?", (eid,)).fetchone()[0] is None


def test_save_isbn_reads_from_clicked_button_not_global_id(ctx):
    """Same stale-snapshot guard as the title save: scope the input to the clicked
    button's cell, not a bare getElementById on the duplicated .detail-src copy."""
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
    assert "function saveEditionIsbn" in page
    assert ".detail #ed-isbn-" in page and "btn.closest('td')" in page


def test_detected_author_candidates_persist_after_adding_one(ctx):
    """Two candidate authors (both on the linked work) show as quick-adds; adding ONE must
    not hide the OTHER — you can still add the second (multi-author works)."""
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    wid = db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0]
    other = db.execute("INSERT INTO person (primary_name) VALUES ('Geshe Sopa')").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, other))
    db.commit()
    with app.test_client() as c:
        card = c.get(f"/works/detect/{eid}/edit").data.decode()
        assert 'use detected author">+ Jampa Tegchok' in card          # both candidates offered
        assert 'use detected author">+ Geshe Sopa' in card
        c.post(f"/works/detect/{eid}/author/add", data={"pid": pid})   # add ONE
        card2 = c.get(f"/works/detect/{eid}/edit").data.decode()
    assert f'/person/{pid}"' in card2                                   # added one is now a chip
    assert 'use detected author">+ Geshe Sopa' in card2                # the OTHER still addable
    assert 'use detected author">+ Jampa Tegchok' not in card2         # added one no longer a candidate


def test_contributor_picker_reopens_for_multi_add(ctx):
    """The shared picker re-opens after a pick (with the query restored) so multiple people
    can be added from one search — wired via the Typeahead initialQuery + bb:cardrendered."""
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
    assert "initialQuery" in page                                      # _typeahead supports re-open
    assert "bb:cardrendered" in page                                   # card host emits the hook
    assert "_reopenContrib" in page                                    # page re-opens the picker


def test_volume_field_sets_and_clears(ctx):
    """The volume number at the top stores edition.volume; 0 means no volume (NULL)."""
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
        assert "saveVolume(" in page and ">volume<" in page          # numeric field present
        assert c.post(f"/works/detect/{eid}/volume", json={"volume": 3}).get_json()["volume"] == 3
        assert connect(app.config["DB_PATH"]).execute(
            "SELECT volume FROM edition WHERE id=?", (eid,)).fetchone()[0] == "3"
        page = c.get("/works/detect/single").data.decode()
        assert "vol. 3" in page                                      # volume shown in the title/heading
        # 0 → no volume (NULL), and no longer in the title
        assert c.post(f"/works/detect/{eid}/volume", json={"volume": 0}).get_json()["volume"] == 0
        page = c.get("/works/detect/single").data.decode()
        assert "vol. " not in page
    assert connect(app.config["DB_PATH"]).execute(
        "SELECT volume FROM edition WHERE id=?", (eid,)).fetchone()[0] is None


def test_classical_checkbox_toggles_determination(ctx):
    """The 'classical work' checkbox flips the single edition's determination both ways."""
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
        assert "classical work" in page and "toggleClassical(" in page
        assert c.post(f"/works/detect/{eid}/determination",
                      json={"classical": "1"}).get_json()["determination"] == "classical"
        assert c.post(f"/works/detect/{eid}/determination",
                      json={"classical": ""}).get_json()["determination"] == "modern"
    from catalogue.services import work_detect as WD
    assert WD.get_detection(connect(app.config["DB_PATH"]), eid)["determination"] == "modern"


def test_apply_warns_on_authority_work_without_classical(ctx):
    """A MAIN work with a canonical authority id on a not-classical edition → apply returns a
    confirm; the UI's OK ticks 'classical work' then applies (work kept)."""
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    wid = db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0]
    db.execute("UPDATE work SET canonical_system='toh', canonical_number='3824' WHERE id=?", (wid,))
    db.commit()
    with app.test_client() as c:
        r = c.post(f"/works/detect/{eid}/apply", json={}).get_json()
        assert r["status"] == "confirm" and r["field"] == "classical"
        c.post(f"/works/detect/{eid}/determination", json={"classical": "1"})   # OK path
        assert c.post(f"/works/detect/{eid}/apply", json={}).get_json()["status"] == "applied"
    assert wid in [x[0] for x in connect(app.config["DB_PATH"]).execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]          # work kept


def test_apply_no_warn_without_authority_work(ctx):
    """A plain modern edition (no canonical work) applies with no classical prompt."""
    app, eid, pid = ctx
    with app.test_client() as c:
        assert c.post(f"/works/detect/{eid}/apply", json={}).get_json()["status"] == "applied"


def test_authority_search_shows_spinner_and_clock(ctx):
    """In-flight authority searches show a spinner + a live elapsed-seconds clock."""
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
        # The typeahead component is now a shared static file (loaded by the shim) so the
        # PWA can use it too; the spinner+clock helper + styles live there, not inline.
        assert "/static/js/typeahead.js" in page                   # component is loaded
        ta = c.get("/static/js/typeahead.js").data.decode()
    assert "function startSearching" in ta                         # typeahead spinner+clock helper
    assert "ta-spin" in ta and "ta-elapsed" in ta                  # spinner + elapsed markup/CSS
    assert "searching authorities…" in page                        # candidate-box label (page-level)
    assert "searchingLabel" in page                                # picker passes a label


def test_save_actions_show_toast_confirmation(ctx):
    """Every save pops an explicit 'Saved ✓' toast (distinct from the static Applied banner),
    so a post-apply edit visibly registers."""
    app, eid, pid = ctx
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
    assert 'id = \'bb-toast\'' in page and "function bbToast" in page   # shared toast in the shell
    assert "bbToast('Saved ✓')" in page                           # card edits confirm
    assert "Title saved ✓" in page                                # title save confirms


def test_author_add_remove_drives_apply(ctx):
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    other = db.execute("INSERT INTO person (primary_name) VALUES ('Geshe Sopa')").lastrowid
    db.commit()
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/author/add", data={"pid": other})
        assert cs.edition_author_ids(connect(app.config["DB_PATH"]), eid) == [other]
        c.post(f"/works/detect/{eid}/author/remove", data={"pid": other})
        assert cs.edition_author_ids(connect(app.config["DB_PATH"]), eid) == []
        # re-add, then apply (modern) → the edited author wins, lands on the edition
        c.post(f"/works/detect/{eid}/author/add", data={"pid": other})
        c.post(f"/works/detect/{eid}/apply")
    assert cs.edition_author_ids(connect(app.config["DB_PATH"]), eid) == [other]


def test_translator_add(ctx):
    app, eid, pid = ctx
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/translator/add", data={"pid": pid})
    assert cs.edition_translator_ids(connect(app.config["DB_PATH"]), eid) == [pid]


def test_mark_multi_work_moves_to_multi_group(ctx):
    app, eid, pid = ctx
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/structure", data={"multi": "1"})
        page = c.get("/works/detect/single").data.decode()
    assert connect(app.config["DB_PATH"]).execute(
        "SELECT structure FROM edition WHERE id=?", (eid,)).fetchone()[0] == "multi_work"
    # stays in the single pane (so it can be flipped back) but under the Multi-work group
    assert "Old Title" in page and "Multi-work" in page


def test_link_and_unlink_work(ctx):
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    w2 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Lamrim', 'english', 'lamrim')", (w2,))
    db.commit()
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/work/link", data={"work_id": w2})
        assert w2 in [r[0] for r in connect(app.config["DB_PATH"]).execute(
            "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]
        c.post(f"/works/detect/{eid}/work/unlink", data={"work_id": w2})
        assert w2 not in [r[0] for r in connect(app.config["DB_PATH"]).execute(
            "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]


def test_new_work_type_radio_root_commentary(ctx):
    app, eid, pid = ctx
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/work/link", data={
            "english_title": "A Root", "work_type": "root"})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT work_type FROM work WHERE work_type='root'").fetchone()[0] == "root"


def test_commentary_and_root_relationship(ctx):
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    root = db.execute("INSERT INTO work (work_type) VALUES ('root')").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Bodhicaryavatara', 'english', 'bodhicaryavatara')", (root,))
    db.commit()
    with app.test_client() as c:
        # add the commentary as a NEW work, then point it at the existing root. These
        # work↔work (Layer 1) routes are kept though the old degenerate-work UI was
        # replaced by the edition-level Layer-2 "Commentary on" row.
        c.post(f"/works/detect/{eid}/work/set-commentary",
               data={"english_title": "A Guide — commentary"})
        c.post(f"/works/detect/{eid}/work/set-root", data={"work_id": root})
    db = connect(app.config["DB_PATH"])
    comm = db.execute("SELECT id FROM work WHERE work_type='commentary'").fetchone()[0]
    # relationship recorded, both works typed + linked
    assert db.execute("SELECT to_work_id FROM relationship WHERE from_work_id=? AND "
                      "relation='commentary_on'", (comm,)).fetchone()[0] == root
    assert db.execute("SELECT work_type FROM work WHERE id=?", (root,)).fetchone()[0] == "root"
    linked = [r[0] for r in db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]
    assert comm in linked and root in linked


def test_add_new_commentary_work_records_root_relation(ctx):
    """The add-a-new-work form (work_type=commentary + root_work_id) records the
    commentary_on relation when the work is created — not just the work_type."""
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    root = db.execute("INSERT INTO work (work_type) VALUES ('root')").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'Bodhicaryavatara', 'english', 'bodhicaryavatara')", (root,))
    db.commit()
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/add-work", data={
            "english_title": "A Guide — commentary", "subjects": "ethics",
            "work_type": "commentary", "root_work_id": root})
    db = connect(app.config["DB_PATH"])
    comm = db.execute("SELECT id FROM work WHERE work_type='commentary'").fetchone()[0]
    assert db.execute("SELECT to_work_id FROM relationship WHERE from_work_id=? AND "
                      "relation='commentary_on'", (comm,)).fetchone()[0] == root
    assert comm in [r[0] for r in db.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]


def test_add_new_root_work_records_no_relation(ctx):
    """A new ROOT (or no-type) work ignores any stray root_work_id — only a commentary
    names a root."""
    app, eid, pid = ctx
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/add-work", data={
            "english_title": "A Root", "subjects": "ethics", "work_type": "root"})
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM relationship "
                      "WHERE relation='commentary_on'").fetchone()[0] == 0


def test_new_work_form_has_commentary_root_picker(ctx):
    """The add-a-new-work form exposes a root-text picker that toggles on commentary."""
    app, eid, pid = ctx
    with app.test_client() as c:
        html = c.get(f"/works/detect/{eid}/edit").data.decode()
    assert 'name="root_work_id"' in html
    assert 'bbNewWorkType' in html and 'bbMountNewWorkRoot' in html


def test_classical_single_edition_shows_readonly_work_with_edit_link(ctx):
    """A CLASSICAL single-work edition shows its canonical work as the SAME read-only
    "Works In This Edition" section the multi pane uses (Work Basics + collapsed Work
    Details), with a "✎ edit this work →" link to /work/<id>; editing is NOT inline. Its
    edition card drops the modern-only Works chip-row + 'this edition is a commentary' block."""
    from catalogue.db_store import add_alias
    app, _eid, _pid = ctx
    db = connect(app.config["DB_PATH"])
    w = db.execute("INSERT INTO work (work_type, canonical_system, canonical_number) "
                   "VALUES ('root', 'toh', '3824')").lastrowid
    add_alias(db, "work", w, "Root Stanzas", "english")
    ce = db.execute("INSERT INTO edition (title, structure) "
                    "VALUES ('A Classical Text', 'single_work')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (ce, w))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/c.pdf')", (ce,))
    WD.store_detection(db, ce, "single", WD.detect_single(
        db, ce, classical=lambda c: {"system": "toh", "number": "3824", "english": "Root Stanzas"}))
    db.commit()
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
        assert "Works In This Edition" in page                      # shared read-only works section
        assert f'href="/work/{w}"' in page                          # "✎ edit this work →" link
        assert "Root Stanzas" in page                               # the work's title (Work Basics)
        assert f'data-card-url="/work/{w}/card"' not in page        # NOT edited inline any more
        card = c.get(f"/works/detect/{ce}/edit").data.decode()
        assert "this edition is a commentary" not in card           # modern-only block dropped for classical


def test_edition_inherits_work_subjects_then_overrides(ctx):
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    from catalogue.services import subjects as S
    w = db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0]
    S.add_subject(db, "work", w, "Dharma/Emptiness")        # subject lives on the WORK
    db.commit()
    with app.test_client() as c:
        card = c.get(f"/works/detect/{eid}/edit").data.decode()
        assert "Dharma/Emptiness" in card and "inherited from the works" in card
        # adding an edition subject overrides the inherited set
        c.post(f"/subjects/edition/{eid}/add", data={"name": "History"})
        card2 = c.get(f"/works/detect/{eid}/edit").data.decode()
    assert "overriding the works' subjects" in card2 and "History" in card2


def test_new_work_form_attaches_subjects(ctx):
    app, eid, pid = ctx
    from catalogue.services import subjects as S
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/work/link", data={
            "english_title": "Lamp of the Path", "subjects": "Lamrim, Dharma/Path"})
    db = connect(app.config["DB_PATH"])
    w = db.execute("SELECT id FROM work WHERE id IN "
                   "(SELECT work_id FROM work_alias WHERE normalized_key LIKE '%lamp%')").fetchone()[0]
    names = {n for _, n in S.subjects_for(db, "work", w)}
    assert names == {"Lamrim", "Dharma/Path"}


def test_add_new_work_with_authors_and_required(ctx):
    app, eid, pid = ctx
    with app.test_client() as c:
        assert c.post(f"/works/detect/{eid}/work/new", data={"english_title": ""}).status_code == 400
        c.post(f"/works/detect/{eid}/work/new", data={
            "english_title": "Ocean of Reasoning", "sanskrit_title": "",
            "canonical_system": "toh", "canonical_number": "3824",
            "work_type": "commentary", "author_pids": [str(pid)]})
    db = connect(app.config["DB_PATH"])
    row = db.execute("SELECT id, work_type FROM work WHERE canonical_number='3824'").fetchone()
    assert row and row[1] == "commentary"
    assert pid in cs.work_author_ids(db, row[0])                     # author attached
    assert row[0] in [r[0] for r in db.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]   # linked


def test_author_add_by_name_creates_person(ctx):
    # The contributor picker's "➕ add as a new person": POST a name (no pid) → create
    # the person and link them as author. This is what made adding a brand-new author
    # (e.g. "Laurence Dreyfus", absent from the DB) silently do nothing before.
    app, eid, pid = ctx
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/author/add", data={"name": "Laurence Dreyfus", "pid": ""})
    db = connect(app.config["DB_PATH"])
    new = db.execute("SELECT id FROM person WHERE primary_name='Laurence Dreyfus'").fetchone()
    assert new and new[0] in cs.edition_author_ids(db, eid)


def test_bulk_author_route_resolves_or_creates(ctx):
    # Bulk "Add author" over ticked editions — same {ids, name} contract as bulk-subject.
    app, eid, pid = ctx
    db = connect(app.config["DB_PATH"])
    eid2 = db.execute("INSERT INTO edition (title) VALUES ('Second')").lastrowid
    db.commit()
    with app.test_client() as c:
        r = c.post("/works/detect/bulk-author", json={"ids": [eid, eid2], "name": "Laurence Dreyfus"})
        j = r.get_json()
        assert r.status_code == 200 and len(j["assigned"]) == 2
        new_pid = j["person_id"]
        # Re-adding the SAME name (different case) reuses the person — no duplicate.
        r2 = c.post("/works/detect/bulk-author", json={"ids": [eid], "name": "laurence dreyfus"})
        assert r2.get_json()["person_id"] == new_pid
    db = connect(app.config["DB_PATH"])
    assert new_pid in cs.edition_author_ids(db, eid)
    assert new_pid in cs.edition_author_ids(db, eid2)
    assert db.execute("SELECT COUNT(*) FROM person WHERE primary_name LIKE 'Laurence Dreyfus' "
                      "COLLATE NOCASE").fetchone()[0] == 1
