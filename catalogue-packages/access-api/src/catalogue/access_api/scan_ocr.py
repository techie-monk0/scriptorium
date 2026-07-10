"""Scan / OCR provenance — the single access layer for digitization metadata.

This module is the *only* code that should touch the digitization tables
(`digitization_engine`, `digitization_event`, `provenance_kind`) or the holding
provenance columns (`provenance_kind`, `current_capture_event_id`,
`current_ocr_event_id`). Everything else — the sweep pipeline, the web UI, the
backfill script, any external client — goes through the functions here.

Why a separate module (not more helpers in db.py): digitization provenance is a
self-contained concern with its own vocabulary, its own history table, and its
own invariants (one *current* event per stage, engine.stage must match the
event's stage). Keeping it behind a small typed surface means clients never hand-
write the supersede/repoint dance and can't desync `holding.current_*_event_id`
from the event log.

Contract for clients:
  * Pass an open sqlite3.Connection (from catalogue.db_store.db.connect / init_db).
  * Call `ensure_schema(conn)` once after init_db if you might be on an
    un-migrated DB (idempotent; a no-op on a current DB).
  * Reads return frozen dataclasses; writes take keywords and return the row
    they wrote. Nothing here commits — the caller owns the transaction, matching
    the rest of the codebase (e.g. domain/sweep.py commits at end of its pass).

Schema + migration + backfill rationale: docs/access/scan_ocr_provenance_model.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# ── Vocabulary constants ─────────────────────────────────────────────────────
# Stages. An event is either a *capture* (paper/screen → images) or an *ocr*
# pass (images → text layer). They are separate events with separate engines and
# dates: a book scanned once may be re-OCRed many times as engines improve.
STAGE_CAPTURE = "capture"
STAGE_OCR = "ocr"
STAGES = (STAGE_CAPTURE, STAGE_OCR)

# How a recorded value was obtained — keeps an honest "unknown" distinguishable
# from a human-confirmed or pipeline-emitted value when you read it back later.
EVIDENCE_PIPELINE = "pipeline"               # emitted by our own scan/OCR run
EVIDENCE_PDF_METADATA = "pdf_metadata"       # mined from /Producer, /Creator, XMP
EVIDENCE_STRUCTURAL = "structural_inference"  # inferred from PDF page structure
EVIDENCE_MANUAL = "manual"                   # a human entered/confirmed it
EVIDENCE_UNKNOWN = "unknown"                 # could not be determined
EVIDENCES = (
    EVIDENCE_PIPELINE, EVIDENCE_PDF_METADATA, EVIDENCE_STRUCTURAL,
    EVIDENCE_MANUAL, EVIDENCE_UNKNOWN,
)

# (a) born-digital vs scanned vs downloaded — made explicit instead of inferred
# from text_status.
PROV_BORN_DIGITAL = "born_digital"
PROV_SCANNED = "scanned"
PROV_DOWNLOADED = "downloaded"
PROV_UNKNOWN = "unknown"

# Seed vocabularies. Open (a new engine is a row, not a migration), mirroring the
# §12.4 lookup-table convention used by text_status / digitizer_kind in schema.sql.
_PROVENANCE_KINDS = [
    (PROV_BORN_DIGITAL, "Born-digital (typeset/publisher PDF, no scan)"),
    (PROV_SCANNED,      "Scanned from a physical copy"),
    (PROV_DOWNLOADED,   "Downloaded digital text (BDRC / archive.org / etc.)"),
    (PROV_UNKNOWN,      "Provenance not determined"),
]
_ENGINES = [
    # capture-stage engines / sources
    ("flatbed",        STAGE_CAPTURE, "Flatbed scanner"),
    ("book_scanner",   STAGE_CAPTURE, "Overhead / book scanner"),
    ("phone",          STAGE_CAPTURE, "Phone camera capture"),
    ("bdrc_download",  STAGE_CAPTURE, "Downloaded from BDRC"),
    ("archive_org",    STAGE_CAPTURE, "Downloaded from archive.org"),
    ("publisher_pdf",  STAGE_CAPTURE, "Publisher-supplied digital PDF"),
    ("capture_unknown", STAGE_CAPTURE, "Capture source unknown"),
    # ocr-stage engines (the three former digitizer_kind codes, split out)
    ("tesseract_iast", STAGE_OCR, "OCRmyPDF + Tesseract (eng + Shreeshrii IAST)"),
    ("gcv",            STAGE_OCR, "OCRmyPDF + Cloud Vision (ualiawan fork / adapter)"),
    ("abbyy",          STAGE_OCR, "ABBYY FineReader (manual on Mac) → import PDF/A"),
    ("ocr_none",       STAGE_OCR, "No OCR performed"),
    ("ocr_unknown",    STAGE_OCR, "OCR engine unknown"),
]

# Existing digitizer_kind code → new (stage, engine) so the backfill can map the
# old column. All three legacy codes are OCR-stage; capture was never modeled.
LEGACY_DIGITIZER_MAP = {
    "ocrmypdf_tesseract": (STAGE_OCR, "tesseract_iast"),
    "ocrmypdf_gcv":       (STAGE_OCR, "gcv"),
    "abbyy_import":       (STAGE_OCR, "abbyy"),
}

__all__ = [
    "STAGE_CAPTURE", "STAGE_OCR", "STAGES",
    "EVIDENCE_PIPELINE", "EVIDENCE_PDF_METADATA", "EVIDENCE_STRUCTURAL",
    "EVIDENCE_MANUAL", "EVIDENCE_UNKNOWN",
    "PROV_BORN_DIGITAL", "PROV_SCANNED", "PROV_DOWNLOADED", "PROV_UNKNOWN",
    "LEGACY_DIGITIZER_MAP",
    "DigitizationEvent", "Provenance",
    "ensure_schema", "register_engine",
    "engines", "provenance_kinds", "events", "latest", "provenance",
    "record_event", "set_provenance_kind", "infer_provenance_kind",
]


# ── Return shapes ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DigitizationEvent:
    """One capture or OCR pass over a holding. `params` is the decoded
    params_json (see the doc's §4 for the documented keys: dpi, color_mode,
    px_width/px_height, ocr_langs, pdfa, source_url, …)."""
    id: int
    holding_id: int
    stage: str
    engine: "str | None"
    engine_version: "str | None"
    performed_at: "str | None"
    params: dict = field(default_factory=dict)
    quality_score: "float | None" = None
    evidence: str = EVIDENCE_UNKNOWN
    superseded: bool = False
    notes: "str | None" = None
    created_at: "str | None" = None


@dataclass(frozen=True)
class Provenance:
    """A holding's resolved provenance: the (a) kind plus the *current* (non-
    superseded) capture and OCR events. Either event may be None when that stage
    has no record."""
    holding_id: int
    kind: "str | None"
    capture: "DigitizationEvent | None"
    ocr: "DigitizationEvent | None"


# ── Schema ownership ─────────────────────────────────────────────────────────
# This module owns the DDL for its own tables so it is self-contained and a
# client can stand the schema up against any catalogue connection. When folding
# into the central schema, paste _SCHEMA_SQL into schema.sql and the ALTERs into
# db.py::_migrate under a `version < 4` gate (see the doc's §5).
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS provenance_kind (code TEXT PRIMARY KEY, label TEXT);

-- One engine table, discriminated by stage, so a single FK serves both sides.
CREATE TABLE IF NOT EXISTS digitization_engine (
  code  TEXT PRIMARY KEY,
  stage TEXT NOT NULL,                       -- 'capture' | 'ocr'
  label TEXT NOT NULL
);

-- The history log: one row per capture or OCR pass. Re-OCR marks the prior
-- same-stage row superseded=1 and inserts a new one, preserving full history.
CREATE TABLE IF NOT EXISTS digitization_event (
  id             INTEGER PRIMARY KEY,
  holding_id     INTEGER NOT NULL REFERENCES holding(id) ON DELETE CASCADE,
  stage          TEXT NOT NULL,              -- 'capture' | 'ocr'
  engine         TEXT REFERENCES digitization_engine(code),
  engine_version TEXT,                       -- 'Tesseract 5.3.4'; NULL = unknown
  performed_at   TEXT,                        -- (d) scan/OCR date; NULL = unknown
  params_json    TEXT,                        -- (e) structured details (see doc §4)
  quality_score  REAL,                        -- real OCR confidence when known
  evidence       TEXT NOT NULL DEFAULT 'unknown',
  superseded     INTEGER NOT NULL DEFAULT 0,
  notes          TEXT,
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS digitization_event_holding_idx
  ON digitization_event(holding_id, stage);
"""

# Denormalized "latest" pointers + (a) on holding, added only if missing. SQLite
# has no ADD COLUMN IF NOT EXISTS, so guard on PRAGMA table_info.
_HOLDING_COLUMNS = {
    "provenance_kind":          "TEXT REFERENCES provenance_kind(code)",
    "current_capture_event_id": "INTEGER REFERENCES digitization_event(id)",
    "current_ocr_event_id":     "INTEGER REFERENCES digitization_event(id)",
}


def ensure_schema(conn) -> None:
    """Create the digitization tables, seed the vocabularies, and add the
    holding provenance columns — all idempotent. Safe to call on a fresh or a
    fully-migrated DB. A client that always runs the catalogue's normal init can
    skip this once the DDL is folded into schema.sql/_migrate; it exists so this
    module stands alone and tests/backfills don't depend on that fold landing."""
    conn.executescript(_SCHEMA_SQL)
    conn.executemany(
        "INSERT OR IGNORE INTO provenance_kind (code, label) VALUES (?, ?)",
        _PROVENANCE_KINDS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO digitization_engine (code, stage, label) VALUES (?, ?, ?)",
        _ENGINES,
    )
    have = {r[1] for r in conn.execute("PRAGMA table_info(holding)").fetchall()}
    for col, decl in _HOLDING_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE holding ADD COLUMN {col} {decl}")


def register_engine(conn, code: str, stage: str, label: str) -> None:
    """Add a capture/OCR engine to the open vocabulary (a new value is data, not
    a migration). Idempotent; never clobbers an existing label."""
    _require_stage(stage)
    conn.execute(
        "INSERT OR IGNORE INTO digitization_engine (code, stage, label) VALUES (?, ?, ?)",
        (code, stage, label),
    )


# ── Reads ────────────────────────────────────────────────────────────────────
def engines(conn, stage: "str | None" = None) -> "list[tuple[str, str, str]]":
    """(code, stage, label) for every registered engine, optionally one stage."""
    if stage is not None:
        _require_stage(stage)
        rows = conn.execute(
            "SELECT code, stage, label FROM digitization_engine WHERE stage = ? "
            "ORDER BY code", (stage,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT code, stage, label FROM digitization_engine ORDER BY stage, code"
        ).fetchall()
    return [tuple(r) for r in rows]


def provenance_kinds(conn) -> "list[tuple[str, str]]":
    """(code, label) for every provenance kind."""
    return [tuple(r) for r in conn.execute(
        "SELECT code, label FROM provenance_kind ORDER BY code").fetchall()]


def events(conn, holding_id: int, *, stage: "str | None" = None,
           include_superseded: bool = True) -> "list[DigitizationEvent]":
    """All digitization events for a holding, newest first. Filter by `stage`
    and/or hide superseded passes."""
    sql = ["SELECT ", _EVENT_COLS, " FROM digitization_event WHERE holding_id = ?"]
    args: list = [holding_id]
    if stage is not None:
        _require_stage(stage)
        sql.append(" AND stage = ?")
        args.append(stage)
    if not include_superseded:
        sql.append(" AND superseded = 0")
    sql.append(" ORDER BY id DESC")
    return [_row_to_event(r) for r in conn.execute("".join(sql), args).fetchall()]


def latest(conn, holding_id: int, stage: str) -> "DigitizationEvent | None":
    """The current (non-superseded) event for a stage, or None."""
    _require_stage(stage)
    row = conn.execute(
        f"SELECT {_EVENT_COLS} FROM digitization_event "
        "WHERE holding_id = ? AND stage = ? AND superseded = 0 "
        "ORDER BY id DESC LIMIT 1", (holding_id, stage)).fetchone()
    return _row_to_event(row) if row else None


def provenance(conn, holding_id: int) -> Provenance:
    """A holding's resolved provenance: kind + current capture + current OCR.

    Reads the denormalized `current_*_event_id` pointers when set (one indexed
    lookup each) and falls back to the newest non-superseded event per stage, so
    it is correct even on a holding whose pointers were never populated."""
    row = conn.execute(
        "SELECT provenance_kind, current_capture_event_id, current_ocr_event_id "
        "FROM holding WHERE id = ?", (holding_id,)).fetchone()
    if row is None:
        raise ValueError(f"no holding with id {holding_id}")
    kind, cap_id, ocr_id = row
    cap = _event_by_id(conn, cap_id) if cap_id else latest(conn, holding_id, STAGE_CAPTURE)
    ocr = _event_by_id(conn, ocr_id) if ocr_id else latest(conn, holding_id, STAGE_OCR)
    return Provenance(holding_id=holding_id, kind=kind, capture=cap, ocr=ocr)


# ── Writes ───────────────────────────────────────────────────────────────────
def record_event(conn, holding_id: int, *, stage: str,
                  engine: "str | None" = None,
                  engine_version: "str | None" = None,
                  performed_at: "str | None" = None,
                  params: "dict | None" = None,
                  quality_score: "float | None" = None,
                  evidence: str = EVIDENCE_UNKNOWN,
                  notes: "str | None" = None,
                  supersede: bool = True) -> DigitizationEvent:
    """Record a capture or OCR pass and make it the current one for its stage.

    When `supersede` (the default), any existing current event of the same stage
    is marked superseded and `holding.current_<stage>_event_id` is repointed at
    the new row — so `provenance()` always reflects the latest pass while older
    passes stay in the log. Pass supersede=False only to backfill a historical
    pass that should NOT become current.

    `engine`, if given, must be registered with a matching stage. Does not
    commit; the caller owns the transaction."""
    _require_stage(stage)
    if evidence not in EVIDENCES:
        raise ValueError(f"unknown evidence {evidence!r}; expected one of {EVIDENCES}")
    if engine is not None:
        eng_stage = conn.execute(
            "SELECT stage FROM digitization_engine WHERE code = ?", (engine,)).fetchone()
        if eng_stage is None:
            raise ValueError(
                f"engine {engine!r} is not registered; call register_engine() first")
        if eng_stage[0] != stage:
            raise ValueError(
                f"engine {engine!r} is a {eng_stage[0]!r}-stage engine, "
                f"cannot be used for a {stage!r} event")

    if supersede:
        conn.execute(
            "UPDATE digitization_event SET superseded = 1 "
            "WHERE holding_id = ? AND stage = ? AND superseded = 0",
            (holding_id, stage))

    cur = conn.execute(
        "INSERT INTO digitization_event "
        "(holding_id, stage, engine, engine_version, performed_at, params_json, "
        " quality_score, evidence, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (holding_id, stage, engine, engine_version, performed_at,
         json.dumps(params) if params else None, quality_score, evidence, notes))
    event_id = cur.lastrowid

    if supersede:
        col = "current_capture_event_id" if stage == STAGE_CAPTURE else "current_ocr_event_id"
        conn.execute(
            f"UPDATE holding SET {col} = ? WHERE id = ?", (event_id, holding_id))

    return _event_by_id(conn, event_id)


def set_provenance_kind(conn, holding_id: int, kind: str) -> None:
    """Set (a) — born_digital | scanned | downloaded | unknown. Does not commit."""
    if conn.execute(
            "SELECT 1 FROM provenance_kind WHERE code = ?", (kind,)).fetchone() is None:
        raise ValueError(f"unknown provenance kind {kind!r}")
    conn.execute(
        "UPDATE holding SET provenance_kind = ? WHERE id = ?", (kind, holding_id))


def infer_provenance_kind(text_status: "str | None") -> str:
    """Best-guess provenance kind from a holding's text_status, for backfilling
    (a) on existing rows. `native` is born-digital; OCR/image-only rows came from
    *some* capture but we cannot yet tell scanned-here from downloaded, so they
    map to `scanned` provisionally (refine later from source/path). Unknown/empty
    status → unknown."""
    if text_status == "native":
        return PROV_BORN_DIGITAL
    if text_status in ("ocr_good", "ocr_poor", "image_only"):
        return PROV_SCANNED
    return PROV_UNKNOWN


# ── Internals ────────────────────────────────────────────────────────────────
_EVENT_COLS = (
    "id, holding_id, stage, engine, engine_version, performed_at, params_json, "
    "quality_score, evidence, superseded, notes, created_at"
)


def _require_stage(stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")


def _event_by_id(conn, event_id: int) -> "DigitizationEvent | None":
    row = conn.execute(
        f"SELECT {_EVENT_COLS} FROM digitization_event WHERE id = ?",
        (event_id,)).fetchone()
    return _row_to_event(row) if row else None


def _row_to_event(row) -> DigitizationEvent:
    (id_, holding_id, stage, engine, engine_version, performed_at, params_json,
     quality_score, evidence, superseded, notes, created_at) = row
    return DigitizationEvent(
        id=id_, holding_id=holding_id, stage=stage, engine=engine,
        engine_version=engine_version, performed_at=performed_at,
        params=json.loads(params_json) if params_json else {},
        quality_score=quality_score, evidence=evidence,
        superseded=bool(superseded), notes=notes, created_at=created_at)
