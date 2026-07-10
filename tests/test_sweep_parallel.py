"""Parallel sweep path — same observable outcomes as the serial path.

Workers always use the production `extract`/`score_text` pair (custom
extractors don't pickle across the pool boundary), so these tests seed
real EPUBs the default extractor can read.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from catalogue.db_store import init_db
from catalogue.services.sweep import SweepConfig, sweep


def _make_epub(path: Path, body: str) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("OEBPS/ch.xhtml", f"<html><body>{body}</body></html>")


@pytest.fixture
def env(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    db = init_db(tmp_path / "cat.db")
    yield mount, db, tmp_path
    db.close()


def test_parallel_sweep_same_outputs_as_serial(env):
    """Three files → three holdings, one raw_extract_cache row each,
    sweep_state populated, regardless of `workers`. The order workers
    finish in is non-deterministic; the *result set* must not be."""
    mount, db, _ = env
    for i in range(3):
        # Vary the body so file_hash differs; identical content would
        # (correctly) dedup-upsert into one holding via the §6 invariant.
        _make_epub(mount / f"book{i}.epub",
                   f"<p>Chapter {i} of book {i} " * 100 + "</p>")

    rep = sweep(db, SweepConfig(mount_root=mount), workers=3)
    assert rep.scanned == 3
    assert rep.new_holdings == 3
    assert rep.ocr_good == 3

    rows = db.execute(
        "SELECT file_path, text_status FROM holding ORDER BY file_path"
    ).fetchall()
    assert [Path(r[0]).name for r in rows] == ["book0.epub", "book1.epub", "book2.epub"]
    assert all(r[1] == "ocr_good" for r in rows)

    (n_state,) = db.execute("SELECT count(*) FROM sweep_state").fetchone()
    assert n_state == 3
    (n_cache,) = db.execute("SELECT count(*) FROM raw_extract_cache").fetchone()
    assert n_cache == 3


def test_parallel_sweep_skips_unchanged_files_on_rerun(env):
    """Second sweep with the same files: `skipped_unchanged` covers
    all of them, no new holdings, workers are never spawned for those
    files (parent filters via sweep_state before dispatch)."""
    mount, db, _ = env
    for i in range(3):
        _make_epub(mount / f"book{i}.epub", f"<p>book{i} text " * 80 + "</p>")

    sweep(db, SweepConfig(mount_root=mount), workers=2)
    rep2 = sweep(db, SweepConfig(mount_root=mount), workers=2)
    assert rep2.skipped_unchanged == 3
    assert rep2.new_holdings == 0


def test_parallel_sweep_progress_callback_fires_per_file(env):
    """`cfg.progress` runs on the parent — once per file, both for
    cache-skipped and worker-completed paths. Workers never see it
    (callbacks don't pickle), so the call count is the only invariant
    we can assert cheaply."""
    mount, db, _ = env
    for i in range(4):
        _make_epub(mount / f"b{i}.epub", f"<p>b{i} text " * 40 + "</p>")

    calls: list[str] = []

    def _progress(_rep, path):
        calls.append(path.name)

    rep = sweep(db, SweepConfig(mount_root=mount, progress=_progress),
                workers=2)
    assert rep.scanned == 4
    assert sorted(calls) == ["b0.epub", "b1.epub", "b2.epub", "b3.epub"]


def test_warn_when_pdf_in_suffixes_but_fitz_missing(env, monkeypatch, capsys):
    """Silent soft-dep failure cost a full sweep cycle once. Pin the
    warning so future PyMuPDF removals are noisy."""
    import builtins
    real_import = builtins.__import__

    def _no_fitz(name, *a, **kw):
        if name == "fitz":
            raise ImportError("simulated missing fitz")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_fitz)

    mount, db, _ = env
    sweep(db, SweepConfig(mount_root=mount))
    captured = capsys.readouterr()
    assert "PyMuPDF" in captured.err and "image_only" in captured.err


def test_no_warning_when_suffixes_excludes_pdf(env, monkeypatch, capsys):
    """The warning is conditional on `.pdf` being a target suffix. An
    EPUB-only sweep should stay quiet even without fitz."""
    import builtins
    real_import = builtins.__import__
    monkeypatch.setattr(
        builtins, "__import__",
        lambda name, *a, **kw: (_ for _ in ()).throw(ImportError())
            if name == "fitz" else real_import(name, *a, **kw),
    )
    mount, db, _ = env
    sweep(db, SweepConfig(mount_root=mount, suffixes=(".epub",)))
    captured = capsys.readouterr()
    assert "PyMuPDF" not in captured.err


def test_reprocess_clears_state_for_incomplete_holdings_only(env):
    """`reset_for_reprocess` targets only holdings whose work was
    incomplete. `ocr_good` holdings — and their cache rows — stay
    untouched so a partial re-sweep can't accidentally re-OCR them."""
    from catalogue.services.sweep import reset_for_reprocess
    mount, db, _ = env

    # Seed three holdings, only one of which is "done".
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'good')")
    db.execute("INSERT INTO edition (id, title) VALUES (2, 'poor')")
    db.execute("INSERT INTO edition (id, title) VALUES (3, 'image')")
    db.execute(
        "INSERT INTO holding (edition_id, form, file_path, file_hash, "
        "text_status) VALUES "
        "(1, 'electronic', '/m/good.epub', 'h_good', 'ocr_good'),"
        "(2, 'electronic', '/m/poor.pdf', 'h_poor', 'ocr_poor'),"
        "(3, 'electronic', '/m/image.pdf', 'h_image', 'image_only')"
    )
    for path, size, mtime, fh in [
        ("/m/good.epub", 100, 1.0, "h_good"),
        ("/m/poor.pdf", 200, 2.0, "h_poor"),
        ("/m/image.pdf", 300, 3.0, "h_image"),
    ]:
        db.execute(
            "INSERT INTO sweep_state (path, size, mtime, file_hash) "
            "VALUES (?, ?, ?, ?)", (path, size, mtime, fh),
        )
    for fh in ("h_good", "h_poor", "h_image"):
        db.execute(
            "INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
            "VALUES (?, 1, ?)", (fh, f"text for {fh}"),
        )
    db.execute(
        "INSERT INTO review_queue (item_type, payload_json) "
        "VALUES ('low_quality_ocr', '{\"file_hash\":\"h_poor\"}')"
    )
    db.commit()

    rp = reset_for_reprocess(db)
    assert rp.holdings_targeted == 2          # poor + image, not good
    assert rp.sweep_state_cleared == 2
    assert rp.raw_extract_cache_cleared == 2
    assert rp.review_queue_cleared == 1

    # The "good" holding's bookkeeping survives intact.
    assert db.execute(
        "SELECT count(*) FROM sweep_state WHERE file_hash='h_good'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT count(*) FROM raw_extract_cache WHERE file_hash='h_good'"
    ).fetchone()[0] == 1
    # Holdings themselves are NEVER deleted — only the per-stage caches.
    assert db.execute("SELECT count(*) FROM holding").fetchone()[0] == 3


def test_reprocess_with_no_incomplete_holdings_is_a_noop(env):
    from catalogue.services.sweep import reset_for_reprocess
    _, db, _ = env
    rp = reset_for_reprocess(db)
    assert rp.holdings_targeted == 0
    assert rp.sweep_state_cleared == 0


def test_reprocess_can_target_a_single_status(env):
    """Interactive picker boils down to `reset_for_reprocess(statuses=…)`.
    Pin that targeting one status leaves the other categories alone."""
    from catalogue.services.sweep import reset_for_reprocess
    _, db, _ = env
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'a'), (2, 'b')")
    db.execute(
        "INSERT INTO holding (edition_id, form, file_path, file_hash, "
        "text_status) VALUES "
        "(1, 'electronic', '/m/poor.pdf', 'h_poor', 'ocr_poor'),"
        "(2, 'electronic', '/m/image.pdf', 'h_image', 'image_only')"
    )
    for p, h in [("/m/poor.pdf", "h_poor"), ("/m/image.pdf", "h_image")]:
        db.execute(
            "INSERT INTO sweep_state (path, size, mtime, file_hash) "
            "VALUES (?, 1, 1.0, ?)", (p, h),
        )
    db.commit()

    rp = reset_for_reprocess(db, statuses=("image_only",),
                             include_null=False)
    assert rp.holdings_targeted == 1
    # ocr_poor's state survives — we asked for image_only only.
    assert db.execute(
        "SELECT count(*) FROM sweep_state WHERE file_hash='h_poor'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT count(*) FROM sweep_state WHERE file_hash='h_image'"
    ).fetchone()[0] == 0


def test_reprocess_include_null_flag(env):
    """A holding with text_status IS NULL should be picked up iff
    `include_null=True` — independent of the `statuses` tuple."""
    from catalogue.services.sweep import reset_for_reprocess
    _, db, _ = env
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'a')")
    db.execute(
        "INSERT INTO holding (edition_id, form, file_path, file_hash, "
        "text_status) VALUES "
        "(1, 'electronic', '/m/x.pdf', 'h_x', NULL)"
    )
    db.execute(
        "INSERT INTO sweep_state (path, size, mtime, file_hash) "
        "VALUES ('/m/x.pdf', 1, 1.0, 'h_x')"
    )
    db.commit()

    rp_off = reset_for_reprocess(db, statuses=(), include_null=False)
    assert rp_off.holdings_targeted == 0

    rp_on = reset_for_reprocess(db, statuses=(), include_null=True)
    assert rp_on.holdings_targeted == 1


def test_count_by_status_breaks_down_incomplete_buckets(env):
    from catalogue.services.sweep import count_by_status
    _, db, _ = env
    db.execute(
        "INSERT INTO edition (id, title) VALUES (1, 'a'), (2, 'b'), (3, 'c')"
    )
    db.execute(
        "INSERT INTO holding (edition_id, form, text_status) VALUES "
        "(1, 'electronic', 'ocr_good'),"     # excluded
        "(2, 'electronic', 'image_only'),"
        "(3, 'electronic', NULL)"
    )
    db.commit()
    c = count_by_status(db)
    assert c == {"image_only": 1, "ocr_poor": 0, "none": 0, "NULL": 1}


def test_workers_1_uses_serial_path_and_respects_custom_extractor(env):
    """The serial path is the only one that honors a custom
    `cfg.extractor` (closures don't pickle). `workers=1` must dispatch
    there even though parallel code exists."""
    mount, db, _ = env
    _make_epub(mount / "x.epub", "<p>seed</p>")

    seen: list[Path] = []

    def _custom_extractor(p):
        seen.append(p)
        from catalogue.services.extract import extract as _orig
        return _orig(p)

    rep = sweep(db, SweepConfig(mount_root=mount, extractor=_custom_extractor),
                workers=1)
    assert rep.scanned == 1
    assert len(seen) == 1   # serial path used our custom extractor
