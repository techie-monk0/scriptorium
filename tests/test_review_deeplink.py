"""Verify the /works/detect/<eid>/review deep-link seeds a detection for an orphan
edition (one with no work_detection row) so the Review page anchor resolves — the
"Edit this edition → wrong edition (e55)" bug. Hermetic (Flask test client)."""
from catalogue.db_store import connect
from catalogue.services import work_detect as WD
from catalogue.webui.web import create_app


def _app(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    return app


def test_orphan_edition_gets_seeded_and_deeplink_lands(tmp_path):
    app = _app(tmp_path)
    db = connect(app.config["DB_PATH"])
    # A decoy edition WITH a detection (so the worklist's first row is NOT our target) …
    d = db.execute("INSERT INTO edition (title, structure) VALUES ('AAA Decoy', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/d.pdf')", (d,))
    WD.store_detection(db, d, "single", WD.detect_single(db, d, classical=lambda c: {"english": c["title"]}))
    # … and the ORPHAN: exists, live, but no work_detection row (like edition 769).
    orphan = db.execute("INSERT INTO edition (title, structure) VALUES ('Sutrasammuchaya', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/s.pdf')", (orphan,))
    db.commit()

    # Precondition: orphan is missing from the worklist, decoy is present.
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
    assert f'id="i{d}"' in page
    assert f'id="i{orphan}"' not in page, "precondition: orphan should start absent"

    # Following the Browse "Edit this edition" link seeds a detection and redirects.
    with app.test_client() as c:
        r = c.get(f"/works/detect/{orphan}/review")
        assert r.status_code == 302
        assert r.headers["Location"].endswith(f"#i{orphan}")

    # Now the worklist actually contains the orphan's row, so #i<orphan> resolves.
    with app.test_client() as c:
        page = c.get("/works/detect/single").data.decode()
    assert f'id="i{orphan}"' in page, "orphan row must exist after seeding"

    # And a detection row was written.
    assert WD.get_detection(db, orphan) is not None


def test_review_of_missing_edition_404s(tmp_path):
    app = _app(tmp_path)
    with app.test_client() as c:
        assert c.get("/works/detect/999999/review").status_code == 404
