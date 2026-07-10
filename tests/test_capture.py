"""Step-3b regression tests — phone capture endpoint.

Pins: barcode-scanner UX (ISBN autofocused, valid scan → lookup, invalid /
empty → fallback), Open Library is injectable, photo lands locally (not on
the mount), iOS-Shortcut JSON path.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from catalogue.db_store import connect
from catalogue.webui.web import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Force uploads into the test tmp dir (NOT a network mount).
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(tmp_path / "cap.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app, tmp_path


# ── UX contract: scanner-friendly form ────────────────────────────────────
def test_capture_get_has_autofocused_isbn_field(env):
    c, _, _ = env
    r = c.get("/capture")
    assert r.status_code == 200
    # ISBN input must be autofocused so a scan lands straight in it.
    assert b'name="isbn"' in r.data
    assert b"autofocus" in r.data
    # Multipart so the photo fallback can ride the same form.
    assert b'enctype="multipart/form-data"' in r.data
    # File input is present (the photo fallback).
    assert b'type="file"' in r.data


# ── Valid ISBN-13 → Open Library lookup → metadata stored on staging ──────
def test_valid_isbn_triggers_lookup_and_records_metadata(env):
    c, app, _ = env
    called_with = {}

    def fake_lookup(isbn):
        called_with["isbn"] = isbn
        return {
            "title": "The Way of the Bodhisattva",
            "authors": ["Śāntideva"],
            "publishers": ["Shambhala"],
            "publish_date": "2006",
            "isbn_13": isbn,
            "source": "openlibrary",
        }

    app.config["ISBN_LOOKUP"] = fake_lookup

    r = c.post("/capture", data={"isbn": "978-0-205-30902-3"})
    assert r.status_code == 200
    assert called_with["isbn"] == "9780205309023"   # normalized

    conn = connect(app.config["DB_PATH"])
    row = conn.execute(
        "SELECT raw_isbn, image_path, metadata_json FROM capture_staging"
    ).fetchone()
    conn.close()
    assert row[0] == "9780205309023"            # normalized digits stored
    assert row[1] is None
    md = json.loads(row[2])
    assert md["title"] == "The Way of the Bodhisattva"
    # Stored title keeps diacritics (NFC, §4.8c step 1 applies broadly):
    assert "Śāntideva" in md["authors"]


# ── Invalid ISBN: fall back to manual path, no lookup attempted ───────────
def test_invalid_isbn_falls_back_and_skips_lookup(env):
    c, app, _ = env

    def boom(_isbn):
        raise AssertionError("must not call lookup with invalid ISBN")

    app.config["ISBN_LOOKUP"] = boom
    r = c.post("/capture", data={
        "isbn": "9780205309022",                 # bad checksum
        "note": "shelf 4",
    })
    assert r.status_code == 200

    conn = connect(app.config["DB_PATH"])
    row = conn.execute(
        "SELECT raw_isbn, metadata_json, free_text_note FROM capture_staging"
    ).fetchone()
    conn.close()
    # Raw digits still stored (so desktop can decide); no metadata stored.
    assert row[0] == "9780205309022"
    assert row[1] is None
    assert row[2] == "shelf 4"


# ── Valid ISBN, lookup misses: still queued, no metadata ─────────────────
def test_valid_isbn_with_empty_lookup_still_stages(env):
    c, app, _ = env
    app.config["ISBN_LOOKUP"] = lambda _isbn: None   # OL has no record

    r = c.post("/capture", data={"isbn": "9780205309023"})
    assert r.status_code == 200

    conn = connect(app.config["DB_PATH"])
    row = conn.execute(
        "SELECT raw_isbn, metadata_json FROM capture_staging"
    ).fetchone()
    conn.close()
    assert row[0] == "9780205309023"
    assert row[1] is None                          # no metadata


# ── No ISBN, photo only: manual path works ────────────────────────────────
def test_photo_upload_lands_locally_not_on_mount(env):
    c, app, tmp = env
    upload_dir = Path(app.config["UPLOAD_DIR"])

    r = c.post(
        "/capture",
        data={
            "note": "scanned spine",
            "photo": (io.BytesIO(b"fake jpeg bytes"), "spine.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200

    conn = connect(app.config["DB_PATH"])
    (image_path,) = conn.execute(
        "SELECT image_path FROM capture_staging"
    ).fetchone()
    conn.close()

    saved = Path(image_path)
    assert saved.exists()
    assert saved.read_bytes() == b"fake jpeg bytes"
    # Stored under the configured local upload dir, never a network path.
    assert upload_dir in saved.parents
    # And the filename was sanitized (no path traversal possible).
    assert "/" not in saved.name and ".." not in saved.name


def test_photo_filename_with_traversal_is_neutered(env):
    c, app, _ = env
    upload_dir = Path(app.config["UPLOAD_DIR"])
    r = c.post(
        "/capture",
        data={
            "isbn": "",
            "note": "x",
            "photo": (io.BytesIO(b"data"), "../../../etc/passwd"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    # Nothing should have escaped the upload dir.
    files = list(upload_dir.iterdir())
    assert len(files) == 1
    assert files[0].parent == upload_dir
    assert ".." not in files[0].name


# ── Empty submission rejected (don't litter staging) ──────────────────────
def test_empty_submission_returns_400(env):
    c, _, _ = env
    r = c.post("/capture", data={})
    assert r.status_code == 400


# ── iOS Shortcut path: JSON response ──────────────────────────────────────
def test_shortcut_json_response_on_accept_header(env):
    c, app, _ = env
    app.config["ISBN_LOOKUP"] = lambda _i: {"title": "T", "isbn_13": _i,
                                            "authors": [], "publishers": [],
                                            "publish_date": None,
                                            "source": "openlibrary"}
    r = c.post(
        "/capture",
        data={"isbn": "9780205309023"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["saved_id"]
    assert body["metadata"]["title"] == "T"


def test_shortcut_header_alias_works(env):
    """X-Requested-With: shortcut also triggers JSON, for Shortcuts that
    don't set Accept cleanly."""
    c, app, _ = env
    app.config["ISBN_LOOKUP"] = lambda _i: None
    r = c.post(
        "/capture",
        data={"isbn": "9780205309023"},
        headers={"X-Requested-With": "shortcut"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["metadata"] is None


# ── Staging detail surfaces the metadata + prefills the resolve form ─────
def test_staging_detail_renders_metadata_and_prefills_title(env):
    c, app, _ = env
    app.config["ISBN_LOOKUP"] = lambda _i: {
        "title": "Bodhicaryāvatāra",
        "authors": ["Śāntideva"],
        "publishers": ["Shambhala"],
        "publish_date": "2006",
        "isbn_13": _i, "source": "openlibrary",
    }
    c.post("/capture", data={"isbn": "9780205309023"})

    r = c.get("/staging/1")
    assert r.status_code == 200
    body = r.data
    # Open Library section renders.
    assert b"Open Library lookup" in body
    assert "Bodhicaryāvatāra".encode() in body
    assert "Śāntideva".encode() in body
    # Title input is prefilled with the resolved title.
    assert 'value="Bodhicaryāvatāra"'.encode() in body


# ── Idempotency / interplay with Step 3a ─────────────────────────────────
def test_captured_isbn_drives_dedup_on_resolve(env):
    """If an edition with the captured ISBN already exists, the staging
    detail page surfaces it first — preventing accidental duplicates at
    resolve time (§7.3)."""
    c, app, _ = env
    conn = connect(app.config["DB_PATH"])
    conn.execute(
        "INSERT INTO edition (id, title, isbn) VALUES (1, 'Existing', '9780205309023')"
    )
    conn.commit()
    conn.close()

    app.config["ISBN_LOOKUP"] = lambda _i: None
    c.post("/capture", data={"isbn": "9780205309023"})

    r = c.get("/staging/1")
    body = r.data.decode()
    # The matching edition is offered as a pickable "this is a duplicate" match.
    assert 'value="match:1"' in body              # existing edition #1 surfaced
