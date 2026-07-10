# FRBR data model — target (2026-06-04)

A redesign of the catalogue's core schema to support **real multiplicity** (one composition →
many translations/editions) with entity cross-navigation, per the Apple-Classical / FRBR model.

**Constraints set by the user:**
- **Work is shared** across editions — the source of multiplicity (composition → all its translations).
- **Translator is single-homed on the edition** (no longer on `edition_work` *and* `work_contributor`).
  A translator is an instance of a **person**, and one person can translate **many** editions.
- **Author stays on the Work** (the composer of the composition).
- **Movements / chapters are OUT of scope** (a Work is atomic for now; no sub-work structure).

This is **FRBR-lite**: FRBR's *Expression* (the translation as an abstract realization) and
*Manifestation* (a specific printing/book) are **collapsed into `edition`**.

> ## ⚠ OPEN DECISION — 2-layer vs 3-layer (not yet decided, 2026-06-04)
> **"All translations of a work" does NOT need Expression** — it's the work↔edition many-to-many:
> merge duplicate work rows into one shared Work, then `SELECT editions WHERE edition_work.work_id=W`
> lists every translation. Expression buys only ONE extra thing: **one translation realized in
> multiple manifestations** — i.e. *reprints* AND *the same translation reused in another book*
> (a standalone translation later folded into a collected-works anthology). Without it, a reused/
> reprinted translation appears **once per book** under the work (redundant, not missing).
>
> **Measured on the live DB** (the 15 duplicate work-title groups): ~**5 genuinely different
> translations** of the same work (→ need only work-sharing); ~**5 same-translation reprint/reuse**
> (→ Expression would dedupe); **several are multi-VOLUME sets** (Lamrim Chenmo vols 1–3; *Sounds of
> Innate Freedom*) — a `volume` concern, NOT Expression; 5 have no translator recorded.
>
> - **2-layer (this doc's DDL):** `work → edition(=translation+book)`, translator on the **edition**.
>   Meets the stated need; reused/reprinted translations are not deduped. Matches "translator in edition".
> - **3-layer:** `work → translation(=Expression) → edition(=book)`, translator on the **translation**
>   (one node across reprints/anthologies; +1 table, +1 join everywhere, +a translation-dedupe pass).
>   NOTE: this relocates the translator off `edition` onto `translation`.
>
> Decide before building. The DDL below is the 2-layer form; the 3-layer adds a `translation` table
> between `work` and `edition` and moves `edition_translator` → `translation_translator`.

Mapping:

| FRBR | Music analogy | This model |
|---|---|---|
| Work | composition | `work` (shared, author attaches here) |
| Expression + Manifestation | performance + album | `edition` (translator + publisher/year/isbn) |
| Item | a disc/copy | `holding` |
| Person | composer / performer | `person` (author **and** translator) |

---

## Entity-relationship (target)

```
                    work_author (role)                       edition_translator (seq)
        person ───────────────────────< work        edition >─────────────────────── person
          │  ▲                          │  ▲           │  ▲                              ▲
          │  │ person_alias             │  │ work_alias│  │                              │
          │  └────────                  │  └────────   │  │  (a person can translate     │
          │                             │              │  │   many editions; an edition  │
          │   ┌──────────  edition_work (sequence, locator)  many-to-many  ──────────┐  │
          │   │            work  >───────────────────────────────────<  edition       │  │
          │   │             (one work in MANY editions; one edition holds MANY works)  │  │
          │   ▼                                                          │             │  │
          │  holding (Item: a copy/file)  >──────────────────────────────┘             │  │
          └────────────────────────────────────────────────────────────────────────────┘

Author lives on the WORK (work_author).   Translator lives on the EDITION (edition_translator).
Multiplicity = edition_work is MANY-TO-MANY (today it is effectively 1:1, with works un-shared).
```

---

## Target DDL

```sql
-- PERSON — the shared agent identity (authors AND translators are persons). UNCHANGED.
CREATE TABLE person (
  id INTEGER PRIMARY KEY, primary_name TEXT NOT NULL, dates TEXT,
  role_hint TEXT, external_id TEXT, verification_status TEXT DEFAULT 'provisional');
CREATE TABLE person_alias (
  id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  text TEXT NOT NULL, scheme TEXT, normalized_key TEXT NOT NULL);

-- WORK — the abstract composition (FRBR Work). SHARED across editions → multiplicity.
CREATE TABLE work (
  id INTEGER PRIMARY KEY,
  work_type         TEXT,
  original_language TEXT,            -- language of the composition (e.g. 'sa', 'bo')
  canonical_system  TEXT,            -- 'toh' / '84000' …  (the shared-identity anchor)
  canonical_number  TEXT,
  sanskrit_title    TEXT,            -- original-language title of the work
  tibetan_title     TEXT,
  era               TEXT,
  notes             TEXT);
CREATE TABLE work_alias (            -- titles of the work, any language/scheme (for search/dedup)
  id INTEGER PRIMARY KEY, work_id INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
  text TEXT NOT NULL, scheme TEXT, normalized_key TEXT NOT NULL);
CREATE INDEX work_alias_key_idx ON work_alias(normalized_key);

-- AUTHOR(s) of the work — the COMPOSER. (Replaces work_contributor role='author'.)
CREATE TABLE work_author (
  work_id   INTEGER NOT NULL REFERENCES work(id)   ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  role      TEXT NOT NULL DEFAULT 'author',  -- author | attributed | compiler | reviser
  PRIMARY KEY (work_id, person_id, role));

-- EDITION — a published translation/printing (FRBR Expression+Manifestation collapsed).
CREATE TABLE edition (
  id INTEGER PRIMARY KEY,
  title    TEXT NOT NULL,            -- title as printed
  subtitle TEXT, volume TEXT,
  language TEXT,                     -- language of THIS edition (the translation language)
  publisher TEXT, year INTEGER, isbn TEXT,
  notes TEXT,
  review_status TEXT, review_flags TEXT, review_note TEXT, reviewed_at TEXT);

-- TRANSLATOR(s) — SINGLE HOME on the edition. A person translates MANY editions; an edition
-- may have co-translators. Symmetric to work_author. (Replaces edition_work.translator_person_id
-- AND work_contributor role='translator'.)
CREATE TABLE edition_translator (
  edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL REFERENCES person(id)  ON DELETE CASCADE,
  seq        INTEGER NOT NULL DEFAULT 1,             -- co-translator ordering
  PRIMARY KEY (edition_id, person_id));
CREATE INDEX edition_translator_person_idx ON edition_translator(person_id);

-- EDITION_WORK — which Work(s) an edition realizes. The MANY-TO-MANY that gives multiplicity.
CREATE TABLE edition_work (
  edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  work_id    INTEGER NOT NULL REFERENCES work(id)    ON DELETE CASCADE,
  sequence   INTEGER NOT NULL DEFAULT 1,             -- order within an anthology edition
  locator    TEXT,                                    -- where in the book
  PRIMARY KEY (edition_id, work_id));
CREATE INDEX edition_work_work_idx ON edition_work(work_id);   -- "all editions of this work"

-- HOLDING — a physical/electronic copy of an edition (FRBR Item). UNCHANGED (FK to edition).
CREATE TABLE holding (
  id INTEGER PRIMARY KEY, edition_id INTEGER NOT NULL REFERENCES edition(id) ON DELETE CASCADE,
  form TEXT, file_path TEXT, file_hash TEXT, content_hash TEXT, holding_type TEXT,
  text_status TEXT, ocr_quality_score REAL, shelf_location TEXT, notes TEXT);
```

---

## What it changes vs the current schema

| Current | Target | Why |
|---|---|---|
| `work` minted 1-per-edition (364 works, 0 shared) | `work` **deduped/shared** | multiplicity: one composition → many editions |
| `work_contributor (role=author\|translator)` | split into `work_author` + `edition_translator` | author belongs to the Work; translator to the Edition |
| `edition_work.translator_person_id` (per link) | **dropped** → `edition_translator` (per edition) | translator single-homed on the edition |
| translator double-homed (208 + 207) | one home (`edition_translator`) | removes the smell |
| no edition `language` | `edition.language` | the translation's language |

Unchanged: `person`, `person_alias`, `work_alias`, `holding`, the review/verify columns + tables,
`edition` bibliographic fields.

---

## Navigation this unlocks (the Apple-Classical cross-links)

```sql
-- composer → his compositions
SELECT work_id FROM work_author WHERE person_id = :p;
-- composition → all its translations/editions   (the multiplicity payoff)
SELECT e.* FROM edition_work ew JOIN edition e ON e.id = ew.edition_id WHERE ew.work_id = :w;
-- edition → its translator(s)
SELECT person_id FROM edition_translator WHERE edition_id = :e ORDER BY seq;
-- translator → all editions they translated
SELECT edition_id FROM edition_translator WHERE person_id = :p;
-- edition → works → their authors
SELECT wa.person_id FROM edition_work ew JOIN work_author wa ON wa.work_id = ew.work_id
WHERE ew.edition_id = :e;
```

---

## Migration plan (sandbox-first; the hard part is work dedup)

Run entirely on a `sandbox.py fork` copy, verify, then promote. Steps:

1. **Add** `edition.language`, the `work_author`, `edition_translator`, and the rebuilt `edition_work`
   tables (alongside the old ones).
2. **`work_author`** ← `work_contributor WHERE role='author'`.
3. **`edition_translator`** ← DISTINCT translators per edition, gathered from
   `edition_work.translator_person_id` ∪ (`work_contributor role='translator'` via its edition_work).
   Keyed to the **edition** (collapses per-work translators to the book level — see tradeoffs).
4. **Rebuild `edition_work`** without `translator_person_id` (keep edition_id, work_id, sequence, locator).
5. **Drop** `work_contributor`.
6. **⚠ Work dedup — the real work.** Today every edition has its own work row, so multiplicity is
   latent, not present. Merge work rows that are the same composition, re-pointing `edition_work` and
   merging `work_alias`:
   - **Tier 1 (safe, automatic):** same `canonical_number` (22 works).
   - **Tier 2 (safe-ish):** identical `fold_key(title)` **and** identical author-set — the **15**
     measured fold-key collisions (two *Stages of Meditation*, two *Laṅkāvatāra Sūtra*, …).
   - **Tier 3 (human):** fuzzy title + author overlap → review queue. Build a **`work` merge tool**
     (the analog of the existing person-merge in `contributor_edit`): re-point `edition_work`, move
     `work_alias`, carry `canonical_number`, delete the loser. *This is where the multiplicity in the
     back-catalogue actually comes from.*
7. **Backfill ~55 editions with no `edition_work`** — create a whole-book single Work (the
   `single_work` default) so every edition is navigable to a work.

**Honest expectation:** the existing 364 works dedup to roughly **340-ish** (≈15-37 merges). Most
compositions in this corpus appear **once**, so multiplicity is mostly **prospective** — its real
payoff is that *new* additions of a second translation now link to the existing Work instead of
forking a duplicate. The model is right; just don't expect the back-catalogue to suddenly sprout
many-performance fan-outs it doesn't contain.

---

## Tradeoffs / open questions (decide before migrating)

1. **Co-translators:** handled — `edition_translator` is a join table (multiple persons per edition),
   not a single FK. Satisfies "single home on the edition" + "P translates many editions" + co-translators.
2. **Per-work translator inside an anthology:** with the translator on the *edition*, an anthology gets
   ONE book-level translator set. If a single book has *different* translators per contained text, that
   granularity is lost. Measured frequency here is low; flag it. (Re-introducing a per-`edition_work`
   translator would re-create the double-home — avoid unless a real case demands it.)
3. **Expression layer — OPEN (see the box at the top).** Collapsing it into `edition` means a
   translation reused/reprinted in 2 books forks 2 edition rows (both re-point to the same shared Work,
   so "all translations of W" still works — just listed twice, not deduped). Measured ~5 such cases
   now. Decide 2-layer vs 3-layer before migrating.
4. **Original-language edition:** a Tibetan/Sanskrit *original* printing is just an `edition` whose
   `language` = the work's `original_language` and whose `edition_translator` is empty.
5. **`work_author` roles:** keep a small controlled vocab (author / attributed / compiler / reviser)
   rather than free text.

---

## Volume modeling — REQUIRED (user, 2026-06-04) · TODO before any UI/CLI build

A multi-volume work is **one logical publication issued in N physical books** — e.g. *The Great
Treatise on the Stages of the Path* (Lamrim Chenmo) vols 1–3 by the same committee; *Sounds of Innate
Freedom* (Brunnhölzl); *In Clear Words / Prasannapadā* vols. Today these surface as *duplicate work
rows* (one per volume) and must NOT be treated as re-translations or reprints — they are **volumes of
one set**.

**Requirement:** group the volumes of one publication, ordered, with a shared set identity, so the UI
can show "vol 2 of 3" and navigate the whole set; a work's "all editions/translations" list must show
the SET once, not each volume separately.

**Design sketch (finalise during the FRBR build; depends on the 2-/3-layer choice):**
- `edition.volume` (text designation, e.g. "Vol. 2") **already exists** on the schema.
- Add a volume **set** grouping — options:
  - **(a) self-group:** `edition.volume_set_id` (nullable) + `edition.volume_seq` (int). Editions
    sharing `volume_set_id` are volumes of one set, ordered by `volume_seq`. Lightest.
  - **(b) explicit set table:** `volume_set(id, label, n_volumes)` + `edition.volume_set_id`. Cleaner
    if a set needs its own metadata (series title, total count).
- The set's works = union of its volumes' `edition_work`. "All translations of W" dedupes by set:
  show the set, not each volume row.
- Migration: detect existing volume splits (same title fold-key + same translator + a volume token in
  the title/`volume`), group them, and DO NOT merge them as duplicate works.

**This is a prerequisite:** the dashboard UI/CLI must not be built until volume grouping is modelled,
or multi-volume sets will render as bogus duplicate works.

## Build order (when approved)

0. **DECIDE 2-layer vs 3-layer** (the box at top) and **finalise volume modeling** (above). Both are
   blockers — they change the schema. *Do not start the dashboard UI/CLI until both are settled.*
1. `work` merge tool + the dedup pass (Tiers 1–3) — *do this first*, on a sandbox, with review.
   The dedup pass MUST exclude multi-volume splits (group them as a set, don't merge as duplicates).
2. Schema migration (tables above + volume grouping) + the data moves (steps 1–5, 7).
3. Repoint the app/service reads: `work_author` for authors, `edition_translator` (or
   `translation_translator` if 3-layer) for translators, `edition_work` for the work↔edition graph;
   add the cross-link queries to the editor pane.
4. Drop `work_contributor` and the old `edition_work.translator_person_id` once reads are migrated.

*Migration is destructive and corpus-wide → sandbox-first, backup, human-review the Tier-3 merges,
and confirm before promoting to live.*
