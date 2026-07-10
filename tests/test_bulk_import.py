"""Step-3b regression tests — Barcode to PC CSV-mode bulk import.

Keystroke-mode lives at /capture and is covered by test_capture.py; this
file pins the CSV/list path.
"""
from __future__ import annotations

import io
import json

import pytest

from catalogue.db_store import connect
from catalogue.webui.web import _extract_isbns_from_csv, create_app


# ── CSV extractor — tolerate Barcode-to-PC shapes ────────────────────────
def test_extractor_handles_plain_lines():
    assert _extract_isbns_from_csv(
        "9780205309023\n9780374528379\n", limit=10
    ) == ["9780205309023", "9780374528379"]


def test_extractor_handles_isbn_plus_timestamp_csv():
    """Barcode to PC's CSV export typically pairs each scan with a
    timestamp column — we want the ISBN, not the timestamp."""
    raw = (
        "9780205309023,2026-05-28T10:00:00\n"
        "9780374528379,2026-05-28T10:00:05\n"
    )
    assert _extract_isbns_from_csv(raw, limit=10) == [
        "9780205309023", "9780374528379"
    ]


def test_extractor_handles_hyphenated_isbns():
    assert _extract_isbns_from_csv(
        "978-0-205-30902-3\n", limit=10
    ) == ["9780205309023"]


def test_extractor_skips_blank_and_comment_lines():
    raw = "# header\n\n9780205309023\n  \n# another\n"
    assert _extract_isbns_from_csv(raw, limit=10) == ["9780205309023"]


def test_extractor_respects_limit():
    raw = "\n".join(["9780205309023"] * 5)
    assert len(_extract_isbns_from_csv(raw, limit=3)) == 3


def test_extractor_ignores_short_numeric_columns():
    """A `1,9780205309023` row (e.g. row-index + ISBN) must not be
    misread as ISBN `1`."""
    assert _extract_isbns_from_csv(
        "1,9780205309023\n", limit=10
    ) == ["9780205309023"]


# ── End-to-end: import endpoint ──────────────────────────────────────────
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(tmp_path / "bulk.db")
    app.testing = True
    with app.test_client() as c:
        yield c, app, tmp_path


def test_pasted_list_imports_and_calls_lookup_per_row(env):
    c, app, _ = env
    seen: list[str] = []

    def fake_lookup(isbn):
        seen.append(isbn)
        return {"title": f"book-{isbn[-1]}", "isbn_13": isbn,
                "authors": [], "publishers": [], "publish_date": None,
                "source": "openlibrary"}

    app.config["ISBN_LOOKUP"] = fake_lookup

    lines = "9780205309023\n9780374528379\n"
    r = c.post("/capture/import", data={"lines": lines})
    assert r.status_code == 200
    assert seen == ["9780205309023", "9780374528379"]

    conn = connect(app.config["DB_PATH"])
    rows = conn.execute(
        "SELECT raw_isbn, metadata_json, free_text_note "
        "FROM capture_staging ORDER BY id"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["9780205309023", "9780374528379"]
    assert all(r[2] == "bulk import (Barcode to PC CSV mode)" for r in rows)
    assert json.loads(rows[0][1])["title"] == "book-3"


def test_file_upload_imports(env):
    c, app, _ = env
    app.config["ISBN_LOOKUP"] = lambda _i: None
    csv = b"9780205309023,when\n9780374528379,when\n"

    r = c.post(
        "/capture/import",
        data={"file": (io.BytesIO(csv), "scans.csv")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    conn = connect(app.config["DB_PATH"])
    (n,) = conn.execute("SELECT count(*) FROM capture_staging").fetchone()
    conn.close()
    assert n == 2


def test_invalid_checksums_are_rejected_not_staged(env):
    """§14.4: CSV import goes through the same validation path as
    POST /capture, which 422s a bad checksum. Bad ISBNs must NOT silently
    land in staging masquerading as valid."""
    c, app, _ = env

    def boom(_):
        raise AssertionError("lookup must not be called on invalid ISBN")

    app.config["ISBN_LOOKUP"] = boom

    r = c.post("/capture/import",
               data={"lines": "9780205309022\n"})   # bad checksum
    assert r.status_code == 200

    conn = connect(app.config["DB_PATH"])
    rows = conn.execute(
        "SELECT raw_isbn FROM capture_staging"
    ).fetchall()
    conn.close()
    # Not staged. The bad row is counted as `invalid` in the report (the
    # HTML/JSON response) — separately tested by report-shape tests.
    assert rows == []


def test_duplicates_within_a_batch_are_counted_and_deduped(env):
    """Scanning the same book twice in one session should record once
    and surface the dup count so the user notices."""
    c, app, _ = env
    app.config["ISBN_LOOKUP"] = lambda _i: None

    lines = "9780205309023\n9780205309023\n9780205309023\n"
    r = c.post("/capture/import", data={"lines": lines})
    assert r.status_code == 200

    conn = connect(app.config["DB_PATH"])
    (n,) = conn.execute("SELECT count(*) FROM capture_staging").fetchone()
    conn.close()
    assert n == 1  # one row, not three


def test_lookup_failure_does_not_abort_the_batch(env):
    """One bad lookup must not poison the rest of the batch — Step 3b's
    'never raise' contract extends to bulk import."""
    c, app, _ = env

    def flaky(isbn):
        if isbn == "9780374528379":
            raise OSError("simulated OL outage")
        return None

    app.config["ISBN_LOOKUP"] = flaky
    r = c.post("/capture/import",
               data={"lines": "9780205309023\n9780374528379\n"})
    assert r.status_code == 200

    conn = connect(app.config["DB_PATH"])
    (n,) = conn.execute("SELECT count(*) FROM capture_staging").fetchone()
    conn.close()
    assert n == 2


def test_json_response_for_shortcut(env):
    c, app, _ = env
    app.config["ISBN_LOOKUP"] = lambda _i: None
    r = c.post(
        "/capture/import",
        data={"lines": "9780205309023\n"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["scanned"] == 1
    assert body["imported"] == 1
    assert body["invalid"] == 0
    assert isinstance(body["ids"], list) and len(body["ids"]) == 1


def test_import_form_renders(env):
    c, _, _ = env
    r = c.get("/capture/import")
    assert r.status_code == 200
    assert b"Barcode to PC" in r.data
    assert b"name=\"lines\"" in r.data
    assert b"name=\"file\"" in r.data


def test_capture_page_links_to_import(env):
    c, _, _ = env
    r = c.get("/capture")
    assert b"/capture/import" in r.data
    assert b"Barcode to PC" in r.data
