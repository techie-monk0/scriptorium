# Title Recognition — Status

_Last updated: 2026-06-01_

## Goal

Give every edition its **real, published title** (with subtitle and volume/part where
applicable), derived **only from the book's own content** — the OCR'd title page and the
Library-of-Congress CIP / copyright block — **never from the filename**. Filenames are
ingest artifacts (e.g. `Creation and Completion_ Essential Points of Tantric -- Jamgon
Kongtrul`) and are treated as a baseline to be replaced, not a source.

## How it works

Two independent passes over `edition`, run separately so the network/ISBN job and the
local-LLM job don't block each other:

```
python3 -m catalogue.edition_resolve catalogue-db/catalogue.db --only identifier
python3 -m catalogue.edition_resolve catalogue-db/catalogue.db --only llm
```

Each edition is partitioned by whether it has a usable identifier; `--only identifier`
owns the ISBN/CIP path, `--only llm` owns the page-LLM path. Both commit per-row
(resumable). Verbose output marks the winning source per book: `cip` / `isbn` / `page`.

### Identifier pass (`resolve_edition`)

1. **`cip.parse_cip(text)`** — OCR-tolerant structured parser for the CIP block.
   Detects format (labelled / freeform / abbreviated / british_library / card) and
   extracts a structured record so the **title and ISBN come from the same block**.
2. **Prefer the CIP block's own ISBN** (`found_in="cip"`) as the identifier — co-located
   with the title ⇒ guaranteed same book — else fall back to a whole-text ISBN scan
   ranked by proximity to a CIP/copyright marker.
3. **Resolve** the ISBN against OpenLibrary (via `BookIdentifier` / scheme plugins).
   ISBNs are **checksum-validated** (ISBN-13 + ISBN-10 mod-11), so an OCR-mangled number
   is dropped rather than trusted.
4. **Title-confirmation gate** (`_titles_agree`, ≥40% significant-word overlap,
   diacritic-folded): the looked-up title is trusted **only if it agrees with the book's
   own CIP/page title**. On disagreement the looked-up title **and its metadata are
   dropped** (the ISBN is kept only if it's the CIP's own). This is what stops a stray /
   mis-OCR'd ISBN from renaming the book to a different work.
5. **Precedence:** labelled CIP > structured alternative (free-form CIP or page title that
   genuinely adds a subtitle/volume) > clean ISBN title > page > free-form CIP.

Writes the chosen title/isbn/publisher/year to `edition` and queues a revertable
`edition_metadata` review item.

### LLM pass (page title)

For editions with no usable identifier, `_page_title` runs the OCR'd title-page text
through the LLM ladder (`gemma3:12b` local → Claude Haiku fallback), guarded by a
mojibake check (`work_titles.looks_mojibake`) for born-digital custom-font garbage.
Queues a `title_proposal` review item.

### Supporting facts

- **EPUBs are re-extracted in spine (reading) order** (`_epub_ordered_names`), fixing the
  scrambled-ZIP-order problem so the title page and CIP page are actually found.
- Review items follow the `(db, item_id, *, commit=True) -> bool` contract; both pass
  outputs are revertable (`reject_edition_metadata` restores the prior `old_*` fields;
  rejecting a `title_proposal` reverts the title).

## Current state (live DB: `catalogue-db/catalogue.db`)

- **419** editions total.
- **FULL RESET (2026-06-01):** all `edition_metadata` + `title_proposal` items were
  reverted (144 applied titles unwound newest-first via the real revert helpers) and the
  queue cleared — **every edition is back to its filename baseline, all 123 touched
  editions verified restored, 0 title-flow review items remain**. This wipes the prior 69
  accepted LLM titles + the in-progress identifier results so **both passes can re-run
  cleanly overnight** with the head-`<title>` and CIP block-selection fixes live.
  Backup: `catalogue-db/catalogue.db.bak-titlereset-*`.
- After the reset, **all ~419** editions carry a filename baseline; the overnight
  identifier + LLM passes re-derive the lot from scratch.

### Validated wins

- The CIP-wiring + gate is **live-proven on e5**: its stray/mis-OCR'd ISBN resolved to a
  *different book* ("The Nyingma School of Tibetan Buddhism…"); the gate detected the
  title disagreement and dropped it, keeping the correct CIP title "Creation & completion:
  essential points of tantric meditation".
- Multi-volume works get distinct correct titles (page/CIP subtitle beats a generic ISBN
  title via the structure gate).
- Full test suite **633 passing** (`tests/test_cip.py`, `test_book_identifier.py`,
  `test_edition_resolve.py` incl. gate tests, `test_extract.py` spine-order).

### Recent maintenance

- **Fixed CIP block SELECTION** (`cip.parse_cip`): it anchored on the *first* `_MARKER`
  hit, but the bare-institution markers (`library of congress`, `national library`) match
  ordinary prose — a scholarly book mentions "the Library of Congress catalogs…", a
  "Library of Congress P.L. 480 program", and "National Library of Nepal" manuscript
  sources in the intro/bibliography, LONG before the copyright-page CIP. So two different
  Treasury-series books both parsed an intro window → no title → fell to the page LLM →
  both got the series name "Treasury of the Buddhist Sciences". Fix: prefer the
  unambiguous "Cataloging-in-Publication" phrase (`_STRONG_MARKER`); accept a bare-marker
  window only if it carries real CIP evidence (`_CIP_EVIDENCE`: ISBN/LCCN/p. cm/field
  labels/"is available"). Genuinely corrected **6** editions whose decoy marker was >1
  window before the real CIP (e30, e31, e33 — was the very wrong "THE Dalai LAMA" →
  *Vajra Rosary Tantra*, e175, e192, e288). Regression tests in `tests/test_cip.py`.

- Purged the prior **pre-gate** identifier results (36 `edition_metadata` + 36 ISBNs)
  via `reject_edition_metadata` so the gated logic re-derives cleanly. Accepted LLM titles
  were untouched (zero overlap). Backup: `catalogue-db/catalogue.db.bak-preidpurge-*`.
- **Fixed EPUB `<head><title>` leak** in `extract.py` (`_TextHTMLParser` skipped only
  `script`/`style`, not `head`): a templated/stale cover-page `<title>` (e.g. a leftover
  "The Diamond Cutter Sutra" on the Chittamani Tara book) was pulled into the body stream
  and, being first in spine order, became line 1 of the extraction — poisoning title
  recognition. 117/120 epub caches carried such head text. Now skip the whole `<head>`.
  Regression test: `test_epub_skips_head_title_metadata`. Re-extracted all 120 epubs into
  `raw_extract_cache` at a bumped `extract_version` (readers take the newest). Only **one**
  committed title was actually corrupted (e4 → reverted + re-resolved to ISBN title
  "Secret Revelations of Chittamani Tara"); the other Diamond-Cutter-head books survived
  via CIP/ISBN, and the rest are still filename baselines awaiting the normal pass (which
  now reads clean text). Backup: `catalogue-db/catalogue.db.bak-headtitlefix-*`.

## Known gaps / limitations

- Worst-OCR free-form CIP blocks (no ` / ` SOR, "p. cm" mangled) fall back to the page
  LLM — usually fine, sometimes the page LLM over-reads.
- **Free-form CIP title extraction still emits OCR junk on a minority** (separate from the
  block-selection fix above): e.g. page-number runs ("9 8 7 6 5 4 3 2 1 …"), bare ISBN
  fragments ("ISB N 0-0 6-…"), truncations ("masters", "enment"), or a leading PUA glyph
  ("⟨pua⟩e rice seedling sutra"). These compete in the resolver/gate; a freeform-title
  sanity filter is a worthwhile follow-up.
- **Abbreviated-stub books with no CIP title** (e.g. e30: "…Cataloging-in-Publication Data
  is available. LCCN… ISBN…") depend entirely on resolving the co-located ISBN. If
  OpenLibrary misses that ISBN, the title falls to the page LLM — and for the
  series-front-loaded books the 3,500-char page window only sees the series page (the real
  title page is ~8k in). This is the open `MAX_FRONT_MATTER` window question (NOT yet
  changed — a fixed bigger window is itself a heuristic; a structural title-page locator
  would be better).
- Some old records (e.g. LIOH v1, e381) have an unrecoverable OCR'd ISBN; titled by
  page/CIP instead.
- **Part/Volume designation is handled by the title pass itself, NOT a separate detector**
  (investigated + rejected). The rule: a volume/part is wanted *only when it's part of the
  printed title/subtitle* — and `suggest_title` already captures that (verified live on
  LIOH v1–v3 → "…Part One: The Preliminaries" / "…Part Two: The Fundamentals" /
  "…Part Three: The Ultimate Goals", conf 0.95 each). A standalone front-matter serial
  scanner was prototyped and dropped: on the real corpus it fired on 36 epubs but ~25+ were
  false positives — internal TOC section headings ("Chapter 9", "Part One: <section>") and
  blurbs about *other* volumes ("Volume 5, which contains…"). The volume number is usually
  NOT in the OCR'd text anyway (it's on the cover image or only in the filename, which the
  no-filename rule forbids as a title source). The `suggest_title` prompt now explicitly
  says to keep an in-title designation but never invent one from headers/TOC/blurbs.
- ~174 editions remain on filename baselines until the two passes finish across the corpus.

## What to do next

1. **Finish the gated identifier re-run** across the corpus:
   `python3 -m catalogue.edition_resolve catalogue-db/catalogue.db --only identifier`
   (re-derives the previously-purged 36 + any others through CIP → CIP-own-ISBN → gate).
2. **Run the LLM pass** for the identifier-less remainder:
   `python3 -m catalogue.edition_resolve catalogue-db/catalogue.db --only llm`
3. **Review** the queued proposals in the UI (`/review`) — accept/reject; both are
   revertable.
4. **Mark the unresolvable remainder** explicitly as `(untitled) — <filename>` so a
   filename baseline is never mistaken for a recognized title.
5. **Title-page reach for abbreviated-stub / series-front-loaded books** (the e30 class).
   These have no CIP title (only a co-located ISBN) and front-load a long series page, so
   the 3,500-char page-LLM window (`MAX_FRONT_MATTER`) never reaches the real title page
   (~8k in) → the LLM picks the series name. Don't just bump the window (that's another
   heuristic that breaks when a title page sits past whatever number we pick); build a
   **structural title-page locator** (find the actual title page in reading order and feed
   *that* to the LLM), and resolve the stub's co-located ISBN first. _(Surfaced by e30/e31
   debugging.)_
6. **Free-form CIP title sanity filter.** The free-form extractor still emits OCR junk on a
   minority — page-number runs ("9 8 7 6 5 4 3 2 1 …"), bare ISBN fragments
   ("ISB N 0-0 6-…"), truncations ("masters", "enment"), a leading PUA glyph
   ("⟨pua⟩e rice seedling sutra"). Add a reject/repair filter so these never win the
   precedence/gate. _(Surfaced by the corpus-wide CIP re-scan.)_
7. ~~Add a Part/Volume title-page detector~~ — **investigated and rejected** (see Known
   gaps): in-title/subtitle designations are already captured by `suggest_title`; a
   standalone front-matter scanner was net-harmful (false positives on TOC headings /
   other-volume blurbs). Prompt tightened to keep in-title designations only.
8. _(Deferred, dependency decision)_ adopt the third-party `regex` library for fuzzy
   `{e<=2}` CIP label matching — the project is currently stdlib-only and most OCR
   tolerance is already handled by the normalize pre-pass + equivalence sets.

## Key files

- `catalogue/cip.py` — OCR-tolerant CIP-block parser.
- `catalogue/book_identifier.py` — scheme-agnostic identifier API (ISBN/LCCN), checksum-validated.
- `catalogue/edition_resolve.py` — the two passes, the gate, precedence, CLI walk.
- `catalogue/work_titles.py` — page-LLM title derivation + mojibake guard.
- `catalogue/extract.py` — EPUB spine-order extraction.
- Tests: `tests/test_cip.py`, `tests/test_book_identifier.py`, `tests/test_edition_resolve.py`,
  `tests/test_extract.py`, `tests/test_work_titles.py`.
