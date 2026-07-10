"""The `--spines` warm pass in catalogue/cli/fetch_covers.py (fetch_spines_all + main).

Offline: covers.fetch_cover is stubbed, so no network. Pins that the pass reuses an
already-cached cover (no fetch), falls back to a palette spine when there's no cover, is
idempotent, and never touches the cover cache (independent namespaces).
"""
from __future__ import annotations

import io

from PIL import Image

from catalogue.db_store import init_db
from catalogue.cli import fetch_covers
from catalogue.services import covers


def _png(rgb=(40, 90, 160)):
    buf = io.BytesIO()
    Image.new("RGB", (120, 180), rgb).save(buf, "PNG")
    return buf.getvalue()


def _edition(db, title="T", isbn=None):
    eid = db.execute("INSERT INTO edition (title, isbn) VALUES (?, ?)", (title, isbn)).lastrowid
    return eid


def test_spines_reuse_cached_cover_without_fetch(tmp_path, monkeypatch):
    db = init_db(tmp_path / "s.db")
    eid = _edition(db, "Tantric Ethics")
    db.commit()
    cache = str(tmp_path / "cc")
    covers.write_cache(cache, f"e{eid}", _png())               # pre-seed the cover
    monkeypatch.setattr(covers, "fetch_cover",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    s = fetch_covers.fetch_spines_all(db, cache, embedded=False)
    assert s["from_cover"] == 1 and s["fetched"] == 0
    spine = covers.cached_path(cache, f"spine-e{eid}")
    assert spine and spine.endswith(".svg")
    assert b"<image" in open(spine, "rb").read()               # cover-derived art
    db.close()


def test_spines_palette_when_no_cover(tmp_path, monkeypatch):
    db = init_db(tmp_path / "s.db")
    eid = _edition(db, "No Cover Book")
    db.commit()
    cache = str(tmp_path / "cc")
    monkeypatch.setattr(covers, "fetch_cover", lambda *a, **k: None)
    s = fetch_covers.fetch_spines_all(db, cache, embedded=False)
    assert s["palette"] == 1
    data = open(covers.cached_path(cache, f"spine-e{eid}"), "rb").read()
    assert data.startswith(b"<svg") and b"<image" not in data  # palette spine, no art
    # The spine pass must NOT have created a cover-cache entry (independent namespaces).
    assert covers.cached_path(cache, f"e{eid}") is None
    db.close()


def test_spines_idempotent_second_run_cached(tmp_path, monkeypatch):
    db = init_db(tmp_path / "s.db")
    _edition(db, "Book")
    db.commit()
    cache = str(tmp_path / "cc")
    monkeypatch.setattr(covers, "fetch_cover", lambda *a, **k: None)
    fetch_covers.fetch_spines_all(db, cache, embedded=False)
    s2 = fetch_covers.fetch_spines_all(db, cache, embedded=False)
    assert s2["cached"] == 1 and s2["palette"] == 0            # already built → skipped
    db.close()


def test_main_spines_flag_runs_both_passes(tmp_path, monkeypatch, capsys):
    db = tmp_path / "m.db"
    conn = init_db(db)
    eid = _edition(conn, "Book", isbn="9780861712908")
    conn.commit(); conn.close()
    cache = tmp_path / "cc"
    monkeypatch.setattr(covers, "fetch_cover", lambda *a, **k: None)
    fetch_covers.main(["--spines", "--no-embedded", "--db", str(db), "--cache", str(cache)])
    out = capsys.readouterr().out
    assert "covers:" in out and "spines:" in out               # both summaries printed
    assert covers.cached_path(str(cache), f"spine-e{eid}")     # spine warmed
