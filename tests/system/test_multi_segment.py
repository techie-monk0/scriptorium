"""Multi-work review pane: per-segment operator resolution (no auto-create, no dedup).

Each AI-detected segment of a multi_work edition is resolved by the operator one at a
time — either CREATE a new work from the detection, or attach an EXISTING work (the same
work picker the single-work pane uses). These black-box tests pin that contract:
create-from-detection mints + links the work and retires that detection from the triage
list (the new work lives in "Works In This Edition"); use-existing links an existing work
with no new row and marks the segment resolved; "Delete all" clears the AI proposal but
keeps resolved works; unlink detaches; the whole surface is gated on the
`multi_work_detection` feature flag.
"""
from __future__ import annotations

import json
import sqlite3


def _seed_multi(seed, *, eid: int, title: str = "An Anthology of Treatises") -> int:
    """Arrange a multi_work edition + holding + a 2-segment 'deterministic' detection."""
    seed("INSERT INTO edition (id, title, structure) VALUES (?, ?, 'multi_work')", (eid, title))
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
         (eid, f"/seg{eid}.pdf"))
    payload = {
        "stored_title": title, "n_sections": 3, "n_titles": 2, "applied": False,
        "methods": {"deterministic": {"works": [
            {"title": "First Treatise", "authors": ["Nagarjuna"],
             "canonical": {"system": "toh", "number": "3824"},
             "title_sanskrit": "Mulamadhyamakakarika"},
            {"title": "Second Treatise", "authors": [], "canonical": {}},
        ]}},
    }
    seed("INSERT INTO work_detection (edition_id, kind, payload_json) VALUES (?, 'multi', ?)",
         (eid, json.dumps(payload)))
    return eid


def _conn(app):
    c = sqlite3.connect(app.config["DB_PATH"])
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _segments(app, eid):
    conn = _conn(app)
    row = conn.execute("SELECT payload_json FROM work_detection WHERE edition_id = ?",
                       (eid,)).fetchone()
    conn.close()
    return json.loads(row[0])["methods"]["deterministic"]["works"]


def test_create_from_detection_mints_links_and_records(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=600)

    r = c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0",
               data={"from_detection": "1"})
    assert r.status_code == 302

    conn = _conn(app)
    link = conn.execute(
        "SELECT work_id, sequence FROM edition_work WHERE edition_id = ?", (eid,)).fetchone()
    assert link is not None and link[1] == 1            # linked at the segment's sequence
    wid = link[0]
    # the new work carries the segment's detected fields
    assert conn.execute("SELECT 1 FROM work_alias WHERE work_id=? AND text='First Treatise'",
                        (wid,)).fetchone()
    assert conn.execute("SELECT canonical_system, canonical_number FROM work WHERE id=?",
                        (wid,)).fetchone() == ("toh", "3824")
    assert conn.execute("SELECT 1 FROM work_author WHERE work_id=?", (wid,)).fetchone()
    conn.close()
    # making it into a new work retires the detection from the AI-triage list (the work now
    # lives in "Works In This Edition") — only the still-unresolved segment remains
    segs = _segments(app, eid)
    assert len(segs) == 1 and segs[0]["title"] == "Second Treatise"


def test_make_new_retires_the_detection_but_keeps_the_work(app_env, seed, monkeypatch):
    """Making an AI detection into a new work removes it from the AI-triage list (it now
    lives in 'Works In This Edition'), while the new work stays linked to the edition.
    A following 'Delete all' therefore can't sweep the curated work away."""
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=614)
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0",
           data={"from_detection": "1"})
    # the detection is gone from the triage list...
    segs = _segments(app, eid)
    assert len(segs) == 1 and segs[0]["title"] == "Second Treatise"
    # ...but the minted work is linked, and survives a Delete-all
    conn = _conn(app)
    wid = conn.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0]
    conn.close()
    c.post(f"/works/detect/{eid}/segments/clear")
    conn = _conn(app)
    assert conn.execute("SELECT 1 FROM edition_work WHERE edition_id=? AND work_id=?",
                        (eid, wid)).fetchone() is not None
    conn.close()


def test_use_existing_work_links_without_creating(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=601)
    seed("INSERT INTO work (id) VALUES (900)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (900, 'A Pre-existing Work', 'english', 'a pre-existing work')")

    before = _conn(app).execute("SELECT COUNT(*) FROM work").fetchone()[0]
    r = c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=1",
               data={"work_id": "900"})
    assert r.status_code == 302

    conn = _conn(app)
    assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] == before   # no new work
    assert conn.execute(
        "SELECT sequence FROM edition_work WHERE edition_id=? AND work_id=900", (eid,)
    ).fetchone()[0] == 2
    conn.close()
    assert _segments(app, eid)[1]["work_id"] == 900


def test_unlink_detaches_and_clears_segment(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=602)
    seed("INSERT INTO work (id) VALUES (905)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (905, 'Linked Text', 'english', 'linked text')")
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0",
           data={"work_id": "905"})
    wid = _segments(app, eid)[0]["work_id"]

    r = c.post(f"/works/detect/{eid}/segment/unlink?method=deterministic&idx=0")
    assert r.status_code == 302
    conn = _conn(app)
    assert conn.execute(
        "SELECT 1 FROM edition_work WHERE edition_id=? AND work_id=?", (eid, wid)).fetchone() is None
    conn.close()
    assert "work_id" not in _segments(app, eid)[0]      # segment back to unresolved


def test_repick_drops_the_old_link(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=603)
    seed("INSERT INTO work (id) VALUES (900)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (900, 'Original', 'english', 'original')")
    seed("INSERT INTO work (id) VALUES (901)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (901, 'Replacement', 'english', 'replacement')")
    # resolve the segment to one existing work, then re-pick the SAME segment to another
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0",
           data={"work_id": "900"})
    first_wid = _segments(app, eid)[0]["work_id"]
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0",
           data={"work_id": "901"})

    conn = _conn(app)
    links = [r[0] for r in conn.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchall()]
    conn.close()
    assert links == [901]                               # old link dropped, only the new one
    assert first_wid == 900
    assert _segments(app, eid)[0]["work_id"] == 901


def test_segment_routes_404_when_feature_off(app_env, seed, monkeypatch):
    """The AI-detection SURFACE (per-segment resolve/delete) is gated off with the feature.
    add-work / work-detach are generic edition↔work link/detach shared with the classical
    single-work pane, so they are NOT gated — covered by test_add_work_* above."""
    c, app, _ = app_env
    monkeypatch.setattr("catalogue.services.features.feature_enabled",
                        lambda name, default=False: False)
    eid = _seed_multi(seed, eid=604)
    q = "method=deterministic&idx=0"
    assert c.post(f"/works/detect/{eid}/segment/link?{q}",
                  data={"work_id": "1"}).status_code == 404
    assert c.post(f"/works/detect/{eid}/segment/delete?{q}").status_code == 404


def test_delete_segment_removes_it_from_proposal(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=606)
    assert len(_segments(app, eid)) == 2
    r = c.post(f"/works/detect/{eid}/segment/delete?method=deterministic&idx=0")
    assert r.status_code == 302
    segs = _segments(app, eid)
    assert len(segs) == 1 and segs[0]["title"] == "Second Treatise"


def test_delete_resolved_segment_also_detaches_its_work(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=607)
    seed("INSERT INTO work (id) VALUES (910)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (910, 'A Linked Text', 'english', 'a linked text')")
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0", data={"work_id": "910"})
    c.post(f"/works/detect/{eid}/segment/delete?method=deterministic&idx=0")
    conn = _conn(app)
    assert conn.execute("SELECT 1 FROM edition_work WHERE edition_id=? AND work_id=910",
                        (eid,)).fetchone() is None
    conn.close()
    assert len(_segments(app, eid)) == 1


def test_add_work_links_an_existing_missed_work(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=608)
    seed("INSERT INTO work (id) VALUES (920)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (920, 'A Missed Text', 'english', 'a missed text')")
    before = _conn(app).execute("SELECT COUNT(*) FROM work").fetchone()[0]
    r = c.post(f"/works/detect/{eid}/add-work", data={"work_id": "920"})
    assert r.status_code == 302
    conn = _conn(app)
    assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] == before   # no new work
    assert conn.execute("SELECT 1 FROM edition_work WHERE edition_id=? AND work_id=920",
                        (eid,)).fetchone()
    conn.close()


def test_add_work_can_add_a_brand_new_work(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=609)
    before = _conn(app).execute("SELECT COUNT(*) FROM work").fetchone()[0]
    r = c.post(f"/works/detect/{eid}/add-work", data={"english_title": "A Missed Treatise"})
    assert r.status_code == 302
    conn = _conn(app)
    assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] == before + 1
    assert conn.execute(
        "SELECT 1 FROM edition_work ew JOIN work_alias wa ON wa.work_id = ew.work_id "
        "WHERE ew.edition_id=? AND wa.text='A Missed Treatise'", (eid,)).fetchone()
    conn.close()


def test_add_work_adds_any_number_one_per_pick(app_env, seed, monkeypatch):
    """The premise the operator confirmed: a single 'add a work' section adds unlimited works."""
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=610)
    for t in ("Added One", "Added Two", "Added Three"):
        c.post(f"/works/detect/{eid}/add-work", data={"english_title": t})
    conn = _conn(app)
    assert conn.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=?",
                        (eid,)).fetchone()[0] == 3
    conn.close()


def test_detach_work_unlinks_and_clears_segment(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=611)
    seed("INSERT INTO work (id) VALUES (930)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (930, 'Z', 'english', 'z')")
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0", data={"work_id": "930"})
    assert _segments(app, eid)[0]["work_id"] == 930
    r = c.post(f"/works/detect/{eid}/work-detach?wid=930")
    assert r.status_code == 302
    conn = _conn(app)
    assert conn.execute("SELECT 1 FROM edition_work WHERE edition_id=? AND work_id=930",
                        (eid,)).fetchone() is None
    conn.close()
    assert "work_id" not in _segments(app, eid)[0]        # the segment returns to triage


def test_work_note_editable_inline_in_review_pane(app_env, seed, monkeypatch):
    """A linked work in the multi-work Review pane offers an inline per-edition note
    editor (edition_work.note); the work-note route persists it, blank clears it, and
    the saved value pre-fills the field on re-render."""
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=620)
    seed("INSERT INTO work (id) VALUES (950)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (950, 'A Root Text', 'english', 'a root text')")
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0", data={"work_id": "950"})

    # the editable Works-In-This-Edition section exposes the note form for the linked work
    body = c.get("/works/detect/multi").data.decode()
    assert f'/works/detect/{eid}/work-note?wid=950' in body
    assert "save note" in body

    # save persists onto the join
    r = c.post(f"/works/detect/{eid}/work-note?wid=950",
               data={"note": "only chs. 1-3 of the root text"})
    assert r.status_code == 302
    conn = _conn(app)
    assert conn.execute("SELECT note FROM edition_work WHERE edition_id=? AND work_id=950",
                        (eid,)).fetchone()[0] == "only chs. 1-3 of the root text"
    conn.close()
    # ...and pre-fills on re-render
    assert "only chs. 1-3 of the root text" in c.get("/works/detect/multi").data.decode()

    # blank clears it
    c.post(f"/works/detect/{eid}/work-note?wid=950", data={"note": "  "})
    conn = _conn(app)
    assert conn.execute("SELECT note FROM edition_work WHERE edition_id=? AND work_id=950",
                        (eid,)).fetchone()[0] is None
    conn.close()


def test_clear_all_segments_empties_lists_but_keeps_resolved_works(app_env, seed, monkeypatch):
    """'Delete all' clears the AI proposal text only — works already resolved from segments
    (here a linked existing work, but equally one made-new earlier) STAY linked to the
    edition. The bug this pins: Delete-all used to detach the operator's curated works."""
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=612)
    seed("INSERT INTO work (id) VALUES (940)")
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (940, 'Resolved One', 'english', 'resolved one')")
    c.post(f"/works/detect/{eid}/segment/link?method=deterministic&idx=0", data={"work_id": "940"})
    assert len(_segments(app, eid)) == 2
    r = c.post(f"/works/detect/{eid}/segments/clear")
    assert r.status_code == 302
    assert _segments(app, eid) == []                      # every method's works emptied
    conn = _conn(app)
    assert conn.execute("SELECT 1 FROM edition_work WHERE edition_id=? AND work_id=940",
                        (eid,)).fetchone() is not None    # the resolved work is KEPT
    conn.close()


def test_clear_all_segments_404_when_feature_off(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setattr("catalogue.services.features.feature_enabled",
                        lambda name, default=False: False)
    assert c.post("/works/detect/1/segments/clear").status_code == 404


def test_ai_section_hidden_once_all_segments_gone(app_env, seed, monkeypatch):
    """With detected texts present the AI-triage section + 'Delete all' show; after a
    clear-all the whole section disappears (nothing left to triage)."""
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=613, title="Vanishing Section Book")
    body = c.get("/works/detect/multi").data.decode()
    assert "AI-detected texts" in body and "Delete all" in body
    c.post(f"/works/detect/{eid}/segments/clear")
    body = c.get("/works/detect/multi").data.decode()
    assert "AI-detected texts" not in body and "Delete all" not in body
    assert "Works In This Edition" in body                # the running-result section stays


def test_multi_pane_renders_two_sections(app_env, seed, monkeypatch):
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    _seed_multi(seed, eid=605, title="Render Check Anthology")
    body = c.get("/works/detect/multi").data.decode()
    assert "Works In This Edition" in body                 # the running-result section
    assert "Add a work the detection missed" in body       # shared add picker (under-detection)
    assert "Replace with an existing work" in body         # per-segment replace
    assert "Delete" in body                                 # per-segment delete
    assert "Make this into new work" in body                # per-segment mint-new (from_detection)
    assert 'name="from_detection"' in body                  # ...wired to the create-from-detection path
    assert "First Treatise" in body                         # the detected text is shown
    assert "data-bb-refresh" in body                        # forms submit in place — no auto-advance


def _det(app, eid):
    """The detection payload dict for an edition (test read helper)."""
    conn = _conn(app)
    row = conn.execute("SELECT payload_json FROM work_detection WHERE edition_id = ?",
                       (eid,)).fetchone()
    conn.close()
    return json.loads(row[0])


def test_mark_reviewed_toggles_multi_done(app_env, seed, monkeypatch):
    """A multi-work edition has no single-apply; its contained texts are curated by hand.
    '✓ Mark reviewed' flips the detection done (so it leaves the Books backlog), and
    '↺ Unmark reviewed' flips it back — no AI segmentation involved."""
    c, app, _ = app_env
    monkeypatch.setenv("CATALOGUE_FEATURES", "multi_work_detection")
    eid = _seed_multi(seed, eid=620, title="Hand-Curated Anthology")
    body = c.get("/works/detect/multi").data.decode()
    assert "Mark reviewed" in body                          # offered before
    assert _det(app, eid)["applied"] is False

    r = c.post(f"/works/detect/{eid}/reviewed", json={"value": "1"})
    assert r.status_code == 200 and r.get_json()["reviewed"] is True
    d = _det(app, eid)
    assert d["applied"] is True and d["reviewed_manual"] is True

    body = c.get("/works/detect/multi").data.decode()
    assert "Unmark reviewed" in body                        # now offered to undo the manual review

    r = c.post(f"/works/detect/{eid}/reviewed", json={"value": "0"})
    assert r.get_json()["reviewed"] is False
    d = _det(app, eid)
    assert d["applied"] is False and "reviewed_manual" not in d


def test_remarked_multi_single_curates_works_inline_in_books_tab(app_env, seed):
    """A single-detection edition RE-MARKED multi-work (structure='multi_work', kind='single'
    detection) is NOT listed on /works/detect/multi — so it must be curatable IN the Books
    tab: the 'Works In This Edition' section + add picker render inline, with no misleading
    'segment it in the multi-work pane' jump (the bug: that link landed on a different book)."""
    c, app, _ = app_env
    eid = 630
    seed("INSERT INTO edition (id, title, structure) VALUES (?, 'Atisha in Tibet', 'multi_work')", (eid,))
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/a.pdf')", (eid,))
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    seed("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    payload = {"stored_title": "Atisha in Tibet", "applied": False,
               "determination": "modern", "methods": {}}
    seed("INSERT INTO work_detection (edition_id, kind, payload_json) VALUES (?, 'single', ?)",
         (eid, json.dumps(payload)))

    body = c.get("/works/detect/single").data.decode()
    assert "Works In This Edition" in body                  # curated inline, in the Books tab
    assert f"/works/detect/{eid}/add-work" in body          # the add/link picker is present
    assert f'href="/work/{wid}"' in body                    # "✎ edit this work →" link
    assert "segment it in the multi-work pane" not in body  # the misleading cross-pane link is gone
    # And it can be marked reviewed from here (action bar offers it, single-apply is off).
    assert "Mark reviewed" in body and "✓ Apply" not in body
