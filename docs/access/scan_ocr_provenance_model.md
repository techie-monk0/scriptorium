# Scan / OCR provenance model (2026-06-24)

Per-holding provenance for **how each book became digital text**: whether it is
digital-first, whether/how it was OCRed, with which engines, when, and at what
resolution. Built on the existing `holding` grain (the physical file — one
edition can have several scans), extending what `text_status` / `digitizer_used`
already started.

Companion access module: `catalogue/access/scan_ocr.py` (the *only* code that
touches these tables — see §6). The OCR engine that produces this provenance is the
separate [`techie-monk0/scholia-rag-ocr`](https://github.com/techie-monk0/scholia-rag-ocr) repo.

---

## 1. Requirements

For every holding we want to answer:

- **(a)** Is it a digital-first book?
- **(b)** If not, has it been OCRed?
- **(c)** Which engines was it **scanned** and **OCRed** with?
- **(d)** When was it scanned / OCRed?
- **(e)** Scan details useful later — resolution, colour mode, languages, etc.

For existing books with a text layer, mine the PDF for engine/resolution where
present; otherwise record an honest **unknown**.

## 2. What already exists (and the gap)

| Need | Today | Verdict |
|------|-------|---------|
| (a) digital-first | implicit in `holding.text_status` (`native`) | make explicit |
| (b) OCRed | `text_status` = `ocr_good`/`ocr_poor` | keep as-is |
| (c) engines | `holding.digitizer_used` → `digitizer_kind` | **empty on all 668; bundles scan+OCR** |
| (d) date | `date_added` is the *catalogue* date | **missing** |
| (e) scan details | — | **missing** |

The core modelling fix: `digitizer_kind` collapses **capture** (paper → images)
and **OCR** (images → text) into one code. Requirement (c) explicitly wants both,
and they are separate events with separate engines and dates — a book scanned
once may be re-OCRed many times as engines improve. So we split the two stages
and record **history**, not just latest state (re-OCR is already first-class in
`text_status`).

## 3. Schema additions

### 3.1 Reference tables (open vocabularies, §12.4)

```sql
-- (a) born-digital vs scanned vs downloaded — explicit, not inferred
CREATE TABLE IF NOT EXISTS provenance_kind (code TEXT PRIMARY KEY, label TEXT);
--   born_digital | scanned | downloaded | unknown

-- (c) ONE engine table, discriminated by stage → a single FK serves both sides
CREATE TABLE IF NOT EXISTS digitization_engine (
  code  TEXT PRIMARY KEY,
  stage TEXT NOT NULL,                       -- 'capture' | 'ocr'
  label TEXT NOT NULL
);
--   capture: flatbed, book_scanner, phone, bdrc_download, archive_org,
--            publisher_pdf, capture_unknown
--   ocr:     tesseract_iast, gcv, abbyy, ocr_none, ocr_unknown
```

`digitization_engine` **supersedes** `digitizer_kind`. The three legacy codes map
onto the `ocr` rows (`ocrmypdf_tesseract`→`tesseract_iast`, `ocrmypdf_gcv`→`gcv`,
`abbyy_import`→`abbyy`); capture was never modelled. `text_status` is unchanged
and still answers (b).

### 3.2 Event log (history — the core)

```sql
CREATE TABLE IF NOT EXISTS digitization_event (
  id             INTEGER PRIMARY KEY,
  holding_id     INTEGER NOT NULL REFERENCES holding(id) ON DELETE CASCADE,
  stage          TEXT NOT NULL,              -- 'capture' | 'ocr'
  engine         TEXT REFERENCES digitization_engine(code),
  engine_version TEXT,                       -- 'Tesseract 5.3.4'; NULL = unknown
  performed_at   TEXT,                        -- (d) scan/OCR date; NULL = unknown
  params_json    TEXT,                        -- (e) structured details (§4)
  quality_score  REAL,                        -- real OCR confidence when known
  evidence       TEXT NOT NULL DEFAULT 'unknown',
  superseded     INTEGER NOT NULL DEFAULT 0,  -- 1 once a later same-stage pass replaces it
  notes          TEXT,
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS digitization_event_holding_idx
  ON digitization_event(holding_id, stage);
```

One row per capture and per OCR pass. A re-OCR marks the prior same-stage row
`superseded = 1` and inserts a new one — latest state stays queryable, history
is preserved. `evidence` (`pipeline | pdf_metadata | structural_inference |
manual | unknown`) keeps an honest "unknown" distinct from a confirmed value.

### 3.3 Denormalized "latest" on `holding`

```sql
ALTER TABLE holding ADD COLUMN provenance_kind TEXT REFERENCES provenance_kind(code); -- (a)
ALTER TABLE holding ADD COLUMN current_capture_event_id INTEGER REFERENCES digitization_event(id);
ALTER TABLE holding ADD COLUMN current_ocr_event_id     INTEGER REFERENCES digitization_event(id);
```

Cheap display without a join/aggregate: `provenance()` follows these pointers and
falls back to the newest non-superseded event per stage if they were never set.
`digitizer_used` is retired after backfill; `ocr_quality_score` (currently a `1.0`
placeholder on every row) is superseded by per-event `quality_score`.

`provenance_kind` is also exposed externally on the `v_holding_files` read-contract view, so
the cross-repo `ocr_pipeline` consumer (`audit_born_digital.py`) can read born-digital-vs-
scanned instead of recomputing it.

That same view also carries **`pub_id`** — the stable, opaque edition-identity token external
tools (BuddhistLLM's RAG corpus) cite by and `ocr_pipeline`'s `build_rag_manifest.py` stamps into
each manifest row. A tool stores only `pub_id` + `content_hash` and looks up the rest live. Identity
stability (never rebinds, always resolves) and the un-deletable guarantee for a cited edition are the
stability contract — see `docs/access/external_tool_dependency_contract.md`.

## 4. `params_json` convention (documented, not enforced)

```json
{
  "dpi": 400,
  "color_mode": "grayscale",          // bitonal | grayscale | color
  "px_width": 2480, "px_height": 3508,
  "page_count": 312,
  "ocr_langs": ["eng", "iast"],
  "pdfa": "pdfa-2b",
  "source_url": "https://...",         // for downloaded / scanned-elsewhere
  "born_digital_signal": "fonts+vectors, no full-page raster"
}
```

Promote a key to a real column only if you need to filter/sort on it (likely
`dpi`, `color_mode`); everything else stays in JSON so the schema doesn't churn.

## 5. Migration & backfill

**Fold into the central schema** when ready: paste §3.1–3.2 DDL into
`catalogue/db/schema.sql` (with the seed `INSERT OR IGNORE`s), and the §3.3
`ALTER`s into `catalogue/db/db.py::_migrate` under a `version < 4` gate, then bump
`schema_meta.schema_version` to `4`. Until then `scan_ocr.ensure_schema(conn)`
stands the same objects up idempotently, so the module and its tests stand alone.

**Backfill (best-effort, honest unknowns):**

1. Seed vocabularies; map `digitizer_kind` → `digitization_engine`
   (`scan_ocr.LEGACY_DIGITIZER_MAP`).
2. `provenance_kind` for all 668 from `text_status`
   (`scan_ocr.infer_provenance_kind`): `native`→`born_digital`, OCR/image-only→
   `scanned` (provisional; refine `scanned` vs `downloaded` from source/path later).
3. Per holding, mine the PDF and `record_event(...)`:
   - **OCR engine/version** ← `/Producer`, `/Creator`, XMP (OCRmyPDF stamps itself
     and often the Tesseract version) → `evidence='pdf_metadata'`, else
     `engine='ocr_unknown'`, `evidence='unknown'`.
   - **born-digital vs scanned** ← page structure (full-page raster + invisible
     text = scanned; real fonts/vectors, no full-page image = born-digital) →
     `evidence='structural_inference'`.
   - **resolution** ← *computed* per page (embedded image px ÷ display size in
     points); not a metadata field.
   - **date** ← `/CreationDate`/`/ModDate` as a low-confidence hint only.
4. Anything unrecoverable → `engine=*_unknown`, `performed_at=NULL`,
   `evidence='unknown'`.
5. **Wire the pipeline** (`catalogue/domain/sweep.py`) to `record_event(...,
   evidence='pipeline')` at scan/OCR time — it knows engine, version, DPI, langs,
   and date at that moment, so new books are never unknown. This is the real win;
   backfill only bounds the legacy tail.

## 6. Access module — `catalogue/access/scan_ocr.py`

Lives in `catalogue/access/` — the home for all per-concern DB access API
modules (see that package's `__init__` for the shared convention).

All digitization-provenance reads/writes go through this module; **no other code
touches `digitization_engine` / `digitization_event` / `provenance_kind` or the
holding provenance columns directly.** Clients pass an open connection (from
`db.connect`/`init_db`) and get frozen dataclasses back. Nothing here commits —
the caller owns the transaction, matching the rest of the codebase.

**Constants:** `STAGE_CAPTURE`/`STAGE_OCR`; `EVIDENCE_*`; `PROV_*`;
`LEGACY_DIGITIZER_MAP`.

**Return shapes:** `DigitizationEvent` (with `params` decoded to a dict) and
`Provenance` (`kind`, `capture`, `ocr`).

**Schema ownership:**
- `ensure_schema(conn)` — create tables, seed vocab, add holding columns; idempotent.
- `register_engine(conn, code, stage, label)` — add an engine to the vocabulary.

**Reads:**
- `engines(conn, stage=None)` → `(code, stage, label)` list.
- `provenance_kinds(conn)` → `(code, label)` list.
- `events(conn, holding_id, *, stage=None, include_superseded=True)` → list, newest first.
- `latest(conn, holding_id, stage)` → current event or `None`.
- `provenance(conn, holding_id)` → `Provenance` (kind + current capture + current OCR).

**Writes (no commit):**
- `record_event(conn, holding_id, *, stage, engine=None, engine_version=None,
  performed_at=None, params=None, quality_score=None, evidence=EVIDENCE_UNKNOWN,
  notes=None, supersede=True)` → the inserted `DigitizationEvent`. Supersedes the
  prior current same-stage event and repoints `holding.current_<stage>_event_id`.
  `supersede=False` backfills a historical pass without making it current.
- `set_provenance_kind(conn, holding_id, kind)` — set (a).

**Helper:** `infer_provenance_kind(text_status)` — backfill (a) from `text_status`.

Validation: stage must be known; a given `engine` must be registered with a
matching stage; `evidence`/`kind` must be known codes — all raise `ValueError`.

```python
from catalogue.db import db
from catalogue.access import scan_ocr as so

conn = db.connect(path)
so.ensure_schema(conn)                       # no-op once folded into schema.sql

so.set_provenance_kind(conn, hid, so.infer_provenance_kind("ocr_good"))
so.record_event(conn, hid, stage=so.STAGE_OCR, engine="tesseract_iast",
                engine_version="Tesseract 5.3.4", performed_at="2026-01-15",
                params={"dpi": 400, "color_mode": "grayscale",
                        "ocr_langs": ["eng", "iast"]},
                quality_score=0.92, evidence=so.EVIDENCE_PDF_METADATA)

prov = so.provenance(conn, hid)              # → kind + current capture + current OCR
conn.commit()
```

> **Pattern note.** `scan_ocr.py` is the first of a planned set of per-concern DB
> access modules living in `catalogue/access/` (one bounded vocabulary +
> invariants behind a small typed surface, owning its own DDL, committing
> nothing). Future modules for other parts of the schema drop into the same
> package and follow this shape.

## 7. Design decisions

- **Event log over flat columns** — re-OCR is a real workflow; flat columns
  remember only the latest pass. Denormalized pointers recover the cheap-read
  benefit of flat columns without losing history.
- **One engine table, stage-discriminated** — a single FK on `digitization_event`
  serves both capture and OCR; `record_event` enforces engine.stage == event.stage.
- **`evidence` is mandatory** — a value's trustworthiness (pipeline-emitted vs.
  guessed vs. unknown) matters as much as the value when read back later.
- **Provenance lives on the holding, not the edition** — one edition can have
  several scans; the file is the thing that was digitized.
