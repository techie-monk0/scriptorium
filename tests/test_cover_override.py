"""Operator cover override — pin a cover from a holding's page or an uploaded image
(catalogue/webui/routes/bookfiles cover endpoints + the persistent COVERS_PINNED store).

Pins: the override wins over auto art, survives in its own persistent dir, a page number
selects which page renders, reset reverts to auto, and non-images are rejected.
"""
from __future__ import annotations

import io
import os

import pytest

from catalogue.db_store import connect
from catalogue.webui.web import create_app

# A minimal valid PNG (1x1) for the upload path.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100" "05fe02fea70000000049454e44ae426082")


@pytest.fixture
def app(tmp_path):
    a = create_app(tmp_path / "c.db", ingest_verify=False)
    a.testing = True
    a.config["_TMP"] = tmp_path
    return a


def _edition_with_pdf(app, pages=1):
    """An edition whose single holding points at a real local PDF of `pages` pages."""
    import fitz
    pdf = app.config["_TMP"] / "book.pdf"
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(pdf)); doc.close()
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title, isbn) VALUES ('B', '')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
               "VALUES (?, 'electronic', ?, 'ocr_good')", (eid, str(pdf)))
    hid = db.execute("SELECT id FROM holding WHERE edition_id = ?", (eid,)).fetchone()[0]
    db.commit(); db.close()
    return eid, hid


def _pinned_files(app):
    d = app.config["COVERS_PINNED"]
    return os.listdir(d) if os.path.isdir(d) else []


def _is_raster(data):
    """True for a JPEG or PNG (covers may be normalised to JPEG on pin)."""
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"


# ── upload ──────────────────────────────────────────────────────────────────────
def test_upload_pins_cover_and_route_serves_it(app):
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    db.commit(); db.close()
    c = app.test_client()
    r = c.post(f"/edition/{eid}/cover/upload",
               data={"image": (io.BytesIO(_PNG), "c.png")}, content_type="multipart/form-data")
    assert r.get_json()["ok"] is True
    assert _pinned_files(app)                                  # persisted in the pinned dir
    # The cover route serves the pinned upload (PNG or normalised JPEG), not a fetched
    # /placeholder cover.
    assert _is_raster(c.get(f"/edition/{eid}/cover.jpg").data)


def test_upload_rejects_non_image(app):
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    db.commit(); db.close()
    r = app.test_client().post(f"/edition/{eid}/cover/upload",
                               data={"image": (io.BytesIO(b"not an image"), "x.txt")},
                               content_type="multipart/form-data")
    assert r.status_code == 415 and r.get_json()["ok"] is False
    assert not _pinned_files(app)


# ── from a holding's page ─────────────────────────────────────────────────────────
def test_from_holding_pins_rendered_page(app):
    eid, hid = _edition_with_pdf(app, pages=2)
    c = app.test_client()
    r = c.post(f"/edition/{eid}/cover/from-holding/{hid}", data={"page": "2"})
    assert r.get_json()["ok"] is True
    assert _pinned_files(app)
    assert _is_raster(c.get(f"/edition/{eid}/cover.jpg").data)         # a rendered page image


def test_from_holding_page_out_of_range_is_422(app):
    eid, hid = _edition_with_pdf(app, pages=1)
    r = app.test_client().post(f"/edition/{eid}/cover/from-holding/{hid}", data={"page": "9"})
    assert r.status_code == 422 and not _pinned_files(app)


def test_from_holding_not_downloaded_is_409(app):
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/nope/online-only.pdf')", (eid,))
    hid = db.execute("SELECT id FROM holding WHERE edition_id = ?", (eid,)).fetchone()[0]
    db.commit(); db.close()
    r = app.test_client().post(f"/edition/{eid}/cover/from-holding/{hid}")
    assert r.status_code == 409 and not _pinned_files(app)


# ── reset ─────────────────────────────────────────────────────────────────────────
def test_reset_clears_the_pin(app):
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    db.commit(); db.close()
    c = app.test_client()
    c.post(f"/edition/{eid}/cover/upload",
           data={"image": (io.BytesIO(_PNG), "c.png")}, content_type="multipart/form-data")
    assert _pinned_files(app)
    assert c.post(f"/edition/{eid}/cover/reset").get_json()["ok"] is True
    assert not _pinned_files(app)


# ── the Cover subsection renders in the editable card ──────────────────────────────
def test_edit_card_shows_cover_subsection(app):
    eid, hid = _edition_with_pdf(app, pages=1)
    html = app.test_client().get(f"/works/detect/{eid}/edit").get_data(as_text=True)
    assert "Cover" in html
    assert f"/edition/{eid}/cover.jpg" in html
    assert f"/edition/{eid}/cover/from-holding/{hid}" in html
    assert f"/edition/{eid}/cover/upload" in html
