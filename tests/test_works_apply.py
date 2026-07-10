"""Part D: apply a verified single-work detection into canonical rows.
classical → link the canonical work + drop the degenerate one; modern → author(s)
to the edition + drop the work. Hermetic."""
import pytest

from catalogue.db_store import init_db, connect, fold_key
from catalogue.db_store import contributor_store as cs
from catalogue.services import work_detect as WD, works_apply as WA, work_identity as WI
from catalogue.services import contributor_undo as undo, subjects as S
from catalogue.webui.web import create_app


def _graph(db, eid):
    """A comparable snapshot of an edition's work-graph for roundtrip assertions."""
    ew = sorted(db.execute("SELECT work_id, sequence FROM edition_work WHERE edition_id=?", (eid,)))
    ea = sorted(db.execute("SELECT person_id, role FROM edition_author WHERE edition_id=?", (eid,)))
    wks = sorted(r[0] for r in db.execute("SELECT id FROM work"))
    return (ew, ea, wks)


def _edition_with_degenerate_work(db, title, author):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (author,)).lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, pid))
    eid = db.execute("INSERT INTO edition (title, structure) VALUES (?, 'single_work')",
                     (title,)).lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)",
               (eid, wid))
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/x.pdf')", (eid,))
    return eid, wid, pid


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "c.db")
    yield conn
    conn.close()


def test_apply_modern_moves_author_and_drops_work(db):
    eid, wid, pid = _edition_with_degenerate_work(db, "Insight Into Emptiness", "Jampa Tegchok")
    WD.store_detection(db, eid, "single", WD.detect_single(
        db, eid, classical=lambda c: {"english": c["title"]}))   # → modern (no signal)
    res = WA.apply_single(db, eid)
    assert res["determination"] == "modern"
    assert cs.edition_author_ids(db, eid) == [pid]               # author moved to the edition
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (wid,)).fetchone()[0] == 0   # degenerate work gone
    assert WD.get_detection(db, eid)["applied"] is True


def test_apply_classical_links_canonical_and_drops_degenerate(db):
    eid, wid, pid = _edition_with_degenerate_work(db, "Fundamental Wisdom", "Nāgārjuna")

    def fake(ctx):
        return {"english": ctx["title"], "authority_en": "The Root Stanzas",
                "sanskrit": "Mūlamadhyamakakārikā", "tibetan": "dbu ma rtsa ba",
                "system": "toh", "number": "3824", "confidence": 0.95}

    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=fake))
    res = WA.apply_single(db, eid)
    assert res["determination"] == "classical"
    cwid = res["work_id"]
    # edition now links the canonical work, which carries the canonical# + native titles + author
    row = db.execute("SELECT canonical_system, canonical_number, sanskrit_title, review_status "
                     "FROM work WHERE id=?", (cwid,)).fetchone()
    assert row[0] == "toh" and row[1] == "3824" and row[2] == "Mūlamadhyamakakārikā" and row[3] == "ok"
    assert cs.work_author_ids(db, cwid) == [pid]
    assert db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0] == cwid
    if cwid != wid:
        assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (wid,)).fetchone()[0] == 0


def test_two_editions_one_canonical_text_share_one_work(db):
    # two editions of the MMK → one canonical work after applying both
    def fake(ctx):
        return {"english": ctx["title"], "authority_en": "The Root Stanzas",
                "sanskrit": "Mūlamadhyamakakārikā", "system": "toh", "number": "3824",
                "confidence": 0.95}
    e1, _, _ = _edition_with_degenerate_work(db, "Fundamental Wisdom", "Nāgārjuna")
    e2, _, _ = _edition_with_degenerate_work(db, "Root Verses on the Middle Way", "Nāgārjuna")
    for e in (e1, e2):
        WD.store_detection(db, e, "single", WD.detect_single(db, e, classical=fake))
        WA.apply_single(db, e)
    assert db.execute("SELECT COUNT(*) FROM work WHERE canonical_number='3824'").fetchone()[0] == 1
    w = db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (e1,)).fetchone()[0]
    assert db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (e2,)).fetchone()[0] == w


def test_apply_idempotent_and_skips_unknown(db):
    eid, _, _ = _edition_with_degenerate_work(db, "X", "Y")
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": "X"}))
    WA.apply_single(db, eid)
    n_works = db.execute("SELECT COUNT(*) FROM work").fetchone()[0]
    WA.apply_single(db, eid)                                     # second apply: no further change
    assert db.execute("SELECT COUNT(*) FROM work").fetchone()[0] == n_works
    assert WA.apply_single(db, 999999)["status"] == "skip"


def test_apply_multi_materialises_chosen_method(db):
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Anthology', 'multi_work')").lastrowid
    # whole-book degenerate work currently on the edition
    w0 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, w0))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "multi", {
        "stored_title": "Anthology", "n_sections": 5, "n_titles": 3,
        "file": {}, "methods": {
            "deterministic": {"works": [{"title": "Whole Book", "authors": []}]},
            "claude": {"works": [
                {"title": "Song of Saraha", "authors": ["Saraha"], "canonical": {}},
                {"title": "Song of Tilopa", "authors": ["Tilopa"], "canonical": {}}]}}})

    res = WA.apply_multi(db, eid, "claude")
    assert res["status"] == "applied" and len(res["works_created"]) == 2
    # the edition now contains the two segmented works (whole-book work dropped)
    titles = sorted(db.execute(
        "SELECT wa.text FROM edition_work ew JOIN work_alias wa ON wa.work_id = ew.work_id "
        "WHERE ew.edition_id = ? AND wa.scheme = 'english'", (eid,)).fetchall())
    assert [t[0] for t in titles] == ["Song of Saraha", "Song of Tilopa"]
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (w0,)).fetchone()[0] == 0   # whole-book gone
    assert WD.get_detection(db, eid)["applied_method"] == "claude"
    # authors resolved to persons
    assert db.execute("SELECT COUNT(*) FROM person WHERE primary_name IN ('Saraha','Tilopa')").fetchone()[0] == 2


def test_apply_attaches_folder_subject(db, monkeypatch):
    from catalogue.services import subjects as S
    monkeypatch.setattr(S, "subject_root", lambda db: "/lib/01 Books - Dharma")
    # modern → subject lands on the EDITION
    em, _, _ = _edition_with_degenerate_work(db, "Insight", "Jampa Tegchok")
    db.execute("UPDATE holding SET file_path=? WHERE edition_id=?",
               ("/lib/01 Books - Dharma/Emptiness/A.pdf", em))
    WD.store_detection(db, em, "single", WD.detect_single(db, em, classical=lambda c: {"english": c["title"]}))
    res = WA.apply_single(db, em)
    assert res["determination"] == "modern"
    assert {n for _, n in S.subjects_for(db, "edition", em)} == {"Emptiness"}
    undo.apply_undo(db, res["undo_token"])
    assert S.subjects_for(db, "edition", em) == []                       # reverted

    # classical → subject lands on the WORK (edition inherits)
    ec, _, _ = _edition_with_degenerate_work(db, "Fundamental Wisdom", "Nāgārjuna")
    db.execute("UPDATE holding SET file_path=? WHERE edition_id=?",
               ("/lib/01 Books - Dharma/Madhyamaka/B.pdf", ec))
    WD.store_detection(db, ec, "single", WD.detect_single(db, ec, classical=lambda c: {
        "english": c["title"], "sanskrit": "Mūlamadhyamakakārikā", "system": "toh",
        "number": "3824", "confidence": 0.95}))
    res = WA.apply_single(db, ec)
    assert res["determination"] == "classical"
    assert {n for _, n in S.subjects_for(db, "work", res["work_id"])} == {"Madhyamaka"}
    assert S.subjects_for(db, "edition", ec) == []                       # edition inherits, no own


@pytest.mark.parametrize("mechanism", ["authority_id", "name_search", "add_new_native",
                                       "add_new_english", "root_text", "shared"])
def test_apply_keeps_curated_work_from_any_mechanism(db, mechanism):
    """REGRESSION: a work the operator added by ANY mechanism survives apply — only the
    auto-minted degenerate placeholder is dropped (the 'Fire Offerings + toh:3871' bug)."""
    eid, degen, pid = _edition_with_degenerate_work(db, "A Manual of Ritual Fire Offerings", "Ngawang")
    # make the placeholder look like a real auto-minted one (english book title + filename)
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) VALUES (?,?,?,?)",
               (degen, "A Manual of Ritual Fire Offerings", "english",
                fold_key("A Manual of Ritual Fire Offerings")))
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'manual.pdf', 'filename', 'manualpdf')", (degen,))

    if mechanism == "authority_id":          # toh:/bdr: paste → canonical set
        cur, _, _ = WI.create_work(db, english_title="Entering the Way",
                                   canonical_system="toh", canonical_number="3871")
    elif mechanism == "name_search":         # picked an existing real work (has canonical)
        cur, _, _ = WI.create_work(db, english_title="Some Real Text",
                                   canonical_system="bdrc", canonical_number="bdr:WA1")
    elif mechanism == "add_new_native":      # typed a Tibetan-only new work (wylie alias)
        cur, _, _ = WI.create_work(db, tibetan_title="dbu ma rtsa ba")
    elif mechanism == "add_new_english":     # typed a new English title (≠ the book title)
        cur, _, _ = WI.create_work(db, english_title="A Different Treatise")
    elif mechanism == "root_text":           # set as root → work_type='root'
        cur, _, _ = WI.create_work(db, english_title="A Root", work_type="root")
    else:                                    # shared across another edition
        cur, _, _ = WI.create_work(db, english_title="Shared")
        e2 = db.execute("INSERT INTO edition (title) VALUES ('Other')").lastrowid
        cs.link_work(db, e2, cur)
    cs.link_work(db, eid, cur)
    WD.store_detection(db, eid, "single",
                       WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))  # → modern

    WA.apply_single(db, eid)
    linked = [r[0] for r in db.execute("SELECT work_id FROM edition_work WHERE edition_id=?", (eid,))]
    assert cur in linked                                          # the curated work SURVIVES
    assert degen not in linked                                    # the placeholder is dropped
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (degen,)).fetchone()[0] == 0


def test_rename_syncs_placeholder_then_apply_drops_it(db):
    """Renaming a garbled edition renames its placeholder work too, and apply then drops
    the placeholder (not keeps it under the fixed name)."""
    eid, degen, pid = _edition_with_degenerate_work(db, "Grbled Titel", "Author")
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) VALUES (?,?,?,?)",
               (degen, "Grbled Titel", "english", fold_key("Grbled Titel")))
    WD.store_detection(db, eid, "single",
                       WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    WA.sync_placeholder_title(db, eid, "Garbled Title")                  # operator fixes the name
    db.execute("UPDATE edition SET title=? WHERE id=?", ("Garbled Title", eid)); db.commit()
    assert [t for (t,) in db.execute(
        "SELECT text FROM work_alias WHERE work_id=? AND scheme='english'", (degen,))] == ["Garbled Title"]
    WA.apply_single(db, eid)
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (degen,)).fetchone()[0] == 0


def test_apply_drops_modern_placeholder_with_auto_subject_and_migrates_it(db):
    """A MODERN book has no work, so its filename-auto-minted placeholder is dropped on apply
    even when a directory subject got auto-attached — and that subject MOVES to the edition so
    it isn't lost. (Live bug: edition 292 'The Two Truths' kept work #138 'Contradiction and
    Context' because the work carried an auto-attached subject.) Reversible."""
    eid, degen, pid = _edition_with_degenerate_work(db, "The Two Truths", "Author")
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) VALUES (?,?,?,?)",
               (degen, "Contradiction and Context", "english", fold_key("Contradiction and Context")))
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'two truths.pdf', 'filename', 'twotruthspdf')", (degen,))
    S.add_subject(db, "work", degen, "Madhyamaka")                  # dir-scan auto-attached a subject
    WD.store_detection(db, eid, "single",
                       WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))  # → modern
    res = WA.apply_single(db, eid)
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (degen,)).fetchone()[0] == 0   # dropped
    assert db.execute("SELECT person_id FROM edition_author WHERE edition_id=?",
                      (eid,)).fetchall() == [(pid,)]               # modern: author on the edition
    esub = [n for _i, n in S.subjects_for(db, "edition", eid)]
    assert "Madhyamaka" in esub                                    # subject preserved on the edition
    # undo restores the work AND its own subject
    from catalogue.services import contributor_undo as U
    U.apply_undo(db, res["undo_token"])
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (degen,)).fetchone()[0] == 1
    assert [n for _i, n in S.subjects_for(db, "work", degen)] == ["Madhyamaka"]


def test_apply_keeps_classical_placeholder_with_subject(db):
    """On a CLASSICAL edition a subject still PROTECTS the work (the work IS the text) — only
    modern placeholders ignore the subject. So a classical text isn't dropped."""
    eid, degen, pid = _edition_with_degenerate_work(db, "Mūlamadhyamakakārikā", "Nāgārjuna")
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, 'mmk.pdf', 'filename', 'mmkpdf')", (degen,))
    S.add_subject(db, "work", degen, "Madhyamaka")
    # a classical determination (canonical present) → the placeholder must NOT be dropped
    WD.store_detection(db, eid, "single", WD.detect_single(
        db, eid, classical=lambda c: {"english": c["title"], "system": "toh", "number": "3824"}))
    WA.apply_single(db, eid)
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (degen,)).fetchone()[0] == 1   # kept


def test_apply_drops_placeholder_stranded_by_earlier_rename(db):
    """#217 recovery: a placeholder whose English alias was left as the OLD garbled name by a
    pre-fix rename is still recognised (via the detection stored_title) and dropped on apply."""
    eid, degen, pid = _edition_with_degenerate_work(db, "Old Garbled", "Author")
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) VALUES (?,?,?,?)",
               (degen, "Old Garbled", "english", fold_key("Old Garbled")))
    WD.store_detection(db, eid, "single",
                       WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    db.execute("UPDATE edition SET title='Fixed Title' WHERE id=?", (eid,)); db.commit()  # rename WITHOUT sync
    WA.apply_single(db, eid)
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (degen,)).fetchone()[0] == 0


def test_apply_single_classical_is_reversible(db):
    eid, wid, pid = _edition_with_degenerate_work(db, "Fundamental Wisdom", "Nāgārjuna")

    def fake(ctx):
        return {"english": ctx["title"], "sanskrit": "Mūlamadhyamakakārikā",
                "system": "toh", "number": "3824", "confidence": 0.95}

    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=fake))
    before = _graph(db, eid)
    res = WA.apply_single(db, eid)
    assert res["undo_token"] and _graph(db, eid) != before        # mutated
    out = undo.apply_undo(db, res["undo_token"])
    assert out.get("edition_id") == eid
    assert _graph(db, eid) == before                              # fully restored
    assert WD.get_detection(db, eid).get("applied") is not True   # report flag reverted
    # the canonical work the op minted is gone again
    assert db.execute("SELECT COUNT(*) FROM work WHERE canonical_number='3824'").fetchone()[0] == 0


def test_apply_single_modern_is_reversible(db):
    eid, wid, pid = _edition_with_degenerate_work(db, "Insight Into Emptiness", "Jampa Tegchok")
    WD.store_detection(db, eid, "single",
                       WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    before = _graph(db, eid)
    res = WA.apply_single(db, eid)
    assert cs.edition_author_ids(db, eid) == [pid]
    undo.apply_undo(db, res["undo_token"])
    assert _graph(db, eid) == before                              # degenerate work + link back
    assert cs.edition_author_ids(db, eid) == []                   # author edge removed


def test_apply_multi_is_reversible(db):
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Anthology', 'multi_work')").lastrowid
    w0 = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, w0))
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/x.pdf')", (eid,))
    WD.store_detection(db, eid, "multi", {
        "stored_title": "Anthology", "n_sections": 2, "n_titles": 2, "file": {},
        "methods": {"claude": {"works": [
            {"title": "Song of Saraha", "authors": ["Saraha"], "canonical": {}},
            {"title": "Song of Tilopa", "authors": ["Tilopa"], "canonical": {}}]}}})
    before = _graph(db, eid)
    res = WA.apply_multi(db, eid, "claude")
    assert _graph(db, eid) != before
    undo.apply_undo(db, res["undo_token"])
    assert _graph(db, eid) == before                              # whole-book work + link restored
    assert db.execute("SELECT COUNT(*) FROM work WHERE id=?", (w0,)).fetchone()[0] == 1


def test_undo_refused_after_intervening_edit(db):
    eid, wid, pid = _edition_with_degenerate_work(db, "X", "Y")
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": "X"}))
    res = WA.apply_single(db, eid)
    db.execute("INSERT INTO edition_author (edition_id, person_id) VALUES (?, ?)",
               (eid, db.execute("INSERT INTO person (primary_name) VALUES ('Z')").lastrowid))
    db.commit()
    assert "error" in undo.apply_undo(db, res["undo_token"])      # fingerprint changed → refused


def test_web_apply_button_and_route(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    eid, wid, pid = _edition_with_degenerate_work(db, "Insight Into Emptiness", "Jampa Tegchok")
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    db.commit()
    with app.test_client() as c:
        page = c.get("/works/detect/single").data
        assert b'data-key="s"' in page and b"applyEdition(" in page        # apply-and-next wired
        # AJAX apply (the `s` flow) returns JSON incl. the undo token
        r = c.post(f"/works/detect/{eid}/apply", headers={"X-Requested-With": "fetch"}).get_json()
        assert r["status"] == "applied" and r["undo_token"]
    db = connect(app.config["DB_PATH"])
    assert cs.edition_author_ids(db, eid) == [pid]                        # applied
    assert WD.get_detection(db, eid)["applied"] is True


def test_web_undo_button_and_route(tmp_path):
    from catalogue.services import work_undo
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    eid, wid, pid = _edition_with_degenerate_work(db, "Insight Into Emptiness", "Jampa Tegchok")
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": c["title"]}))
    db.commit()
    with app.test_client() as c:
        c.post(f"/works/detect/{eid}/apply")
        page = c.get("/works/detect/single").data
        assert b"\xe2\x86\xa9 Undo" in page                               # ↩ Undo button rendered
        token = work_undo.pending_undo(connect(app.config["DB_PATH"]), eid)
        assert token
        c.post("/works/detect/undo", data={"token": token})
    db = connect(app.config["DB_PATH"])
    assert cs.edition_author_ids(db, eid) == []                           # undone
    assert WD.get_detection(db, eid).get("applied") is not True
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE edition_id=?", (eid,)).fetchone()[0] == 1
