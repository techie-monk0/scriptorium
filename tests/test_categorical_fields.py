"""The scalar controlled-vocab field module (catalogue.contracts.fields) and its wiring:
the genre field end-to-end, the shared registry that also backs tenet_system/tradition/
work_type, out-of-vocab rejection on both write paths, and superset (taxonomy) queries.

Uses the test-kit fixtures (`cat_acc`, `cat_db`): a SYSTEM-bound Access over a throwaway DB.
"""
from __future__ import annotations

import pytest

from catalogue.contracts import (
    IntegrityViolation, fields_for, get_field, query_closure, subtree, validate_categorical,
)
from catalogue.contracts.fields import GENRE_VALUES, TENET_VOCAB
from catalogue.db_store import connect, init_db
from catalogue.webui.web import create_app


# ── the registry / pure helpers ───────────────────────────────────────────────
def test_genre_is_a_flat_registered_field():
    f = get_field("work", "genre")
    assert f is not None and f.strict and f.taxonomy is None
    assert f.values == GENRE_VALUES == ("Argumentative", "Doxography", "Monograph")


def test_all_four_field_families_share_the_registry():
    names = {(f.entity, f.name) for f in fields_for("work")}
    assert {("work", "genre"), ("work", "tenet_system"),
            ("work", "tradition"), ("work", "work_type")} <= names
    # tenet_system is declared on person too (same vocab, one source).
    assert get_field("person", "tenet_system").values == TENET_VOCAB


def test_validate_rejects_out_of_vocab_and_allows_clearing():
    assert validate_categorical("work", {"genre": "Monograph"}) == ()
    assert validate_categorical("work", {"genre": "Novel"})          # non-empty error tuple
    assert validate_categorical("work", {"genre": None}) == ()       # clearing is fine
    assert validate_categorical("work", {"genre": ""}) == ()
    # non-strict fields (tradition) accept free text — no error.
    assert validate_categorical("work", {"tradition": "Some New Lineage"}) == ()


def test_tenet_superset_expands_to_its_subtree():
    f = get_field("work", "tenet_system")
    assert set(subtree(f, "Madhyamaka")) == {
        "Madhyamaka", "Svātantrika-Madhyamaka", "Prāsaṅgika-Madhyamaka",
        "Sautrāntika-Svātantrika-Madhyamaka", "Yogācāra-Svātantrika-Madhyamaka"}
    # a leaf expands to just itself; a flat field (genre) is plain equality.
    assert subtree(f, "Prāsaṅgika-Madhyamaka") == ("Prāsaṅgika-Madhyamaka",)
    assert query_closure("work", "genre", "Doxography") == ("Doxography",)


# ── migration ──────────────────────────────────────────────────────────────────
def test_migration_adds_genre_column(tmp_path):
    conn = init_db(tmp_path / "t.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(work)")}
    assert "genre" in cols
    conn.close()


# ── access-API round trip + the two write paths ────────────────────────────────
def _new_work(acc):
    return acc.works.writes.create({}).target.id


def test_genre_round_trips_through_the_store(cat_acc):
    wid = _new_work(cat_acc)
    cat_acc.works.writes.set_scalars(wid, {"genre": "Doxography"})
    cat_acc.rw.commit()
    assert cat_acc.works.reads.get(wid).genre == "Doxography"


def test_store_rejects_out_of_vocab_genre(cat_acc):
    wid = _new_work(cat_acc)
    with pytest.raises(IntegrityViolation):
        cat_acc.works.writes.set_scalars(wid, {"genre": "Novel"})


def test_superset_query_matches_descendant_leaves(cat_acc):
    a, b, c = _new_work(cat_acc), _new_work(cat_acc), _new_work(cat_acc)
    cat_acc.works.writes.set_scalars(a, {"tenet_system": "Prāsaṅgika-Madhyamaka"})
    cat_acc.works.writes.set_scalars(b, {"tenet_system": "Yogācāra-Svātantrika-Madhyamaka"})
    cat_acc.works.writes.set_scalars(c, {"tenet_system": "Cittamātra"})
    cat_acc.rw.commit()
    hits = set(cat_acc.works.writes._s.ids_by_categorical("tenet_system", "Madhyamaka"))
    assert a in hits and b in hits and c not in hits   # superset matches both Madhyamaka leaves


# ── web UI (the surface the operator actually uses) ─────────────────────────────
@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "web.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app


def test_forms_show_genre_select(client):
    c, _ = client
    body = c.get("/works").data
    assert b'name="genre"' in body
    assert b"Argumentative" in body and b"Doxography" in body


def test_creating_a_work_saves_genre(client):
    c, app = client
    r = c.post("/works/new", data={"seed_alias": "Tarkajvālā", "genre": "Argumentative"})
    assert r.status_code == 302
    conn = connect(app.config["DB_PATH"])
    row = conn.execute("SELECT genre FROM work ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row[0] == "Argumentative"


# ── /review edit flow (cards are loaded inline in /review via their card routes) ─
def _last_id(app, table):
    conn = connect(app.config["DB_PATH"])
    rid = conn.execute(f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()
    return rid


def _col(app, table, col, rid):
    conn = connect(app.config["DB_PATH"])
    v = conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (rid,)).fetchone()
    conn.close()
    return v


def test_review_work_card_sets_genre_tenet_tradition(client):
    """A work's controlled-vocab fields set via the work card (the /review inline editor),
    which posts to /work/<id>/edit. work_type is set by the root/commentary checkboxes."""
    c, app = client
    c.post("/works/new", data={"seed_alias": "Madhyamakāvatāra"})
    wid = _last_id(app, "work")
    r = c.post(f"/work/{wid}/edit", data={
        "genre": "Doxography", "tenet_system": "Madhyamaka", "tradition": "Gelug"})
    assert r.status_code == 302
    assert _col(app, "work", "genre", wid) == ("Doxography",)
    assert _col(app, "work", "tenet_system", wid) == ("Madhyamaka",)
    assert _col(app, "work", "tradition", wid) == ("Gelug",)
    # the card (what /review injects) renders the controls with the saved selection
    card = c.get(f"/work/{wid}/card").data
    assert b'name="genre"' in card and b'name="tenet_system"' in card and b'name="tradition"' in card
    assert b"selected>Doxography" in card or b"Doxography</option>" in card


def test_review_person_card_sets_tenet_and_tradition(client):
    """An author's tenet_system + tradition set via the person card (posts /person/<id>/edit)."""
    c, app = client
    c.post("/people/new", data={"primary_name": "Tsongkhapa"})
    pid = _last_id(app, "person")
    r = c.post(f"/person/{pid}/edit", data={
        "primary_name": "Tsongkhapa", "tenet_system": "Prāsaṅgika-Madhyamaka", "tradition": "Gelug"})
    assert r.status_code == 302
    assert _col(app, "person", "tenet_system", pid) == ("Prāsaṅgika-Madhyamaka",)
    assert _col(app, "person", "tradition", pid) == ("Gelug",)
    card = c.get(f"/person/{pid}/card").data
    assert b'name="tenet_system"' in card and b'name="tradition"' in card


def test_review_edition_card_sets_tradition(client):
    """An edition's tradition set via the edition card (posts /edition/<id>/edit)."""
    c, app = client
    conn = connect(app.config["DB_PATH"])
    conn.execute("INSERT INTO edition (title) VALUES ('A Collected Volume')")
    conn.commit()
    conn.close()
    eid = _last_id(app, "edition")
    r = c.post(f"/edition/{eid}/edit", data={"title": "A Collected Volume", "tradition": "Rimé"})
    assert r.status_code == 302
    assert _col(app, "edition", "tradition", eid) == ("Rimé",)
    card = c.get(f"/edition/{eid}/card").data
    assert b'name="tradition"' in card
