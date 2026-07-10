"""The offline content-index bundle (`export_content_index` + `GET /api/v1/content-index`).

The PWA downloads this SQLite file to search inside books offline, using the SAME FTS5
schema/tokenizer the server uses → identical results. These tests build the index over a
seeded fixture and query the resulting file directly (proving the client path works), then
check the HTTP delivery (gzip + ETag/304).
"""
from __future__ import annotations

import gzip
import io
import sqlite3
import tempfile


def _seed_text(seed, eid, title, page, content):
    seed("INSERT OR IGNORE INTO edition (id, title) VALUES (?, ?)", (eid, title))
    seed("INSERT INTO edition_text (edition_id, page, content) VALUES (?, ?, ?)",
         (eid, page, content))


def test_built_index_is_queryable_with_same_fts(app_env, seed):
    c, app, tmp = app_env
    _seed_text(seed, 1, "Lamp for the Path", 1,
               "The bodhisattva cultivates tathāgatagarbha and patience.")
    pid = seed("INSERT INTO person (primary_name) VALUES ('Jane Author')").lastrowid
    seed("INSERT INTO edition_author (edition_id, person_id, role, seq) VALUES (1, ?, 'author', 1)", (pid,))

    from catalogue.db_store import connect
    from catalogue.services import export_content_index as ECI
    src = connect(app.config["DB_PATH"])
    dest = str(tmp / "index.db")
    stats = ECI.build_content_index(src, dest)
    src.close()
    assert stats["editions"] == 1 and stats["passages"] == 1

    # Query the standalone file exactly as the client would (index-only diacritic folding).
    idx = sqlite3.connect(dest)
    rows = idx.execute(
        "SELECT et.edition_id, snippet(edition_text_fts, 0, '[', ']', '…', 16) "
        "FROM edition_text_fts JOIN edition_text et ON et.id = edition_text_fts.rowid "
        "WHERE edition_text_fts MATCH 'tathagatagarbha' ORDER BY bm25(edition_text_fts)"
    ).fetchall()
    assert rows and rows[0][0] == 1
    assert "tathāgatagarbha" in rows[0][1] and "[" in rows[0][1]   # diacritics + highlight
    # The edition map carries title + by-line.
    ed = idx.execute("SELECT title, authors FROM edition WHERE id = 1").fetchone()
    assert ed[0] == "Lamp for the Path" and "Jane Author" in ed[1]
    idx.close()


def test_endpoint_serves_gzipped_sqlite_with_etag_304(app_env, seed):
    c, _, _ = app_env
    _seed_text(seed, 1, "A Book", 1, "unique_marker_zzz lives in the body text here.")

    r = c.get("/api/v1/content-index")
    assert r.status_code == 200
    assert r.headers["Content-Encoding"] == "gzip"
    etag = r.headers["ETag"]
    # The body is a gzipped SQLite database the client can open and MATCH against.
    raw = gzip.decompress(r.data)
    assert raw[:16].startswith(b"SQLite format 3")
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.write(raw); fd.close()
    idx = sqlite3.connect(fd.name)
    hit = idx.execute("SELECT edition_id FROM edition_text_fts "
                      "JOIN edition_text et ON et.id = edition_text_fts.rowid "
                      "WHERE edition_text_fts MATCH 'unique_marker_zzz'").fetchone()
    assert hit and hit[0] == 1
    idx.close()

    # Unchanged data → 304 on a matching ETag (no rebuild/redownload).
    assert c.get("/api/v1/content-index", headers={"If-None-Match": etag}).status_code == 304
