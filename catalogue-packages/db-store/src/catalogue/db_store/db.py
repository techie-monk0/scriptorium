"""DB connection + init gate.

Step-1 contract (§13): before anything else proceeds, verify
  - SQLite ≥ 3.27
  - FTS5 compiled in
  - `unicode61 remove_diacritics 2` actually folds at search time
…falling back to `pysqlite3-binary` if the stdlib build is too old or
lacks FTS5 (§4.5).
"""
from __future__ import annotations

import json
import os
import unicodedata
from importlib import resources
from pathlib import Path

# Prefer stdlib sqlite3; fall back to pysqlite3 only if needed.
try:
    import sqlite3 as _sqlite  # type: ignore
    _SQLITE_SOURCE = "stdlib"
except ImportError:  # pragma: no cover — stdlib always present
    import pysqlite3 as _sqlite  # type: ignore
    _SQLITE_SOURCE = "pysqlite3"


SCHEMA_PATH = Path(__file__).parent / "schema.sql"
# Open vocabularies seeded into lookup tables at init (§12.4: a new value is an
# INSERT, not a migration). Override the location for tests / deployments.
VOCAB_PATH = Path(os.environ.get("CATALOGUE_VOCAB", Path(__file__).parent / "vocab.json"))
MIN_SQLITE = (3, 27, 0)


class InitGateError(RuntimeError):
    """Raised when the SQLite/FTS5/folding gate fails."""


class SchemaDriftError(RuntimeError):
    """Raised when a DB is missing a table/column/index the current code expects.

    This is the guard for the class of bug where a long-lived DB predates a
    schema change and `_migrate` never ran on it: writes to the missing column
    fail (or, worse, are silently rolled back) and changes vanish. Refusing to
    operate is safer than losing writes one at a time."""


# ── Normalization helpers (§4.2, §4.8c) ────────────────────────────────────
def nfc(text: str) -> str:
    """First post-OCR step (§4.8c, step 1). Stored/searchable form."""
    return unicodedata.normalize("NFC", text)


# IAST/phonetic digraph collapses (§4.2): the spec's worked example requires
# Śāntideva / Shantideva / Santideva → all `santideva`. NFKD-strip alone gives
# `santideva` / `shantideva` / `santideva`; the `sh→s` collapse closes the gap.
# Order matters: longest first; applied AFTER NFKD-strip + lowercase.
_DIGRAPHS = (
    ("sh", "s"),   # ś / sh
    ("ch", "c"),   # c / ch (phonetic vs IAST is ambiguous; conservative collapse)
    ("ph", "p"),
    ("th", "t"),
    ("kh", "k"),
    ("gh", "g"),
    ("jh", "j"),
    ("bh", "b"),
    ("dh", "d"),
    ("rh", "r"),
)


def search_normalize(text: str) -> str:
    """Search-time normalizer aligned with FTS5's index-time fold
    (`unicode61 remove_diacritics 2`): NFKD-decompose, strip combining
    marks, lowercase. Whitespace collapsed but tokens preserved.

    DISTINCT FROM `fold_key`. The §4.5 worked example —
    `tathagatagarbha` matching `tathāgatagarbha` — requires the search
    fold to match the FTS5 index fold (which only strips diacritics);
    applying `fold_key`'s aggressive digraph collapse here would turn
    the query into `tatagatagarba` and break that invariant.

    §4.5 mentions "digraph collapse" in the same paragraph as the worked
    example. The plan is internally inconsistent; the worked example
    wins — see tests/system/test_search.py.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def fold_key(text: str) -> str:
    """NFKD-decompose-then-strip + digraph-collapse fold for the resolution
    match key (§4.2). Distinct from NFC: used ONLY for alias `normalized_key`
    and resolver lookups — NEVER written back as stored text.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    out = stripped.lower().strip()
    for src, dst in _DIGRAPHS:
        out = out.replace(src, dst)
    return out


def add_alias(db, kind: str, parent_id: int, text: str, scheme: str) -> int:
    """Single chokepoint for alias inserts so `normalized_key` always equals
    `fold_key(text)` — the §4.2 invariant. Used by the web CRUD paths and the
    proposal promoter (catalogue/promote.py)."""
    assert kind in ("work", "person")
    table = f"{kind}_alias"
    col = f"{kind}_id"
    cur = db.execute(
        f"INSERT INTO {table} ({col}, text, scheme, normalized_key) "
        "VALUES (?, ?, ?, ?)",
        (parent_id, text, scheme, fold_key(text)),
    )
    return cur.lastrowid


# ── Connection ────────────────────────────────────────────────────────────
def _check_sqlite_version(conn) -> None:
    parts = tuple(int(p) for p in _sqlite.sqlite_version.split("."))
    if parts < MIN_SQLITE:
        raise InitGateError(
            f"SQLite {_sqlite.sqlite_version} < {'.'.join(map(str, MIN_SQLITE))}; "
            "install pysqlite3-binary."
        )


def _check_fts5_and_folding(conn) -> None:
    """Create a throwaway FTS5 table and verify diacritic folding works.

    Adversarial: an inserted `tathāgatagarbha` must match the bare-Latin query
    `tathagatagarbha`, AND `snippet()` must return the diacriticked form.
    """
    cur = conn.cursor()
    # Drop any stale probe from a prior aborted init; then create fresh.
    cur.execute("DROP TABLE IF EXISTS _fts_probe")
    try:
        cur.execute(
            "CREATE VIRTUAL TABLE _fts_probe USING fts5"
            "(content, tokenize='unicode61 remove_diacritics 2')"
        )
    except _sqlite.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            raise InitGateError(
                "database is locked — another process (e.g. a resolver or the "
                "staging load pass) holds the write lock. Retry once it "
                f"releases; this is not an FTS5 problem. ({e})"
            ) from e
        raise InitGateError(f"FTS5 not available: {e}") from e

    cur.execute("INSERT INTO _fts_probe(content) VALUES (?)", ("tathāgatagarbha",))
    (hit,) = cur.execute(
        "SELECT count(*) FROM _fts_probe WHERE _fts_probe MATCH ?",
        ("tathagatagarbha",),
    ).fetchone()
    if hit != 1:
        raise InitGateError(
            "FTS5 unicode61 remove_diacritics 2 did not fold — "
            "tathagatagarbha failed to match tathāgatagarbha."
        )

    (stored,) = cur.execute(
        "SELECT snippet(_fts_probe, 0, '', '', '', 64) FROM _fts_probe "
        "WHERE _fts_probe MATCH ?",
        ("tathagatagarbha",),
    ).fetchone()
    if "tathāgatagarbha" not in stored:
        raise InitGateError(
            "FTS5 stored text lost diacritics — folding leaked into storage."
        )

    cur.execute("DROP TABLE _fts_probe")


class DryRunConnection:
    """A sqlite connection whose `commit()` is a NO-OP. Every write stays in an
    uncommitted transaction and is discarded on `rollback`/`close`, so the web UI and
    CLI picker can be exercised against the real DB without anything persisting — used
    for `--dry-run` / `CATALOGUE_DRY_RUN`. Everything else delegates to the real
    connection (sqlite3.Connection has no __dict__, so it can't be monkey-patched in
    place — hence this thin proxy)."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def commit(self):           # swallow — the whole point of dry-run
        pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._conn.rollback()
        return False


# Connection opening lives in connection.py (the raw-connector chokepoint); re-exported
# here so the long-standing `from catalogue.db_store import connect` / `.db import connect`
# imports keep working. The read/write split: connect_rw (default) vs connect_ro (read-only).
from .connection import connect, connect_ro, connect_rw, new_export_db  # noqa: E402,F401


def init_db(db_path: str | os.PathLike, *, _verify: bool = True) -> "_sqlite.Connection":
    """Connect, run the init gate (SQLite version + FTS5 + folding probe),
    apply schema. Safe to call repeatedly; schema uses IF NOT EXISTS.

    After applying schema + migrations, assert the resulting DB conforms to the
    schema the current code expects (every table/column/index present). On a
    fresh DB this is a tautology; on a long-lived DB it is the post-condition
    that the migration brought it fully forward — catching a column declared in
    schema.sql but not handled by `_migrate` before any write silently fails on
    it. `_verify=False` is the internal escape hatch used while *building* the
    reference schema (which would otherwise recurse)."""
    conn = connect(db_path)
    _check_sqlite_version(conn)
    _check_fts5_and_folding(conn)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    load_vocab(conn)        # seed lookup tables from vocab.json BEFORE _migrate, so
    _migrate(conn)          # the holding_type backfill can satisfy its FK.
    conn.commit()
    if _verify:
        assert_schema_current(conn)
    return conn


# ── schema-conformance guard ────────────────────────────────────────────────
# The reference is "whatever a freshly-built DB looks like" — schema.sql + every
# migration, captured once into an in-memory DB. Comparing a live DB against it
# needs no hand-maintained manifest: the source of truth is the code that already
# builds the schema, so the two can never drift apart.
_EXPECTED_SCHEMA: "dict | None" = None


def _capture_schema(conn) -> dict:
    """Structural fingerprint of a connection: {tables: {name: {columns}},
    indexes: {names}, triggers: {names}, views: {names}}. Names only — presence
    is what a missing-column bug turns on; types are reported separately."""
    tables = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall():
        tables[name] = {r[1] for r in conn.execute(
            f"PRAGMA table_info({name})").fetchall()}

    def names(typ):
        return {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? "
            "AND name NOT LIKE 'sqlite_%'", (typ,)).fetchall()}
    return {"tables": tables, "indexes": names("index"),
            "triggers": names("trigger"), "views": names("view")}


def expected_schema() -> dict:
    """The structure a freshly-initialised DB has under the current code. Built
    once (an in-memory init_db with verification disabled) and cached."""
    global _EXPECTED_SCHEMA
    if _EXPECTED_SCHEMA is None:
        ref = init_db(":memory:", _verify=False)
        try:
            _EXPECTED_SCHEMA = _capture_schema(ref)
        finally:
            ref.close()
    return _EXPECTED_SCHEMA


def schema_drift(conn) -> dict:
    """What the current code expects that `conn` is MISSING. Returns
    {missing_tables, missing_columns: {table: [cols]}, missing_indexes,
    missing_triggers, missing_views}; all empty ⇒ conformant. Extra objects in
    the live DB are ignored (forward-compatible)."""
    want = expected_schema()
    have = _capture_schema(conn)
    missing_cols = {}
    for table, cols in want["tables"].items():
        if table in have["tables"]:
            gap = cols - have["tables"][table]
            if gap:
                missing_cols[table] = sorted(gap)
    return {
        "missing_tables": sorted(set(want["tables"]) - set(have["tables"])),
        "missing_columns": missing_cols,
        "missing_indexes": sorted(want["indexes"] - have["indexes"]),
        "missing_triggers": sorted(want["triggers"] - have["triggers"]),
        "missing_views": sorted(want["views"] - have["views"]),
    }


def schema_is_current(conn) -> bool:
    return not any(schema_drift(conn).values())


def assert_schema_current(conn) -> None:
    """Raise `SchemaDriftError` (listing exactly what is missing) if `conn` is
    not conformant. No-op otherwise. The fix is always the same — run
    `init_db(path)`, which migrates the DB forward — so the message says so."""
    d = schema_drift(conn)
    if not any(d.values()):
        return
    lines = []
    if d["missing_tables"]:
        lines.append(f"  missing tables: {d['missing_tables']}")
    for table, cols in d["missing_columns"].items():
        lines.append(f"  table {table} missing columns: {cols}")
    for kind in ("indexes", "triggers", "views"):
        if d[f"missing_{kind}"]:
            lines.append(f"  missing {kind}: {d[f'missing_{kind}']}")
    raise SchemaDriftError(
        "database schema is behind the code — writes to the missing objects "
        "would fail or be silently lost:\n" + "\n".join(lines) +
        "\n\nFix: restart the app (startup runs init_db, which migrates the DB "
        "forward), or run "
        "`python -c \"from catalogue.db_store import init_db; init_db('<db>').close()\"`.")


def load_vocab(conn, path: "str | os.PathLike | None" = None) -> None:
    """Seed open vocabularies from a JSON config into their lookup tables
    (§12.4: a new value is data, not a migration). The file maps a lookup-table
    name → list of {code, label}; each row is INSERT OR IGNORE'd, so reloading
    is idempotent and never clobbers an edited label. Missing file is a no-op."""
    if path is not None:
        p = Path(path)
        if not p.exists():
            return
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        # Merge the shipped vocab.json with any user overlay (vocab.local.json) so
        # user-added lookup codes / traditions seed the DB too. See authority_vocab.
        from .authority_vocab import vocab_config
        data = vocab_config()
    for table, rows in data.items():
        if table.startswith("_") or not isinstance(rows, list):
            continue  # `_comment` and other non-vocab keys
        for row in rows:
            code = (row or {}).get("code")
            if not code:
                continue
            conn.execute(
                f"INSERT OR IGNORE INTO {table} (code, label) VALUES (?, ?)",
                (code, row.get("label")),
            )
    # `tradition` is a name-keyed vocab (not code/label), so it lives under the
    # `_tradition` config key as a bare list of names. Seed idempotently; the first
    # entry is the library default (see catalogue.db_store.migrate_tradition).
    for name in data.get("_tradition") or []:
        if isinstance(name, str) and name.strip():
            conn.execute("INSERT OR IGNORE INTO tradition (name) VALUES (?)", (name,))


def derive_holding_type(form, file_path, archival_pdf_path) -> "str | None":
    """Best-guess `holding_type` for a holding from its form + file extension.
    Physical copies are `physical`; electronic files key off the extension
    (pdf/epub). Returns None when nothing is known (leave the column NULL)."""
    if form == "physical":
        return "physical"
    ext = os.path.splitext(file_path or archival_pdf_path or "")[1].lstrip(".").lower()
    if ext in ("pdf", "epub"):
        return ext
    return "physical" if form == "physical" else None


# ── v4: digitization-provenance DDL / vocab (MIRROR of access_api/scan_ocr.py) ─────────
# Folded here so every catalogue DB carries the scan/OCR provenance tables + the holding
# pointer columns without a client having to call scan_ocr.ensure_schema. scan_ocr.py stays
# the standalone typed surface; these constants must stay in sync with its _SCHEMA_SQL /
# _PROVENANCE_KINDS / _ENGINES / _HOLDING_COLUMNS / LEGACY_DIGITIZER_MAP. See
# docs/access/scan_ocr_provenance_model.md §5.
_PROVENANCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS provenance_kind (code TEXT PRIMARY KEY, label TEXT);

CREATE TABLE IF NOT EXISTS digitization_engine (
  code  TEXT PRIMARY KEY,
  stage TEXT NOT NULL,                        -- 'capture' | 'ocr'
  label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digitization_event (
  id             INTEGER PRIMARY KEY,
  holding_id     INTEGER NOT NULL REFERENCES holding(id) ON DELETE CASCADE,
  stage          TEXT NOT NULL,               -- 'capture' | 'ocr'
  engine         TEXT REFERENCES digitization_engine(code),
  engine_version TEXT,
  performed_at   TEXT,
  params_json    TEXT,
  quality_score  REAL,
  evidence       TEXT NOT NULL DEFAULT 'unknown',
  superseded     INTEGER NOT NULL DEFAULT 0,
  notes          TEXT,
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS digitization_event_holding_idx
  ON digitization_event(holding_id, stage);
"""

_PROVENANCE_KINDS = [
    ("born_digital", "Born-digital (typeset/publisher PDF, no scan)"),
    ("scanned",      "Scanned from a physical copy"),
    ("downloaded",   "Downloaded digital text (BDRC / archive.org / etc.)"),
    ("unknown",      "Provenance not determined"),
]
_DIGITIZATION_ENGINES = [
    ("flatbed",         "capture", "Flatbed scanner"),
    ("book_scanner",    "capture", "Overhead / book scanner"),
    ("phone",           "capture", "Phone camera capture"),
    ("bdrc_download",   "capture", "Downloaded from BDRC"),
    ("archive_org",     "capture", "Downloaded from archive.org"),
    ("publisher_pdf",   "capture", "Publisher-supplied digital PDF"),
    ("capture_unknown", "capture", "Capture source unknown"),
    ("tesseract_iast",  "ocr", "OCRmyPDF + Tesseract (eng + Shreeshrii IAST)"),
    ("gcv",             "ocr", "OCRmyPDF + Cloud Vision (ualiawan fork / adapter)"),
    ("abbyy",           "ocr", "ABBYY FineReader (manual on Mac) → import PDF/A"),
    ("ocr_none",        "ocr", "No OCR performed"),
    ("ocr_unknown",     "ocr", "OCR engine unknown"),
]
_HOLDING_PROVENANCE_COLUMNS = {
    "provenance_kind":          "TEXT REFERENCES provenance_kind(code)",
    "current_capture_event_id": "INTEGER REFERENCES digitization_event(id)",
    "current_ocr_event_id":     "INTEGER REFERENCES digitization_event(id)",
}
# Legacy digitizer_used code → the new ocr-stage engine (all three legacy codes are OCR).
_LEGACY_DIGITIZER_MAP = {
    "ocrmypdf_tesseract": "tesseract_iast",
    "ocrmypdf_gcv":       "gcv",
    "abbyy_import":       "abbyy",
}


# SQLite expression that mints a RFC-4122 v4 UUID (8-4-4-4-12 hex, version nibble '4',
# variant nibble 8/9/a/b). Used by the edition.pub_id mint trigger and the one-time
# backfill so both produce the same shape. random()/randomblob are non-deterministic, so
# this can't be a column DEFAULT — hence the AFTER INSERT trigger.
_UUID4_SQL = (
    "lower("
    "hex(randomblob(4)) || '-' || "
    "hex(randomblob(2)) || '-4' || substr(hex(randomblob(2)), 2) || '-' || "
    "substr('89ab', (abs(random()) % 4) + 1, 1) || substr(hex(randomblob(2)), 2) || '-' || "
    "hex(randomblob(6)))"
)


def ensure_field_columns(conn) -> None:
    """Add every scalar controlled-vocab column declared in the CategoricalField registry
    (catalogue.contracts.fields) that a table is missing — the registry-driven half of the
    migration, so a new controlled-vocab field needs no bespoke ALTER. Additive/nullable and
    idempotent (`PRAGMA table_info` guards each add). Columns are plain TEXT; a field-specific
    classifier's companion columns (tenet_source/…) stay in that field's own migrator."""
    from catalogue.contracts.fields import FIELDS
    for f in FIELDS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({f.entity})").fetchall()}
        if f.name not in have:
            conn.execute(f"ALTER TABLE {f.entity} ADD COLUMN {f.name} TEXT")


def _migrate(conn) -> None:
    """Idempotent additive migrations for columns added after a DB was
    first created. SQLite has no `ADD COLUMN IF NOT EXISTS` — inspect
    `PRAGMA table_info` and add only what's missing. Drops are NOT done
    here (§13: 'Ask before … any schema change that would require a
    migration')."""
    def cols(table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    if "metadata_json" not in cols("capture_staging"):
        conn.execute("ALTER TABLE capture_staging ADD COLUMN metadata_json TEXT")
    # §14.2: capture sources are ios/web/csv/manual — distinct axis from
    # `form` (electronic/physical). Additive, nullable; old rows stay valid.
    if "source" not in cols("capture_staging"):
        conn.execute("ALTER TABLE capture_staging ADD COLUMN source TEXT")
    # §14.2: the client's ISO-8601 timestamp. `created_at` is server time;
    # `scanned_at` preserves the phone's time-of-scan across queue flush
    # delays (offline → reconnect → POST minutes later).
    if "scanned_at" not in cols("capture_staging"):
        conn.execute("ALTER TABLE capture_staging ADD COLUMN scanned_at TEXT")
    # §14.6: the cross-format "already in catalogue?" verdict computed at capture
    # time (1 found / 0 not found / NULL never checked). Persisted so the
    # not-in-catalogue capture log can be shown without re-running network lookups.
    if "in_catalogue" not in cols("capture_staging"):
        conn.execute("ALTER TABLE capture_staging ADD COLUMN in_catalogue INTEGER")

    # Per-holding format facet (pdf|epub|physical, open vocab). On first add,
    # backfill from each holding's form + file extension so existing rows get a
    # sensible value instead of NULL.
    if "holding_type" not in cols("holding"):
        conn.execute(
            "ALTER TABLE holding ADD COLUMN holding_type TEXT "
            "REFERENCES holding_type(code)"
        )
        for hid, form, fp, arch in conn.execute(
            "SELECT id, form, file_path, archival_pdf_path FROM holding"
        ).fetchall():
            ht = derive_holding_type(form, fp, arch)
            if ht:
                conn.execute(
                    "UPDATE holding SET holding_type = ? WHERE id = ?", (ht, hid)
                )
    # Free-text note per holding (condition, provenance, "missing dust jacket"…).
    if "notes" not in cols("holding"):
        conn.execute("ALTER TABLE holding ADD COLUMN notes TEXT")

    # A bind whose cross-link harvest failed (network down) is bound but partial —
    # flag it for re-harvest so dedup stays conservative (authority_dedup_plan.md §6.17).
    if "harvest_incomplete" not in cols("person"):
        conn.execute("ALTER TABLE person ADD COLUMN harvest_incomplete "
                     "INTEGER NOT NULL DEFAULT 0")

    # Undo precondition fingerprint (added after undo_log itself) — guards a stale undo
    # from clobbering newer edits / a reused id (contributor_undo.apply_undo).
    if "precheck" not in cols("undo_log"):
        conn.execute("ALTER TABLE undo_log ADD COLUMN precheck TEXT")

    # Free-text curator note on a person (disambiguation rationale / conflation
    # history). Additive, nullable; old rows stay valid. Mirrors holding.notes.
    if "notes" not in cols("person"):
        conn.execute("ALTER TABLE person ADD COLUMN notes TEXT")

    # A picked-but-not-yet-committed authority id: the add-person form parks the
    # operator's pick here (instead of external_id) when it can't confidently dedup,
    # so the row stays in the review worklist and acceptance runs on-bind dedup.
    # Additive, nullable; not a dedup key-set member. See schema.sql.
    if "suggested_external_id" not in cols("person"):
        conn.execute("ALTER TABLE person ADD COLUMN suggested_external_id TEXT")

    # 3-state person reconciliation (§verify). Backfill: a person that already
    # carries an authority id is 'verified'; everything else starts 'provisional'.
    # Column-presence guarded (no version bump) — matches the holding_type pattern.
    if "verification_status" not in cols("person"):
        conn.execute(
            "ALTER TABLE person ADD COLUMN verification_status TEXT "
            "DEFAULT 'provisional'")
        conn.execute(
            "UPDATE person SET verification_status = 'verified' "
            "WHERE external_id IS NOT NULL")

    # Content fingerprint (text-layer hash when trustworthy, else byte hash) —
    # stable across annotation/re-save, so the reconcile pass can tell an
    # annotated copy from a genuine content change. Backfill from the cached raw
    # text where present so existing rows populate without a re-sweep.
    if "content_hash" not in cols("holding"):
        conn.execute("ALTER TABLE holding ADD COLUMN content_hash TEXT")
        from .signature import of as _signature_of   # data-layer primitive — no upward import
        for hid, fh, ts in conn.execute(
            "SELECT id, file_hash, text_status FROM holding"
        ).fetchall():
            row = conn.execute(
                "SELECT raw_text FROM raw_extract_cache WHERE file_hash = ? "
                "ORDER BY extract_version DESC LIMIT 1", (fh,)
            ).fetchone() if fh else None
            sig = _signature_of(row[0] if row else None, ts, fh)
            if sig:
                conn.execute("UPDATE holding SET content_hash = ? WHERE id = ?",
                             (sig.wire, hid))

    # Per-holding ISBN (the manifestation's product id). ISBN identifies a FORMAT, so it
    # belongs on the holding, not the edition — print/epub/pdf of one book carry DIFFERENT
    # ISBNs. Additive/nullable; backfilled from each holding's edition so existing holdings
    # keep their ISBN, and so consolidating format-dup editions (whose holdings move) never
    # loses a format's ISBN. edition.isbn is retained as the edition's display/primary.
    # Index added here (not schema.sql): on a pre-existing DB the executescript runs BEFORE
    # this migration, so holding.isbn wouldn't yet exist for the index.
    # When the user last opened this copy in the viewer — powers the home page's
    # "Recently opened" shelf. Additive/nullable; old rows stay NULL and the shelf
    # falls back to date_added until a real open is recorded.
    if "last_opened" not in cols("holding"):
        conn.execute("ALTER TABLE holding ADD COLUMN last_opened TEXT")

    if "isbn" not in cols("holding"):
        conn.execute("ALTER TABLE holding ADD COLUMN isbn TEXT")
        conn.execute(
            "UPDATE holding SET isbn = "
            "(SELECT e.isbn FROM edition e WHERE e.id = holding.edition_id) "
            "WHERE isbn IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS holding_isbn_idx ON holding(isbn)")

    # Which configured library root (vocab `_library_roots`.id) owns each holding.
    # Set at ingest by mount.owning_root_id; backfilled here for existing holdings by
    # longest-prefix match so per-root repoint/remove (WHERE root_id = X) is exact.
    # NULL when the file sits under no configured root. Index added here (the
    # executescript runs before this migration on a pre-existing DB).
    if "root_id" not in cols("holding"):
        # Structural column-add only. Populating root_id for *pre-existing* rows needs
        # library-root resolution (catalogue.services.mount) — a business/filesystem
        # concern that must NOT be imported down here. Normal ingest (services/sweep.py)
        # sets root_id on every insert, so any live DB is already populated; a one-off
        # legacy backfill for a pre-root_id DB lives at services.mount.backfill_holding_root_ids().
        conn.execute("ALTER TABLE holding ADD COLUMN root_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS holding_root_id_idx ON holding(root_id)")

    # Locator kind for a contained work (page|chapter|section, open vocab) — pages
    # for PDF/physical, chapter/section for reflowable EPUB. Value stays in
    # section_locator; this just names what the value means. Nullable (old links
    # keep their free-text locator with an unspecified kind).
    if "locator_type" not in cols("edition_work"):
        conn.execute(
            "ALTER TABLE edition_work ADD COLUMN locator_type TEXT "
            "REFERENCES locator_type(code)"
        )

    # Per-appearance freeform note for a work within ONE edition (e.g. "only chs. 1–3
    # of the root text", "verse portions only"). Scoped to this printing, so it lives
    # on the join, not on work/edition. Nullable, additive — old links stay valid.
    if "note" not in cols("edition_work"):
        conn.execute("ALTER TABLE edition_work ADD COLUMN note TEXT")

    # CIP title components, parsed by catalogue/cip.py but previously discarded.
    # The edition carries the full ISBD breakdown of THIS printing's title page
    # (subtitle after ':', volume designation, and the '=' parallel/native title
    # split by script into sanskrit/tibetan); the work carries only the
    # original-language native title (belongs to the abstract work, not a printing).
    # All nullable, additive — old rows keep title-only and stay valid.
    for col in ("subtitle", "volume", "sanskrit_title", "tibetan_title"):
        if col not in cols("edition"):
            conn.execute(f"ALTER TABLE edition ADD COLUMN {col} TEXT")
    for col in ("sanskrit_title", "tibetan_title"):
        if col not in cols("work"):
            conn.execute(f"ALTER TABLE work ADD COLUMN {col} TEXT")

    # Volume self-grouping (FRBR model, 2026-06-04). Editions sharing volume_set_id
    # are the volumes of one multi-volume publication, ordered by volume_seq; NULL =
    # standalone. Additive/nullable INTEGER columns — old rows stay valid. The
    # supporting index lives in schema.sql (created on every init). See
    # catalogue/work_dedup.py (groups volumes; must NOT merge them as duplicates).
    for col in ("volume_set_id", "volume_seq"):
        if col not in cols("edition"):
            conn.execute(f"ALTER TABLE edition ADD COLUMN {col} INTEGER")
    # Index here (not schema.sql): on a pre-existing DB the executescript in init_db
    # runs BEFORE this migration, so volume_set_id wouldn't yet exist for the index.
    conn.execute("CREATE INDEX IF NOT EXISTS edition_volume_set_idx "
                 "ON edition(volume_set_id)")

    # OpenLibrary work key resolved from this edition's ISBN — clusters editions of
    # one work across formats (print/epub/pdf carry different ISBNs) so a phone scan
    # can flag "already have this in another form". Nullable/additive. Index added
    # here (not schema.sql) for the same reason as edition_volume_set_idx above.
    if "ol_work_key" not in cols("edition"):
        conn.execute("ALTER TABLE edition ADD COLUMN ol_work_key TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS edition_ol_work_key_idx "
                 "ON edition(ol_work_key)")

    # Catalogue-review verdict per edition (the book): a 'reviewed/needs-fix'
    # status, the 4-flag structured checklist (JSON), a free-text note, and a
    # timestamp. All nullable/additive; NULL review_status = unreviewed. The
    # edition_verify_resolution table is created by schema.sql (executescript
    # runs on every init), so only the columns need an ALTER here.
    for col in ("review_status", "review_flags", "review_note", "reviewed_at"):
        if col not in cols("edition"):
            conn.execute(f"ALTER TABLE edition ADD COLUMN {col} TEXT")

    # Operator-set single-vs-multi-work classification per edition ('single_work' |
    # 'multi_work'; NULL = unclassified). Drives which detection pass runs. Additive
    # /nullable — old rows stay valid. Set via the /editions/structure tool.
    if "structure" not in cols("edition"):
        conn.execute("ALTER TABLE edition ADD COLUMN structure TEXT")

    # Works-review verdict per work (the works twin of the edition review): NULL =
    # unreviewed / 'ok' / 'needs_fix', a note, and a timestamp. Additive/nullable.
    # The edition_author table (new) is created by schema.sql's executescript.
    for col in ("review_status", "review_note", "reviewed_at"):
        if col not in cols("work"):
            conn.execute(f"ALTER TABLE work ADD COLUMN {col} TEXT")

    # Subject kind discriminator: 'topic' (aboutness, the default) vs 'series'
    # (a Series/Collection grouping). Additive, NOT NULL with a DEFAULT so old
    # rows backfill to 'topic' — which is correct, since every pre-existing
    # subject is topical. See catalogue/domain/subject_tree.py.
    if "kind" not in cols("subject"):
        conn.execute(
            "ALTER TABLE subject ADD COLUMN kind TEXT NOT NULL DEFAULT 'topic'")

    # Soft-delete tombstone (entity-API): catalog ROOTS get a nullable `deleted_at`
    # (NULL = live; an ISO-8601 string = soft-deleted). A tombstone freezes the id —
    # never reused, so the recycled-id corruption class dies even without AUTOINCREMENT —
    # and makes delete recoverable; the access-API reads filter `deleted_at IS NULL`.
    # Holdings are deliberately NOT here: they hard-delete (≈1-to-1 with a file, and
    # content_hash already guards id reuse). Additive/nullable; every existing row is live.
    # See docs/access/entity_api_model.md §6.
    for _t in ("edition", "work", "person", "subject", "collection", "tradition"):
        if "deleted_at" not in cols(_t):
            conn.execute(f"ALTER TABLE {_t} ADD COLUMN deleted_at TEXT")

    # ── v2: §14.5 status code is 'raw', not 'pending'. ─────────────────────
    # Older DBs were created with DEFAULT 'pending'; bring them in line with
    # the contract. Idempotent.
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    version = int(version[0]) if version else 1

    if version < 2:
        # Rename pending → raw so the partial unique index + dedup query
        # find the open rows. Resolved rows already use 'resolved'.
        conn.execute(
            "UPDATE capture_staging SET status = 'raw' WHERE status = 'pending'"
        )
        # Add the partial unique index (schema.sql only creates it on
        # fresh DBs; old DBs need it explicitly).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS capture_staging_raw_isbn_uq "
            "ON capture_staging(raw_isbn) "
            "WHERE status = 'raw' AND raw_isbn IS NOT NULL"
        )

    if version < 3:
        # M7: add ON DELETE CASCADE to FK columns. SQLite has no ALTER FK,
        # so rebuild the affected tables in a single transaction. The
        # 12-step ALTER procedure (sqlite.org/lang_altertable.html) is the
        # supported path; foreign_keys is toggled OFF for the rebuild.
        _rebuild_with_cascade(conn)

    # ── v4: digitization provenance (scan/OCR) folded into the central schema. ─────
    # Schema is additive + idempotent (tables IF NOT EXISTS, vocab OR IGNORE, columns
    # guarded on PRAGMA), so it runs every init like the deleted_at columns above. The
    # one-time backfill (provenance kind from text_status; a current OCR event from the
    # legacy digitizer_used code) is gated on version<4 so it doesn't redo work. Mirrors
    # access_api/scan_ocr.py. See docs/access/scan_ocr_provenance_model.md §5.
    conn.executescript(_PROVENANCE_SCHEMA_SQL)
    conn.executemany(
        "INSERT OR IGNORE INTO provenance_kind (code, label) VALUES (?, ?)", _PROVENANCE_KINDS)
    conn.executemany(
        "INSERT OR IGNORE INTO digitization_engine (code, stage, label) VALUES (?, ?, ?)",
        _DIGITIZATION_ENGINES)
    for _col, _decl in _HOLDING_PROVENANCE_COLUMNS.items():
        if _col not in cols("holding"):
            conn.execute(f"ALTER TABLE holding ADD COLUMN {_col} {_decl}")

    if version < 4:
        # (a) provenance kind from text_status, only where still unset (infer_provenance_kind).
        conn.execute(
            "UPDATE holding SET provenance_kind = CASE "
            "WHEN text_status = 'native' THEN 'born_digital' "
            "WHEN text_status IN ('ocr_good', 'ocr_poor', 'image_only') THEN 'scanned' "
            "ELSE 'unknown' END "
            "WHERE provenance_kind IS NULL")
        # (b) one current OCR event per holding from the legacy digitizer_used code.
        for hid, code in conn.execute(
                "SELECT id, digitizer_used FROM holding "
                "WHERE digitizer_used IS NOT NULL AND current_ocr_event_id IS NULL").fetchall():
            engine = _LEGACY_DIGITIZER_MAP.get(code)
            if engine is None:
                continue
            cur = conn.execute(
                "INSERT INTO digitization_event (holding_id, stage, engine, evidence) "
                "VALUES (?, 'ocr', ?, 'unknown')", (hid, engine))
            conn.execute("UPDATE holding SET current_ocr_event_id = ? WHERE id = ?",
                         (cur.lastrowid, hid))
        # External read view gains provenance_kind (additive column for consumers). Recreated
        # here, not in schema.sql, because the column is added by this migration — schema.sql's
        # executescript runs before it exists.
        conn.execute("DROP VIEW IF EXISTS v_holding_files")
        conn.execute(
            "CREATE VIEW v_holding_files AS "
            "SELECT edition_id, file_path, content_hash, text_status, provenance_kind "
            "FROM holding WHERE file_path IS NOT NULL AND TRIM(file_path) <> ''")

    # ── v5: optimistic-concurrency `rev` + idempotency keys (entity-API). ─────────
    # A monotonic version counter per ROOT row, bumped on every update, so a write planned against
    # rev=N is rejected (StaleWrite) if a concurrent update advanced it — catching lost updates the
    # identity fingerprint can't see (two edits to non-identity columns). Additive/idempotent (the
    # same 6 roots that carry deleted_at; NOT NULL DEFAULT 0 backfills existing rows). The
    # idempotency_key table lets a retried create dedup to the row it already made. See §6.
    for _t in ("edition", "work", "person", "subject", "collection", "tradition"):
        if "rev" not in cols(_t):
            conn.execute(f"ALTER TABLE {_t} ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idempotency_key ("
        " key TEXT PRIMARY KEY,"
        " entity_kind TEXT NOT NULL,"
        " entity_id INTEGER NOT NULL,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)")

    # Audit log (entity-API): one row per applied write — WHO (principal) did WHAT (op) to WHICH
    # root (entity_kind/id), WHEN, with a compact JSON detail (changed columns / cascade count).
    # Written in the SAME transaction as the mutation, so it commits/rolls back with it. Additive
    # and append-only; never required for correctness, only for an after-the-fact trail. See §6.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        " id INTEGER PRIMARY KEY,"
        " ts TEXT DEFAULT CURRENT_TIMESTAMP,"
        " principal TEXT NOT NULL,"
        " op TEXT NOT NULL,"
        " entity_kind TEXT NOT NULL,"
        " entity_id INTEGER,"
        " detail TEXT)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS audit_log_entity_idx "
        "ON audit_log(entity_kind, entity_id)")

    # Pre-destructive checkpoint (entity-API): a destructive op snapshots the rows it HARD-removes
    # (e.g. an edition delete drops its holdings — soft-delete tombstones the root but the children
    # are gone) into a JSON blob BEFORE deleting them, in the same transaction. A `restore` re-inserts
    # them, so the whole delete is reversible, not just the root tombstone. Additive; append-only. §6.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS checkpoint ("
        " id INTEGER PRIMARY KEY,"
        " ts TEXT DEFAULT CURRENT_TIMESTAMP,"
        " principal TEXT,"
        " op TEXT,"
        " entity_kind TEXT NOT NULL,"
        " entity_id INTEGER,"
        " snapshot TEXT NOT NULL)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS checkpoint_entity_idx "
        "ON checkpoint(entity_kind, entity_id)")

    # ── FRBR Phase D: retire the legacy `work_contributor` table once its data is
    # in work_author / edition_translator. Self-healing on any pre-FRBR DB (it moves
    # the data first), and a no-op once the table is gone. The translator OVERRIDE
    # column edition_work.translator_person_id is intentionally KEPT.
    from . import migrate_frbr
    migrate_frbr.absorb_legacy(conn)

    # ── Live-root views (entity-API soft-delete): one `v_live_<root>` per soft-deletable root
    # that hides tombstoned rows (deleted_at IS NOT NULL). A read that must not surface a
    # soft-deleted edition/work/person/subject/collection/tradition selects FROM the view instead
    # of the base table; WRITES still target the base table (a simple view is read-only). This is
    # the foundation that lets a delete route through the access-API (tombstone) without the row
    # leaking into reads that predate soft-delete. DROP+CREATE every init (not IF NOT EXISTS) so
    # `SELECT *` re-expands to the current column set after a later ALTER — the same reason v4
    # recreates v_holding_files above. Additive/idempotent. See docs/access/entity_api_model.md §6.
    for _t in ("edition", "work", "person", "subject", "collection", "tradition"):
        conn.execute(f"DROP VIEW IF EXISTS v_live_{_t}")
        conn.execute(f"CREATE VIEW v_live_{_t} AS SELECT * FROM {_t} WHERE deleted_at IS NULL")

    # ── v6: wishlist (books wanted but not yet owned). The `wishlist_item` table itself
    # is created by schema.sql's executescript (CREATE TABLE IF NOT EXISTS, so old DBs get
    # it too); only its live view needs the DROP+CREATE here, for the same SELECT * re-
    # expansion guarantee as the catalogue roots above. No data backfill — the table is new.
    conn.execute("DROP VIEW IF EXISTS v_live_wishlist_item")
    conn.execute("CREATE VIEW v_live_wishlist_item AS "
                 "SELECT * FROM wishlist_item WHERE deleted_at IS NULL")

    # ── v7: tradition classifier.
    # The tradition vocab, edition_tradition table, and the full work_tradition
    # shape are all created by schema.sql's executescript (INSERT OR IGNORE /
    # CREATE IF NOT EXISTS, so fresh + old DBs both get them). Only the columns
    # ADDED to the pre-existing work_tradition join need catch-up ALTERs here —
    # a DB created before v7 has just (work_id, tradition_id). Additive, nullable;
    # old (empty) join stays valid.
    _wt = cols("work_tradition")
    if "confidence" not in _wt:
        conn.execute("ALTER TABLE work_tradition ADD COLUMN confidence REAL")
    if "source" not in _wt:
        conn.execute("ALTER TABLE work_tradition ADD COLUMN source TEXT")
    if "evidence" not in _wt:
        conn.execute("ALTER TABLE work_tradition ADD COLUMN evidence TEXT")
    if "created_at" not in _wt:
        # No DEFAULT on ALTER ADD (SQLite forbids non-constant defaults on add,
        # and the join is empty anyway); fresh rows get CURRENT_TIMESTAMP via the
        # schema.sql CREATE. Existing rows: none to backfill.
        conn.execute("ALTER TABLE work_tradition ADD COLUMN created_at TEXT")

    # ── v8: author-lineage field on person (the source of the default tradition for
    # their works/editions). Additive, nullable; seeded from config by migrate_tradition.
    if "tradition" not in cols("person"):
        conn.execute("ALTER TABLE person ADD COLUMN tradition TEXT")

    # ── v9: single-field tradition on work (mirrors person.tradition), user-overridable.
    if "tradition" not in cols("work"):
        conn.execute("ALTER TABLE work ADD COLUMN tradition TEXT")

    # ── v10: single-field tradition on edition (mirrors work.tradition), user-overridable.
    # Edition-level override for anthologies / mixed volumes; defaults from the edition's
    # works' tradition (else the authors' lineage, else the library default) in the UI.
    if "tradition" not in cols("edition"):
        conn.execute("ALTER TABLE edition ADD COLUMN tradition TEXT")

    # ── v11: edition.pub_id — the stable, opaque external identity + its stability contract
    # (S1–S3). See docs/access/external_tool_dependency_contract.md. Additive/idempotent:
    #   • the column (nullable; also in schema.sql for fresh DBs),
    #   • a write-once MINT trigger (AFTER INSERT sets a v4 UUID when NULL) — covers every
    #     insert path, including the ~906 legacy raw-SQL sites, with no app change,
    #   • an IMMUTABLE trigger (BEFORE UPDATE OF pub_id) that ABORTs any change to a non-null
    #     pub_id — S1 (no rebind), enforced below the ORM/service layer,
    #   • a partial UNIQUE index (S1 no-reuse: two editions can't share a token),
    #   • a one-time backfill of existing rows.
    if "pub_id" not in cols("edition"):
        conn.execute("ALTER TABLE edition ADD COLUMN pub_id TEXT")
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS edition_pub_id_mint "
        "AFTER INSERT ON edition WHEN NEW.pub_id IS NULL BEGIN "
        f"UPDATE edition SET pub_id = {_UUID4_SQL} WHERE id = NEW.id; END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS edition_pub_id_immutable "
        "BEFORE UPDATE OF pub_id ON edition "
        "WHEN OLD.pub_id IS NOT NULL AND NEW.pub_id IS NOT OLD.pub_id BEGIN "
        "SELECT RAISE(ABORT, 'edition.pub_id is write-once (stability contract S1)'); END"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS edition_pub_id_uq "
        "ON edition(pub_id) WHERE pub_id IS NOT NULL")
    conn.execute(f"UPDATE edition SET pub_id = {_UUID4_SQL} WHERE pub_id IS NULL")

    # External read-contract view gains pub_id (joined from edition). Recreated here, after
    # both edition.pub_id and holding.provenance_kind exist, so it always carries the full
    # guaranteed column set regardless of the DB's prior version. See v_holding_files in
    # schema.sql (the fresh-DB placeholder this overwrites).
    conn.execute("DROP VIEW IF EXISTS v_holding_files")
    conn.execute(
        "CREATE VIEW v_holding_files AS "
        "SELECT h.edition_id, e.pub_id, h.file_path, h.content_hash, h.text_status, "
        "h.provenance_kind "
        "FROM holding h JOIN edition e ON e.id = h.edition_id "
        "WHERE h.file_path IS NOT NULL AND TRIM(h.file_path) <> ''")

    # ── v12: edition.superseded_by — the S2 forwarding pointer (a merged/cited edition
    # tombstones + points at its winner, so resolve() never dangles). Additive/nullable; only
    # set for a cited loser. See external_deps.supersede / resolve.
    if "superseded_by" not in cols("edition"):
        conn.execute("ALTER TABLE edition ADD COLUMN superseded_by INTEGER REFERENCES edition(id)")

    # ── v13: scalar controlled-vocab columns (genre, genre_mode, …) driven by the
    # CategoricalField registry (catalogue.contracts.fields) — the single source of truth for
    # every enum-like column. A new controlled-vocab field is added here with NO bespoke ALTER.
    # Additive/nullable + idempotent: skips columns already present (incl. tenet_system / the
    # tradition columns created above and in schema.sql).
    ensure_field_columns(conn)

    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
        "('schema_version', '13')"
    )
    # Stamp the external read-contract version (the surface out-of-process tools verify against)
    # from its single source of truth, the published descriptor. See external_contract.py and
    # docs/access/external_tool_dependency_contract.md.
    from .external_contract import CONTRACT_VERSION as _erc_version
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
        "('external_read_contract_version', ?)", (str(_erc_version),))


# ── v3: FK CASCADE rebuild ─────────────────────────────────────────────────
# Per SQLite's "Making Other Kinds Of Table Schema Changes" procedure.
# Tables rebuilt to add ON DELETE CASCADE to existing FK columns.
_CASCADE_REBUILDS: tuple[tuple[str, str], ...] = (
    ("holding",
     "id INTEGER PRIMARY KEY, "
     "edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE, "
     "form TEXT REFERENCES form_type(code), "
     "file_path TEXT, file_hash TEXT, content_hash TEXT, shelf_location TEXT, "
     "ocr_quality_score REAL, "
     "text_status TEXT REFERENCES text_status(code), "
     "isbn TEXT, "
     "holding_type TEXT REFERENCES holding_type(code), "
     "digitizer_used TEXT REFERENCES digitizer_kind(code), "
     "archival_pdf_path TEXT, "
     "notes TEXT, "
     "date_added TEXT DEFAULT CURRENT_TIMESTAMP, "
     "last_opened TEXT, "
     "root_id INTEGER"),
    ("work_alias",
     "id INTEGER PRIMARY KEY, "
     "work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE, "
     "text TEXT NOT NULL, "
     "scheme TEXT REFERENCES alias_scheme(code), "
     "normalized_key TEXT NOT NULL"),
    ("person_alias",
     "id INTEGER PRIMARY KEY, "
     "person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE, "
     "text TEXT NOT NULL, "
     "scheme TEXT REFERENCES alias_scheme(code), "
     "normalized_key TEXT NOT NULL"),
    ("edition_work",
     "edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE, "
     "work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE, "
     "sequence INTEGER, "
     "translator_person_id INTEGER REFERENCES person(id) ON DELETE SET NULL, "
     "section_locator TEXT, "
     "locator_type TEXT REFERENCES locator_type(code), "
     "note TEXT, "
     "PRIMARY KEY (edition_id, work_id, sequence)"),
    ("relationship",
     "id INTEGER PRIMARY KEY, "
     "from_work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE, "
     "relation TEXT NOT NULL REFERENCES relation_type(code), "
     "to_work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE, "
     "from_section_locator TEXT"),
    ("collection_member",
     "collection_id INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE, "
     "work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE, "
     "PRIMARY KEY (collection_id, work_id)"),
    ("work_subject",
     "work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE, "
     "subject_id INTEGER NOT NULL REFERENCES subject(id) ON DELETE CASCADE, "
     "PRIMARY KEY (work_id, subject_id)"),
    ("work_tradition",
     "work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE, "
     "tradition_id INTEGER NOT NULL REFERENCES tradition(id) ON DELETE CASCADE, "
     "PRIMARY KEY (work_id, tradition_id)"),
    ("edition_text",
     "id INTEGER PRIMARY KEY, "
     "edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE, "
     "page INTEGER, "
     "content TEXT NOT NULL"),
)

# Indexes that must be recreated after the table rebuild (CREATE TABLE
# loses indexes when the table is renamed away).
_CASCADE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS work_alias_key_idx ON work_alias(normalized_key)",
    "CREATE INDEX IF NOT EXISTS person_alias_key_idx ON person_alias(normalized_key)",
    "CREATE INDEX IF NOT EXISTS rel_from_idx ON relationship(from_work_id)",
    "CREATE INDEX IF NOT EXISTS rel_to_idx ON relationship(to_work_id)",
    "CREATE INDEX IF NOT EXISTS holding_hash_idx ON holding(file_hash)",
    "CREATE INDEX IF NOT EXISTS holding_edition_idx ON holding(edition_id)",
    "CREATE INDEX IF NOT EXISTS holding_isbn_idx ON holding(isbn)",
    "CREATE INDEX IF NOT EXISTS edition_text_edition_idx ON edition_text(edition_id)",
)


def _has_cascade(conn, table: str) -> bool:
    """True iff every FK on `table` already declares ON DELETE CASCADE
    (or SET NULL for the translator slot). Cheap guard so the rebuild
    only runs once per DB even if schema_meta is missing."""
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    if not rows:
        return True  # no FKs to upgrade
    # Column 6 is on_delete in the PRAGMA result.
    return all(r[6] in ("CASCADE", "SET NULL") for r in rows)


def _rebuild_with_cascade(conn) -> None:
    """v3 migration: rebuild FK-heavy tables with ON DELETE CASCADE so
    deleting a work/edition/person cleans up the join rows automatically
    (M7). Uses SQLite's documented 12-step ALTER procedure.

    edition_text's FTS5 triggers reference the table by name, so they are
    dropped + recreated from schema.sql after the rebuild.
    """
    # If every target table is already CASCADE-clean, no work to do.
    if all(_has_cascade(conn, t) for t, _ in _CASCADE_REBUILDS):
        return

    # FTS5 triggers will fire on the DELETE FROM edition_text inside the
    # rebuild and corrupt the shadow index. Drop them first; schema.sql
    # recreates them via IF NOT EXISTS on the next init pass — but the
    # next init runs AFTER _migrate, so recreate them explicitly here.
    conn.execute("DROP TRIGGER IF EXISTS edition_text_ai")
    conn.execute("DROP TRIGGER IF EXISTS edition_text_ad")
    conn.execute("DROP TRIGGER IF EXISTS edition_text_au")

    # Python's sqlite3 default isolation_level auto-opens an implicit
    # transaction on DML; we need autocommit to issue PRAGMA + BEGIN
    # cleanly. Restore at the end.
    prev_isolation = conn.isolation_level
    conn.commit()                       # close any implicit txn
    conn.isolation_level = None         # autocommit
    conn.execute("PRAGMA foreign_keys = OFF")
    # Step 2 of SQLite's documented 12-step ALTER: with legacy_alter_table OFF
    # (the modern default), `RENAME TO _tmp` rewrites every OTHER table's FK that
    # references this one to point at the temp name — which we then DROP, leaving a
    # dangling reference (e.g. reading_position → holding). We recreate each table
    # with correct FK DDL ourselves, so renames must NOT touch other tables.
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN")
        for table, columns in _CASCADE_REBUILDS:
            if _has_cascade(conn, table):
                continue
            old_cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()]
            tmp = f"_{table}_old_v3"
            conn.execute(f"ALTER TABLE {table} RENAME TO {tmp}")
            conn.execute(f"CREATE TABLE {table} ({columns})")
            col_list = ", ".join(old_cols)
            conn.execute(
                f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {tmp}"
            )
            conn.execute(f"DROP TABLE {tmp}")
        for idx_sql in _CASCADE_INDEXES:
            conn.execute(idx_sql)
        # Recreate FTS triggers (they reference the now-rebuilt edition_text).
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS edition_text_ai "
            "AFTER INSERT ON edition_text BEGIN "
            "INSERT INTO edition_text_fts(rowid, content) "
            "VALUES (new.id, new.content); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS edition_text_ad "
            "AFTER DELETE ON edition_text BEGIN "
            "INSERT INTO edition_text_fts(edition_text_fts, rowid, content) "
            "VALUES('delete', old.id, old.content); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS edition_text_au "
            "AFTER UPDATE ON edition_text BEGIN "
            "INSERT INTO edition_text_fts(edition_text_fts, rowid, content) "
            "VALUES('delete', old.id, old.content); "
            "INSERT INTO edition_text_fts(rowid, content) "
            "VALUES (new.id, new.content); END"
        )
        # PRAGMA foreign_key_check before COMMIT — abort if rebuild
        # introduced any dangling reference.
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            conn.execute("ROLLBACK")
            raise InitGateError(
                f"CASCADE migration would orphan rows: {broken[:5]}"
            )
        conn.execute("COMMIT")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = prev_isolation


def sqlite_source() -> str:
    """'stdlib' or 'pysqlite3' — handy for diagnostics/tests."""
    return _SQLITE_SOURCE


if __name__ == "__main__":
    import sys
    from .paths import default_db_path
    target = sys.argv[1] if len(sys.argv) > 1 else default_db_path()
    conn = init_db(target)
    print(f"init OK — sqlite={_sqlite.sqlite_version} via {_SQLITE_SOURCE} → {target}")
