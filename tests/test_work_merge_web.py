"""Black-box HTTP tests for the work-merge picker (/work/<wid>/merge*)."""
from __future__ import annotations

import pytest

from catalogue.db_store import add_alias, connect, init_db
from catalogue.webui.web import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    with app.test_client() as c:
        yield c, app


def _seed_dupe(app):
    """Two works with the SAME fold-key title and same author, each in its own
    edition — a duplicate pair the picker should offer to merge."""
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Kamalaśīla')").lastrowid
    add_alias(db, "person", pid, "Kamalaśīla", "english")
    ids = []
    for tag in ("A", "B"):
        wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
        add_alias(db, "work", wid, "Stages of Meditation", "english")
        db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (?,?,'author')",
                   (wid, pid))
        eid = db.execute("INSERT INTO edition (title) VALUES (?)",
                         (f"Stages of Meditation ({tag})",)).lastrowid
        db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)",
                   (eid, wid))
        ids.append((wid, eid))
    db.commit(); db.close()
    return ids


def test_candidates_lists_the_duplicate(env):
    c, app = env
    (w1, _), (w2, _) = _seed_dupe(app)
    r = c.get(f"/work/{w1}/merge/candidates")
    assert r.status_code == 200
    cands = r.get_json()
    assert {x["work_id"] for x in cands} == {w2}
    assert cands[0]["title"] == "Stages of Meditation"
    assert cands[0]["authors"] == ["Kamalaśīla"]


def test_plan_preview(env):
    c, app = env
    (w1, _), (w2, e2) = _seed_dupe(app)
    plan = c.get(f"/work/{w1}/merge?into={w2}").get_json()
    assert "error" not in plan
    assert plan["dup"]["id"] == w1 and plan["canon"]["id"] == w2


def test_apply_merge_folds_and_repoints(env):
    c, app = env
    (w1, e1), (w2, e2) = _seed_dupe(app)
    res = c.post(f"/work/{w1}/merge", json={"into": w2}).get_json()
    assert res["merged"] == w1 and res["into"] == w2
    db = connect(app.config["DB_PATH"])
    # loser gone; both editions now point at the winner
    assert db.execute("SELECT 1 FROM work WHERE id=?", (w1,)).fetchone() is None
    eids = {r[0] for r in db.execute(
        "SELECT edition_id FROM edition_work WHERE work_id=?", (w2,)).fetchall()}
    assert eids == {e1, e2}
    db.close()


def test_self_merge_returns_error(env):
    c, app = env
    (w1, _), _ = _seed_dupe(app)
    plan = c.get(f"/work/{w1}/merge?into={w1}").get_json()
    assert "error" in plan


def test_work_page_offers_merge_target_search(env):
    """The merge section has a picker to choose ANY target work, not only the
    auto-detected same-title candidates."""
    c, app = env
    (w1, _), _ = _seed_dupe(app)
    page = c.get(f"/work/{w1}").data.decode()
    assert "merge-target-btn" in page and "search for a work to merge into" in page
    assert "/works/search" in page                     # the picker searches all works/aliases


def test_merge_into_arbitrary_differently_titled_work(env):
    """The picker can target a work with a DIFFERENT title (not an auto-candidate); the
    same preview→merge route folds it. This is the case the user hit (no same-title dupe)."""
    c, app = env
    db = connect(app.config["DB_PATH"])
    w1 = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", w1, "Entering the Middle Way", "english")
    w2 = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    add_alias(db, "work", w2, "Madhyamakāvatāra", "iast")          # different title entirely
    e = db.execute("INSERT INTO edition (title) VALUES ('A Book')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?,?,1)", (e, w1))
    db.commit(); db.close()
    assert "error" not in c.get(f"/work/{w1}/merge?into={w2}").get_json()   # preview OK across titles
    res = c.post(f"/work/{w1}/merge", json={"into": w2}).get_json()
    assert res["merged"] == w1 and res["into"] == w2
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT 1 FROM work WHERE id=?", (w1,)).fetchone() is None     # folded away
    assert db.execute("SELECT edition_id FROM edition_work WHERE work_id=?", (w2,)).fetchone()[0] == e
    db.close()
