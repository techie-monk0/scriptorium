"""Schema v4 — digitization provenance folded into the central schema + backfill.

The scan/OCR provenance tables (formerly stood up ad hoc by scan_ocr.ensure_schema) are now part
of init_db: every DB carries them, the holding pointer columns, and a one-time backfill of (a) the
provenance kind from text_status and (b) a current OCR event from the legacy digitizer_used code.
The external v_holding_files view gains provenance_kind. See db.py::_migrate / scan_ocr.py.
"""
from catalogue.access_api import scan_ocr
from catalogue.db_store import init_db
from catalogue.db_store.db import _migrate


def _downgrade_to_v3(conn):
    """Simulate a pre-v4 DB so the next _migrate runs the version<4 backfill."""
    conn.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
    conn.commit()


def test_fresh_db_has_provenance_schema_at_v4(tmp_path):
    conn = init_db(tmp_path / "t.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"provenance_kind", "digitization_engine", "digitization_event"} <= tables
    hcols = {r[1] for r in conn.execute("PRAGMA table_info(holding)")}
    assert {"provenance_kind", "current_capture_event_id", "current_ocr_event_id"} <= hcols
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "13"
    # the engine vocab seeded both stages
    stages = {r[0] for r in conn.execute("SELECT DISTINCT stage FROM digitization_engine")}
    assert stages == {"capture", "ocr"}


def test_backfill_provenance_kind_from_text_status(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid = conn.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    for path, ts in (("/native.pdf", "native"), ("/ocr.pdf", "ocr_good"), ("/img.pdf", "image_only")):
        conn.execute("INSERT INTO holding (edition_id, file_path, text_status) VALUES (?, ?, ?)",
                     (eid, path, ts))
    _downgrade_to_v3(conn)
    _migrate(conn)
    conn.commit()
    kinds = dict(conn.execute("SELECT text_status, provenance_kind FROM holding"))
    assert kinds["native"] == "born_digital"
    assert kinds["ocr_good"] == "scanned"
    assert kinds["image_only"] == "scanned"


def test_legacy_digitizer_becomes_current_ocr_event(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid = conn.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    hid = conn.execute(
        "INSERT INTO holding (edition_id, file_path, digitizer_used) "
        "VALUES (?, '/a.pdf', 'ocrmypdf_tesseract')", (eid,)).lastrowid
    _downgrade_to_v3(conn)
    _migrate(conn)
    conn.commit()
    prov = scan_ocr.provenance(conn, hid)               # reads the folded tables directly
    assert prov.ocr is not None
    assert prov.ocr.engine == "tesseract_iast" and prov.ocr.stage == "ocr"
    assert conn.execute(
        "SELECT current_ocr_event_id FROM holding WHERE id=?", (hid,)).fetchone()[0] == prov.ocr.id


def test_view_exposes_provenance_kind(tmp_path):
    conn = init_db(tmp_path / "t.db")
    eid = conn.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, file_path, text_status, provenance_kind) "
                 "VALUES (?, '/a.pdf', 'ocr_good', 'scanned')", (eid,))
    conn.commit()
    assert conn.execute("SELECT provenance_kind FROM v_holding_files").fetchone()[0] == "scanned"


def test_migration_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    conn = init_db(p)                                   # second init re-runs _migrate
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "13"
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='digitization_event'").fetchone()[0] == 1
    # view recreated cleanly (single instance, with the new column)
    vcols = [r[1] for r in conn.execute("PRAGMA table_info(v_holding_files)")]
    assert "provenance_kind" in vcols
