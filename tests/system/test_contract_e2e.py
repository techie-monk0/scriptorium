"""Cross-repo e2e for the edition-identity stability contract.

Exercises the REAL catalogue (init_db + services.entity_undo delete/merge + tool_policy) and the
REAL bdrag catalogue bridge against ONE catalogue DB — no fakes, no models, no sqlite-vec (the
identity contract is all pure SQLite). Proves the whole chain: bdrag links + claims -> the catalogue
refuses to hard-delete the cited edition -> a catalogue merge forwards it -> bdrag's citation still
resolves, to the survivor.

Skips cleanly if the BuddhistLLM checkout (or its deps in this env) isn't present. See
docs/access/external_tool_dependency_contract.md.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

_BDRAG_SRC = Path.home() / "Dev" / "BuddhistLLM" / "src"
if not (_BDRAG_SRC / "bdrag" / "catalogue.py").exists():
    pytest.skip("BuddhistLLM checkout not present — cross-repo e2e skipped", allow_module_level=True)
sys.path.insert(0, str(_BDRAG_SRC))

from catalogue.access_api import tool_policy                         # noqa: E402
from catalogue.contracts import Capability, CapabilityRestricted     # noqa: E402
from catalogue.db_store import init_db                               # noqa: E402
from catalogue.services import entity_undo                           # noqa: E402

try:
    from bdrag import catalogue as CAT                               # noqa: E402
    from bdrag.impl.sqlite_store import SqliteStore                  # noqa: E402
except ImportError as e:                                             # bdrag deps missing in this env
    pytest.skip(f"bdrag not importable here ({e}) — cross-repo e2e skipped", allow_module_level=True)


def _seed_catalogue(catdb):
    conn = init_db(catdb)
    win = conn.execute("INSERT INTO edition (title) VALUES ('Winner Edition')").lastrowid
    dup = conn.execute("INSERT INTO edition (title) VALUES ('Duplicate Edition')").lastrowid
    p_win = conn.execute("SELECT pub_id FROM edition WHERE id=?", (win,)).fetchone()[0]
    p_dup = conn.execute("SELECT pub_id FROM edition WHERE id=?", (dup,)).fetchone()[0]
    conn.execute("INSERT INTO holding (edition_id, form, file_path, content_hash) VALUES (?,?,?,?)",
                 (win, 'electronic', '/lib/winner.pdf', 'hw'))
    conn.execute("INSERT INTO holding (edition_id, form, file_path, content_hash) VALUES (?,?,?,?)",
                 (dup, 'electronic', '/lib/dup.pdf', 'hd'))
    conn.commit(); conn.close()
    return win, dup, p_win, p_dup


def _seed_corpus(corpus, source_path):
    cc = sqlite3.connect(corpus)
    store = SqliteStore(cc); store.initialize()
    store.insert_document(title="dup", source_path=source_path, source_format="pdf",
                          extraction_method="x", mojibake_repaired=0, quality_score=1.0,
                          needs_review=0, content_hash="c", clean_text="")
    cc.commit()
    return cc, store


def test_cited_edition_undeletable_and_citation_survives_merge(tmp_path):
    catdb = tmp_path / "catalogue.db"
    win, dup, p_win, p_dup = _seed_catalogue(catdb)

    # bdrag corpus: a document whose source is the duplicate's catalogue file
    cc, store = _seed_corpus(tmp_path / "corpus.db", "/lib/dup.pdf")

    # bdrag links + claims against the REAL catalogue (no manifest -> catalogue-path fallback)
    res = CAT.link_documents(store, corpus="e2e", db=str(catdb), manifest=str(tmp_path / "none.jsonl"))
    assert res == {"linked": 1, "claimed": 1, "unresolved": 0}
    assert cc.execute("SELECT edition_pub_id FROM documents").fetchone()[0] == p_dup
    cc.close()

    # the REAL catalogue now refuses to hard-delete the cited edition (UI path + policy layer)
    conn = init_db(catdb)
    assert entity_undo.delete_edition(conn, dup)["status"] == "blocked"
    with pytest.raises(CapabilityRestricted):
        tool_policy.enforce(conn, Capability.PURGE, dup)

    # a REAL services merge forwards the cited duplicate (tombstone), not deletes it
    assert entity_undo.merge_editions(conn, dup, win)["status"] == "merged"
    assert conn.execute("SELECT COUNT(*) FROM edition WHERE id=?", (dup,)).fetchone()[0] == 1
    conn.commit(); conn.close()

    # bdrag's citation token still resolves — now to the winner's LIVE title, no re-embed
    r = CAT.resolve(p_dup, db=str(catdb))
    assert r is not None and r.status == "superseded"
    assert r.canonical_pub_id == p_win and r.title == "Winner Edition"

    # the (id, version) staleness check has NO false positive: the merge re-pointed the dup's file
    # onto the winner, so the embedded content is still a live holding of the canonical edition.
    cc2 = sqlite3.connect(tmp_path / "corpus.db")
    assert CAT.stale_documents(SqliteStore(cc2), db=str(catdb)) == []
    cc2.close()
