"""Step-3a regression tests — review queue, staging resolution, edition
link editing, work + alias management.

Pins the invariants from §3, §4.1, §4.2, §5, §7.3, §12.4 / §12.7.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import connect, fold_key, init_db
from catalogue.webui.web import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "step3a.db")
    app.testing = True
    return app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def db(app):
    """Direct DB handle for setup/assertions outside request lifecycle."""
    conn = connect(app.config["DB_PATH"])
    yield conn
    conn.close()


# ── Review queue ──────────────────────────────────────────────────────────
def _enqueue(db, item_type: str, payload: dict) -> int:
    cur = db.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES (?, ?)",
        (item_type, json.dumps(payload)),
    )
    db.commit()
    return cur.lastrowid


def test_review_list_filters_by_type_and_status(client, db):
    iid_ocr = _enqueue(db, "low_quality_ocr", {"score": 0.2})
    iid_am  = _enqueue(db, "alias_merge", {"a": "x"})

    # Item rows are linked by id — using the link is precise enough to
    # distinguish a row hit from the (always-present) dropdown <option>.
    def row_links(html: bytes) -> set[bytes]:
        import re
        return set(re.findall(rb'href="/review-queue/(\d+)"', html))

    r = client.get("/review-queue")
    assert r.status_code == 200
    assert row_links(r.data) == {str(iid_ocr).encode(), str(iid_am).encode()}

    r = client.get("/review-queue?type=alias_merge")
    assert row_links(r.data) == {str(iid_am).encode()}

    r = client.get("/review-queue?status=resolved")
    assert row_links(r.data) == set()  # both still pending


def test_review_detail_renders_payload(client, db):
    iid = _enqueue(db, "fuzzy_match", {"left": "X", "right": "Y", "score": 0.81})
    r = client.get(f"/review-queue/{iid}")
    assert r.status_code == 200
    assert b"fuzzy_match" in r.data
    assert b"0.81" in r.data


def test_resolve_marks_resolved_and_sets_timestamp(client, db):
    iid = _enqueue(db, "alias_merge", {})
    r = client.post(f"/review-queue/{iid}/resolve", data={"action": "resolve"})
    assert r.status_code in (302, 303)
    row = db.execute(
        "SELECT status, resolved_at FROM review_queue WHERE id=?", (iid,)
    ).fetchone()
    assert row[0] == "resolved"
    assert row[1] is not None


# ── §4.8d: OCR override flips the holding's text_status ───────────────────
def test_ocr_override_flips_holding_status_to_good(client, db):
    """End-to-end: a Step-2 low_quality_ocr item, when overridden in the
    review UI, must update the linked holding's text_status from ocr_poor
    to ocr_good (user's explicit override)."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    db.execute(
        "INSERT INTO holding (edition_id, form, file_hash, text_status, "
        "ocr_quality_score) VALUES (1, 'electronic', 'h123', 'ocr_poor', 0.35)"
    )
    db.commit()
    iid = _enqueue(db, "low_quality_ocr", {"file_hash": "h123", "score": 0.35})

    r = client.post(f"/review-queue/{iid}/resolve", data={"action": "ocr_override"})
    assert r.status_code in (302, 303)

    (status,) = db.execute(
        "SELECT text_status FROM holding WHERE file_hash='h123'"
    ).fetchone()
    assert status == "ocr_good"
    (rs,) = db.execute(
        "SELECT status FROM review_queue WHERE id=?", (iid,)
    ).fetchone()
    assert rs == "resolved"


def test_double_resolve_is_idempotent(client, db):
    iid = _enqueue(db, "alias_merge", {})
    client.post(f"/review-queue/{iid}/resolve", data={"action": "resolve"})
    first = db.execute(
        "SELECT resolved_at FROM review_queue WHERE id=?", (iid,)
    ).fetchone()[0]

    # Second resolve must not error and must not bump resolved_at.
    r = client.post(f"/review-queue/{iid}/resolve", data={"action": "resolve"})
    assert r.status_code in (302, 303)
    second = db.execute(
        "SELECT resolved_at FROM review_queue WHERE id=?", (iid,)
    ).fetchone()[0]
    assert first == second


def test_review_detail_404s_for_missing_item(client):
    assert client.get("/review-queue/99999").status_code == 404


# ── Staging resolution (§7.3) ─────────────────────────────────────────────
def test_staging_list_shows_only_pending(client, db):
    db.execute(
        "INSERT INTO capture_staging (form, raw_isbn, free_text_note, status) "
        "VALUES ('physical', '978-A', 'shelf 1', 'raw')"
    )
    db.execute(
        "INSERT INTO capture_staging (form, raw_isbn, free_text_note, status) "
        "VALUES ('physical', '978-B', 'shelf 2', 'resolved')"
    )
    db.commit()
    r = client.get("/staging")
    assert b"978-A" in r.data
    assert b"978-B" not in r.data


def test_resolve_staging_to_new_edition_creates_both(client, db):
    db.execute(
        "INSERT INTO capture_staging (form, raw_isbn, free_text_note) "
        "VALUES ('physical', '978-NEW', 'shelf 3')"
    )
    db.commit()
    (sid,) = db.execute("SELECT id FROM capture_staging").fetchone()

    r = client.post(f"/staging/{sid}/resolve",
                    data={"resolution": "new", "title": "Brand New Book", "isbn": "978-NEW"})
    assert r.status_code in (302, 303)

    (n_e, n_h) = db.execute(
        "SELECT (SELECT count(*) FROM edition), (SELECT count(*) FROM holding)"
    ).fetchone()
    assert n_e == 1 and n_h == 1

    (form, shelf) = db.execute(
        "SELECT form, shelf_location FROM holding"
    ).fetchone()
    assert form == "physical"
    assert shelf == "shelf 3"

    (status,) = db.execute(
        "SELECT status FROM capture_staging WHERE id=?", (sid,)
    ).fetchone()
    assert status == "resolved"


def test_resolve_staging_to_existing_edition_does_not_duplicate(client, db):
    """§7.3 (revised): a scan that MATCHES an existing edition is confirmed as a
    duplicate and cleared from the inbox WITHOUT creating a second edition — and,
    per the 'add nothing on match' rule, without adding a holding either (the book
    is already catalogued)."""
    db.execute("INSERT INTO edition (id, title, isbn) VALUES (1, 'Existing', '9780205309023')")
    db.execute(
        "INSERT INTO capture_staging (form, raw_isbn, free_text_note) "
        "VALUES ('physical', '9780205309023', 'shelf 9')"
    )
    db.commit()
    (sid,) = db.execute("SELECT id FROM capture_staging").fetchone()

    r = client.post(f"/staging/{sid}/resolve", data={"resolution": "match:1"})
    assert r.status_code in (302, 303)

    (n_e,) = db.execute("SELECT count(*) FROM edition").fetchone()
    (n_h,) = db.execute("SELECT count(*) FROM holding").fetchone()
    (status,) = db.execute("SELECT status FROM capture_staging WHERE id=?", (sid,)).fetchone()
    assert n_e == 1                 # no second edition
    assert n_h == 0                 # nothing added — already in the catalogue
    assert status == "resolved"     # cleared from the inbox


def test_double_resolve_staging_is_idempotent(client, db):
    db.execute(
        "INSERT INTO capture_staging (form, free_text_note) "
        "VALUES ('physical', 'shelf 7')"
    )
    db.commit()
    (sid,) = db.execute("SELECT id FROM capture_staging").fetchone()
    client.post(f"/staging/{sid}/resolve", data={"resolution": "new", "title": "Once"})
    client.post(f"/staging/{sid}/resolve", data={"resolution": "new", "title": "Twice"})

    # Only one holding should exist — the second resolve sees a resolved row and no-ops.
    (n_h,) = db.execute("SELECT count(*) FROM holding").fetchone()
    assert n_h == 1


# ── Edition view + edit ──────────────────────────────────────────────────
def test_edition_detail_renders_and_edit_persists(client, db):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Draft')")
    db.commit()
    assert client.get("/edition/1").status_code == 200

    # ISBN entered WITH dashes must be canonicalized to digits-only on write, so
    # it matches the digits-only form the capture/match layer compares against.
    client.post("/edition/1/edit", data={
        "title": "Final Title", "publisher": "Shambhala",
        "year": "2006", "isbn": "978-0-415-50800-1", "language": "en", "notes": "",
    })
    row = db.execute(
        "SELECT title, publisher, year, isbn, language FROM edition WHERE id=1"
    ).fetchone()
    assert row == ("Final Title", "Shambhala", 2006, "9780415508001", "en")


def test_edition_edit_isbn_with_and_without_dashes_canonicalize_identically(client, db):
    """Same ISBN entered with or without dashes/spaces must persist identically."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'A')")
    db.execute("INSERT INTO edition (id, title) VALUES (2, 'B')")
    db.commit()
    client.post("/edition/1/edit", data={"title": "A", "isbn": "978-1-61429-541-9"})
    client.post("/edition/2/edit", data={"title": "B", "isbn": "9781614295419"})
    a = db.execute("SELECT isbn FROM edition WHERE id=1").fetchone()[0]
    b = db.execute("SELECT isbn FROM edition WHERE id=2").fetchone()[0]
    assert a == b == "9781614295419"


def test_edition_work_link_add_and_remove(client, db):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'Volume')")
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.execute("INSERT INTO work (id) VALUES (2)")
    db.commit()

    client.post("/edition/1/work/add",
                data={"work_id": "1", "sequence": "1"})
    client.post("/edition/1/work/add",
                data={"work_id": "2", "sequence": "2",
                      "section_locator": "ch.4–9"})

    rows = db.execute(
        "SELECT work_id, sequence, section_locator FROM edition_work "
        "WHERE edition_id=1 ORDER BY sequence"
    ).fetchall()
    assert rows == [(1, 1, None), (2, 2, "ch.4–9")]

    client.post("/edition/1/work/remove",
                data={"work_id": "1", "sequence": "1"})
    rows = db.execute(
        "SELECT work_id FROM edition_work WHERE edition_id=1"
    ).fetchall()
    assert rows == [(2,)]


def test_duplicate_edition_work_link_is_a_409_not_500(client, db):
    """Composite PK (edition, work, sequence) prevents accidental
    duplicates; the UI must surface 409 rather than crashing."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'V')")
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.commit()
    client.post("/edition/1/work/add", data={"work_id": "1", "sequence": "1"})
    r = client.post("/edition/1/work/add", data={"work_id": "1", "sequence": "1"})
    assert r.status_code == 409


# ── §4.1 / §4.2 — Work aliases: normalized_key always equals fold_key ─────
def test_add_alias_auto_computes_normalized_key(client, db):
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.commit()
    client.post("/work/1/alias/add",
                data={"text": "Bodhicaryāvatāra", "scheme": "iast"})
    (text, scheme, key) = db.execute(
        "SELECT text, scheme, normalized_key FROM work_alias WHERE work_id=1"
    ).fetchone()
    assert text == "Bodhicaryāvatāra"     # stored text keeps diacritics
    assert scheme == "iast"
    assert key == fold_key("Bodhicaryāvatāra")  # invariant tested directly


def test_multiple_alias_schemes_collapse_to_same_normalized_key(client, db):
    """§4.2 worked example: IAST `Śāntideva`, phonetic `Shantideva`, plain
    `Santideva` all must produce the same normalized_key."""
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.commit()
    for text, scheme in [("Śāntideva", "iast"),
                         ("Shantideva", "phonetic"),
                         ("Santideva", "english")]:
        client.post("/work/1/alias/add",
                    data={"text": text, "scheme": scheme})
    keys = [r[0] for r in db.execute(
        "SELECT normalized_key FROM work_alias WHERE work_id=1"
    ).fetchall()]
    assert keys == ["santideva", "santideva", "santideva"]


def test_alias_with_empty_text_is_rejected(client, db):
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.commit()
    r = client.post("/work/1/alias/add", data={"text": "  ", "scheme": "iast"})
    assert r.status_code == 400
    (n,) = db.execute("SELECT count(*) FROM work_alias").fetchone()
    assert n == 0


def test_alias_with_unknown_scheme_is_rejected_by_fk(client, db):
    """§12.4: the open vocabulary is still a guardrail — typos in scheme
    code get rejected by the FK, not silently stored."""
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.commit()
    r = client.post("/work/1/alias/add",
                    data={"text": "ok", "scheme": "no_such_scheme"})
    assert r.status_code == 400


def test_alias_delete(client, db):
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.commit()
    client.post("/work/1/alias/add", data={"text": "x", "scheme": "english"})
    (aid,) = db.execute("SELECT id FROM work_alias").fetchone()
    client.post(f"/work/1/alias/{aid}/delete")
    (n,) = db.execute("SELECT count(*) FROM work_alias").fetchone()
    assert n == 0


# ── People + alias parallel ───────────────────────────────────────────────
def test_new_person_seeds_alias_with_fold_key(client, db):
    client.post("/people/new",
                data={"primary_name": "Śāntideva", "scheme": "iast"})
    (name,) = db.execute("SELECT primary_name FROM person").fetchone()
    (text, key) = db.execute(
        "SELECT text, normalized_key FROM person_alias"
    ).fetchone()
    assert name == "Śāntideva"
    assert text == "Śāntideva"
    assert key == fold_key("Śāntideva") == "santideva"


# ── Smoke: all step-3a routes render ──────────────────────────────────────
def test_all_routes_render(client, db):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.execute(
        "INSERT INTO capture_staging (form, free_text_note) "
        "VALUES ('physical', 'n')"
    )
    iid = _enqueue(db, "alias_merge", {"k": 1})

    for path in ("/", "/staging", f"/staging/1",
                 "/review-queue", f"/review-queue/{iid}",
                 "/works", "/work/1",
                 "/people", "/edition/1"):
        assert client.get(path).status_code == 200, path
