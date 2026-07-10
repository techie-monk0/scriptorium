"""_inbox/ sidecar ingest (catalogue/domain/inbox) — phone-drop intake.

Pins: sidecar metadata applied to the swept holding, edition flagged for review +
'ingest' queue row, sidecar consumed (idempotent), placeholder/unswept guards, and
OCR-on-ingest for image-only files via the existing digitize pipeline (stubbed).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from catalogue.db_store import db as dbmod
from catalogue.services import inbox, sweep, digitize
from catalogue.services.extract import ExtractedText


@pytest.fixture
def env():
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    conn = dbmod.init_db(fd.name)
    mount = Path(tempfile.mkdtemp())
    (mount / "_inbox").mkdir()
    yield conn, mount
    conn.close()
    Path(fd.name).unlink()


def _text_cfg(mount, *, image_only=False):
    return sweep.SweepConfig(
        mount_root=mount,
        extractor=lambda p: ExtractedText(
            text="" if image_only else "real text " * 40,
            page_count=1, producer="x", is_image_only=image_only,
            page_texts=None if image_only else ("real text",)))


def _drop(mount, stem, payload, content=b"%PDF-1.4 real content"):
    (mount / "_inbox" / f"{stem}.pdf").write_bytes(content)
    (mount / "_inbox" / f"{stem}.pdf.json").write_text(json.dumps(payload))


def test_sidecar_applied_flagged_and_consumed(env):
    conn, mount = env
    _drop(mount, "My Book", {"isbn": "9780861711765", "shelf": "A3",
                             "note": "gift", "source": "ios-shortcut"})
    rep = sweep.sweep(conn, _text_cfg(mount), workers=1)
    assert rep.inbox.applied == 1

    e = conn.execute("SELECT isbn, review_status FROM edition").fetchone()
    assert e == ("9780861711765", "needs_fix")
    h = conn.execute("SELECT shelf_location, notes FROM holding").fetchone()
    assert h[0] == "A3" and "gift" in h[1]

    rq = conn.execute("SELECT payload_json FROM review_queue WHERE item_type='ingest'").fetchall()
    assert len(rq) == 1 and json.loads(rq[0][0])["kind"] == "inbox_sidecar"

    assert (mount / "_inbox" / "My Book.pdf.json.done").exists()
    assert not (mount / "_inbox" / "My Book.pdf.json").exists()


def test_sweep_postpass_keys_new_isbn_edition(env):
    conn, mount = env
    _drop(mount, "Keyed", {"isbn": "9780861711765"})
    cfg = _text_cfg(mount)
    cfg.work_key_fetch = lambda isbn: "/works/OL77W"   # injected (offline)
    sweep.sweep(conn, cfg, workers=1)
    # The sidecar set the ISBN; the post-pass keyed the edition.
    assert conn.execute("SELECT ol_work_key FROM edition").fetchone()[0] == "/works/OL77W"


def test_idempotent_resweep(env):
    conn, mount = env
    _drop(mount, "Book", {"isbn": "9780861711765"})
    sweep.sweep(conn, _text_cfg(mount), workers=1)
    rep2 = sweep.sweep(conn, _text_cfg(mount), workers=1)
    assert rep2.inbox.applied == 0
    assert conn.execute("SELECT count(*) FROM review_queue WHERE item_type='ingest'").fetchone()[0] == 1


def test_placeholder_and_unswept_skipped_and_retried(env):
    conn, mount = env
    (mount / "_inbox" / "stub.pdf").write_bytes(b"")             # 0-byte placeholder
    (mount / "_inbox" / "stub.pdf.json").write_text(json.dumps({"isbn": "9780861711765"}))
    (mount / "_inbox" / "fresh.pdf").write_bytes(b"%PDF real")   # present but NOT swept yet
    (mount / "_inbox" / "fresh.pdf.json").write_text(json.dumps({"isbn": "9780861711765"}))
    rep = inbox.apply_inbox_sidecars(conn, _text_cfg(mount))     # inbox-only, no walk
    assert rep.applied == 0
    assert rep.skipped_offline == 1     # the 0-byte placeholder
    assert rep.skipped_unswept == 1     # present media, no holding row yet
    # both sidecars left in place for a later run
    assert (mount / "_inbox" / "stub.pdf.json").exists()
    assert (mount / "_inbox" / "fresh.pdf.json").exists()


def test_ocr_on_ingest_for_image_only(env):
    conn, mount = env
    _drop(mount, "scan", {"isbn": "9780861711765"})
    cfg = _text_cfg(mount, image_only=True)
    # Walk creates the image_only holding...
    rep = sweep.SweepReport()
    sweep._process(conn, cfg, mount / "_inbox" / "scan.pdf", rep)
    assert conn.execute("SELECT text_status FROM holding").fetchone()[0] == "image_only"

    class StubDig:
        kind = "ocrmypdf_tesseract"
        def digitize(self, src, out):
            out = Path(out); out.mkdir(parents=True, exist_ok=True)
            d = out / "o.pdf"; d.write_bytes(b"%PDF ocr")
            return digitize.DigitizeResult(
                archival_pdf_path=d, text="dharma " * 50,
                digitizer_used="ocrmypdf_tesseract", page_count=1,
                page_texts=("dharma text",))

    r = inbox.apply_inbox_sidecars(conn, cfg, digitizer=StubDig())
    assert r.applied == 1 and r.ocr_run == 1
    assert conn.execute("SELECT text_status FROM holding").fetchone()[0] in ("ocr_good", "ocr_poor")
    assert conn.execute("SELECT count(*) FROM page_text_cache").fetchone()[0] == 1
