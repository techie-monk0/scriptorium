"""Step-1 regression tests.

Each test pins a v2–v8 invariant. If one fails, an assumption the rest of
the plan rests on has broken.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

import pytest

from catalogue.db_store import (
    InitGateError,
    fold_key,
    init_db,
    nfc,
)


# ── Fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


# ── §4.5 / Step-1 init gate ────────────────────────────────────────────────
def test_init_is_idempotent(tmp_path):
    p = tmp_path / "x.db"
    init_db(p).close()
    init_db(p).close()  # second pass must not raise


def test_fts5_folds_diacritic_drops(db):
    """The headline §4.5 invariant: `tathagatagarbha` matches `tathāgatagarbha`."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    db.execute(
        "INSERT INTO edition_text (edition_id, page, content) VALUES (1, 1, ?)",
        ("Discussion of tathāgatagarbha doctrine.",),
    )
    (hits,) = db.execute(
        "SELECT count(*) FROM edition_text_fts WHERE edition_text_fts MATCH ?",
        ("tathagatagarbha",),
    ).fetchone()
    assert hits == 1


def test_stored_text_keeps_diacritics(db):
    """Folding is index-only; stored text and snippet() retain diacritics (§4.5)."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    db.execute(
        "INSERT INTO edition_text (edition_id, page, content) VALUES (1, 1, ?)",
        ("Śāntideva wrote the Bodhicaryāvatāra.",),
    )
    (stored,) = db.execute("SELECT content FROM edition_text WHERE edition_id = 1").fetchone()
    assert "Śāntideva" in stored and "Bodhicaryāvatāra" in stored

    (snip,) = db.execute(
        "SELECT snippet(edition_text_fts, 0, '[', ']', '…', 32) "
        "FROM edition_text_fts WHERE edition_text_fts MATCH ?",
        ("santideva",),
    ).fetchone()
    assert "Śāntideva" in snip  # diacritics survive in the snippet


# ── §4.2 / §4.8c — NFC vs NFKD-strip are DIFFERENT jobs ────────────────────
def test_nfc_canonicalizes_decomposed_macron():
    """OCR may emit `a`+U+0304 instead of precomposed `ā`. NFC fixes it (§4.8c step 1)."""
    decomposed = "a" + "̄" + "rya"     # a + combining macron
    precomposed = "ārya"
    assert decomposed != precomposed         # different byte strings…
    assert nfc(decomposed) == precomposed    # …same NFC form


def test_fold_key_strips_diacritics_for_resolution():
    """§4.2 worked example: Śāntideva / Shantideva / Santideva → `santideva`."""
    assert fold_key("Śāntideva") == "santideva"
    assert fold_key("Shantideva") == "santideva"
    assert fold_key("Santideva") == "santideva"
    # Sanity: bare ASCII collapses too (bodhi has no digraphs).
    assert fold_key("Bodhicaryāvatāra") == "bodicaryavatara"  # 'dh' → 'd'


def test_nfc_and_fold_key_are_distinct_outputs():
    """Guard against ever conflating the two (§4.2 explicit warning)."""
    s = "Śāntideva"
    assert nfc(s) == s                # NFC keeps diacritics
    assert fold_key(s) == "santideva"   # fold strips diacritics + collapses digraphs
    assert nfc(s) != fold_key(s)


# ── §4.5 — phonetic↔Wylie are NOT folded by FTS ────────────────────────────
def test_phonetic_does_not_fold_into_wylie(db):
    """Non-reversible pairs like `byang chub` / `jangchub` must NOT match
    through FTS folding — they are linked only at the entity/alias level
    (deferred query-expansion, §4.5)."""
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    db.execute(
        "INSERT INTO edition_text (edition_id, page, content) VALUES (1, 1, ?)",
        ("the Tibetan term byang chub means awakening",),
    )
    (hits,) = db.execute(
        "SELECT count(*) FROM edition_text_fts WHERE edition_text_fts MATCH ?",
        ("jangchub",),
    ).fetchone()
    assert hits == 0, (
        "phonetic↔Wylie pairs must remain distinct in FTS; matching them is "
        "the deferred query-expansion feature, not folding."
    )


# ── §12.4 — open vocabularies are lookup tables, NOT enums ────────────────
def test_new_relation_type_is_a_data_insert_not_a_migration(db):
    """Adding a relation type must not require schema change."""
    db.execute("INSERT INTO relation_type (code, label) VALUES ('quotes', 'Quotes')")
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.execute("INSERT INTO work (id) VALUES (2)")
    db.execute(
        "INSERT INTO relationship (from_work_id, relation, to_work_id) VALUES (1, 'quotes', 2)"
    )  # would fail under a CHECK/enum
    db.commit()


def test_unknown_relation_type_is_rejected_by_fk(db):
    """Lookup table still constrains — typo prevention without enum rigidity."""
    db.execute("INSERT INTO work (id) VALUES (1)")
    db.execute("INSERT INTO work (id) VALUES (2)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO relationship (from_work_id, relation, to_work_id) "
            "VALUES (1, 'no_such_relation', 2)"
        )


def test_seeded_vocabularies_present(db):
    """Smoke-check the §5 seeds landed."""
    for code in ("native", "ocr_good", "ocr_poor", "image_only", "none"):
        assert db.execute("SELECT 1 FROM text_status WHERE code=?", (code,)).fetchone()
    for code in ("comments_on", "sub_comments_on", "summarizes", "cites"):
        assert db.execute("SELECT 1 FROM relation_type WHERE code=?", (code,)).fetchone()
    # §4.4: `same_cycle` must NOT be a relation type.
    assert not db.execute("SELECT 1 FROM relation_type WHERE code='same_cycle'").fetchone()


# ── §5 / §12.3 — per-stage versioned caches ────────────────────────────────
def test_versioned_cache_keys_isolate_stage_versions(db):
    """Same file_hash at different extract_versions must coexist —
    bumping a stage version invalidates only that stage."""
    db.execute(
        "INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) VALUES (?, ?, ?)",
        ("abc", 1, "first parse"),
    )
    db.execute(
        "INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) VALUES (?, ?, ?)",
        ("abc", 2, "improved parse"),
    )
    (n,) = db.execute(
        "SELECT count(*) FROM raw_extract_cache WHERE file_hash=?", ("abc",)
    ).fetchone()
    assert n == 2

    # Duplicate (hash, version) must conflict — guards against fossilized errors.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO raw_extract_cache (file_hash, extract_version, raw_text) "
            "VALUES (?, ?, ?)",
            ("abc", 1, "double-write"),
        )


def test_classification_cache_records_model_rung(db):
    """§4.9 escalation: cache must remember which rung produced the answer,
    so re-runs check the cache and don't re-climb."""
    db.execute(
        "INSERT INTO classification_cache "
        "(content_hash, classify_version, result_json, confidence, model_rung) "
        "VALUES (?, ?, ?, ?, ?)",
        ("h1", 1, '{"kind":"root"}', 0.92, "qwen3:8b"),
    )
    (rung,) = db.execute(
        "SELECT model_rung FROM classification_cache WHERE content_hash='h1'"
    ).fetchone()
    assert rung == "qwen3:8b"


# ── §5 — text_status drives the three-tier needs-work list ─────────────────
def test_holding_text_status_constrained_by_lookup(db):
    db.execute("INSERT INTO edition (id, title) VALUES (1, 'e')")
    db.execute(
        "INSERT INTO holding (edition_id, form, text_status) VALUES (1, 'electronic', 'ocr_good')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO holding (edition_id, form, text_status) "
            "VALUES (1, 'electronic', 'not_a_status')"
        )


# ── Init-gate negative path ────────────────────────────────────────────────
def test_init_gate_catches_broken_folding(monkeypatch, tmp_path):
    """If FTS5 ever stops folding (e.g. wrong tokenizer arg), the gate must
    refuse to proceed — folding is load-bearing for §4.5."""
    from catalogue.db_store import db as dbmod

    real_check = dbmod._check_fts5_and_folding

    def broken(conn):
        # Simulate a folding regression by inserting and searching with a
        # tokenizer that does NOT fold.
        cur = conn.cursor()
        cur.execute(
            "CREATE VIRTUAL TABLE _probe USING fts5(content, tokenize='unicode61 remove_diacritics 0')"
        )
        cur.execute("INSERT INTO _probe(content) VALUES ('tathāgatagarbha')")
        (hit,) = cur.execute(
            "SELECT count(*) FROM _probe WHERE _probe MATCH ?", ("tathagatagarbha",)
        ).fetchone()
        cur.execute("DROP TABLE _probe")
        if hit != 1:
            raise InitGateError("folding regression detected")

    monkeypatch.setattr(dbmod, "_check_fts5_and_folding", broken)
    with pytest.raises(InitGateError):
        dbmod.init_db(tmp_path / "broken.db")


# ── connect() guards against the phantom-DB footgun + busy_timeout ──────────
def test_connect_rejects_a_connection_object(tmp_path):
    """Passing an OPEN connection where a path is expected used to create a phantom
    DB file named '<sqlite3.Connection object at 0x…>' and silently swallow writes.
    connect() now refuses it with a clear error."""
    import glob
    import os as _os

    from catalogue.db_store import connect
    conn = connect(tmp_path / "real.db")
    with pytest.raises(TypeError, match="phantom|Connection"):
        connect(conn)                      # the bug: a connection, not a path
    assert not glob.glob(_os.path.join(str(tmp_path), "*Connection object*"))


def test_connect_sets_a_generous_busy_timeout(tmp_path):
    """WAL allows one writer; a too-short busy_timeout makes a concurrent writer fail
    with 'database is locked' instead of waiting. Pin it at 30s."""
    from catalogue.db_store import connect
    conn = connect(tmp_path / "bt.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
