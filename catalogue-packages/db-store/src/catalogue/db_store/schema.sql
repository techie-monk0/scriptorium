-- Buddhist Library Catalogue — schema (step 1)
-- Honors §5 (data model), §12 rule 4 (open vocabularies as lookup tables,
-- not CHECK/enums) and §5 cache layout.

PRAGMA foreign_keys = ON;

-- ── Open vocabularies ──────────────────────────────────────────────────────
-- Every vocabulary that may grow is a lookup table; adding a value is an
-- INSERT, not a migration (§12.4).
CREATE TABLE IF NOT EXISTS work_type           (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS alias_scheme        (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS text_status         (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS relation_type       (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS review_item_type    (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS form_type           (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS holding_type         (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS locator_type         (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS digitizer_kind      (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS contributor_role    (code TEXT PRIMARY KEY, label TEXT);

INSERT OR IGNORE INTO text_status (code, label) VALUES
  ('native',     'Native digital text'),
  ('ocr_good',   'OCR text, quality acceptable'),
  ('ocr_poor',   'OCR text, quality poor — re-OCR'),
  ('image_only', 'No text layer — image-only'),
  ('none',       'No text available');

INSERT OR IGNORE INTO alias_scheme (code, label) VALUES
  ('iast', 'IAST'), ('wylie', 'Wylie/EWTS'), ('acip', 'ACIP'),
  ('thl', 'THL phonetic'), ('phonetic', 'Publisher phonetic'),
  ('english', 'English'), ('other', 'Other'),
  ('filename', 'Filename-derived (superseded)'),
  ('bo', 'Tibetan script'), ('sa', 'Sanskrit/Devanagari script');

INSERT OR IGNORE INTO relation_type (code, label) VALUES
  ('comments_on',     'Comments on'),
  ('sub_comments_on', 'Sub-commentary on'),
  ('summarizes',      'Summarizes'),
  ('cites',           'Cites');
-- `same_cycle` intentionally absent — collection membership, not a graph edge (§4.4).

INSERT OR IGNORE INTO review_item_type (code, label) VALUES
  ('alias_merge', 'Alias merge'),
  ('toc_classification', 'TOC classification'),
  ('fuzzy_match', 'Fuzzy match'),
  ('edition_dedup', 'Edition dedup'),
  ('low_confidence_extraction', 'Low-confidence extraction'),
  ('low_quality_ocr', 'Low-quality OCR'),
  ('book_toc_pattern', 'Book TOC pattern'),
  ('extraction_note', 'Extraction note (advisory)'),
  ('ingest', 'Ingest / reconcile (new, re-OCR, moved, missing file)'),
  ('work_authorship', 'Work authorship (single-source candidate)'),
  ('person_authority', 'Person authority (BDRC BLMP fuzzy hit — confirm)'),
  ('work_canonical', 'Work canonical id (BDRC BLMP fuzzy hit — confirm)'),
  ('person_work_joint', 'Person identity via authored work (confirm / conflict)'),
  ('title_proposal', 'Title re-derived from title page (confirm / revert)'),
  ('edition_metadata', 'Edition metadata from ISBN/LCCN (confirm / revert)'),
  ('edition_verify', 'Edition metadata diff vs authorities (confirm / dismiss)'),
  ('work_merge', 'Work dedup (fuzzy duplicate works — confirm merge / keep separate)');

INSERT OR IGNORE INTO form_type (code, label) VALUES
  ('electronic', 'Electronic'), ('physical', 'Physical');

-- Contributor roles (§5). work_contributor.role FK-references this; the
-- proposal promoter (catalogue/promote.py) writes 'author'/'translator'.
INSERT OR IGNORE INTO contributor_role (code, label) VALUES
  ('author', 'Author'),
  ('translator', 'Translator'),
  ('editor', 'Editor'),
  ('compiler', 'Compiler');

-- §4.8b: open vocabulary so a new Digitizer drops in as a row, not code.
INSERT OR IGNORE INTO digitizer_kind (code, label) VALUES
  ('ocrmypdf_tesseract', 'OCRmyPDF + Tesseract (eng + Shreeshrii IAST)'),
  ('ocrmypdf_gcv',       'OCRmyPDF + Cloud Vision (ualiawan fork / adapter)'),
  ('abbyy_import',       'ABBYY FineReader (manual on Mac) → import PDF/A');

-- ── Core entities ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS work (
  id                INTEGER PRIMARY KEY,
  work_type         TEXT REFERENCES work_type(code),
  original_language TEXT,
  era               TEXT,
  canonical_system  TEXT,                    -- e.g. 'toh', 'derge'
  canonical_number  TEXT,                    -- nullable, auto-filled
  sanskrit_title    TEXT,                    -- native-script/IAST original title of the work
  tibetan_title     TEXT,                    -- native-script/Wylie original title of the work
  notes             TEXT,
  review_status     TEXT,                    -- works-review verdict: NULL=unreviewed / 'ok' / 'needs_fix'
  review_note       TEXT,
  reviewed_at       TEXT,
  -- The work's Buddhist tradition (a `tradition.name`, or NULL = unclassified). Single
  -- editable field mirroring person.tradition; defaults from the authors' lineage plus
  -- subject/title signals, user-overridable.
  tradition         TEXT,
  -- The work's doctrinal / tenet system (siddhānta home — e.g. 'Prāsaṅgika-Madhyamaka';
  -- a free-text label, or NULL). Single editable field mirroring person.tenet_system; the
  -- classifier companions (tenet_source/conf/evidence) are added by
  -- catalogue.db_store.migrate_tenet.
  tenet_system      TEXT,
  -- The work's rhetorical genre (a controlled vocab: Argumentative / Doxography / Monograph,
  -- or NULL). Single editable field declared in catalogue.contracts.fields (the scalar
  -- controlled-vocab registry that also backs tenet_system / tradition / work_type).
  genre             TEXT,
  deleted_at        TEXT                      -- soft-delete tombstone (NULL = live); id frozen once set
);

CREATE TABLE IF NOT EXISTS person (
  id           INTEGER PRIMARY KEY,
  primary_name TEXT NOT NULL,
  role_hint    TEXT,
  origin       TEXT,
  dates        TEXT,
  external_id  TEXT,                         -- BDRC / Wikidata / VIAF id
  -- 3-state reconciliation (§verify): 'provisional' (typed/extracted, not yet
  -- checked) → 'verified' (matched to an authority, external_id set) OR
  -- 'confirmed_local' (human confirmed no authority record exists; THIS row is
  -- canonical — e.g. a modern self-published author). Lets authority-absent
  -- people leave the verify worklist, which external_id IS NULL alone cannot.
  verification_status TEXT DEFAULT 'provisional',
  -- 1 when the bind harvested the hub id but the cross-link fetch FAILED (network
  -- down): the row is bound yet its key-set is partial, so dedup can't fully see it.
  -- Marks it for re-harvest and lets on-bind dedup stay conservative (suggest, not
  -- auto-merge). Cleared to 0 on a complete (re)bind. See authority_dedup_plan.md §6.17.
  harvest_incomplete INTEGER NOT NULL DEFAULT 0,
  -- A picked-but-not-yet-committed authority id (namespaced, e.g. 'wikidata:Q…').
  -- The add-person form stores the operator's pick here instead of external_id when
  -- it can't confidently dedup, so the row enters the review worklist (which requires
  -- external_id IS NULL) and acceptance there runs on-bind dedup before it's final.
  -- NOT a key-set member (kept out of person_external_id), so it never makes a row
  -- look bound. Cleared when the suggestion is bound or dismissed. See §dedup.
  suggested_external_id TEXT,
  -- free-text curator note: disambiguation rationale, conflation history, the
  -- "why this row is its own person" reasoning that no structured field captures.
  notes        TEXT,
  -- The author's Buddhist tradition / lineage (a `tradition.name`, or NULL if unknown).
  -- Seeded from the config author→lineage map (catalogue.db_store.migrate_tradition),
  -- editable in the person UI, and the source of the DEFAULT tradition for the works /
  -- editions they authored (else the library default — the first `_tradition` entry).
  tradition    TEXT,
  -- The author's doctrinal / tenet system (siddhānta home — e.g. 'Prāsaṅgika-Madhyamaka';
  -- a free-text label, or NULL). Editable in the person UI; the classifier companions
  -- (tenet_source/conf/evidence) are added by catalogue.db_store.migrate_tenet.
  tenet_system TEXT,
  deleted_at   TEXT                           -- soft-delete tombstone (NULL = live); id frozen once set
);

CREATE TABLE IF NOT EXISTS edition (
  id        INTEGER PRIMARY KEY,
  title     TEXT NOT NULL,
  subtitle  TEXT,                            -- ISBD subtitle (after ':') of this edition
  volume    TEXT,                            -- volume designation ('v. 1', 'Volume 2', …)
  -- Volume self-grouping (FRBR model, 2026-06-04): editions sharing volume_set_id
  -- are the N physical books of ONE multi-volume publication (e.g. Lamrim Chenmo
  -- vols 1–3), ordered by volume_seq. NULL = standalone. "All editions of a work"
  -- dedupes by volume_set_id so a set shows once, not once per volume — a set must
  -- NEVER be merged as duplicate works (see catalogue/work_dedup.py).
  volume_set_id INTEGER,
  volume_seq    INTEGER,
  sanskrit_title TEXT,                       -- ISBD parallel '=' title, native-script/IAST
  tibetan_title  TEXT,                       -- ISBD parallel '=' title, native-script/Wylie
  publisher TEXT,
  year      INTEGER,
  isbn      TEXT,
  -- OpenLibrary work key ('/works/OL…W') resolved from this edition's ISBN. Clusters
  -- editions of one work across formats (print/epub/pdf carry DIFFERENT ISBNs), so a
  -- phone scan can detect "already have this in another form". Nullable; populated by
  -- the capture verdict + catalogue/cli/backfill_ol_work_key.py. Index in db._migrate.
  ol_work_key TEXT,
  language  TEXT,
  notes     TEXT,
  -- Whether this edition holds ONE classical text or several. Operator-set (the
  -- /editions/structure checkbox tool); drives which detection runs: 'single_work'
  -- → single Skt/Tib autodetect, 'multi_work' → the segmentation pass. NULL = not
  -- yet classified. Open vocab (§5), not a CHECK.
  structure TEXT,
  review_status TEXT,                        -- catalogue-review verdict: NULL=unreviewed / 'ok' / 'needs_fix'
  review_flags  TEXT,                        -- JSON {title,contributors,structure,authors: bool}
  review_note   TEXT,
  reviewed_at   TEXT,
  -- The edition's Buddhist tradition (a `tradition.name`, or NULL). Single editable field
  -- mirroring work.tradition / person.tradition; an edition-level override for anthologies /
  -- mixed volumes. Defaults in the UI from the edition's works (else authors' lineage, else
  -- library default).
  tradition     TEXT,
  -- Stable, opaque external identity for consumers (BuddhistLLM RAG, …): a write-once
  -- UUID minted by the edition_pub_id_mint trigger, immutable thereafter (the
  -- edition_pub_id_immutable trigger enforces it), never reused. Decoupled from the int
  -- PK so a citation never inherits id-reuse. The stability contract (S1–S3) — see
  -- docs/access/external_tool_dependency_contract.md and citation_edition_contract_plan.md.
  pub_id        TEXT,
  -- Forwarding pointer (stability S2): when this edition is merged into another, it is
  -- tombstoned and superseded_by = the winner's id, so resolve(pub_id) follows the chain to the
  -- canonical live edition instead of dangling. NULL = not superseded. Only set for a CITED loser
  -- (a non-cited loser still hard-deletes). See external_deps.supersede / resolve.
  superseded_by INTEGER REFERENCES edition(id),
  deleted_at    TEXT                          -- soft-delete tombstone (NULL = live); id frozen once set
);

-- Persisted resolution of an edition_verify diff so a re-run does not resurface
-- a decision the operator already made (dismiss / accept-ours / accept-external
-- scalar / backfill). One row per (edition, field) the operator acted on.
CREATE TABLE IF NOT EXISTS edition_verify_resolution (
  id          INTEGER PRIMARY KEY,
  edition_id  INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  field       TEXT NOT NULL,                 -- title|publisher|year|authors|translators
  category    TEXT,                          -- empty_fillable|role_swap|title_variant|weak_match|hard_conflict
  decision    TEXT NOT NULL,                 -- accepted_ours|accepted_external|backfilled|dismissed
  value       TEXT,                          -- the value written, when a write occurred
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS edition_verify_resolution_eid_idx
  ON edition_verify_resolution(edition_id);

CREATE TABLE IF NOT EXISTS holding (
  id                INTEGER PRIMARY KEY,
  edition_id        INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  form              TEXT REFERENCES form_type(code),
  file_path         TEXT,
  file_hash         TEXT,                    -- SHA-256 of bytes (exact file identity)
  content_hash      TEXT,                    -- fingerprint of the TEXT layer when
                                             -- trustworthy ('t:'…), else byte hash
                                             -- ('b:'…); stable across annotation
  shelf_location    TEXT,
  ocr_quality_score REAL,
  text_status       TEXT REFERENCES text_status(code),
  -- ISBN identifies a FORMAT/product (this manifestation), so it lives on the holding,
  -- not the edition: print/epub/pdf of one book carry DIFFERENT ISBNs. edition.isbn is
  -- kept as the edition's display/primary.
  isbn              TEXT,
  holding_type      TEXT REFERENCES holding_type(code),  -- pdf|epub|physical (open vocab, catalogue/vocab.json)
  digitizer_used    TEXT REFERENCES digitizer_kind(code),
  archival_pdf_path TEXT,
  notes             TEXT,
  date_added        TEXT DEFAULT CURRENT_TIMESTAMP,
  -- Last time the user actually opened this copy in the viewer (/file or /open).
  -- Powers the home page's "Recently opened" shelf. NULL = never opened; that
  -- shelf falls back to date_added so it is populated before the first open.
  last_opened       TEXT,
  -- Which configured library root (vocab `_library_roots`.id) owns this file, set
  -- at ingest by mount.owning_root_id (longest-prefix match). Makes per-root
  -- repoint/remove exact instead of re-deriving by string prefix. NULL = the file
  -- sits under no configured root. Not a DB FK — roots live in vocab, not a table.
  root_id           INTEGER
);

-- Last reading position per copy (file), so the in-app reader can resume where you
-- left off — and sync that spot across devices (it is server-side, not per-browser).
-- `locator` is opaque and format-specific: a PDF page number ("42") or an EPUB CFI.
-- `fraction` is 0..1 progress for a progress indicator. One row per holding.
CREATE TABLE IF NOT EXISTS reading_position (
  holding_id INTEGER PRIMARY KEY REFERENCES holding(id) ON DELETE CASCADE,
  locator    TEXT,
  fraction   REAL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- NB: bookmarks + the reader-sync `rev` counter are NOT defined here. They are a
-- self-contained, reader-OWNED concern (catalogue.webui.reader_state) that owns its own DDL
-- via ensure_schema() — so the bookmark/annotation/sync tables and their SQL live behind one
-- typed module in the reader/webui package, not in the central schema or the route.

-- Alternate/variant ISBNs for one edition. Different printings of the SAME
-- book carry different ISBNs; this is the explicit "this edition is also known
-- under these ISBNs" link (no copy implied — that is a `holding`). The exact-
-- ISBN "already in catalogue?" verdict (§14.6) reads this alongside
-- edition.isbn + holding.isbn so a scan of ANY known ISBN resolves to the edition.
CREATE TABLE IF NOT EXISTS edition_isbn (
  id          INTEGER PRIMARY KEY,
  edition_id  INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  isbn        TEXT NOT NULL,                 -- normalized 13-digit
  note        TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS edition_isbn_alt_isbn_idx ON edition_isbn(isbn);
CREATE INDEX IF NOT EXISTS edition_isbn_alt_eid_idx  ON edition_isbn(edition_id);
CREATE UNIQUE INDEX IF NOT EXISTS edition_isbn_alt_uq ON edition_isbn(edition_id, isbn);

-- ── Aliases (§4.1) ────────────────────────────────────────────────────────
-- normalized_key is the NFKD-decompose-then-strip fold for resolution only
-- (§4.2). Stored `text` keeps diacritics (NFC).
CREATE TABLE IF NOT EXISTS work_alias (
  id             INTEGER PRIMARY KEY,
  work_id        INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  text           TEXT NOT NULL,
  scheme         TEXT REFERENCES alias_scheme(code),
  normalized_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_alias_key_idx ON work_alias(normalized_key);

CREATE TABLE IF NOT EXISTS person_alias (
  id             INTEGER PRIMARY KEY,
  person_id      INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  text           TEXT NOT NULL,
  scheme         TEXT REFERENCES alias_scheme(code),
  normalized_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS person_alias_key_idx ON person_alias(normalized_key);

-- A person carries ONE `external_id` (the hub id, usually Wikidata) but a single
-- Wikidata hit cross-links to several regional authorities at once (BDRC P2477,
-- DILA P1187, VIAF P214 — see catalogue/wikidata.cross_ids). Those live here, one
-- row per scheme, so resolving a person once records every authority id it maps to.
CREATE TABLE IF NOT EXISTS person_external_id (
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  scheme    TEXT NOT NULL,            -- 'wikidata' | 'bdrc' | 'dila' | 'viaf'
  value     TEXT NOT NULL,            -- full namespaced id, e.g. 'bdr:P4954'
  PRIMARY KEY (person_id, scheme)
);
-- person_dedup keys identity off authority ids: look up "who else owns this
-- value?" (cross-link index) and "who else holds this hub id?" (person.external_id).
-- Both must stay O(log n) as the corpus grows. See authority_dedup_model.md §9.
CREATE INDEX IF NOT EXISTS person_external_id_value_idx ON person_external_id(value);
CREATE INDEX IF NOT EXISTS person_external_id_hub_idx   ON person(external_id);

-- ── Relationships & classification ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edition_work (
  edition_id           INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  work_id              INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  sequence             INTEGER,
  translator_person_id INTEGER REFERENCES person(id) ON DELETE SET NULL,
  section_locator      TEXT,
  locator_type         TEXT REFERENCES locator_type(code),  -- page|chapter|section (open vocab)
  note                 TEXT,  -- per-appearance annotation, e.g. "only chs. 1–3 of the root text"
  PRIMARY KEY (edition_id, work_id, sequence)
);

-- ── FRBR model (2026-06-04): author on the WORK, translator on the EDITION ────
-- These REPLACE the former `work_contributor` table (dropped in Phase D; its data
-- was moved here by catalogue/migrate_frbr.py, which db._migrate runs automatically
-- on any pre-FRBR DB). The translator OVERRIDE edition_work.translator_person_id is
-- intentionally kept (NULL ⇒ inherit the edition's set). See frbr_migration_plan.md.

-- AUTHOR(s) of the work — the composer. (Replaces work_contributor role='author'.)
CREATE TABLE IF NOT EXISTS work_author (
  work_id   INTEGER NOT NULL REFERENCES work(id)   ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  role      TEXT NOT NULL DEFAULT 'author',     -- author | attributed | compiler | reviser
  PRIMARY KEY (work_id, person_id, role)
);
CREATE INDEX IF NOT EXISTS work_author_person_idx ON work_author(person_id);

-- TRANSLATOR(s) — book-level home on the edition. A person translates MANY editions;
-- an edition may have co-translators (seq orders them). edition_work.translator_person_id
-- survives as a nullable per-work OVERRIDE (NULL ⇒ inherit this set). (Will replace
-- work_contributor role='translator' + the edition_work per-link default.)
CREATE TABLE IF NOT EXISTS edition_translator (
  edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL REFERENCES person(id)  ON DELETE CASCADE,
  seq        INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (edition_id, person_id)
);
CREATE INDEX IF NOT EXISTS edition_translator_person_idx ON edition_translator(person_id);
-- Book-level authorship: a plain single-author book is an EDITION with its
-- author(s) and NO work row (a "work" is reserved for a text with identity beyond
-- one book — a classical Skt/Tib text, a recurring text, or an anthology member).
-- Mirror of edition_translator. See catalogue/db/contributor_store.py.
CREATE TABLE IF NOT EXISTS edition_author (
  edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL REFERENCES person(id)  ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'author',     -- author | editor | compiler
  seq        INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (edition_id, person_id, role)
);
CREATE INDEX IF NOT EXISTS edition_author_person_idx ON edition_author(person_id);

CREATE TABLE IF NOT EXISTS relationship (
  id                   INTEGER PRIMARY KEY,
  from_work_id         INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  relation             TEXT NOT NULL REFERENCES relation_type(code),
  to_work_id           INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  from_section_locator TEXT
);
CREATE INDEX IF NOT EXISTS rel_from_idx ON relationship(from_work_id);
CREATE INDEX IF NOT EXISTS rel_to_idx   ON relationship(to_work_id);

-- Layer 2 (edition→work): THIS published book, by its modern (edition-level) author,
-- is a MODERN COMMENTARY ON the classical work `to_work_id`. Distinct from the work↔work
-- `relationship` above (the classical/scholastic structure among the contained works).
-- Many-to-many: a modern book may comment on several source texts. The target may be a
-- work also contained in this edition (internal) or one held elsewhere (external).
-- See docs/design/commentary_relationships_model.md.
CREATE TABLE IF NOT EXISTS edition_commentary_on (
  edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  to_work_id INTEGER NOT NULL REFERENCES work(id)    ON DELETE CASCADE,
  PRIMARY KEY (edition_id, to_work_id)
);
CREATE INDEX IF NOT EXISTS edition_commentary_on_work_idx ON edition_commentary_on(to_work_id);

CREATE TABLE IF NOT EXISTS collection (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  deleted_at TEXT                              -- soft-delete tombstone (NULL = live); id frozen once set
);
CREATE TABLE IF NOT EXISTS collection_member (
  collection_id INTEGER NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
  work_id       INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  PRIMARY KEY (collection_id, work_id)
);

CREATE TABLE IF NOT EXISTS subject (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  -- 'topic' (aboutness — the default) vs 'series' (a Series/Collection grouping:
  -- volumes/books that belong together). Series live in the same table so they
  -- reuse the hierarchy/browse machinery, but every TOPICAL view filters
  -- kind='topic' so a series never pollutes the subject facets, and a series tag
  -- does NOT satisfy the "every work/edition needs a real subject" invariant.
  kind TEXT NOT NULL DEFAULT 'topic',
  deleted_at TEXT                            -- soft-delete tombstone (NULL = live); id frozen once set
);
CREATE TABLE IF NOT EXISTS work_subject (
  work_id    INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subject(id) ON DELETE CASCADE,
  PRIMARY KEY (work_id, subject_id)
);
CREATE TABLE IF NOT EXISTS edition_subject (
  edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  subject_id INTEGER NOT NULL REFERENCES subject(id) ON DELETE CASCADE,
  PRIMARY KEY (edition_id, subject_id)
);
-- Folder-name → subject-label overrides for path-derived subjects, e.g.
-- '01 books - dharma' → 'Dharma'. Empty label drops the segment. See
-- catalogue/domain/subjects.py (clean_segment / segment_label).
CREATE TABLE IF NOT EXISTS subject_folder_map (
  raw_key TEXT PRIMARY KEY,   -- casefold of a raw directory segment
  label   TEXT NOT NULL       -- subject label to use ('' drops the segment)
);

-- Read-only DRY-RUN cache for the works-rebuild detection passes (Part B single /
-- Part C multi). One row per edition holds the detected title/author/translator/
-- canonical correspondence as JSON, for the /works/detect review report — NOT the
-- canonical works themselves. Rebuilt by `catalogue/cli/work_detect.py`; nothing
-- downstream depends on it. See catalogue/domain/work_detect.py.
CREATE TABLE IF NOT EXISTS work_detection (
  edition_id   INTEGER PRIMARY KEY REFERENCES edition(id) ON DELETE CASCADE,
  kind         TEXT,                       -- 'single' | 'multi'
  payload_json TEXT NOT NULL,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Cache of rough LLM English glosses of native (Tibetan/Sanskrit) titles, keyed by
-- the folded title + model, so a gloss is computed ONCE and reused across re-runs
-- and across editions of the same text (no repeated LLM calls). See
-- catalogue/domain/work_detect.py (cached_gloss).
CREATE TABLE IF NOT EXISTS gloss_cache (
  text_key   TEXT NOT NULL,                -- separator-folded native title
  model      TEXT NOT NULL,                -- 'gemma3:12b' | 'claude' | …
  gloss      TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (text_key, model)
);

CREATE TABLE IF NOT EXISTS tradition (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  deleted_at TEXT                              -- soft-delete tombstone (NULL = live); id frozen once set
);
-- Seed vocabulary lives in vocab.json under the `_tradition` key (config, not DDL —
-- "like the subjects and other things") and is loaded by db.load_vocab at init. The
-- medium-granularity default set is the four Tibetan schools + the sub-lineages in the
-- subject tree (Shangpa Kagyu, Kadam, Jonang) + two scope labels ('Common', 'Rimé').
-- Tradition tags attach at BOTH levels (design decision 2026-07): work_tradition is
-- the canonical lineage of the text; edition_tradition overrides for anthologies /
-- mixed volumes whose physical book spans traditions its work(s) don't capture.
-- Both are multi-label (a work/edition may carry several traditions, e.g. a Rimé
-- collection). confidence/source/evidence make every assignment auditable:
--   source ∈ 'rule-subject' | 'rule-author' | 'llm' | 'human'
--   confidence 0..1 (human = 1.0); evidence = short why-string / JSON from the pass.
CREATE TABLE IF NOT EXISTS work_tradition (
  work_id      INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  tradition_id INTEGER NOT NULL REFERENCES tradition(id) ON DELETE CASCADE,
  confidence   REAL,
  source       TEXT,
  evidence     TEXT,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (work_id, tradition_id)
);
CREATE TABLE IF NOT EXISTS edition_tradition (
  edition_id   INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  tradition_id INTEGER NOT NULL REFERENCES tradition(id) ON DELETE CASCADE,
  confidence   REAL,
  source       TEXT,
  evidence     TEXT,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (edition_id, tradition_id)
);

-- ── Capture & review ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS capture_staging (
  id             INTEGER PRIMARY KEY,
  form           TEXT REFERENCES form_type(code),
  raw_isbn       TEXT,
  image_path     TEXT,
  free_text_note TEXT,
  metadata_json  TEXT,                        -- Open Library lookup result (§7.3)
  source         TEXT,                        -- §14.2: ios/web/csv/manual
  scanned_at     TEXT,                        -- §14.2: ISO-8601 client time
  in_catalogue   INTEGER,                     -- §14.6 verdict at capture time: 1 found / 0 not / NULL unchecked
  status         TEXT NOT NULL DEFAULT 'raw', -- §14.5: 'raw' until desktop resolves
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
-- §14.5 idempotency: at most one open (raw) row per ISBN. Partial unique
-- index also serializes concurrent POST /capture (otherwise SELECT-then-
-- INSERT races create duplicates under WAL).
CREATE UNIQUE INDEX IF NOT EXISTS capture_staging_raw_isbn_uq
  ON capture_staging(raw_isbn)
  WHERE status = 'raw' AND raw_isbn IS NOT NULL;

-- ── Wishlist (books wanted but not yet owned) ─────────────────────────────
-- A book the operator intends to ACQUIRE — distinct from the catalogue (which is
-- books we hold) and from capture_staging (an active intake outbox). Kept OUT of
-- the edition graph so search/browse/replica keep meaning "books I own"; a wishlist
-- item only becomes a real edition+holding on acquisition. One shared library-wide
-- list (not per-user). Resolution reuses services.{isbn,cip,intake_match}. Soft-deletes
-- like the catalogue roots (deleted_at freezes the id). `matched_edition_id` is a real
-- FK with ON DELETE SET NULL (safe: roots soft-delete, id never reused — see
-- docs/access/entity_api_model.md §6 and the id-reuse hazard notes).
CREATE TABLE IF NOT EXISTS wishlist_item (
  id                 INTEGER PRIMARY KEY,          -- NOT AUTOINCREMENT (repo convention)
  source             TEXT NOT NULL,                -- 'manual' | 'isbn' | 'cip' | 'scan'
  status             TEXT NOT NULL DEFAULT 'unresolved',
                                                   -- unresolved|resolved|ambiguous|owned|acquired
  -- raw inputs (kept so a failed/ambiguous resolve can be retried or edited)
  raw_isbn           TEXT,
  raw_title          TEXT,
  raw_author         TEXT,
  raw_cip_text       TEXT,
  -- resolved snapshot (populated by services/wishlist_resolve.py)
  resolved_title     TEXT,
  resolved_subtitle  TEXT,
  resolved_authors   TEXT,                         -- JSON array of contributor names
  resolved_publisher TEXT,
  resolved_year      INTEGER,
  resolved_isbn      TEXT,                          -- normalized 13-digit
  ol_work_key        TEXT,                          -- OpenLibrary work key (cross-format match)
  lccn               TEXT,
  cover_url          TEXT,                          -- OpenLibrary cover by ISBN/work key
  candidates_json    TEXT,                          -- ambiguous-candidate list for the user to pick
  -- dedupe / acquisition loop
  matched_edition_id INTEGER REFERENCES edition(id) ON DELETE SET NULL,
  priority           INTEGER,
  notes              TEXT,
  added_at           TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at         TEXT,
  acquired_at        TEXT,
  deleted_at         TEXT,                          -- soft-delete tombstone (NULL = live); id frozen once set
  rev                INTEGER NOT NULL DEFAULT 0     -- optimistic-concurrency version
);
CREATE INDEX IF NOT EXISTS wishlist_item_status_idx   ON wishlist_item(status);
CREATE INDEX IF NOT EXISTS wishlist_item_isbn_idx     ON wishlist_item(resolved_isbn);
CREATE INDEX IF NOT EXISTS wishlist_item_work_key_idx ON wishlist_item(ol_work_key);
CREATE INDEX IF NOT EXISTS wishlist_item_edition_idx  ON wishlist_item(matched_edition_id);

-- A library-level "starred" (favourite) flag on an edition — the curated Starred home
-- rail + the highlighted star shown on every cover. Distinct from reader bookmarks (which
-- are per-position, inside a book): this marks a whole edition you care about. One shared
-- list (not per-user). Unlike the soft-delete roots this is a plain toggle: `star` is
-- idempotent (UNIQUE edition_id), `unstar` HARD-deletes the row, and the FK CASCADEs so a
-- hard edition delete takes its star with it. Reads JOIN to LIVE editions only, so a
-- tombstoned (or recycled — see the id-reuse hazard notes) id never resurfaces a star.
CREATE TABLE IF NOT EXISTS starred_edition (
  id         INTEGER PRIMARY KEY,                          -- NOT AUTOINCREMENT (repo convention)
  edition_id INTEGER NOT NULL UNIQUE
               REFERENCES edition(id) ON DELETE CASCADE,
  starred_at TEXT DEFAULT CURRENT_TIMESTAMP,
  rev        INTEGER NOT NULL DEFAULT 0                    -- bumped on every (re)star, for the list ETag
);
CREATE INDEX IF NOT EXISTS starred_edition_edition_idx ON starred_edition(edition_id);

CREATE TABLE IF NOT EXISTS review_queue (
  id           INTEGER PRIMARY KEY,
  item_type    TEXT NOT NULL REFERENCES review_item_type(code),
  payload_json TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  resolved_at  TEXT
);

-- Files the operator has explicitly ignored during reconcile ("don't show me
-- this again"). A scanned file matching an ignored row by path OR file_hash is
-- dropped from classification, so re-scans never re-enqueue it. Keyed on path
-- (a file the operator doesn't want catalogued), with file_hash kept so the
-- ignore survives a move. Un-ignore by deleting the row.
CREATE TABLE IF NOT EXISTS ingest_ignore (
  id           INTEGER PRIMARY KEY,
  path         TEXT UNIQUE,
  file_hash    TEXT,
  content_hash TEXT,
  ignored_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ingest_ignore_hash_idx ON ingest_ignore(file_hash);

-- Promotion provenance (§8 step 5). Records exactly which canonical rows a
-- single review_queue item's promotion created, so a revert deletes precisely
-- those works (cascading their contributor/edition_work/alias rows). work_ids
-- and person_ids are JSON arrays; person_ids is an audit of persons this
-- promotion freshly created. Revert garbage-collects any person left fully
-- orphaned, so a person shared with another work is never deleted. One row per
-- promoted review item.
CREATE TABLE IF NOT EXISTS promotion (
  id             INTEGER PRIMARY KEY,
  review_item_id INTEGER NOT NULL UNIQUE REFERENCES review_queue(id) ON DELETE CASCADE,
  holding_id     INTEGER,
  work_ids       TEXT NOT NULL,                -- JSON array of created work.id
  person_ids     TEXT NOT NULL,                -- JSON array of newly-created person.id
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ── Searchable text (§4.5) ────────────────────────────────────────────────
-- edition_text stores NFC-normalized text WITH diacritics intact.
-- The FTS5 mirror does index-only diacritic folding via remove_diacritics 2.
CREATE TABLE IF NOT EXISTS edition_text (
  id         INTEGER PRIMARY KEY,
  edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  page       INTEGER,
  content    TEXT NOT NULL                  -- NFC, diacritics preserved
);
CREATE INDEX IF NOT EXISTS edition_text_edition_idx ON edition_text(edition_id);

CREATE VIRTUAL TABLE IF NOT EXISTS edition_text_fts USING fts5(
  content,
  content='edition_text', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS edition_text_ai AFTER INSERT ON edition_text BEGIN
  INSERT INTO edition_text_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS edition_text_ad AFTER DELETE ON edition_text BEGIN
  INSERT INTO edition_text_fts(edition_text_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS edition_text_au AFTER UPDATE ON edition_text BEGIN
  INSERT INTO edition_text_fts(edition_text_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO edition_text_fts(rowid, content) VALUES (new.id, new.content);
END;

-- ── Per-stage versioned caches (§5, §6, §12.3) ────────────────────────────
-- Keys are (content_hash, stage_version); improving one stage invalidates
-- only that stage. Raw and parsed kept separate.
CREATE TABLE IF NOT EXISTS raw_extract_cache (
  file_hash       TEXT NOT NULL,
  extract_version INTEGER NOT NULL,
  raw_text        TEXT,
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (file_hash, extract_version)
);

CREATE TABLE IF NOT EXISTS parsed_toc_cache (
  file_hash     TEXT NOT NULL,
  parse_version INTEGER NOT NULL,
  parsed_json   TEXT,
  created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (file_hash, parse_version)
);

CREATE TABLE IF NOT EXISTS classification_cache (
  content_hash     TEXT NOT NULL,
  classify_version INTEGER NOT NULL,
  result_json      TEXT,                    -- root/commentary + resolver result
  confidence       REAL,
  model_rung       TEXT,                    -- 'qwen3:8b' | 'qwen3:14b' | 'claude-haiku' (§4.9)
  created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (content_hash, classify_version)
);

-- [v15] book-level section analysis (locator.extract_sections + peek →
-- structure + contained texts). Keyed like the other per-stage caches; the
-- runners' bare connect() path also creates it lazily (process._SECTION_CACHE_DDL).
CREATE TABLE IF NOT EXISTS section_cache (
  file_hash       TEXT NOT NULL,
  section_version INTEGER NOT NULL,
  result_json     TEXT,
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (file_hash, section_version)
);

-- Per-page text (text-layer or OCR), persisted durably so a future training corpus
-- can be chunked WITHOUT re-OCRing (re-OCR is costly/lossy; re-chunking is free). One
-- row per page; a chunker streams a page-range via the indexed (file_hash, page_no),
-- matching Section.locator "pages a-b". Populated by sweep (text layer) and digitize
-- (fresh OCR, overwriting empty image-only pages).
CREATE TABLE IF NOT EXISTS page_text_cache (
  file_hash       TEXT NOT NULL,
  extract_version INTEGER NOT NULL,
  page_no         INTEGER NOT NULL,
  text            TEXT,
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (file_hash, extract_version, page_no)
);
CREATE INDEX IF NOT EXISTS page_text_cache_fh_idx
  ON page_text_cache(file_hash, extract_version);

CREATE TABLE IF NOT EXISTS resolver_cache (
  query_hash       TEXT NOT NULL,
  resolver_version INTEGER NOT NULL,
  source           TEXT,                    -- 'bdrc' | '84000' | …
  raw_json         TEXT,
  parsed_json      TEXT,
  created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (query_hash, resolver_version)
);

-- ── Indexes called out in §5 ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS holding_hash_idx     ON holding(file_hash);
CREATE INDEX IF NOT EXISTS holding_edition_idx  ON holding(edition_id);
CREATE INDEX IF NOT EXISTS work_canon_idx       ON work(canonical_system, canonical_number);
CREATE INDEX IF NOT EXISTS edition_isbn_idx     ON edition(isbn);
-- edition_volume_set_idx is created in db._migrate (the volume_set_id column is
-- added there, AFTER this script runs on a pre-existing DB).

-- ── Sweep state (§6, §7.1) ───────────────────────────────────────────────
-- Resumability/idempotency for the WebDAV sweep: change-detect by
-- (path, size, mtime) BEFORE hashing; resume after a disconnect by skipping
-- everything already in this table.
CREATE TABLE IF NOT EXISTS sweep_state (
  path       TEXT PRIMARY KEY,
  size       INTEGER NOT NULL,
  mtime      REAL NOT NULL,
  file_hash  TEXT NOT NULL,
  scanned_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS sweep_state_hash_idx ON sweep_state(file_hash);

-- Unreachable / corrupt / locked files (§7.1: skip + log, never abort).
CREATE TABLE IF NOT EXISTS sweep_problem_log (
  id          INTEGER PRIMARY KEY,
  path        TEXT NOT NULL,
  message     TEXT NOT NULL,
  occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS sweep_problem_path_idx ON sweep_problem_log(path);

-- Undo journal for reversible contributor ops (merge / delete / split). Before a
-- destructive op runs, the full pre-op rows of every person it touches (the person
-- rows themselves + their alias / external-id / work_author / edition_translator
-- edges, plus any edition_work translator override) are captured into `payload` as
-- JSON. `apply_undo` restores them verbatim in one transaction, so a mistaken merge
-- or delete is fully reversible. One row per undoable op; consumed (deleted) on undo.
CREATE TABLE IF NOT EXISTS undo_log (
  id         INTEGER PRIMARY KEY,
  op         TEXT NOT NULL,                     -- merge | delete | split
  summary    TEXT,                              -- human label for the Undo button
  payload    TEXT NOT NULL,                     -- JSON snapshot (see contributor_undo)
  precheck   TEXT,                              -- fingerprint of the involved persons'
                                                -- POST-op state; undo is refused unless
                                                -- the live state still matches (no
                                                -- intervening edit / id reuse to clobber)
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Schema version (for future migrations of fixed columns only) ─────────
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY, value TEXT
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1');

-- ── External-tool dependency (the "flag") + purge-guard ──────────────────────
-- One row per (edition, tool) that a first-party external tool (BuddhistLLM RAG, …) has
-- consumed the edition. Set by access_api.external_deps.claim; MONOTONIC — never cleared (a
-- citation already emitted to a user can't be recalled). Data is segregated per tool (a WHERE
-- on `tool`). See docs/access/external_tool_dependency_contract.md, citation_edition_contract_plan.md.
CREATE TABLE IF NOT EXISTS edition_external_dependency (
  edition_id INTEGER NOT NULL REFERENCES edition(id),   -- RESTRICT (no cascade): a 2nd delete guard
  tool       TEXT NOT NULL,                             -- e.g. 'buddhistllm'
  corpus     TEXT,                                      -- which corpus/version consumed it (advisory)
  claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (edition_id, tool)
);
CREATE INDEX IF NOT EXISTS edition_external_dependency_tool_idx
  ON edition_external_dependency(tool);

-- Purge-guard: a flagged edition may NOT be hard-deleted — tombstone (deleted_at) instead, so its
-- id + pub_id stay frozen (stability S1: no reuse). Fires BEFORE DELETE, below the ~906 legacy
-- raw-SQL sites, so even `services`' hard-delete is caught. A tombstone is an UPDATE, so it is
-- unaffected. The FK above is a second guard if this trigger is ever dropped.
CREATE TRIGGER IF NOT EXISTS edition_purge_guard
BEFORE DELETE ON edition
WHEN EXISTS (SELECT 1 FROM edition_external_dependency WHERE edition_id = OLD.id)
BEGIN
  SELECT RAISE(ABORT,
    'edition has external-tool dependencies; tombstone (deleted_at), do not hard-delete (stability contract)');
END;

-- ── External read-contract (stable view) ─────────────────────────────────────
-- The sanctioned surface for OUT-OF-PROCESS consumers that open this DB file directly
-- (../ocr_pipeline, ../BuddhistLLM). They MUST read this view, never the base `holding`
-- table. GUARANTEED columns: edition_id, file_path, content_hash, text_status
-- (provenance_kind joins later, with the digitization-provenance work). Additive only —
-- never remove or rename a column here; absorb internal schema changes by adjusting the
-- SELECT so external consumers never have to. Created idempotently on every init_db
-- (executescript), so existing DBs get it on next open. See
-- docs/access/entity_api_model.md §7 and the repo plan's "External consumers" section.
CREATE VIEW IF NOT EXISTS v_holding_files AS
  SELECT edition_id, file_path, content_hash, text_status
  FROM holding
  WHERE file_path IS NOT NULL AND TRIM(file_path) <> '';

-- ── Live-root views (entity-API soft-delete) ─────────────────────────────────
-- One `v_live_<root>` per soft-deletable root (edition / work / person / subject /
-- collection / tradition) = `SELECT * FROM <root> WHERE deleted_at IS NULL`. A read that
-- must not surface a tombstoned row selects FROM the view; writes still target the base
-- table. These are created in db.py::_migrate (DROP+CREATE every init, so `SELECT *`
-- re-expands after a column ALTER) — NOT here — for that re-expansion guarantee; this note
-- marks where they live. See docs/access/entity_api_model.md §6.
