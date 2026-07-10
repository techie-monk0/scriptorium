"""Hierarchical subjects + Series/Collections — the shared subject_tree service,
the prefix-inclusive book filter, the curation tree, and the series namespace. and catalogue/domain/subject_tree.py. The hierarchy is
the '/' path in subject.name; series are a separate kind in the same table.
"""
import pytest

from catalogue.db_store import init_db
from catalogue.services import subjects as S
from catalogue.services import subject_tree as T
from catalogue.services import search as SEARCH
from catalogue.webui.web import create_app


def _edition(db, title="Ed", volume=None):
    return db.execute("INSERT INTO edition (title, volume) VALUES (?, ?)",
                      (title, volume)).lastrowid


def _work(db):
    return db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid


# ── ancestor materialization ──────────────────────────────────────────────────
def test_create_path_materializes_ancestors(tmp_path):
    db = init_db(tmp_path / "c.db")
    S.get_or_create_subject(db, "Buddhism/Tibetan/Dzogchen")
    names = {r[0] for r in db.execute("SELECT name FROM subject").fetchall()}
    assert names == {"Buddhism", "Buddhism/Tibetan", "Buddhism/Tibetan/Dzogchen"}


def test_materialize_ancestors_backfills_legacy(tmp_path):
    db = init_db(tmp_path / "c.db")
    # Simulate legacy rows inserted without going through get_or_create (no ancestors).
    db.execute("INSERT INTO subject (name, kind) VALUES ('A/B/C', 'topic')")
    db.execute("INSERT INTO subject (name, kind) VALUES ('A/B', 'topic')")
    created = T.materialize_ancestors(db)
    assert created == ["A"]
    assert {r[0] for r in db.execute("SELECT name FROM subject").fetchall()} == {"A", "A/B", "A/B/C"}


# ── descendant resolution + the one inheritance query ─────────────────────────
def test_editions_for_subject_is_prefix_inclusive(tmp_path):
    db = init_db(tmp_path / "c.db")
    e_top = _edition(db)                       # tagged directly on the parent
    e_sub = _edition(db)                       # tagged on a child
    S.add_subject(db, "edition", e_top, "Buddhism")
    S.add_subject(db, "edition", e_sub, "Buddhism/Emptiness")
    bud = db.execute("SELECT id FROM subject WHERE name='Buddhism'").fetchone()[0]
    emp = db.execute("SELECT id FROM subject WHERE name='Buddhism/Emptiness'").fetchone()[0]
    # Buddhism rolls up its own + the child's editions; the child is just itself.
    assert set(T.editions_for_subject(db, bud)) == {e_top, e_sub}
    assert set(T.editions_for_subject(db, bud, include_descendants=False)) == {e_top}
    assert set(T.editions_for_subject(db, emp)) == {e_sub}


def test_editions_for_subject_inherits_through_work(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    wid = _work(db)
    db.execute("INSERT INTO edition_work (edition_id, work_id) VALUES (?, ?)", (eid, wid))
    S.add_subject(db, "work", wid, "Buddhism/Tantra")     # subject on the WORK
    bud = db.execute("SELECT id FROM subject WHERE name='Buddhism'").fetchone()[0]
    # The edition inherits its work's subject, and Buddhism rolls up the child.
    assert set(T.editions_for_subject(db, bud)) == {eid}


# ── the forest (curation tree + API) ──────────────────────────────────────────
def test_subject_forest_rollup_and_shape(tmp_path):
    db = init_db(tmp_path / "c.db")
    e1, e2 = _edition(db), _edition(db)
    S.add_subject(db, "edition", e1, "Buddhism/Emptiness")
    S.add_subject(db, "edition", e2, "Buddhism/Tantra")
    forest = {n["name"]: n for n in T.subject_forest(db, kind="topic")}
    assert forest["Buddhism"]["depth"] == 0
    assert forest["Buddhism"]["has_children"] is True
    assert forest["Buddhism"]["n_books_total"] == 2     # rolled up
    assert forest["Buddhism"]["n_books_direct"] == 0    # container tags nothing itself
    emp = forest["Buddhism/Emptiness"]
    assert emp["depth"] == 1 and emp["leaf_label"] == "Emptiness"
    assert emp["parent_id"] == forest["Buddhism"]["id"]


def test_subject_page_children_crumbs_and_books(tmp_path):
    db = init_db(tmp_path / "c.db")
    e = _edition(db)
    S.add_subject(db, "edition", e, "Buddhism/Emptiness")
    bud = db.execute("SELECT id FROM subject WHERE name='Buddhism'").fetchone()[0]
    emp = db.execute("SELECT id FROM subject WHERE name='Buddhism/Emptiness'").fetchone()[0]
    page = T.subject_page(db, bud)
    assert page["subject"]["leaf_label"] == "Buddhism"
    assert page["n_books"] == 1                          # descendant-inclusive
    assert [c["id"] for c in page["children"]] == [emp]
    assert page["crumbs"] == []                          # top-level
    child = T.subject_page(db, emp)
    assert [c["name"] for c in child["crumbs"]] == ["Buddhism"]


# ── rename cascades down the tree ─────────────────────────────────────────────
def test_rename_parent_cascades_to_descendants(tmp_path):
    db = init_db(tmp_path / "c.db")
    S.get_or_create_subject(db, "Buddhism/Emptiness")
    S.get_or_create_subject(db, "Buddhism/Tantra")
    bud = db.execute("SELECT id FROM subject WHERE name='Buddhism'").fetchone()[0]
    S.rename_subject(db, bud, "Dharma")
    names = {r[0] for r in db.execute("SELECT name FROM subject").fetchall()}
    assert names == {"Dharma", "Dharma/Emptiness", "Dharma/Tantra"}


# ── prefix-inclusive book filter (search.find_books) ──────────────────────────
def test_find_books_subject_is_prefix_inclusive(tmp_path):
    db = init_db(tmp_path / "c.db")
    e1, e2 = _edition(db, "Top"), _edition(db, "Sub")
    S.add_subject(db, "edition", e1, "Buddhism")
    S.add_subject(db, "edition", e2, "Buddhism/Emptiness")
    eids = {r["edition_id"] for r in SEARCH.find_books(db, subject="Buddhism")}
    assert eids == {e1, e2}
    assert {r["edition_id"] for r in SEARCH.find_books(db, subject="Buddhism/Emptiness")} == {e2}
    assert SEARCH.find_books(db, subject="Nonexistent") == []


# ── kind isolation: a series tag is NOT a topic ───────────────────────────────
def test_series_tag_does_not_satisfy_topical_invariant(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    S.add_subject(db, "edition", eid, "Sounds of Freedom", subject_kind="series")
    # The book is in a series but has NO topic → still "uncategorized".
    assert S.ensure_categorized(db, "edition", eid) is True
    assert S.has_uncategorized(db, "edition", eid) is True
    # Adding a real topic lifts the placeholder; the series tag stays.
    S.add_subject(db, "edition", eid, "Buddhism/Dzogchen")
    assert S.has_uncategorized(db, "edition", eid) is False
    series = S.subjects_for(db, "edition", eid, subject_kind="series")
    assert [n for _, n in series] == ["Sounds of Freedom"]


def test_series_excluded_from_topical_forest(tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition(db)
    S.add_subject(db, "edition", eid, "My Series", subject_kind="series")
    S.add_subject(db, "edition", eid, "Buddhism/Emptiness")
    topics = {n["name"] for n in T.subject_forest(db, kind="topic")}
    series = {n["name"] for n in T.subject_forest(db, kind="series")}
    assert "My Series" not in topics and "My Series" in series
    assert "Buddhism/Emptiness" in topics


def test_series_page_orders_by_volume(tmp_path):
    db = init_db(tmp_path / "c.db")
    e2 = _edition(db, "Vol 2", volume="2")
    e10 = _edition(db, "Vol 10", volume="10")
    e1 = _edition(db, "Vol 1", volume="1")
    for e in (e2, e10, e1):
        S.add_subject(db, "edition", e, "SIF", subject_kind="series")
    sid = db.execute("SELECT id FROM subject WHERE name='SIF'").fetchone()[0]
    page = T.subject_page(db, sid)
    assert page["subject"]["kind"] == "series"
    assert [b["eid"] for b in page["books"]] == [e1, e2, e10]   # numeric volume order


# ── count_uncurated no longer punishes '/' ────────────────────────────────────
def test_count_uncurated_ignores_hierarchy(tmp_path):
    db = init_db(tmp_path / "c.db")
    e = _edition(db)
    S.add_subject(db, "edition", e, "Buddhism/Emptiness")   # a clean nested topic + its container
    assert S.count_uncurated(db) == 0                        # neither the leaf nor 'Buddhism' is a to-do
    db.execute("INSERT INTO subject (name, kind) VALUES ('Stray', 'topic')")  # true orphan
    assert S.count_uncurated(db) == 1


# ── web surfaces ──────────────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path):
    dbp = tmp_path / "c.db"
    db = init_db(dbp)
    e1, e2 = _edition(db, "Top book"), _edition(db, "Sub book")
    S.add_subject(db, "edition", e1, "Buddhism")
    S.add_subject(db, "edition", e2, "Buddhism/Emptiness")
    db.commit()
    db.close()
    app = create_app(str(dbp))
    return app.test_client()


def test_api_subjects_tree(client):
    j = client.get("/api/v1/subjects").get_json()
    assert j["kind"] == "topic"
    names = {n["name"] for n in j["tree"]}
    assert {"Buddhism", "Buddhism/Emptiness"} <= names


def test_api_subject_page_rolls_up(client):
    # /api/v1/subjects gives ids; find Buddhism.
    tree = client.get("/api/v1/subjects").get_json()["tree"]
    bud = next(n for n in tree if n["name"] == "Buddhism")
    j = client.get(f"/api/v1/subject/{bud['id']}").get_json()
    assert j["n_books"] == 2                       # rolls up the child
    assert len(j["children"]) == 1
    assert client.get("/api/v1/subject/999999").status_code == 404


def test_subject_browse_page_renders(client):
    tree = client.get("/api/v1/subjects").get_json()["tree"]
    bud = next(n for n in tree if n["name"] == "Buddhism")
    r = client.get(f"/subject/{bud['id']}")                # default = Netflix shelves
    assert r.status_code == 200
    assert b"shelf.js" in r.data and b"Emptiness" in r.data   # a sub-topic rail


def test_subject_shelves_one_rail_per_child(tmp_path):
    db = init_db(tmp_path / "c.db")
    e1, e2, e3 = _edition(db), _edition(db), _edition(db)
    S.add_subject(db, "edition", e1, "Buddhism/Emptiness")
    S.add_subject(db, "edition", e2, "Buddhism/Tantra")
    S.add_subject(db, "edition", e3, "Buddhism")          # direct on the parent → leftover shelf
    bud = db.execute("SELECT id FROM subject WHERE name='Buddhism'").fetchone()[0]
    sh = T.subject_shelves(db, bud)
    names = [s["name"] for s in sh["shelves"]]
    assert "Emptiness" in names and "Tantra" in names
    assert names[0] == "Buddhism"                          # leftover (direct) shelf first
    child = next(s for s in sh["shelves"] if s["name"] == "Emptiness")
    assert child["more_url"].startswith("/subject/")       # drills into the child


def test_subject_default_is_shelves_grid_is_optIn(client):
    tree = client.get("/api/v1/subjects").get_json()["tree"]
    bud = next(n for n in tree if n["name"] == "Buddhism")
    r = client.get(f"/subject/{bud['id']}")
    assert r.status_code == 200
    assert b"shelf.js" in r.data and b"subj-children" not in r.data   # netflix shelves, no chips
    rg = client.get(f"/subject/{bud['id']}?view=grid")
    assert b"subj-children" in rg.data and b"tile-grid" in rg.data    # kept variant


def test_review_hub_subjects_is_a_tree(client):
    r = client.get("/review/subjects")
    assert r.status_code == 200
    assert b'data-tree="1"' in r.data and b'data-depth="1"' in r.data
    assert b"Series / Collections" in r.data


def test_bulk_assign_route_handles_topic_and_series(client):
    # Same reusable endpoint, two namespaces (the BulkAssign component posts subject_kind).
    rs = client.post("/works/detect/bulk-subject",
                     json={"ids": [1, 2], "name": "My Series", "subject_kind": "series"})
    js = rs.get_json()
    assert rs.status_code == 200 and js["subject_kind"] == "series" and len(js["assigned"]) == 2
    series = {n["name"] for n in client.get("/api/v1/subjects?kind=series").get_json()["tree"]}
    assert "My Series" in series
    topics = {n["name"] for n in client.get("/api/v1/subjects?kind=topic").get_json()["tree"]}
    assert "My Series" not in topics                     # series stays out of the topic namespace
    rt = client.post("/works/detect/bulk-subject",
                     json={"ids": [1, 2], "name": "Buddhism/Tantra"})
    assert rt.get_json()["subject_kind"] == "topic" and len(rt.get_json()["assigned"]) == 2


def test_series_add_remove_via_chip_editor(client):
    # The edition with only "Buddhism" is e1; add it to a series, then remove.
    r = client.post("/subjects/edition/1/add", data={"name": "My Series", "subject_kind": "series"})
    assert r.status_code in (302, 303)
    j = client.get("/api/v1/subjects?kind=series").get_json()
    sid = next(n["id"] for n in j["tree"] if n["name"] == "My Series")
    card = client.get("/subjects/edition/1").get_data(as_text=True)
    assert f'/subject/{sid}"' in card                    # the series chip is shown
    client.post("/subjects/edition/1/remove", data={"subject_id": sid})
    card2 = client.get("/subjects/edition/1").get_data(as_text=True)
    # The chip (a link to the series page) is gone — the series row may still exist as a
    # datalist option, which is fine; what matters is the membership was detached.
    assert f'/subject/{sid}"' not in card2
    assert "Not in any series." in card2
