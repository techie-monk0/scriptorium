"""Every work/edition must carry a subject.

Enforcement has three layers, all exercised here:
  1. creation auto-tags the `Uncategorized` placeholder when no real subject is given,
     so nothing is ever subject-less (domain `ensure_categorized` + each create path);
  2. the review gate refuses an 'ok' (reviewed) verdict while `Uncategorized` lingers;
  3. `Uncategorized` is a predefined subject — it can't be renamed, merged away, or deleted.

Black-box where it matters (HTTP routes for the popups), domain-level for the invariants.
"""
import pytest

from catalogue.db_store import connect, init_db
from catalogue.services import subjects as S
from catalogue.services import work_identity, work_review, catalogue_review
from catalogue.webui.web import create_app


def _edition(db, title="Ed"):
    return db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid


def _names(db, kind, pid):
    return [n for _, n in S.subjects_for(db, kind, pid)]


# ── domain: the never-subject-less invariant ──────────────────────────────────

def test_ensure_categorized_tags_only_when_subjectless(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    assert S.ensure_categorized(db, "edition", eid) is True        # was bare → tagged
    assert _names(db, "edition", eid) == [S.UNCATEGORIZED]
    assert S.ensure_categorized(db, "edition", eid) is False       # idempotent, no dup
    assert _names(db, "edition", eid) == [S.UNCATEGORIZED]
    # A record that already has a real subject is left untouched.
    e2 = _edition(db)
    S.add_subject(db, "edition", e2, "Emptiness")
    assert S.ensure_categorized(db, "edition", e2) is False
    assert _names(db, "edition", e2) == ["Emptiness"]


def test_create_work_without_subject_is_uncategorized(tmp_path):
    db = init_db(tmp_path / "c.db")
    wid, _, _ = work_identity.create_work(db, english_title="A bare work")
    assert _names(db, "work", wid) == [S.UNCATEGORIZED]
    # …but a supplied subject means no placeholder.
    wid2, _, _ = work_identity.create_work(
        db, english_title="A tagged work", subjects=["Madhyamaka"])
    assert _names(db, "work", wid2) == ["Madhyamaka"]


def test_clear_uncategorized_drops_placeholder_once_real_subject_added(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)
    S.add_subject(db, "edition", eid, "Tantra")
    S.clear_uncategorized(db, "edition", eid)
    assert _names(db, "edition", eid) == ["Tantra"]
    # never strips the LAST tag: an Uncategorized-only record keeps it.
    e2 = _edition(db)
    S.ensure_categorized(db, "edition", e2)
    S.clear_uncategorized(db, "edition", e2)
    assert _names(db, "edition", e2) == [S.UNCATEGORIZED]


def test_add_subject_alone_lifts_uncategorized(tmp_path):
    """The invariant is enforced centrally inside add_subject: attaching any real
    subject drops the placeholder with NO separate clear_uncategorized call, so every
    assignment path keeps a book from being both categorized and Uncategorized."""
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)
    assert _names(db, "edition", eid) == [S.UNCATEGORIZED]
    S.add_subject(db, "edition", eid, "Pramāṇa")               # no explicit clear here
    assert _names(db, "edition", eid) == ["Pramāṇa"]           # placeholder auto-lifted
    # adding the placeholder ITSELF stays exempt, so ensure_categorized still works
    e2 = _edition(db)
    S.add_subject(db, "edition", e2, S.UNCATEGORIZED)
    assert _names(db, "edition", e2) == [S.UNCATEGORIZED]
    # same guarantee for works
    wid, _, _ = work_identity.create_work(db, english_title="W")   # → Uncategorized
    S.add_subject(db, "work", wid, "Abhidharma")
    assert _names(db, "work", wid) == ["Abhidharma"]


# ── domain: the review gate ────────────────────────────────────────────────────

def test_work_cannot_be_marked_reviewed_while_uncategorized(tmp_path):
    db = init_db(tmp_path / "c.db")
    wid, _, _ = work_identity.create_work(db, english_title="W")   # → Uncategorized
    with pytest.raises(S.UncategorizedError):
        work_review.set_review(db, wid, "ok")
    assert db.execute("SELECT review_status FROM work WHERE id=?", (wid,)).fetchone()[0] is None
    # 'needs_fix' is NOT blocked — only the terminal 'ok'/reviewed verdict.
    work_review.set_review(db, wid, "needs_fix")
    # Give it a real subject, drop the placeholder, now 'ok' is allowed.
    S.add_subject(db, "work", wid, "Logic")
    S.clear_uncategorized(db, "work", wid)
    work_review.set_review(db, wid, "ok")
    assert db.execute("SELECT review_status FROM work WHERE id=?", (wid,)).fetchone()[0] == "ok"


def test_edition_cannot_be_marked_reviewed_while_uncategorized(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)
    with pytest.raises(S.UncategorizedError):
        catalogue_review.set_review(db, eid, status="ok")
    assert catalogue_review.get_review(db, eid).get("status") in (None, "")
    S.add_subject(db, "edition", eid, "Lam Rim")
    S.clear_uncategorized(db, "edition", eid)
    catalogue_review.set_review(db, eid, status="ok")
    assert catalogue_review.get_review(db, eid)["status"] == "ok"


# ── domain: predefined subject is protected ────────────────────────────────────

def test_uncategorized_cannot_be_renamed_merged_or_deleted(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)
    sid = S.get_or_create_subject(db, S.UNCATEGORIZED)
    other = S.get_or_create_subject(db, "Real Subject")
    with pytest.raises(S.ProtectedSubjectError):
        S.rename_subject(db, sid, "Renamed")
    with pytest.raises(S.ProtectedSubjectError):
        S.merge_subjects(db, sid, other)
    with pytest.raises(S.ProtectedSubjectError):
        S.delete_subject(db, sid)
    # still present and still tagging the edition
    assert S.get_or_create_subject(db, S.UNCATEGORIZED) == sid
    assert _names(db, "edition", eid) == [S.UNCATEGORIZED]


# ── web: popups (flash banner + inline card error) ────────────────────────────

@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    with app.test_client() as c:
        yield c, app


def test_new_work_without_subject_flashes_uncategorized_notice(web):
    c, app = web
    r = c.post("/works/new", data={"seed_alias": "Bare", "scheme": "english"},
               follow_redirects=True)
    assert r.status_code == 200
    assert b"Uncategorized" in r.data                       # popup notice rendered
    db = connect(app.config["DB_PATH"])
    wid = db.execute("SELECT MAX(id) FROM work").fetchone()[0]
    assert _names(db, "work", wid) == [S.UNCATEGORIZED]


def test_new_work_with_subject_has_no_placeholder(web):
    c, app = web
    c.post("/works/new", data={"seed_alias": "Tagged", "scheme": "english",
                               "subjects": "Emptiness, Madhyamaka"})
    db = connect(app.config["DB_PATH"])
    wid = db.execute("SELECT MAX(id) FROM work").fetchone()[0]
    assert set(_names(db, "work", wid)) == {"Emptiness", "Madhyamaka"}


def test_work_review_route_blocks_uncategorized(web):
    c, app = web
    db = connect(app.config["DB_PATH"])
    wid, _, _ = work_identity.create_work(db, english_title="W")   # Uncategorized
    db.commit()
    r = c.post(f"/work/{wid}/review", data={"status": "ok"}, follow_redirects=True)
    assert b"Uncategorized" in r.data                       # blocked, popup shown
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT review_status FROM work WHERE id=?", (wid,)).fetchone()[0] is None


def test_work_review_route_blocks_uncategorized_json(web):
    c, app = web
    db = connect(app.config["DB_PATH"])
    wid, _, _ = work_identity.create_work(db, english_title="W")
    db.commit()
    r = c.post(f"/work/{wid}/review", json={"status": "ok"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False and "Uncategorized" in r.get_json()["error"]


def test_edition_review_card_blocks_uncategorized_inline(web):
    c, app = web
    db = connect(app.config["DB_PATH"])
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)
    db.commit()
    r = c.post(f"/edition/{eid}/review-card", data={"status": "ok"})
    assert r.status_code == 400
    assert b"Uncategorized" in r.data                       # inline error in the card fragment
    db = connect(app.config["DB_PATH"])
    assert catalogue_review.get_review(db, eid).get("status") in (None, "")


def test_adding_real_subject_via_route_clears_placeholder(web):
    c, app = web
    db = connect(app.config["DB_PATH"])
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)
    db.commit()
    c.post(f"/subjects/edition/{eid}/add", data={"name": "Emptiness"})
    db = connect(app.config["DB_PATH"])
    assert _names(db, "edition", eid) == ["Emptiness"]      # placeholder lifted


def test_ingested_upload_is_born_categorized(tmp_path):
    """An ingestion path (upload register) creates a bare edition — it must still come
    out tagged Uncategorized so it never sits subject-less awaiting review."""
    from catalogue.services import library
    db = init_db(tmp_path / "c.db")
    src = tmp_path / "book.pdf"
    src.write_bytes(b"%PDF-1.4 dummy")
    res = library.ingest_upload(db, src, dest_dir=tmp_path / "store", process=False)
    eid = res["edition_id"]
    assert eid is not None
    assert _names(db, "edition", eid) == [S.UNCATEGORIZED]


def test_subject_delete_route_protects_uncategorized(web):
    c, app = web
    db = connect(app.config["DB_PATH"])
    eid = _edition(db)
    S.ensure_categorized(db, "edition", eid)
    db.commit()
    sid = S.get_or_create_subject(connect(app.config["DB_PATH"]), S.UNCATEGORIZED)
    r = c.post(f"/subject/{sid}/delete", follow_redirects=True)
    assert b"predefined subject" in r.data                  # popup, refused
    db = connect(app.config["DB_PATH"])
    assert S.get_or_create_subject(db, S.UNCATEGORIZED) == sid   # still there
