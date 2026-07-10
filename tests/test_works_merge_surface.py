"""The 'Merge & link works' tier — title_collision_groups + the /works/merge
review surface that feeds the existing work_merge engine."""
import json

import pytest

from catalogue.db_store import connect, init_db
from catalogue.services import promote
from catalogue.cli import work_dedup as WD
from catalogue.webui.web import create_app


def _book(db, *, title="Book"):
    eid = db.execute("INSERT INTO edition (title) VALUES (?)", (title,)).lastrowid
    hid = db.execute(
        "INSERT INTO holding (edition_id, form, text_status) VALUES (?, 'electronic', 'ocr_good')",
        (eid,)).lastrowid
    return hid, eid


def _single(holding_id, *, author, title):
    return {"holding_id": holding_id, "structure": "single_work", "source": "x",
            "book_authors": [author], "works": [{"title": title, "authors": [author],
            "kind": "work", "whole_book": True}]}


def _promote(db, hid, *, author, title):
    rid = db.execute("INSERT INTO review_queue (item_type, payload_json) "
                     "VALUES ('book_toc_pattern', ?)",
                     (json.dumps(_single(hid, author=author, title=title)),)).lastrowid
    return promote.promote_proposal(db, rid)


def test_title_collision_groups_finds_cross_author_only(tmp_path):
    db = init_db(tmp_path / "c.db")
    h1, _ = _book(db, title="B1")
    h2, _ = _book(db, title="B2")
    h3, _ = _book(db, title="B3")
    # Same title, DIFFERENT authors → collision (promote flags it merge_candidate).
    _promote(db, h1, author="Nāgārjuna", title="Root")
    r2 = _promote(db, h2, author="Candrakīrti", title="Root")
    assert r2.merge_candidate_work_ids != []
    # Same title + SAME author elsewhere → that's a tier-2 duplicate, NOT a collision.
    _promote(db, h3, author="Tsongkhapa", title="Lamrim")

    cols = WD.title_collision_groups(db)
    assert len(cols) == 1
    assert cols[0]["fold_key"] == "root"
    assert len(cols[0]["members"]) == 2


@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    # Two editions, same title + author → one work with two editions is the GOAL;
    # to get a duplicate group to merge we promote two *different*-author same-title
    # works (a collision), plus a clean shared one.
    h1, _ = _book(db, title="B1")
    h2, _ = _book(db, title="B2")
    _promote(db, h1, author="Nāgārjuna", title="MMK")
    _promote(db, h2, author="Candrakīrti", title="MMK")   # collision → 2 works
    db.commit()
    with app.test_client() as c:
        yield c, app


def test_works_merge_page_lists_collisions(web):
    c, _ = web
    page = c.get("/works/merge")
    assert page.status_code == 200
    assert b"Merge &amp; link works" in page.data
    assert b"Title collision" in page.data
    assert b"MMK" in page.data


def test_works_merge_post_folds_duplicate(web):
    c, app = web
    db = connect(app.config["DB_PATH"])
    wids = sorted(r[0] for r in db.execute("SELECT id FROM work").fetchall())
    assert len(wids) == 2
    winner, dup = wids[0], wids[1]
    c.post("/works/merge", data={"dup": dup, "into": winner})
    db = connect(app.config["DB_PATH"])
    remaining = [r[0] for r in db.execute("SELECT id FROM work").fetchall()]
    assert remaining == [winner]                       # duplicate folded in
    # both editions now hang off the surviving work
    assert db.execute("SELECT COUNT(*) FROM edition_work WHERE work_id=?",
                      (winner,)).fetchone()[0] == 2
