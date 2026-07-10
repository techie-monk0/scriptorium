"""page_text_cache persistence (sweep + digitize) + backfill CLIs.

Per-page text is now durably stored so a future training corpus can be chunked
without re-OCRing. Pins one-row-per-page from the serial sweep, the EPUB None case,
idempotency, and the two backfill CLIs (resumable, no-op on re-run).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from catalogue.db_store import db as dbmod
from catalogue.services import intake_match
from catalogue.services import sweep
from catalogue.services.extract import ExtractedText


@pytest.fixture
def conn():
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    c = dbmod.init_db(fd.name)
    yield c
    c.close()
    Path(fd.name).unlink()


def _pdf(tmp, name="book.pdf"):
    d = Path(tempfile.mkdtemp())
    f = d / name
    f.write_bytes(b"%PDF-1.4 fake")
    return f


def test_sweep_persists_one_row_per_page(conn):
    f = _pdf(conn)
    cfg = sweep.SweepConfig(mount_root=f.parent, extractor=lambda p: ExtractedText(
        text="one\ntwo", page_count=2, producer="x", is_image_only=False,
        page_texts=("one", "two")))
    sweep._process(conn, cfg, f, sweep.SweepReport())
    rows = conn.execute("SELECT page_no, text FROM page_text_cache ORDER BY page_no").fetchall()
    assert rows == [(1, "one"), (2, "two")]


def test_epub_none_page_texts_writes_nothing(conn):
    f = _pdf(conn, "b.epub")
    cfg = sweep.SweepConfig(mount_root=f.parent, extractor=lambda p: ExtractedText(
        text="whole", page_count=None, producer="epub", is_image_only=False,
        page_texts=None))
    sweep._process(conn, cfg, f, sweep.SweepReport())
    assert conn.execute("SELECT count(*) FROM page_text_cache").fetchone()[0] == 0


def test_reextract_is_idempotent(conn):
    f = _pdf(conn)
    cfg = sweep.SweepConfig(mount_root=f.parent, extractor=lambda p: ExtractedText(
        text="one\ntwo", page_count=2, producer="x", is_image_only=False,
        page_texts=("one", "two")))
    sweep._process(conn, cfg, f, sweep.SweepReport())
    conn.execute("DELETE FROM sweep_state"); conn.commit()
    sweep._process(conn, cfg, f, sweep.SweepReport())
    assert conn.execute("SELECT count(*) FROM page_text_cache").fetchone()[0] == 2


def test_backfill_ol_work_key_resumable(conn):
    conn.execute("INSERT INTO edition (id, title, isbn) VALUES (1, 'a', '9780861711765')")
    conn.execute("INSERT INTO edition (id, title) VALUES (2, 'no-isbn')")
    conn.commit()
    s = intake_match.backfill_work_keys(conn, fetch=lambda i: "/works/OL9W")
    assert s["candidates"] == 1 and s["resolved"] == 1
    assert conn.execute("SELECT ol_work_key FROM edition WHERE id=1").fetchone()[0] == "/works/OL9W"
    # Resumable: nothing left to do.
    assert intake_match.backfill_work_keys(conn, fetch=lambda i: None)["candidates"] == 0
