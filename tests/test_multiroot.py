"""Multiple library mount roots (catalogue/domain/mount.py multi-root API +
holding.root_id + per-root subject derivation + the localhost-gated /settings UI).

See [[mount-root-settings]] and [[sensitive-settings-localhost-only]]. Roots are
validated never to nest/overlap/share-a-prefix or span servers; each holding is
attributed to its owning root by longest prefix; a root with derive_subject OFF
sends ingested files to the Uncategorized net; removal is refused while a root
still owns holdings; and all of it is changeable only from localhost.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import connect, init_db
from catalogue.services import mount, reconcile, subjects
from catalogue.webui.web import create_app


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def roots_vocab(tmp_path, monkeypatch):
    """A throwaway vocab.json + two real on-disk root folders A (derive on) and
    B (derive off), wired into mount.py."""
    a = tmp_path / "A"; (a / "Sub").mkdir(parents=True)
    b = tmp_path / "B"; b.mkdir()
    p = tmp_path / "vocab.json"
    p.write_text(json.dumps({
        "_library_roots": [
            {"id": 1, "path": str(a), "derive_subject": True},
            {"id": 2, "path": str(b), "derive_subject": False},
        ], "_library_root": str(a), "_features": {}}, indent=2))
    monkeypatch.setattr(mount, "VOCAB_PATH", p)
    return {"vocab": p, "A": str(a), "B": str(b), "tmp": tmp_path}


# ── reading / attribution ─────────────────────────────────────────────────────
def test_library_roots_parsed(roots_vocab):
    rs = mount.library_roots()
    assert [(r.id, r.path, r.derive_subject) for r in rs] == [
        (1, roots_vocab["A"], True), (2, roots_vocab["B"], False)]


def test_owning_root_longest_prefix(roots_vocab):
    A, B = roots_vocab["A"], roots_vocab["B"]
    assert mount.owning_root_id(f"{A}/Sub/book.pdf") == 1
    assert mount.owning_root_id(f"{B}/book.pdf") == 2
    assert mount.owning_root_id("/elsewhere/book.pdf") is None


def test_legacy_single_root_fallback(tmp_path, monkeypatch):
    p = tmp_path / "v.json"
    p.write_text(json.dumps({"_library_root": "/old/root", "_features": {}}))
    monkeypatch.setattr(mount, "VOCAB_PATH", p)
    rs = mount.library_roots()
    assert len(rs) == 1 and rs[0].id == 1 and rs[0].path == "/old/root"


# ── validation ────────────────────────────────────────────────────────────────
def test_reject_nested_root(roots_vocab):
    with pytest.raises(mount.RootError):
        mount.validate_new_root(roots_vocab["A"] + "/Sub")


def test_reject_shared_string_prefix(roots_vocab):
    # /…/A vs /…/Alibrary share a string prefix though neither nests the other.
    sibling = roots_vocab["tmp"] / "Alibrary"; sibling.mkdir()
    with pytest.raises(mount.RootError):
        mount.validate_new_root(str(sibling))


def test_reject_nonexistent_and_relative(roots_vocab):
    with pytest.raises(mount.RootError):
        mount.validate_new_root("/no/such/dir/anywhere")
    with pytest.raises(mount.RootError):
        mount.validate_new_root("relative/path")


def test_accept_distinct_sibling(roots_vocab):
    c = roots_vocab["tmp"] / "C"; c.mkdir()
    assert mount.validate_new_root(str(c)) == str(c)


def test_reject_different_server(roots_vocab, monkeypatch):
    # Existing roots look like they're on a webdav mount 'kdrive'; a new local root
    # is on 'local' → a config spanning two servers is rejected.
    monkeypatch.setattr(
        mount, "_server_of",
        lambda path: "kdrive" if (roots_vocab["A"] in path or roots_vocab["B"] in path)
        else "local")
    c = roots_vocab["tmp"] / "C"; c.mkdir()
    with pytest.raises(mount.RootError):
        mount.validate_new_root(str(c))


# ── add / derive-toggle / remove (vocab writers) ──────────────────────────────
def test_add_root_assigns_next_id_and_persists(roots_vocab):
    c = roots_vocab["tmp"] / "C"; c.mkdir()
    new = mount.add_root(str(c), derive_subject=False)
    assert new.id == 3
    data = json.loads(roots_vocab["vocab"].read_text())
    assert [r["id"] for r in data["_library_roots"]] == [1, 2, 3]
    assert data["_library_root"] == roots_vocab["A"]      # primary kept in sync


def test_set_derive_subject_persists(roots_vocab):
    mount.set_derive_subject(2, True)
    assert mount.library_roots()[1].derive_subject is True


def test_remove_root_blocked_when_holdings_present(roots_vocab, tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = db.execute("INSERT INTO edition (title) VALUES ('x')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, root_id) "
               "VALUES (?, 'electronic', ?, 2)", (eid, roots_vocab["B"] + "/b.pdf"))
    db.commit()
    with pytest.raises(mount.RootError):
        mount.remove_root(db, 2)
    assert any(r.id == 2 for r in mount.library_roots())   # still there
    db.close()


def test_remove_root_allowed_when_empty(roots_vocab, tmp_path):
    db = init_db(tmp_path / "c.db")
    mount.remove_root(db, 2)
    assert [r.id for r in mount.library_roots()] == [1]
    db.close()


# ── per-root repoint isolation ────────────────────────────────────────────────
def test_repoint_root_only_moves_its_holdings(roots_vocab, tmp_path):
    db = init_db(tmp_path / "c.db")
    A, B = roots_vocab["A"], roots_vocab["B"]
    eid = db.execute("INSERT INTO edition (title) VALUES ('x')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, file_hash, root_id) "
               "VALUES (?, 'electronic', ?, 'h1', 1)", (eid, f"{A}/Sub/a.pdf"))
    db.execute("INSERT INTO holding (edition_id, form, file_path, file_hash, root_id) "
               "VALUES (?, 'electronic', ?, 'h2', 2)", (eid, f"{B}/b.pdf"))
    db.commit()
    newA = tmp_path / "A2"; (newA / "Sub").mkdir(parents=True)
    mount.repoint_root(db, 1, str(newA), rehash=False)
    paths = {rid: fp for rid, fp in
             db.execute("SELECT root_id, file_path FROM holding").fetchall()}
    assert paths[1] == f"{newA}/Sub/a.pdf"                # root 1 moved
    assert paths[2] == f"{B}/b.pdf"                       # root 2 untouched
    # vocab updated for root 1 only
    assert mount.library_roots()[0].path == str(newA)
    assert mount.library_roots()[1].path == B
    db.close()


# ── per-root subject derivation ───────────────────────────────────────────────
def _edition_with_holding(db, path):
    eid = db.execute("INSERT INTO edition (title) VALUES ('t')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, root_id) "
               "VALUES (?, 'electronic', ?, ?)",
               (eid, path, mount.owning_root_id(path)))
    db.commit()
    return eid


def test_subject_derives_for_derive_on_root(roots_vocab, tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition_with_holding(db, roots_vocab["A"] + "/Madhyamaka/x.pdf")
    assert subjects.suggest_edition_subject(db, eid) == "Madhyamaka"
    db.close()


def test_no_subject_for_derive_off_root(roots_vocab, tmp_path):
    db = init_db(tmp_path / "c.db")
    eid = _edition_with_holding(db, roots_vocab["B"] + "/Personal/x.pdf")
    assert subjects.suggest_edition_subject(db, eid) is None   # → Uncategorized net
    db.close()


# ── localhost gating of sensitive settings ────────────────────────────────────
@pytest.fixture
def app(roots_vocab, tmp_path, monkeypatch):
    monkeypatch.delenv("CATALOGUE_MOUNT_ROOT", raising=False)
    monkeypatch.delenv("CATALOGUE_LIBRARY_ROOT", raising=False)
    a = create_app(tmp_path / "web.db")
    a.testing = True
    return a


def test_settings_root_routes_blocked_for_remote(app, roots_vocab):
    c = roots_vocab["tmp"] / "C"; c.mkdir()
    with app.test_client() as cli:
        # A non-localhost client is refused (403) on every sensitive route.
        r = cli.post("/settings/roots/add", data={"path": str(c)},
                     environ_overrides={"REMOTE_ADDR": "10.0.0.9"})
        assert r.status_code == 403
        r = cli.post("/settings/roots/2/remove",
                     environ_overrides={"REMOTE_ADDR": "10.0.0.9"})
        assert r.status_code == 403
        r = cli.post("/settings/browse", environ_overrides={"REMOTE_ADDR": "10.0.0.9"})
        assert r.status_code == 403
    # config unchanged
    assert [r.id for r in mount.library_roots()] == [1, 2]


def test_settings_add_root_from_localhost(app, roots_vocab):
    c = roots_vocab["tmp"] / "C"; c.mkdir()
    with app.test_client() as cli:
        r = cli.post("/settings/roots/add",
                     data={"path": str(c), "derive_subject": "1"})
    assert r.status_code == 200
    assert [r.path for r in mount.library_roots()][-1] == str(c)


def test_settings_add_overlapping_root_shows_error(app, roots_vocab):
    with app.test_client() as cli:
        r = cli.post("/settings/roots/add",
                     data={"path": roots_vocab["A"] + "/Sub"})
    assert r.status_code == 200 and b"overlap" in r.data.lower()
    assert [r.id for r in mount.library_roots()] == [1, 2]   # not added


def test_remote_settings_page_hides_mount_roots(app, roots_vocab):
    # Sensitive mount-root settings are omitted ENTIRELY on a remote/mobile device (not merely
    # shown read-only) — only the per-device preferences remain.
    with app.test_client() as cli:
        r = cli.get("/settings", environ_overrides={"REMOTE_ADDR": "10.0.0.9"})
    assert r.status_code == 200
    assert b"Library mount roots" not in r.data           # whole section omitted
    assert b"Add a library root" not in r.data            # no add control for remote
    assert b"Device preferences" in r.data                # device prefs still shown


# ── scan form: absolute-path enforcement with >1 root ─────────────────────────
def test_scan_rejects_relative_with_multiple_roots(app, roots_vocab, monkeypatch):
    called = {}
    monkeypatch.setattr(reconcile, "scan_dir",
                        lambda db, roots, **k: called.setdefault("roots", roots) or [])
    monkeypatch.setattr(reconcile, "reconcile", lambda db, scanned, **k: {})
    with app.test_client() as cli:
        r = cli.post("/reconcile/run", data={"roots": "Tantra"})
    assert r.status_code == 302 and "scan_error" in r.headers["Location"]
    assert "roots" not in called                          # never scanned


def test_scan_accepts_absolute_with_multiple_roots(app, roots_vocab, monkeypatch):
    called = {}
    monkeypatch.setattr(reconcile, "reconcile_stream",
                        lambda db, roots, **k: called.setdefault("roots", roots) or {})
    abs_path = roots_vocab["A"] + "/Tantra"
    with app.test_client() as cli:
        cli.post("/reconcile/run", data={"roots": abs_path})
    assert called["roots"] == [abs_path]
