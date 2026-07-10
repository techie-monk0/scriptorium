"""System tests for the /settings mount-root flow (the operator-facing surface of
catalogue/domain/mount.py) and the relative-to-mount-root scan form.

Black-box through the Flask test client: GET the page, request a root change, and
confirm the re-point vs re-scan branches behave — so a moved library is fixed from
the UI without re-importing.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from catalogue.db_store import connect
from catalogue.services import mount, reconcile, skip, filing
from catalogue.webui.web import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    # A throwaway vocab so the routes read/write a test mount root, not the real one.
    vp = tmp_path / "vocab.json"
    old_root = tmp_path / "old"; old_root.mkdir()
    # `_inbox_dirs: []` = explicitly no inbox (so the built-in default doesn't leak into
    # scan-set / inbox-folder assertions); the inbox tests configure it as they need.
    vp.write_text(json.dumps({"_library_root": str(old_root), "_features": {},
                              "_inbox_dirs": []}, indent=2))
    monkeypatch.setattr(mount, "VOCAB_PATH", vp)
    monkeypatch.setattr(filing, "VOCAB_PATH", vp)       # inbox-folder setting reads/writes here
    monkeypatch.delenv("CATALOGUE_MOUNT_ROOT", raising=False)
    monkeypatch.delenv("CATALOGUE_LIBRARY_ROOT", raising=False)
    a = create_app(tmp_path / "web.db")
    a.testing = True
    a.config["_OLD_ROOT"] = str(old_root)
    a.config["_VOCAB"] = vp
    return a


def _seed(app, rel_path, fhash):
    conn = connect(app.config["DB_PATH"])
    eid = conn.execute("INSERT INTO edition (title, isbn) VALUES ('B', '')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path, file_hash, "
                 "content_hash, text_status) VALUES (?, 'electronic', ?, ?, 't:c', 'ocr_good')",
                 (eid, f"{app.config['_OLD_ROOT']}/{rel_path}", fhash))
    conn.commit(); conn.close()


def test_settings_page_shows_current_root(app):
    with app.test_client() as c:
        r = c.get("/settings")
    assert r.status_code == 200
    assert app.config["_OLD_ROOT"].encode() in r.data
    assert b"Library mount root" in r.data


def test_change_to_nonexistent_dir_errors(app):
    with app.test_client() as c:
        r = c.post("/settings/mount-root", data={"mount_root": "/no/such/place"})
    assert r.status_code == 200 and b"No such directory" in r.data
    assert mount.current_mount_root() == app.config["_OLD_ROOT"]   # unchanged


# ── inbox folders setting ──────────────────────────────────────────────────────
def test_inbox_dirs_add_remove_roundtrip(app, tmp_path):
    drop = tmp_path / "_INBOX"; drop.mkdir()
    with app.test_client() as c:
        r = c.post("/settings/inbox-dirs/add", data={"path": str(drop)})
        assert r.status_code == 200 and b"Inbox folder added" in r.data
        assert filing.inbox_dirs() == [str(drop)]
        assert str(drop).encode() in c.get("/settings").data   # listed on the page

        r = c.post("/settings/inbox-dirs/remove", data={"path": str(drop)})
        assert r.status_code == 200 and b"Inbox folder removed" in r.data
        assert filing.inbox_dirs() == []


def test_inbox_dirs_add_rejects_missing_dir(app):
    with app.test_client() as c:
        r = c.post("/settings/inbox-dirs/add", data={"path": "/no/such/inbox"})
    assert r.status_code == 200 and b"No such directory" in r.data
    assert filing.inbox_dirs() == []                 # fixture configures none; nothing added


def test_inbox_dirs_add_is_localhost_only(app, monkeypatch):
    import catalogue.webui.routes.settings as S
    monkeypatch.setattr(S, "_is_local", lambda: False)
    with app.test_client() as c:
        r = c.post("/settings/inbox-dirs/add", data={"path": "/x"})
    assert r.status_code == 403


def test_change_root_previews_present_and_missing(app, tmp_path):
    new = tmp_path / "kDrive 2"; new.mkdir()
    (new / "a.pdf").write_bytes(b"A")                  # present; b.pdf will be missing
    _seed(app, "a.pdf", "stale")
    _seed(app, "b.pdf", "stale")
    with app.test_client() as c:
        r = c.post("/settings/mount-root", data={"mount_root": str(new)})
    assert r.status_code == 200
    assert b"Is this the same library" in r.data       # confirm screen
    assert b"1" in r.data                              # 1 present / 1 missing
    assert mount.current_mount_root() == app.config["_OLD_ROOT"]   # not applied yet


def test_apply_repoint_moves_and_rehashes(app, tmp_path):
    new = tmp_path / "kDrive 2"; new.mkdir()
    body = b"%PDF rewritten on re-sync"
    (new / "a.pdf").write_bytes(body)
    _seed(app, "a.pdf", "stalehash")
    with app.test_client() as c:
        r = c.post("/settings/mount-root/apply",
                   data={"mode": "repoint", "new_root": str(new)})
    assert r.status_code == 200 and b"Re-pointed to the new location" in r.data
    # holding now points at the new file and carries its real byte-hash
    conn = connect(app.config["DB_PATH"])
    fp, fh = conn.execute("SELECT file_path, file_hash FROM holding").fetchone()
    conn.close()
    assert fp == str(new / "a.pdf")
    assert fh == hashlib.sha256(body).hexdigest()
    assert mount.current_mount_root() == str(new)      # config updated
    # a DB snapshot was taken before the bulk write
    assert list(tmp_path.glob("web-backup-*.db")), "expected a pre-repoint snapshot"


def test_apply_rescan_sets_root_and_redirects_to_scan(app, tmp_path):
    new = tmp_path / "fresh"; new.mkdir()
    with app.test_client() as c:
        r = c.post("/settings/mount-root/apply",
                   data={"mode": "rescan", "new_root": str(new)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/reconcile")
    assert mount.current_mount_root() == str(new)


def test_scan_form_joins_subpaths_to_mount_root(app, monkeypatch):
    captured = {}
    monkeypatch.setattr(reconcile, "reconcile_stream",
                        lambda db, roots, **k: captured.setdefault("roots", roots) or {})
    with app.test_client() as c:
        c.post("/reconcile/run", data={"roots": "Tantra\nPoetry /Sounds"})
    root = app.config["_OLD_ROOT"]
    assert captured["roots"] == [f"{root}/Tantra", f"{root}/Poetry /Sounds"]


def test_scan_form_blank_scans_whole_mount_root(app, monkeypatch):
    captured = {}
    monkeypatch.setattr(reconcile, "reconcile_stream",
                        lambda db, roots, **k: captured.setdefault("roots", roots) or {})
    with app.test_client() as c:
        c.post("/reconcile/run", data={"roots": ""})
    assert captured["roots"] == [app.config["_OLD_ROOT"]]


# ── per-root folder exclusion tree ────────────────────────────────────────────
def _wire_vocab_reads(app, monkeypatch):
    """Point the exclusion READ path (skip → db.db.VOCAB_PATH) at the same throwaway
    vocab the routes WRITE to (mount.VOCAB_PATH), so toggles round-trip in-test."""
    import catalogue.db_store.db as dbmod
    monkeypatch.setattr(dbmod, "VOCAB_PATH", app.config["_VOCAB"])
    skip.exclusion_rules.cache_clear()


def test_folders_lists_subdirs_name_sorted(app, monkeypatch):
    _wire_vocab_reads(app, monkeypatch)
    root = app.config["_OLD_ROOT"]
    os.makedirs(root + "/Tantra/Restricted")
    os.makedirs(root + "/Poetry")
    with app.test_client() as c:
        j = c.get("/settings/roots/1/folders").get_json()
    assert [ch["name"] for ch in j["children"]] == ["Poetry", "Tantra"]
    tantra = next(ch for ch in j["children"] if ch["name"] == "Tantra")
    assert tantra["has_children"] and not tantra["excluded"] and not tantra["locked"]


def test_folders_unknown_root_404(app):
    with app.test_client() as c:
        assert c.get("/settings/roots/99/folders").status_code == 404


def test_exclude_rejects_path_outside_root(app):
    with app.test_client() as c:
        r = c.post("/settings/roots/1/exclude", data={"path": "/etc", "excluded": "1"})
    assert r.status_code == 400


def test_exclude_folder_marks_subtree_and_purges(app, monkeypatch, tmp_path):
    _wire_vocab_reads(app, monkeypatch)
    root = app.config["_OLD_ROOT"]
    os.makedirs(root + "/Tantra/Restricted")
    conn = connect(app.config["DB_PATH"])           # a catalogued file under the folder to exclude
    eid = conn.execute("INSERT INTO edition (title) VALUES ('T')").lastrowid
    conn.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
                 (eid, root + "/Tantra/secret.pdf"))
    conn.commit(); conn.close()

    with app.test_client() as c:
        j = c.post("/settings/roots/1/exclude",
                   data={"path": root + "/Tantra", "excluded": "1"}).get_json()
        assert j["ok"] and j["excluded"] and j["excluded_count"] == 1
        # the folder now reports excluded, and its children are locked (inherit exclusion)
        top = c.get("/settings/roots/1/folders").get_json()
        assert next(ch for ch in top["children"] if ch["name"] == "Tantra")["excluded"]
        kids = c.get("/settings/roots/1/folders",
                     query_string={"path": root + "/Tantra"}).get_json()
        assert kids["locked"] and all(ch["locked"] for ch in kids["children"])
        # purge clears the already-ingested holding, after snapshotting the DB
        pr = c.post("/settings/exclusions/purge")
    assert b"Removed" in pr.data
    conn = connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 0
    conn.close()
    assert list(tmp_path.glob("web-backup-*.db")), "expected a pre-purge DB snapshot"


def test_exclude_clears_pending_scan_items(app, monkeypatch):
    _wire_vocab_reads(app, monkeypatch)
    root = app.config["_OLD_ROOT"]
    os.makedirs(root + "/Tantra")
    conn = connect(app.config["DB_PATH"])           # two already-scanned 'new' files
    for p in (root + "/Tantra/new.pdf", root + "/Sutra/keep.pdf"):
        conn.execute("INSERT INTO review_queue (item_type, payload_json) VALUES ('ingest', ?)",
                     (json.dumps({"kind": "new", "path": p}),))
    conn.commit(); conn.close()

    with app.test_client() as c:
        j = c.post("/settings/roots/1/exclude",
                   data={"path": root + "/Tantra", "excluded": "1"}).get_json()
    assert j["pending_removed"] == 1                 # the Tantra scan item is gone
    conn = connect(app.config["DB_PATH"])
    paths = [json.loads(pj)["path"] for (pj,) in conn.execute(
        "SELECT payload_json FROM review_queue WHERE status='pending'").fetchall()]
    conn.close()
    assert paths == [root + "/Sutra/keep.pdf"]       # the un-excluded one stays


def test_exclude_then_reinclude_round_trips(app, monkeypatch):
    _wire_vocab_reads(app, monkeypatch)
    root = app.config["_OLD_ROOT"]
    os.makedirs(root + "/Poetry")
    with app.test_client() as c:
        c.post("/settings/roots/1/exclude", data={"path": root + "/Poetry", "excluded": "1"})
        assert skip.is_excluded(file_path=root + "/Poetry/a.pdf")
        j = c.post("/settings/roots/1/exclude",
                   data={"path": root + "/Poetry", "excluded": "0"}).get_json()
    assert j["excluded"] is False
    assert not skip.is_excluded(file_path=root + "/Poetry/a.pdf")
