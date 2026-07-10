"""Filing reviewed books out of the inbox onto their subject shelf
(catalogue/domain/filing).

Pins the two layers separately: the client-supplied `FilingProtocol` decides WHERE
(empirical modal directory, else a derived `<root>/<leaf>`; auto only when
unambiguous) and the protocol-agnostic `file_edition` executor MOVES — additively,
touching only copies that currently sit in an inbox.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalogue.db_store import init_db
from catalogue.services import filing, mount


# ── fixtures ───────────────────────────────────────────────────────────────────
@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway library tree + vocab + DB. Layout:

        <lib>/Library/_INBOX/                  (inbox)
        <lib>/Library/01 Books - Dharma/       (root, derive_subject off)
              ├─ Tantra/      (existing shelf)
              └─ Emptiness/   (existing shelf)
    """
    lib = tmp_path / "Library"
    inbox = lib / "_INBOX"
    root = lib / "01 Books - Dharma"
    tantra = root / "Tantra"
    emptiness = root / "Emptiness"
    for d in (inbox, tantra, emptiness):
        d.mkdir(parents=True)

    vocab = tmp_path / "vocab.json"
    vocab.write_text(json.dumps({
        "_library_roots": [{"id": 1, "path": str(root), "derive_subject": False}],
        "_inbox_dirs": [str(inbox)]}))           # inbox membership is by configured folder
    monkeypatch.setattr(mount, "VOCAB_PATH", vocab)
    monkeypatch.setattr(filing, "VOCAB_PATH", vocab)

    conn = init_db(tmp_path / "f.db")
    yield conn, {"lib": lib, "inbox": inbox, "root": root,
                 "tantra": tantra, "emptiness": emptiness}
    conn.close()


def _subject(db, name, kind="topic"):
    return db.execute("INSERT INTO subject (name, kind) VALUES (?, ?)",
                      (name, kind)).lastrowid


def _book(db, *, title, path, subjects=(), volume_set_id=None):
    eid = db.execute(
        "INSERT INTO edition (title, isbn, volume_set_id) VALUES (?, '', ?)",
        (title, volume_set_id)).lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
               "VALUES (?, 'electronic', ?, 'ocr_good')", (eid, str(path)))
    for sname in subjects:
        sid = db.execute("SELECT id FROM subject WHERE name = ?", (sname,)).fetchone()[0]
        db.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?, ?)",
                   (eid, sid))
    db.commit()
    return eid


# ── is_in_inbox: the additive guard (by CONFIGURED folder, no magic name) ────────
def test_is_in_inbox_matches_configured_folder(env):
    conn, p = env                              # env configures _inbox_dirs = [<lib>/_INBOX]
    assert filing.is_in_inbox(str(p["inbox"] / "a.pdf"))
    assert filing.is_in_inbox(str(p["inbox"] / "sub" / "a.pdf"))   # nested under the inbox
    assert not filing.is_in_inbox(str(p["tantra"] / "a.pdf"))      # a shelf, not the inbox
    assert not filing.is_in_inbox(None)


def test_is_in_inbox_is_a_path_boundary(env):
    conn, p = env
    vocab = filing.VOCAB_PATH
    data = json.loads(vocab.read_text())
    data["_inbox_dirs"] = [str(p["lib"] / "Drop")]
    vocab.write_text(json.dumps(data))
    assert filing.is_in_inbox(str(p["lib"] / "Drop" / "x.pdf"))
    assert not filing.is_in_inbox(str(p["lib"] / "Dropbox" / "x.pdf"))   # prefix, not boundary


def test_inbox_dirs_defaults_when_unset(env):
    conn, p = env
    vocab = filing.VOCAB_PATH
    vocab.write_text(json.dumps({"_features": {}}))      # no _inbox_dirs key
    # Falls back to the built-in default when one is set; an empty default (no inbox
    # configured, e.g. a fresh public install) yields [], not a bogus "" folder.
    expected = [filing._norm_dir(filing.DEFAULT_INBOX_DIR)] if filing.DEFAULT_INBOX_DIR else []
    assert filing.inbox_dirs() == expected


# ── protocol selection (client provides the protocol) ───────────────────────────
def test_get_protocol_default_and_unknown():
    assert isinstance(filing.get_protocol(), filing.EmpiricalFilingProtocol)
    assert isinstance(filing.get_protocol("empirical"), filing.EmpiricalFilingProtocol)
    assert isinstance(filing.get_protocol("nope"), filing.EmpiricalFilingProtocol)


def test_plan_filing_accepts_a_custom_protocol_instance(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    eid = _book(conn, title="X", path=p["inbox"] / "X.pdf", subjects=["Buddhism/Tantra"])

    sentinel = filing.FilingPlan(auto=True,
                                 destination=filing.Destination("/custom", "x", 9, True))

    class Custom(filing.FilingProtocol):
        def plan(self, db, ctx):
            return sentinel

    assert filing.plan_filing(conn, eid, Custom()) is sentinel


# ── directory resolution: empirical, then derive ────────────────────────────────
def test_subject_directory_is_the_modal_existing_dir(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    _book(conn, title="A", path=p["tantra"] / "A.pdf", subjects=["Buddhism/Tantra"])
    _book(conn, title="B", path=p["tantra"] / "B.pdf", subjects=["Buddhism/Tantra"])
    # an inbox copy of the subject must NOT count toward the modal dir
    _book(conn, title="C", path=p["inbox"] / "C.pdf", subjects=["Buddhism/Tantra"])

    d = filing.subject_directory(conn, "Buddhism/Tantra")
    assert d is not None and d.path == str(p["tantra"]) and d.n_books == 2 and d.exists


def test_derive_directory_for_a_brand_new_subject(env):
    conn, p = env
    _subject(conn, "Buddhism/Tenets")                    # no books yet
    d = filing.derive_directory(conn, "Buddhism/Tenets")
    assert d is not None
    assert d.path == str(p["root"] / "Tenets")
    assert d.n_books == 0 and d.exists is False          # folder not created yet


def test_derive_directory_none_for_unmapped_top_level(env):
    conn, _ = env
    assert filing.derive_directory(conn, "Cooking") is None


# ── plan_filing: auto vs confirm ─────────────────────────────────────────────────
def test_plan_auto_when_single_subject_existing_dir(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    _book(conn, title="Filed", path=p["tantra"] / "Filed.pdf", subjects=["Buddhism/Tantra"])
    eid = _book(conn, title="New", path=p["inbox"] / "New.pdf", subjects=["Buddhism/Tantra"])

    plan = filing.plan_filing(conn, eid)
    assert plan.auto and plan.destination.path == str(p["tantra"])


def test_plan_confirms_when_multiple_subjects(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    _subject(conn, "Buddhism/Emptiness")
    _book(conn, title="T", path=p["tantra"] / "T.pdf", subjects=["Buddhism/Tantra"])
    _book(conn, title="E", path=p["emptiness"] / "E.pdf", subjects=["Buddhism/Emptiness"])
    eid = _book(conn, title="Both", path=p["inbox"] / "Both.pdf",
                subjects=["Buddhism/Tantra", "Buddhism/Emptiness"])

    plan = filing.plan_filing(conn, eid)
    assert not plan.auto
    paths = {c.path for c in plan.candidates}
    assert paths == {str(p["tantra"]), str(p["emptiness"])}


def test_plan_confirms_for_new_subject_with_derived_dir(env):
    conn, p = env
    _subject(conn, "Buddhism/Tenets")
    eid = _book(conn, title="N", path=p["inbox"] / "N.pdf", subjects=["Buddhism/Tenets"])
    plan = filing.plan_filing(conn, eid)
    assert not plan.auto                                 # dir does not exist yet → confirm
    assert plan.candidates[0].path == str(p["root"] / "Tenets")
    assert plan.candidates[0].exists is False


def test_plan_offers_volume_set_sibling_directory(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    series_dir = p["root"] / "Kalachakra Set"
    series_dir.mkdir()
    _book(conn, title="Vol 1", path=series_dir / "v1.pdf",
          subjects=["Buddhism/Tantra"], volume_set_id=77)
    eid = _book(conn, title="Vol 2", path=p["inbox"] / "v2.pdf",
                subjects=["Buddhism/Tantra"], volume_set_id=77)

    plan = filing.plan_filing(conn, eid)
    series_cands = [c for c in plan.candidates if c.path == str(series_dir)]
    assert series_cands and series_cands[0].is_series


# ── file_edition: the additive executor ──────────────────────────────────────────
def test_file_edition_moves_inbox_copy_and_repoints(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    eid = _book(conn, title="New", path=p["inbox"] / "New.pdf", subjects=["Buddhism/Tantra"])
    (p["inbox"] / "New.pdf").write_bytes(b"%PDF data")

    rep = filing.file_edition(conn, eid, str(p["tantra"]))
    assert len(rep["moved"]) == 1
    assert not (p["inbox"] / "New.pdf").exists()
    moved = p["tantra"] / "New.pdf"
    assert moved.read_bytes() == b"%PDF data"

    row = conn.execute("SELECT file_path, root_id FROM holding WHERE edition_id = ?",
                       (eid,)).fetchone()
    assert row[0] == str(moved) and row[1] == 1          # repointed under root id=1


def test_file_edition_is_additive_leaves_filed_copies(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    eid = conn.execute("INSERT INTO edition (title, isbn) VALUES ('Multi','')").lastrowid
    filed = p["emptiness"] / "already.pdf"
    filed.write_bytes(b"keep")
    inbox_copy = p["inbox"] / "fresh.pdf"
    inbox_copy.write_bytes(b"move")
    conn.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
                 "VALUES (?, 'physical', ?, 'none')", (eid, str(filed)))
    conn.execute("INSERT INTO holding (edition_id, form, file_path, text_status) "
                 "VALUES (?, 'electronic', ?, 'ocr_good')", (eid, str(inbox_copy)))
    conn.commit()

    rep = filing.file_edition(conn, eid, str(p["tantra"]))
    assert len(rep["moved"]) == 1 and len(rep["skipped"]) == 1
    assert rep["skipped"][0]["reason"] == "not_in_inbox"
    assert filed.exists() and filed.read_bytes() == b"keep"      # untouched
    assert (p["tantra"] / "fresh.pdf").exists()


def test_file_edition_collision_gets_suffix(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    (p["tantra"] / "Dup.pdf").write_bytes(b"old")        # occupies the name
    eid = _book(conn, title="Dup", path=p["inbox"] / "Dup.pdf", subjects=["Buddhism/Tantra"])
    (p["inbox"] / "Dup.pdf").write_bytes(b"new")

    rep = filing.file_edition(conn, eid, str(p["tantra"]))
    assert rep["moved"][0]["to"] == str(p["tantra"] / "Dup (2).pdf")
    assert (p["tantra"] / "Dup.pdf").read_bytes() == b"old"      # original preserved


def test_file_edition_defers_missing_file(env):
    conn, p = env
    _subject(conn, "Buddhism/Tantra")
    # path recorded in inbox but no bytes on disk (the parent dir exists → 'missing')
    eid = _book(conn, title="Ghost", path=p["inbox"] / "Ghost.pdf",
                subjects=["Buddhism/Tantra"])
    rep = filing.file_edition(conn, eid, str(p["tantra"]))
    assert not rep["moved"] and len(rep["deferred"]) == 1
    assert rep["deferred"][0]["reason"] == "missing_or_offline"


def test_file_edition_create_false_defers_when_no_dir(env):
    conn, p = env
    _subject(conn, "Buddhism/Tenets")
    eid = _book(conn, title="N", path=p["inbox"] / "N.pdf", subjects=["Buddhism/Tenets"])
    (p["inbox"] / "N.pdf").write_bytes(b"x")
    dest = p["root"] / "Tenets"                          # does not exist
    rep = filing.file_edition(conn, eid, str(dest), create=False)
    assert not rep["moved"] and rep["deferred"][0]["reason"] == "no_dir"
    assert not dest.exists()


# ── end-to-end: the review endpoint auto-files on "Mark reviewed" ────────────────
def test_mark_reviewed_endpoint_auto_files(tmp_path, monkeypatch):
    """POST /edition/<eid>/mark-reviewed sets the verdict AND moves the inbox copy onto
    the subject shelf when the destination is unambiguous."""
    from catalogue.db_store import connect
    from catalogue.webui.web import create_app

    lib = tmp_path / "Library"
    inbox = lib / "_INBOX"
    root = lib / "01 Books - Dharma"
    tantra = root / "Tantra"
    for d in (inbox, tantra):
        d.mkdir(parents=True)
    vocab = tmp_path / "vocab.json"
    vocab.write_text(json.dumps({
        "_library_roots": [{"id": 1, "path": str(root), "derive_subject": False}],
        "_inbox_dirs": [str(inbox)]}))           # inbox membership is by configured folder
    monkeypatch.setattr(mount, "VOCAB_PATH", vocab)
    monkeypatch.setattr(filing, "VOCAB_PATH", vocab)

    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    _subject(db, "Buddhism/Tantra")
    _book(db, title="Filed", path=tantra / "Filed.pdf", subjects=["Buddhism/Tantra"])
    eid = _book(db, title="New", path=inbox / "New.pdf", subjects=["Buddhism/Tantra"])
    db.close()
    (tantra / "Filed.pdf").write_bytes(b"old")
    (inbox / "New.pdf").write_bytes(b"%PDF new")

    with app.test_client() as c:
        r = c.post(f"/edition/{eid}/mark-reviewed")
        assert r.status_code == 200
        body = r.get_json()
        assert body["reviewed"] and body["filing"]["auto"]
        assert body["filing"]["destination"] == str(tantra)

    assert not (inbox / "New.pdf").exists()
    assert (tantra / "New.pdf").read_bytes() == b"%PDF new"
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT review_status FROM edition WHERE id = ?",
                      (eid,)).fetchone()[0] == "ok"
    db.close()
