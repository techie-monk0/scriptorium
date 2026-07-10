"""Re-hash holdings from real content (catalogue/cli/rehash.py): disk for hydrated files,
WebDAV for online-only placeholders."""
from __future__ import annotations

import hashlib

from catalogue.db_store import init_db
from catalogue.cli import rehash as R
from catalogue.services import webdav


def _book(db, path, fhash):
    eid = db.execute("INSERT INTO edition (title) VALUES ('B')").lastrowid
    return db.execute("INSERT INTO holding (edition_id, form, file_path, file_hash) "
                      "VALUES (?, 'electronic', ?, ?)", (eid, str(path), fhash)).lastrowid


def test_rehash_disk_and_webdav(tmp_path):
    db = init_db(tmp_path / "r.db")
    # hydrated file on disk with a WRONG recorded hash → corrected from disk
    real = tmp_path / "real.pdf"; real.write_bytes(b"%PDF actual content")
    h_disk = _book(db, real, "WRONGHASH")
    # online-only placeholder (all zeros) with a zero-hash → corrected from WebDAV bytes
    ph = tmp_path / "ph.pdf"; ph.write_bytes(b"\x00" * 4096)
    zero_hash = hashlib.sha256(b"\x00" * 4096).hexdigest()
    h_dav = _book(db, ph, zero_hash)
    db.commit()

    # fake WebDAV mount that serves real bytes for the placeholder's path
    real_bytes = b"%PDF the true placeholder content"
    url = "https://h/" + ph.name
    def op(req, timeout):
        return real_bytes if req.full_url == url else (_ for _ in ()).throw(Exception("404"))
    mount = webdav.Mount(str(tmp_path), webdav.WebDAVClient("https://h", opener=op))

    s = R.rehash(db, mounts=[mount])
    assert s["total"] == 2 and s["changed"] == 2
    assert s["from_disk"] == 1 and s["from_webdav"] == 1 and s["failed"] == []
    got = dict(db.execute("SELECT id, file_hash FROM holding").fetchall())
    assert got[h_disk] == hashlib.sha256(b"%PDF actual content").hexdigest()
    assert got[h_dav] == hashlib.sha256(real_bytes).hexdigest()
    db.close()


def test_rehash_unchanged_when_already_correct(tmp_path):
    db = init_db(tmp_path / "r.db")
    real = tmp_path / "x.pdf"; real.write_bytes(b"%PDF x")
    _book(db, real, hashlib.sha256(b"%PDF x").hexdigest())
    db.commit()
    s = R.rehash(db, mounts=[])
    assert s["rehashed"] == 1 and s["changed"] == 0
    db.close()


def test_rehash_records_failure_when_unfetchable(tmp_path):
    db = init_db(tmp_path / "r.db")
    ph = tmp_path / "ph.pdf"; ph.write_bytes(b"\x00" * 2048)   # placeholder, no mount
    hid = _book(db, ph, "z")
    db.commit()
    s = R.rehash(db, mounts=[])                                # no WebDAV → can't fetch
    assert s["failed"] == [hid] and s["changed"] == 0
    db.close()
