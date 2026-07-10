"""System tests — sweep (§6, §7.1).

The sweep has no HTTP surface; the public Python entry is
`catalogue.sweep.sweep(conn, SweepConfig(...))`. Assertions observe via
the returned `SweepReport`, the read-only mount filesystem, and the
review-queue HTTP surface — NOT via `SELECT` from internal tables.

Plan invariants:
  - "treat the mount as read-only — the DB, caches, and outputs live
    locally on the M4, never written back to the share." (§6, §13)
  - "Detect changes by (path, size, mtime) BEFORE hashing; make the sweep
    resumable and idempotent (survive a disconnect, resume)." (§6)
  - "Skip + log locked/corrupt/unreachable files; flag image-only PDFs."
    (§7.1)
  - "score quality; selective re-OCR of the low-quality minority."
    (§3, §4.8d)
"""
from __future__ import annotations

import os
from pathlib import Path

from catalogue.db_store import init_db
from catalogue.services.sweep import SweepConfig, sweep

from .conftest import make_epub


def _snapshot(root: Path) -> dict:
    out = {}
    for dp, _d, files in os.walk(root):
        for n in files:
            p = Path(dp) / n
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime)
    return out


def test_sweep_never_modifies_mount_contents(tmp_path):
    """§6: 'treat the mount as read-only.' Observable as: every file's
    (size, mtime) is byte-identical before vs after the sweep."""
    mount = tmp_path / "mount"
    mount.mkdir()
    make_epub(mount / "a.epub", ["<p>first</p>"])
    make_epub(mount / "b.epub", ["<p>second</p>"])
    conn = init_db(tmp_path / "cat.db")
    try:
        before = _snapshot(mount)
        sweep(conn, SweepConfig(mount_root=mount))
        after = _snapshot(mount)
        assert before == after
    finally:
        conn.close()


def test_resweep_is_a_no_op_on_unchanged_files(tmp_path):
    """§6: 'change-detect by (path, size, mtime) before hashing.'
    Observable as: SweepReport.scanned == skipped_unchanged on re-run."""
    mount = tmp_path / "mount"
    mount.mkdir()
    make_epub(mount / "a.epub", ["<p>first</p>"])
    conn = init_db(tmp_path / "cat.db")
    try:
        sweep(conn, SweepConfig(mount_root=mount))
        report = sweep(conn, SweepConfig(mount_root=mount))
        assert report.scanned == 1
        assert report.skipped_unchanged == 1
        assert report.new_holdings == 0
    finally:
        conn.close()


def test_sweep_is_resumable_after_an_io_blip(tmp_path):
    """§6: 'resumable and idempotent (survive a disconnect, resume).'
    Setup uses an injected extractor (the public extension point) to
    simulate the disconnect; assertions go through SweepReport."""
    mount = tmp_path / "mount"
    mount.mkdir()
    make_epub(mount / "1.epub", ["<p>one</p>"])
    make_epub(mount / "2.epub", ["<p>two</p>"])
    make_epub(mount / "3.epub", ["<p>three</p>"])

    from catalogue.services.extract import extract as real
    fail_on = {"2.epub"}

    def flaky(path):
        if path.name in fail_on:
            raise OSError("disconnect")
        return real(path)

    conn = init_db(tmp_path / "cat.db")
    try:
        r1 = sweep(conn, SweepConfig(mount_root=mount, extractor=flaky))
        assert r1.errors == 1
        assert r1.new_holdings == 2

        # The mount comes back; second sweep retries only the failed file.
        fail_on.clear()
        r2 = sweep(conn, SweepConfig(mount_root=mount, extractor=real))
        assert r2.skipped_unchanged == 2
        assert r2.new_holdings == 1
    finally:
        conn.close()


def test_low_quality_extraction_surfaces_in_review_queue(app_env, tmp_path):
    """§4.8d: low quality → review queue. Observable via /review."""
    c, app, _ = app_env
    mount = tmp_path / "mount"
    mount.mkdir()
    # Garbage body → quality score below threshold.
    make_epub(mount / "garbage.epub", ["<p>" + ("���� \x00 \x01" * 80) + "</p>"])

    from catalogue.db_store import connect
    conn = connect(app.config["DB_PATH"])
    try:
        sweep(conn, SweepConfig(mount_root=mount, quality_threshold=0.6))
    finally:
        conn.close()

    review = c.get("/review-queue")
    assert review.status_code == 200
    assert b"low_quality_ocr" in review.data


def test_extract_failure_is_logged_and_sweep_continues(tmp_path):
    """§7.1: 'skip + log.' Observable as: errors > 0 but other files still
    processed and SweepReport returned cleanly."""
    mount = tmp_path / "mount"
    mount.mkdir()
    make_epub(mount / "ok.epub", ["<p>good</p>"])
    make_epub(mount / "bad.epub", ["<p>good</p>"])

    from catalogue.services.extract import extract as real

    def flaky(p):
        if p.name == "bad.epub":
            raise OSError("simulated")
        return real(p)

    conn = init_db(tmp_path / "cat.db")
    try:
        report = sweep(conn, SweepConfig(mount_root=mount, extractor=flaky))
        assert report.errors == 1
        assert report.new_holdings == 1   # the other file made it
    finally:
        conn.close()
