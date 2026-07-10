"""Tests for the library mount root + safe re-pointing (catalogue/domain/mount.py)
and the /settings change-root flow.

A moved mount (cloud client re-syncs to a renamed folder) leaves stored absolute
paths stale so every book looks "new". `repoint` prefix-swaps the paths and
re-hashes the bytes now on disk so unchanged content stays unchanged — these tests
pin that, the read-only preview, the single-source-of-truth resolution, and the
web confirm/apply flow.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from catalogue.db_store import init_db
from catalogue.services import mount, sweep, reconcile


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "m.db")
    yield conn
    conn.close()


@pytest.fixture
def vocab(tmp_path, monkeypatch):
    """A throwaway vocab.json holding just `_library_root`, wired into mount.py so
    set/current_mount_root don't touch the real config."""
    p = tmp_path / "vocab.json"
    p.write_text(json.dumps({"_library_root": "/old/root", "_features": {}}, indent=2))
    monkeypatch.setattr(mount, "VOCAB_PATH", p)
    return p


def _book(db, *, path, fhash, chash="t:c", title="B"):
    eid = db.execute("INSERT INTO edition (title, isbn) VALUES (?, '')", (title,)).lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, file_hash, "
               "content_hash, text_status) VALUES (?, 'electronic', ?, ?, ?, 'ocr_good')",
               (eid, path, fhash, chash))
    return eid


# ── trash folder (deleted book files moved here, not unlinked) ───────────────
def test_trash_dir_defaults_when_unset(vocab):
    assert mount.trash_dir() == mount.DEFAULT_TRASH_DIR


def test_set_trash_dir_roundtrips_and_trims_slash(vocab):
    mount.set_trash_dir("/Users/x/kDrive 2/Trash/")
    assert mount.trash_dir() == "/Users/x/kDrive 2/Trash"
    data = json.loads(vocab.read_text())                 # other keys untouched
    assert data["_features"] == {} and data["_trash_dir"] == "/Users/x/kDrive 2/Trash"


def test_move_to_trash_moves_file_and_creates_dir(tmp_path):
    trash = tmp_path / "Trash"                            # not yet created
    src = tmp_path / "book.pdf"
    src.write_bytes(b"%PDF stub")
    dest = mount.move_to_trash(str(src), str(trash))
    assert dest == str(trash / "book.pdf")
    assert not src.exists() and (trash / "book.pdf").read_bytes() == b"%PDF stub"


def test_move_to_trash_suffixes_on_collision(tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    (trash / "book.pdf").write_bytes(b"existing")         # name already taken
    src = tmp_path / "book.pdf"
    src.write_bytes(b"new")
    dest = mount.move_to_trash(str(src), str(trash))
    assert dest == str(trash / "book (2).pdf")
    assert (trash / "book.pdf").read_bytes() == b"existing"  # earlier file untouched


def test_move_to_trash_missing_source_is_noop(tmp_path):
    assert mount.move_to_trash(str(tmp_path / "nope.pdf"), str(tmp_path / "Trash")) is None


# ── set/current_mount_root ──────────────────────────────────────────────────
def test_set_mount_root_surgical_and_trims_slash(vocab):
    assert mount.current_mount_root() == "/old/root"
    mount.set_mount_root("/Users/x/kDrive 2/Books/")     # trailing slash trimmed
    assert mount.current_mount_root() == "/Users/x/kDrive 2/Books"
    data = json.loads(vocab.read_text())                 # other keys untouched
    assert data["_features"] == {} and data["_library_root"] == "/Users/x/kDrive 2/Books"


def test_set_mount_root_preserves_unrelated_formatting(tmp_path, monkeypatch):
    # A vocab with a compact inline object — a json round-trip would reformat it; the
    # surgical replace must leave every other byte alone.
    raw = ('{\n  "_library_root": "/a",\n'
           '  "_x": [{ "code": "k", "label": "v" }]\n}\n')
    p = tmp_path / "v.json"; p.write_text(raw)
    monkeypatch.setattr(mount, "VOCAB_PATH", p)
    mount.set_mount_root("/b")
    assert p.read_text() == raw.replace('"/a"', '"/b"')   # exactly one byte-run changed


# ── plan_repoint (read-only preview) ──────────────────────────────────────────
def test_plan_repoint_counts_present_and_missing(db, tmp_path):
    new = tmp_path / "new"; (new / "sub").mkdir(parents=True)
    (new / "a.pdf").write_bytes(b"A")
    (new / "sub" / "b.pdf").write_bytes(b"B")             # c.pdf intentionally absent
    for name in ("a.pdf", "sub/b.pdf", "c.pdf"):
        _book(db, path=f"/old/{name}", fhash="h")
    db.commit()
    plan = mount.plan_repoint(db, "/old", str(new))
    assert plan["matched"] == 3 and plan["present"] == 2 and plan["missing"] == 1
    assert plan["sample_missing"] == [str(new / "c.pdf")]


def test_plan_repoint_is_read_only(db, tmp_path):
    _book(db, path="/old/a.pdf", fhash="h"); db.commit()
    mount.plan_repoint(db, "/old", str(tmp_path))
    assert db.execute("SELECT file_path FROM holding").fetchone()[0] == "/old/a.pdf"


# ── repoint ───────────────────────────────────────────────────────────────────
def test_repoint_swaps_path_and_rehashes_changed_bytes(db, tmp_path):
    new = tmp_path / "kDrive 2"; new.mkdir()
    body = b"%PDF-1.7 rewritten by the cloud client"
    (new / "a.pdf").write_bytes(body)
    _book(db, path="/old/a.pdf", fhash="STALEHASH", chash="t:keepme")
    db.commit()
    s = mount.repoint(db, "/old", str(new), rehash=True)
    fp, fh, ch = db.execute("SELECT file_path, file_hash, content_hash FROM holding").fetchone()
    assert fp == str(new / "a.pdf")
    assert fh == _sha(body)                  # re-hashed to the bytes now on disk
    assert ch == "t:keepme"                  # text fingerprint left intact (no re-extract)
    assert s["repointed"] == 1 and s["rehashed"] == 1 and s["bytes_changed"] == 1


def test_repoint_only_touches_holdings_under_old_root(db, tmp_path):
    new = tmp_path / "new"; new.mkdir(); (new / "a.pdf").write_bytes(b"A")
    _book(db, path="/old/a.pdf", fhash="h1")
    _book(db, path="/other/keep.pdf", fhash="h2")        # different root → untouched
    db.commit()
    mount.repoint(db, "/old", str(new), rehash=True)
    paths = {r[0] for r in db.execute("SELECT file_path FROM holding").fetchall()}
    assert str(new / "a.pdf") in paths and "/other/keep.pdf" in paths


def test_repoint_without_rehash_keeps_hash(db, tmp_path):
    new = tmp_path / "new"; new.mkdir(); (new / "a.pdf").write_bytes(b"A")
    _book(db, path="/old/a.pdf", fhash="KEEP")
    db.commit()
    mount.repoint(db, "/old", str(new), rehash=False)
    fp, fh = db.execute("SELECT file_path, file_hash FROM holding").fetchone()
    assert fp == str(new / "a.pdf") and fh == "KEEP"


def test_repoint_clears_stale_sweep_state_and_pending_ingest(db, tmp_path):
    new = tmp_path / "new"; new.mkdir(); (new / "a.pdf").write_bytes(b"A")
    _book(db, path="/old/a.pdf", fhash="h")
    db.execute("INSERT INTO sweep_state (path, size, mtime, file_hash) "
               "VALUES ('/old/a.pdf', 1, 1.0, 'h')")
    db.execute("INSERT INTO sweep_state (path, size, mtime, file_hash) "
               "VALUES ('/other/x.pdf', 1, 1.0, 'k')")    # unrelated → survives
    db.execute("INSERT INTO review_queue (item_type, status, payload_json) "
               "VALUES ('ingest', 'pending', ?)",
               (json.dumps({"kind": "new", "path": str(new / "a.pdf")}),))
    db.commit()
    s = mount.repoint(db, "/old", str(new), rehash=True, drop_pending=True)
    assert s["sweep_state_cleared"] == 1 and s["pending_dropped"] == 1
    assert [r[0] for r in db.execute("SELECT path FROM sweep_state").fetchall()] == ["/other/x.pdf"]
    assert db.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending'").fetchone()[0] == 0


def test_repoint_records_missing_files(db, tmp_path):
    new = tmp_path / "new"; new.mkdir()                   # file NOT created at new root
    _book(db, path="/old/gone.pdf", fhash="h")
    db.commit()
    s = mount.repoint(db, "/old", str(new), rehash=True)
    assert s["missing"] == [str(new / "gone.pdf")]
    assert db.execute("SELECT file_path FROM holding").fetchone()[0] == str(new / "gone.pdf")


# ── single source of truth ────────────────────────────────────────────────────
def test_default_mount_root_reads_vocab(vocab, monkeypatch):
    monkeypatch.delenv("CATALOGUE_MOUNT_ROOT", raising=False)
    assert str(sweep.default_mount_root()) == "/old/root"
    mount.set_mount_root("/Users/x/kDrive 2/Books")
    assert str(sweep.default_mount_root()) == "/Users/x/kDrive 2/Books"


def test_env_overrides_vocab(vocab, monkeypatch):
    monkeypatch.setenv("CATALOGUE_MOUNT_ROOT", "/env/wins")
    assert str(sweep.default_mount_root()) == "/env/wins"


def test_library_roots_follows_mount_root(vocab, monkeypatch):
    monkeypatch.delenv("CATALOGUE_MOUNT_ROOT", raising=False)
    monkeypatch.delenv("CATALOGUE_LIBRARY_ROOT", raising=False)
    mount.set_mount_root("/Users/x/Library")
    assert reconcile.library_roots() == ["/Users/x/Library"]
