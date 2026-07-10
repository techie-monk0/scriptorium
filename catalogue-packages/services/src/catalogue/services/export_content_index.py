"""Standalone full-text content index — the OFFLINE "Content search" bundle.

The PWA downloads this once (`GET /api/v1/content-index`, gated behind the Settings
toggle) so it can search INSIDE books with no connection, using the SAME FTS5 schema +
tokenizer the server uses — so client results equal server results. It carries ONLY the
in-book text + a tiny edition map (id / title / authors); no FRBR / authority / review
graph. Read-only build (never writes the catalogue → no snapshot needed).

A native client (iOS/Android) queries this identical SQLite file with its own FTS5-enabled
SQLite build, running the same `match_fts` query.
"""
from __future__ import annotations

import gzip
import os
import tempfile

from catalogue.db_store import new_export_db
from . import search as _search

SCHEMA_VERSION = 1


def _reads(db):
    """The edition READ surface bound over this connection (engine-routed content source)."""
    from catalogue.access_api import system_conn
    return system_conn(db).editions.reads

# Mirrors catalogue/db/schema.sql exactly (external-content FTS5 + the same tokenizer),
# so bm25()/snippet() on the client match the server byte-for-byte.
_DDL = """
CREATE TABLE edition (id INTEGER PRIMARY KEY, title TEXT, authors TEXT);
CREATE TABLE edition_text (
  id INTEGER PRIMARY KEY, edition_id INTEGER NOT NULL, page INTEGER, content TEXT NOT NULL);
CREATE INDEX edition_text_edition_idx ON edition_text(edition_id);
CREATE VIRTUAL TABLE edition_text_fts USING fts5(
  content, content='edition_text', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2");
"""


def build_content_index(db, dest_path: str) -> dict:
    """Build the standalone content index at `dest_path` from the live `db`. Returns stats."""
    out = new_export_db(dest_path)
    try:
        out.executescript(_DDL)
        reads = _reads(db)
        rows = reads.text_passages()
        out.executemany(
            "INSERT INTO edition_text (id, edition_id, page, content) VALUES (?, ?, ?, ?)",
            rows)
        # External-content FTS5: build the index from the populated content table.
        out.execute("INSERT INTO edition_text_fts(edition_text_fts) VALUES('rebuild')")
        # Edition map — only editions that actually carry text, with the same by-line
        # (book authors + translators) the content-search results show. Stored newline-joined.
        eids = reads.edition_ids_with_text()
        for eid in eids:
            ed = reads.get(eid)
            title = (ed.title if ed else None) or f"edition #{eid}"
            authors = "\n".join(_search.edition_people(db, eid))
            out.execute("INSERT INTO edition (id, title, authors) VALUES (?, ?, ?)",
                        (eid, title, authors))
        out.commit()
        out.execute("VACUUM")
        out.commit()
        return {"schema_version": SCHEMA_VERSION, "editions": len(eids),
                "passages": len(rows)}
    finally:
        out.close()


def build_bytes(db) -> bytes:
    """The content index as raw SQLite-file bytes (built in a temp file, then read)."""
    fd, path = tempfile.mkstemp(suffix=".content-index.db")
    os.close(fd)
    try:
        build_content_index(db, path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def signature(db) -> str:
    """Cheap content fingerprint (row count · max id · total text length). Changes whenever
    the indexed text changes, so it backs the download ETag without hashing 100s of MB."""
    n, mx, total = _reads(db).text_signature()
    return f"ci{SCHEMA_VERSION}-{n}-{mx}-{total}"


def build_gzip(db) -> tuple[bytes, str]:
    """(gzipped index bytes, signature) — the cacheable payload the endpoint serves."""
    return gzip.compress(build_bytes(db), 6), signature(db)
