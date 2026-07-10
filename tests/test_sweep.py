"""Step-2 regression tests for the WebDAV sweep (§6, §7.1, §13).

Pins the invariants the pipeline must not regress: read-only mount,
change-detect-before-hash, idempotent upsert by file_hash, NFC-before-
validation, per-stage versioned cache, ocr_poor → review queue,
errors logged + sweep continues.
"""
from __future__ import annotations

import json
import os
import shutil
import unicodedata
import zipfile
from pathlib import Path

import pytest

from catalogue.db_store import init_db
from catalogue.services.extract import ExtractedText
from catalogue.services.sweep import SweepConfig, sweep


# ── Helpers ───────────────────────────────────────────────────────────────
def _make_epub(path: Path, body: str = "<p>Hello bodhisattva.</p>") -> None:
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


# ── First sweep finds the file and creates rows ───────────────────────────
def test_first_sweep_creates_edition_and_holding(env):
    mount, db, _ = env
    _make_epub(mount / "way.epub")

    rep = sweep(db, SweepConfig(mount_root=mount))
    assert rep.scanned == 1
    assert rep.new_holdings == 1
    assert rep.ocr_good == 1

    row = db.execute(
        "SELECT e.title, h.form, h.text_status, h.file_hash "
        "FROM holding h JOIN edition e ON e.id = h.edition_id"
    ).fetchone()
    assert row[0] == "way"
    assert row[1] == "electronic"
    assert row[2] == "ocr_good"
    assert row[3] and len(row[3]) == 64  # sha256


def test_raw_text_is_cached_with_versioned_key(env):
    """§5 / §12.3: raw extracted text is persisted per (hash, extract_version)."""
    mount, db, _ = env
    _make_epub(mount / "x.epub")
    sweep(db, SweepConfig(mount_root=mount, extract_version=1))
    rows = db.execute(
        "SELECT extract_version, raw_text FROM raw_extract_cache"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert "bodhisattva" in rows[0][1]


# ── §6: change-detect on (path, size, mtime) BEFORE hashing ───────────────
def test_resweep_skips_unchanged_files_without_rehashing(env, monkeypatch):
    mount, db, _ = env
    _make_epub(mount / "y.epub")
    sweep(db, SweepConfig(mount_root=mount))

    # Second pass: monkey-patch the hasher to FAIL if invoked. If sweep
    # change-detects correctly, the hasher is never called.
    from catalogue.services import sweep as sweep_mod

    def boom(*a, **kw):
        raise AssertionError("hash called on unchanged file — change detection broke")

    monkeypatch.setattr(sweep_mod, "_hash_file", boom)
    rep = sweep(db, SweepConfig(mount_root=mount))
    assert rep.skipped_unchanged == 1
    assert rep.new_holdings == 0


def test_mtime_change_triggers_reprocessing(env):
    mount, db, _ = env
    f = mount / "z.epub"
    _make_epub(f)
    sweep(db, SweepConfig(mount_root=mount))

    # Bump mtime — simulate the file being modified upstream.
    new_t = f.stat().st_mtime + 100
    os.utime(f, (new_t, new_t))

    rep = sweep(db, SweepConfig(mount_root=mount))
    assert rep.skipped_unchanged == 0
    # Same hash (content unchanged) → no new holding, just refreshed state.
    assert rep.new_holdings == 0


# ── Idempotent upsert by file_hash: moved file updates path ───────────────
def test_moved_file_updates_path_not_duplicates(env):
    mount, db, _ = env
    src = mount / "a.epub"
    _make_epub(src)
    sweep(db, SweepConfig(mount_root=mount))

    # "Move" within the mount.
    dst = mount / "sub" / "a.epub"
    dst.parent.mkdir()
    shutil.move(str(src), str(dst))

    rep = sweep(db, SweepConfig(mount_root=mount))
    assert rep.updated_paths == 1
    assert rep.new_holdings == 0

    (n_holdings,) = db.execute("SELECT count(*) FROM holding").fetchone()
    assert n_holdings == 1
    (path,) = db.execute("SELECT file_path FROM holding").fetchone()
    assert path == str(dst)


# ── §4.8c step 2: low quality → review queue, score recorded ──────────────
def test_low_quality_extraction_enqueues_review(env):
    mount, db, tmp = env
    _make_epub(mount / "scan.epub", body="<p>���� \x00 \x01\x02</p>")

    rep = sweep(db, SweepConfig(mount_root=mount, quality_threshold=0.6))
    assert rep.ocr_poor == 1

    row = db.execute(
        "SELECT item_type, payload_json FROM review_queue"
    ).fetchone()
    assert row[0] == "low_quality_ocr"
    payload = json.loads(row[1])
    assert payload["score"] < 0.6
    assert "file_hash" in payload


def test_score_is_stored_on_holding(env):
    mount, db, _ = env
    _make_epub(mount / "good.epub",
               body="<p>" + ("The Bodhicaryāvatāra is a Mahāyāna text. " * 20) + "</p>")
    sweep(db, SweepConfig(mount_root=mount))
    (score, status) = db.execute(
        "SELECT ocr_quality_score, text_status FROM holding"
    ).fetchone()
    assert status == "ocr_good"
    assert score is not None and 0.0 < score <= 1.0


# ── §6, §7.1, §13: mount is treated read-only ─────────────────────────────
def test_sweep_does_not_modify_mount_contents(env):
    mount, db, _ = env
    _make_epub(mount / "ro.epub")

    # Snapshot every (path, size, mtime) under mount before the sweep.
    def snapshot():
        s = {}
        for dp, _d, fs in os.walk(mount):
            for n in fs:
                p = Path(dp) / n
                st = p.stat()
                s[str(p)] = (st.st_size, st.st_mtime)
        return s

    before = snapshot()
    sweep(db, SweepConfig(mount_root=mount))
    after = snapshot()

    # No files added, removed, or mutated under the mount.
    assert before == after


# ── §6: I/O errors → sweep_problem_log, sweep continues ───────────────────
def test_extract_failure_logs_problem_and_continues(env, monkeypatch):
    mount, db, _ = env
    _make_epub(mount / "ok.epub")
    _make_epub(mount / "bad.epub")

    real_extract = __import__("catalogue.services.extract", fromlist=["extract"]).extract

    def flaky(path):
        if path.name == "bad.epub":
            raise OSError("simulated WebDAV blip")
        return real_extract(path)

    rep = sweep(db, SweepConfig(mount_root=mount, extractor=flaky))
    assert rep.errors == 1
    assert rep.new_holdings == 1  # the other file still processed

    (n_problems,) = db.execute("SELECT count(*) FROM sweep_problem_log").fetchone()
    assert n_problems == 1
    (msg,) = db.execute("SELECT message FROM sweep_problem_log").fetchone()
    assert "simulated" in msg


# ── §6, §13: resumability — partial run + resume ──────────────────────────
def test_sweep_is_resumable_after_mid_run_failure(env, monkeypatch):
    mount, db, _ = env
    # Distinct content per file → distinct hashes (identical content would
    # rightly dedupe via the §6 file-hash idempotent upsert).
    _make_epub(mount / "1.epub", body="<p>first book about emptiness</p>")
    _make_epub(mount / "2.epub", body="<p>second book about compassion</p>")
    _make_epub(mount / "3.epub", body="<p>third book about wisdom</p>")

    # First pass: extractor explodes only on file 2 (simulating a disconnect
    # partway through). Per-file commits mean 1 and 3 land in the DB,
    # 2 lands in sweep_problem_log.
    real = __import__("catalogue.services.extract", fromlist=["extract"]).extract
    seen: list[str] = []

    def half_broken(path):
        seen.append(path.name)
        if path.name == "2.epub":
            raise OSError("disconnect")
        return real(path)

    sweep(db, SweepConfig(mount_root=mount, extractor=half_broken))
    (n,) = db.execute("SELECT count(*) FROM holding").fetchone()
    assert n == 2

    # Second pass with a working extractor — only file 2 should be retried.
    second_seen: list[str] = []

    def working(path):
        second_seen.append(path.name)
        return real(path)

    sweep(db, SweepConfig(mount_root=mount, extractor=working))
    # Files 1 and 3 are change-detect-skipped; only 2 is re-extracted.
    assert second_seen == ["2.epub"]
    (n,) = db.execute("SELECT count(*) FROM holding").fetchone()
    assert n == 3


# ── §4.8c step 1: NFC happens BEFORE quality scoring ──────────────────────
def test_decomposed_text_is_nfc_normalized_before_caching(env):
    """If the extractor returned decomposed forms, the cached raw_text would
    silently fail downstream matching. The extractor's NFC pass guarantees
    cached text is in canonical form."""
    mount, db, _ = env
    body = "<p>" + ("ku" + "̄" + "ta of a" + "̄" + "ya. " * 30) + "</p>"
    _make_epub(mount / "nfc.epub", body=body)
    sweep(db, SweepConfig(mount_root=mount))

    (raw,) = db.execute("SELECT raw_text FROM raw_extract_cache").fetchone()
    assert raw == unicodedata.normalize("NFC", raw)
    assert "kūta" not in raw      # decomposed gone
    assert "kūta" in raw                # precomposed present
