# Multi-work / contained-text segmentation

A **work** is an abstract text (e.g. *The Way of the Bodhisattva*). A **book/edition**
is one physical printing. Many books in the corpus are **collections** — one volume
containing many distinct texts (an anthology of 20 translated sūtras, or a root text
bundled with its commentary). Cataloguing them properly means **segmenting** the book
into multiple work rows, each with its own title and author/translator.

That splitting "never materialized": of 371 proposals, **367 carry `works: [1 entry]`**
(a single work) and only 4 carry 2 works. **87 are `structure = "collection_unsegmented"`** —
recognized as collections but emitted as one work flagged `whole_book: true` /
`unsegmented: true`. Example: a book with `n_sections: 121` (clearly an anthology)
became 1 work, 1 contained_text. The 121 inner texts are not separate works.

The promotion layer faithfully materialized what the payloads contained. **The collapse
to one-work-per-book happens upstream, in proposal-building/extraction — not in
promotion.** This document explains the mechanism and a plan to fix it.

---

## Why segmentation never materialized — the precise mechanism

The segmenter in `catalogue/book_analysis.py` is deliberately *conservative*, tuned for
one archetype: a **Buddhist verse root-text + its commentary**. For a section to become
a work boundary ("anchor"), `_is_anchor` (`book_analysis.py:245`) requires **all three** of:

1. **Verse gate** — `peek_section` only labels a section `root`/`commentary` if
   `section.verse ≥ 0.50` (`book_analysis.py:91`). Prose fails outright.
2. **Distinct-title gate** — `_is_distinct_work_title` (`book_analysis.py:158`) rejects
   page labels, numbered/roman/ordinal chapters, `_CHAPTER_WORD` headings, all-caps
   short headers, anything with a 4-digit number, and mostly-symbol strings.
3. **Strong onset** — either a parsed opening attribution, *or* sustained verse `≥ 0.85`
   (`book_analysis.py:258`).

Then two hard caps in `analyze_book_sections` (`book_analysis.py:314-323`):

- **`_MAX_WORKS = 8`** → if more than 8 anchors survive, it throws them **all** away and
  emits `collection_unsegmented`. **A 121-text anthology can never segment, by
  construction, regardless of signal quality.**
- **0 anchors but `n_reproduced ≥ 5`** → also `collection_unsegmented`.

So the 87 `collection_unsegmented` books fall into two buckets, and **both are correct
refusals under the current rules**:

- **Prose anthologies** (e.g. 20 translated sūtras rendered as prose): every section
  fails the verse gate → 0 anchors → `n_reproduced ≥ 5` collapses to unsegmented.
- **Large verse anthologies / fine-grained bookmarks**: >8 anchors → the `_MAX_WORKS`
  cap collapses them.

The promotion layer then emits exactly one `whole_book: true` work, because that's what
the payload contains. The collapse is **upstream, by design** — the safe bias chosen
after the v15 finding (over-segmentation + ~94% null-author contained-texts). The
segmenter would rather emit one work than 121 garbage ones.

---

## The structural gap behind it

The deepest issue: **the nav/bookmark hierarchy is flattened and discarded.**

- In `_epub_sections`, every `<a>` in the nav doc is collected flat (`locator.py:172`).
- In `_pdf_bookmark_sections`, the outline level `_lvl` is thrown away (`locator.py:213`).

So the code cannot tell **"20 top-level works, each with sub-chapters"** (a clean
anthology — segment it) from **"121 flat fragments"** (mis-segmented — don't). It only
has a flat count, and a flat count of 121 looks identical to noise.

For a born-digital anthology, **the nav tree *is* the authoritative segmentation** — far
more reliable than re-deriving boundaries from verse heuristics. The current pipeline
ignores its best signal.

---

## Audit of the 87 `collection_unsegmented` payloads (2026-06-02)

Re-extracted every book's sections and re-ran the current analysis deterministically
(`ladder=None`, which reproduces structure classification exactly). Script:
`audit_unsegmented.py`; per-book detail: `audit_unsegmented.json`.

**The plan's premise was wrong.** The hypothesized "prose-anthology vs >8-anchor split"
barely exists:

| Collapse path | Count |
|---------------|-------|
| **B — zero anchors**, then `n_reproduced ≥ 5` reclassifies to collection | **86** |
| **A — >8 anchors**, `_MAX_WORKS` cap collapses | **1** (h295, 11 anchors) |

By source: 43 epub-nav, 40 pdf-bookmark, 4 pdf-textlayer. All 87 have `n_reproduced ≥ 5`.

So `_MAX_WORKS = 8` is a **non-issue in practice** — it fires on exactly one book. The
entire phenomenon is **Path B**: the segmenter finds *zero* work boundaries, and the
`n_reproduced ≥ 5 → collection_unsegmented` rule then relabels the book a collection
purely because ≥5 internal sections passed the verse gate (homage + numbered lines).

**Most of Path B is single works misfiled as collections — not anthologies we refused to
split.** The top of the list is dominated by long, verse-heavy *single* texts whose
chapters merely contain verse: *Life of Shabkar* (79 reproduced / 721 sections), *Words
of My Perfect Teacher* (32/508), *Perfection of Wisdom in 8000 Lines* (42/484), the
Lamrim Chenmo (*LRCM*, 77/435), *Dose of Emptiness* (107/610), the Nikāya translations
(*Connected/Numerical/Long Discourses*, 75–83 each). These should be `single_work`; the
collection flag is a **false positive**.

**No cheap deterministic signal separates a true anthology from a single work** — this is
the crux for the fix:

- **Distinct-title ratio fails.** 18 books have ≥30 % of sections passing
  `_is_distinct_work_title`, but most are *single* modern works with descriptive chapter
  titles (*Insight Into Emptiness*, *How to Enjoy Death*, the Dalai Lama "Library of
  Wisdom and Compassion" volumes, the *Course in Buddhist Reasoning and Debate*). 58 of
  87 sit below 0.15 (clearly chapter-structured single works).
- **Per-section attribution fails.** It reads 0 distinct authors on 57 books — *including*
  the genuine anthologies — and where it fires it returns OCR/regex garbage ("It was
  translated", "him in the", "a nun named"), not clean composer names.

The genuinely multi-text books are a small minority (~10–15), e.g. *Luminous Melodies:
Essential Dohās of Indian Mahāsiddhas* (h403, songs by Nāropa/Tantipa/Maitrīpa…), *The
Splendor of an Autumn Moon* (h422, Tsongkhapa's devotional verses), *Buddhahood without
Meditation* (h182), *Mind Training: The Great Collection* (h45), *Sounds of Innate
Freedom Vol. 5: The Indian Texts of Mahāmudrā* (h418). Separating these from the
single-work majority needs an **LLM judgment over the nav/section list**, not a regex.

### What this changes about the plan

1. **Drop the `_MAX_WORKS` concern** (Phase 2's framing). One book. Not the problem.
2. **The dominant bug is over-flagging, not under-segmenting.** The single highest-value,
   lowest-risk fix is to **make a zero-anchor book default to `single_work`**, not
   `collection_unsegmented`. That immediately corrects the ~58+ unambiguous single works
   and shrinks the real problem to the ~10–15 genuine anthologies.
3. **Phase 3 (non-verse anchor on distinct titles) is now shown to be dangerous as
   drafted** — direct evidence it would over-segment the 18 title-shape single works into
   dozens of fake "works," re-introducing the v15 over-segmentation regression. Any
   relaxation of the verse gate must be paired with a positive anthology gate.
4. **Phase 1 (surface `candidate_works`) must be gated**, not unconditional: surfacing a
   100-row "pick the works" checklist for *Life of Shabkar* is noise. Only surface when
   there is positive multi-work evidence.

### Revised priority

- **P0 — Reclassify zero-anchor books as `single_work`.** Change the
  `n_reproduced ≥ 5 → collection_unsegmented` rule (`book_analysis.py:320`): with zero
  anchors, default to `single_work` and emit the one whole-book work. Fixes the majority
  with no segmentation risk. (Verify against the ~10–15 true anthologies first so they
  aren't silently swallowed — flag those separately.)
- **P1 — A positive anthology detector for the residual ~10–15.** An LLM pass over the
  nav/section-title list (plus depth from Phase 2) that answers "is this one work or N
  named works, and what are they?" — applied *only* to books that survive a cheap
  pre-filter, then routed to `candidate_works` review.
- **P2 — Nav-depth capture (old Phase 2)** still worthwhile as input to P1, but no longer
  urgent on its own.

---

# Implementation plan: labeled segmentation + batch removal

Decision (2026-06-02): **batch auto-detection of multi-work books is abandoned.** The
audit showed it can't separate anthology from single-work without an LLM, and in practice
it only ever mislabels single works. We replace it with two things:

- **Batch** emits exactly one whole-book `single_work` proposal per book (no structure
  guessing). This is the P0 fix and the batch-removal in one move.
- **Multi-work segmentation becomes an on-demand tool** that runs only over a
  user-supplied list of holdings known to be collections.

The promotion plumbing (`promote.py`, `review_queue`, `book_toc_pattern`, `promotion`)
is **unchanged** — it materialises a proposal's `works[]` regardless of how they were
derived, so a hand-fed `multi_work` proposal promotes exactly like an auto-detected one
used to.

> Note: file deletion (`rm`) is blocked in this repo — remove tracked files with
> `git rm --cached <f>` and have the user delete the on-disk remnant (see memory
> `rm-blocked-use-git-cached`).

## Part 1 — Remove the dead batch segmentation code

**`catalogue/book_analysis.py` — delete the auto-detection engine.**
Remove `analyze_book_sections`, `_detect_works`, `_is_anchor`, `peek_section`,
`PeekVerdict`, `_PEEK_SYS`, the dataclasses `BookAnalysis` / `ContainedText` /
`book_analysis_from_dict`, and the thresholds `_VERSE_REPRODUCE`, `_ANCHOR_VERSE`,
`_COLLECTION_MIN`, `_MAX_WORKS`. **Relocate** the still-useful title helpers
(`_is_distinct_work_title`, `_part_work`, `_mostly_upper`, and the title regexes) into the
new `catalogue/segment.py` (Part 2) — the labeled segmenter reuses them. Net: the file is
deleted and its reusable ~40 lines move.

**`catalogue/process.py` — strip the book-analysis stage.**
- Remove the `analyze_book` and `section_version` fields from `ProcessConfig`
  (`process.py:82,66`).
- Remove the `section_cache` DDL (`:128`), `load_section_analysis` (`:137`),
  `store_section_analysis` (`:155`), and the book-analysis stage block (`:339-350`).
- Remove the `from .book_analysis import …` line (`:39`).
- Simplify `_emit_book_proposal` (`:358`): **keep** the contributor resolution
  (`resolve_contributors`) and the single whole-book emission; drop the `analysis`
  parameter and hardcode `structure="single_work"`. Collapse `_build_works` (`:406`) to
  just its degenerate whole-book branch (`:428-441`); delete the multi-work branch
  (`:413-427`) and the `unsegmented` payload key.
- The no-TOC whole-book path (`:256-262`) stays, minus the `BookAnalysis` import.

**`catalogue/promote.py` — drop the unsegmented segment.**
Remove the `collection_unsegmented → "unsegmented"` branch in `bucket()` (`:41`) and drop
`"unsegmented"` from `SEGMENTS` (`:27`). `multi_work` stays (the new tool produces it).

**`catalogue/web.py` — drop the dead structure option.**
Remove `"collection_unsegmented"` from the editor `structures` list (`:364`); update the
review-tab comment (`:252`). Segments are driven by `promote.SEGMENTS`, so nothing else
changes.

**Root scripts & runners.**
`git rm --cached peek_sections.py peek_probe.py` (both import the deleted `peek_section`).
In `run_step4.py:58` and `run_resolve.py:73` drop `analyze_book=True` — the batch run no
longer does any segmentation.

**Tests to remove/trim** (replace with the Part-2 tests):
`tests/test_step4_v15.py` (anchor/detect/structure — the whole batch-segmentation suite),
`test_step4_v14.py::test_analyze_book_queues_structure_proposal`, the `peek_section`
assertions in `tests/test_locator.py`, the `collection_unsegmented`/`unsegmented`-bucket
cases in `tests/test_promote.py` and `tests/system/test_promote_workflow.py`. **Keep** the
`multi_work` promote tests — that payload shape is exactly what the new tool emits.

## Part 2 — The labeled segmentation tool

New module `catalogue/segment.py` + CLI `run_segment.py`. Input: a list of holding IDs
(or filenames / file_hashes) the user has confirmed are collections.

**Prerequisite — capture nav depth (the old Phase 2).** Add `level: int` to `Section`
(`locator.py:32`); populate it in `_epub_sections` from nav `<ol>` nesting / NCX
`navPoint` depth (`:172`) and in `_pdf_bookmark_sections` from the `_lvl` already returned
by `get_toc` (`:213`). textlayer sections stay flat (level 0). Depth is what separates a
flat anthology (one work per entry) from "a few major works each with sub-chapters."

**Per-holding algorithm:**
1. Resolve book-level author(s)/translator(s) — reuse `resolver.resolve_contributors`, or
   read them from the holding's existing `book_toc_pattern` proposal.
2. `extract_sections(file_path)` (pass cached `toc_entries` for textlayer books).
3. Drop front/back matter (`classify._is_front_back`) and bare dividers.
4. **Candidate work-starts** = top-level entries (minimum `level`) when depth is present;
   otherwise every surviving entry. Fold deeper entries in as `section_titles`.
5. Parse each work: title (strip `Part N:` via `_part_work`, strip trailing `by X`);
   author (regex `by X` / `Teachings of X`, else inherit book author); translator
   (inherit book translator); `locator`; member section titles.
6. **One bounded LLM cleanup pass** over the work-start list (the ladder, schema-validated
   JSON): normalise composer names, merge mis-split sub-chapters, drop stray apparatus,
   confirm the work count. Cheap — it sees titles + short openings, not full text.
7. Emit a `multi_work` `book_toc_pattern` proposal and **supersede** the holding's existing
   pending single_work proposal (mark it `resolved='superseded'`, or rewrite in place).

The two real shapes from the audit validate this: *Luminous Melodies* / *Buddhahood
without Meditation* are flat lists whose nav titles carry the author (`… by Nāropa`,
`Teachings of Saraha`) — steps 1-5 alone segment them; the LLM pass only normalises. The
nested shapes (root+commentary, *Mind Training: The Great Collection*) rely on step-4 depth
to avoid splitting into chapters.

Review happens in the existing `/review` **multi_work** segment; promotion uses the
existing `promote_proposal` path.

### Engine gaps — what's missing to do labeled segmentation (UI/CLI aside)

Scoping the target workflow — *list all books → user marks which are multi-work → for each,
the engine figures out the works → user approves each or supplies alternate names* — the
engine splits cleanly into a **back half that already exists** and a **front half (the
segmentation intelligence) that does not.**

**Already built (the approve-and-promote half is free):**
- **Promote a `multi_work` proposal → N works** — `promote.promote_proposal` materialises
  `works[]` into `work`/`person`/`work_contributor`/`edition_work`; tested.
- **Per-work override** — the `/review` payload editor (`web.py:369`) rewrites `works[]` per
  row (title/authors/translators/kind/locator), pending-only, recomputes the bucket. "Approve
  each / give alternate names" is just editing the pending proposal — **no engine gap.**
- **Book-level contributor resolution** to inherit per work — `contributors.resolve_contributors`.
- **Section location** — `locator.extract_sections` (per nav/bookmark entry).
- **Front/back filter** — `classify._is_front_back`. **Review routing** — `bucket()` +
  `promote_segment`.

**Missing (all inside "figure out the works"):**

- **G1 — No non-verse-gated segmentation function.** *(PARTIALLY LANDED 2026-06-03.)* The
  verse gate + strong-onset refusal is now behind `enable_verse_gate` (default **OFF**) —
  `peek_section` / `_is_anchor` / `_detect_works` / `analyze_book_sections` and a
  `ProcessConfig.enable_verse_gate` field + `--enable-verse-gate` CLI flag on
  `run_step4.py` / `run_resolve.py`. With the gate off the engine default-INCLUDES: every
  distinct-titled non-front/back section is a work candidate, and the `_MAX_WORKS` /
  `_COLLECTION_MIN` collapse guards do not fire. So the "treat declared-collection sections
  as works" inversion exists at the engine level. **Still open:** nav-depth grouping (G2) and
  the LLM cleanup pass (G3) are NOT applied, so the gate-off engine over-segments nested
  books into chapters; and there is no holding-scoped entry point yet (that is G4). *Caveat:*
  running the existing batch (`analyze_book=True`) without `--enable-verse-gate` now segments
  **every** book aggressively — pass the flag to restore the old conservative batch.
- **G2 — Nav/bookmark hierarchy is discarded.** `Section` has no `level`; `locator` flattens
  the tree (drops `_lvl` at `:213`, collects all `<a>` flat at `:172`). Without depth the
  engine can't tell "20 top-level works each with sub-chapters" from "121 flat fragments" and
  would over-split nested works into chapters. Flat anthologies don't need it; **nested ones
  can't be done correctly without it.** (Same as Part-2's depth prerequisite.)
- **G3 — No section-title→(work, author) parser, and no whole-list LLM segmentation pass.**
  The author is usually *in the title* ("A song **by Tantipa**", "**Teachings of Saraha**"),
  but `find_attribution` reads the opening *body* (verse-gated) and `parse_title_contributors`
  is built for edition filenames — neither parses a section title. And there is no LLM prompt
  that takes the whole section-title list of a known anthology and returns
  `{works:[{title, author, members}]}` (normalise composer names, merge sub-chapters, drop
  apparatus). `peek_section` is per-section + verse-gated — wrong tool.
- **G4 — No entry point to emit a `multi_work` proposal for a labeled holding, nor to
  supersede an already-promoted single work.** Today the only emitter is the dead batch path
  (produces `single_work`/`collection_unsegmented`). There is no `segment_holding(hid) →
  multi_work proposal`. And **86 of these are already promoted as single works**, so the
  engine must revert that promotion (`revert_proposal` exists) and re-queue — but `_queue`
  blind-inserts, so re-segmenting isn't idempotent. The pieces exist; the orchestration
  (revert single → emit multi → review → promote) and the dedup guard don't.
- **G5 (minor) — No mojibake/quality gate on the section path.** A born-digital custom-font
  book yields garbage section titles; the mojibake guard lives only in `edition_resolve`, not
  the section path, so a labeled mojibake book would segment into garbage. Edge case, real.

**Which gaps block which shape:**

| Collection shape | Gaps that block it |
|---|---|
| **Flat anthology, author-in-title** (Luminous Melodies, Buddhahood w/o Meditation) | G1 + regex slice of G3 + G4 — **small new code** |
| **Nested** (root+commentary, *Mind Training: The Great Collection*) | G1 + G2 (depth) + G3 (LLM grouping) + G4 |

**Minimum viable engine:** a `segment_holding` function (G1 + G4) that treats sections as
works and parses `by X`/`Teachings of X` from titles (regex part of G3) segments the *flat*
anthologies end-to-end — the editor + promoter already handle approval and materialisation.
The *nested* shape is what pulls in nav-depth (G2) and the LLM grouping pass (full G3).

## Part 3 — Data migration for the 87 existing proposals

Each existing `collection_unsegmented` proposal already contains one correct `whole_book`
work — it was just bucketed as "unsegmented." One-off migration:
- Rewrite the 87 pending `collection_unsegmented` payloads → `structure="single_work"`,
  drop the `unsegmented` flag, so they bucket as `single_work` and can be bulk-promoted.
- `DROP TABLE section_cache` once no code reads it.
- The books on the user's multi-work list are then superseded by the Part-2 tool's
  `multi_work` proposals.

## Sequencing & verification

1. **P0 ship:** batch emits `single_work` only + migrate the 87 (Parts 1-process + 3).
   Immediately corrects the majority; no segmentation risk.
2. **Cleanup:** delete the dead engine + tests (rest of Part 1).
3. **Build the tool:** locator depth → `segment.py` → `run_segment.py` (Part 2).
4. **Run on the user's list**, eyeball the first few `multi_work` proposals in `/review`,
   then promote.

Verify with the full `pytest` suite plus a black-box system test (per memory
`end-to-end-system-tests-required` / `system-tests-for-major-changes`): seed a holding,
run `run_segment` with an injected ladder, assert an N-work `multi_work` proposal, promote
it, assert N `work` rows. Add a regression test for each real shape (flat `by X`; nested
with depth) per `regression-tests-each-step`. Wrap any long re-OCR/LLM run in
`caffeinate -i -s`.

---

## Appendix — original phased plan (design rationale only)
*Superseded in priority by the plan above; retained for the nav-depth / per-text-author
reasoning.*

### Phase 1 — Stop the dead-end collapse (no auto-segmentation risk)
Today `collection_unsegmented` discards the section list and emits one whole-book work.
Instead, carry the surviving nav entries into the payload as `candidate_works` (those
that pass `_is_distinct_work_title`), so `/review` shows a checklist a human can
accept/merge — rather than a single opaque "whole book." This salvages all 87 books
immediately with **zero false-positive risk**, because nothing is auto-promoted. It is a
payload + `/review` UI change only.

### Phase 2 — Capture nav depth in `Section`
Add a `level: int` to `Section`, populated from NCX `navPoint` nesting / nav `<ol>` depth
(`locator.py:172`) and from the PDF `_lvl` already in hand (`locator.py:213`). Then
segment on **top-level entries**, folding their descendants in as `section_titles`. This
is what distinguishes a real 20-work anthology from 121 fragments, and it directly
retires the crude `_MAX_WORKS = 8` cap.

### Phase 3 — A non-verse anchor path for reliable TOCs
The verse gate is the right test for "is this prose chapter secretly a reproduced text
inside a modern study" — but the *wrong* test for "is this nav entry its own work in an
anthology." Add: a top-level entry from a **trustworthy source** (`epub-nav` /
`pdf-bookmark`, not `pdf-textlayer`) with a clean distinct title can anchor *without*
clearing the verse gate. Keep the verse gate as the gate for the untrustworthy
`pdf-textlayer` source, where titles are OCR-matched folios and over-segmentation is the
real danger.

### Phase 4 — Per-text authorship (fixes the null-author half of v15)
In a 20-sūtra collection, inheriting one book-level author is wrong — each text has its
own composer, or is anonymous/canonical. Run the `peek_section` author LLM per accepted
anchor, and treat **"anonymous/canonical" as a valid value** rather than
null-then-inherit. The translator usually *is* shared and can stay inherited.

**Net:** Phase 1 alone unblocks all 87 books today via human review; Phases 2-3 make
large anthologies auto-segment safely by trusting the nav tree; Phase 4 fixes the
authorship quality the v15 spot-check flagged.

---

## Key code references

| Component | Location | Role |
|-----------|----------|------|
| Container analysis / structure classification | `catalogue/book_analysis.py:299` `analyze_book_sections()` | Sets `single_work` / `multi_work` / `collection_unsegmented`; applies the two caps |
| Work-boundary test | `catalogue/book_analysis.py:245` `_is_anchor()` | The three-gate anchor rule |
| Distinct-title lever | `catalogue/book_analysis.py:158` `_is_distinct_work_title()` | Rejects page labels / chapters / OCR garbage |
| Work detection | `catalogue/book_analysis.py:261` `_detect_works()` | Builds one work per anchor, folds chapters in |
| Per-section peek | `catalogue/book_analysis.py:80` `peek_section()` | Verse gate + author extraction |
| Caps | `catalogue/book_analysis.py:60` `_MAX_WORKS=8`, `:59` `_COLLECTION_MIN=5` | The collapse thresholds |
| Section detection (flattens hierarchy) | `catalogue/locator.py:144` `_epub_sections()`, `:212` `_pdf_bookmark_sections()` | Nav/outline → flat `Section` list; depth discarded |
| Proposal emission | `catalogue/process.py` `_emit_book_proposal()` / `_build_works()` | Builds the `book_toc_pattern` payload `works[]` |
| Promotion | `catalogue/promote.py:121` `promote_proposal()` | Materializes payload into canonical rows (faithful) |

---

## Related stored findings
- `v15-batch-spotcheck-findings` — over-segmentation + ~94% null-author contained-texts;
  the reason the current bias is conservative.
- `step4-section-based-root-detection` — locate sections via epub nav / pdf bookmarks +
  verse-form & "Attributed to" signals.
- `per-entry-classifier-unreliable` — gemma TOC-title root/other classification is a
  coin-flip; do not rely on title-string classification for splitting.

---

# Appendix — gate-OFF dry run across structure × format (2026-06-03)

After the verse gate was put behind `enable_verse_gate` (default OFF, see G1 above), a
no-write dry run measured the gate-OFF vs gate-ON outcomes on a 21-book sample spanning
both structures and all three forms (verse / prose / mixed). Harness:
`run_segment_dryrun.py` (reads files + caches, runs `analyze_book_sections` both ways,
prints a verse profile + work list, writes nothing).

```
python3 run_segment_dryrun.py            # the curated 21-book sample
python3 run_segment_dryrun.py 403 45 70  # specific holding ids
```

Legend: gate-ON's `collection_unsegmented` collapses to **1 whole-book work** (flagged) —
"coll-flag" below. "Correct" = how the book should actually be catalogued.

| Bucket | Holding | File basename | Form | gate-ON | gate-OFF | Correct |
|---|---|---|---|---|---|---|
| MULTI root+comm | h230 | `The Treasury of Knowledge, Book 6…Snow Lion….pdf` | verse 82% | 1 (coll-flag) | **2 (root+comm)** | gate-OFF |
| | h4 | `Chittamani Tara -- Pabongkha.epub` | mixed 24% | 1 (coll-flag) | **10** | gate-OFF |
| | h51 | `Gyaltsab - Entrance for the Children…_commentary.pdf` | prose 12% | **1 (coll-flag)** | 56 | **gate-ON** |
| MULTI anthology | h403 | `Luminous melodies_ essential dohās…Brunnhölzl….epub` | mixed 36% | 1 (coll-flag) | **60** | gate-OFF |
| | h182 | `Buddhahood without Meditation_ Dudjom Lingpa….epub` | mixed 36% | 1 (coll-flag) | **15** | gate-OFF |
| | h45 | `Mind Training The Great Collection Jinpa.pdf` | mixed 19% | 1 (coll-flag) | 1 whole-book | **neither** |
| | h418 | `Sounds of Innate Freedom_Vol5…Brunnhölzl.epub` | verse 82% | 1 (coll-flag) | 2 (junk) | **neither** |
| MULTI verse | h39 | `G Sopa - Peacock in the Poison Grove.epub` | mixed 30% | **2** | **2** | both |
| | h422 | `The Splendor of an Autumn Moon…Tsongkhapa….epub` | mixed 32% | 1 (coll-flag) | **23** | gate-OFF (borderline) |
| MULTI mixed | h211 | `Taking the Result as the Path…Cyrus Stearns.epub` | prose 10% | 2 | 84 | neither (OFF closer) |
| | h405 | `Opening the Treasure of the Profound…Milarepa….pdf` | prose 8% | 1 (single) | 3 | gate-OFF (borderline) |
| SINGLE prose | h274 | `G Jampa Tegchok — Insight Into Emptiness.PDF` | prose 7% | **1 (coll-flag)** | 129 | **gate-ON** |
| | h171 | `How to Enjoy Death.epub` | prose 5% | **1 (coll-flag)** | 90 | **gate-ON** |
| | h275 | `Dose of Emptiness -- Cabezón .pdf` | mixed 18% | **1** | **1** | both |
| SINGLE verse | h57 | `Shantideva - Way of the Bodhisattva.pdf` | no TOC | **1** | **1** | both |
| | h344 | `Note on p384 - G Sopa Motivation 7:08:04AM.pdf` | no TOC | **1** | **1** | both ⚠️ mislinked file |
| | h400 | `One Hundred and Eight Verses in Praise….pdf` | no TOC | **1** | **1** | both |
| SINGLE mixed | h70 | `Life of Shabkar.pdf` | prose 11% | **1 (coll-flag)** | **1** | both |
| | h72 | `Life_of_Milarepa.pdf` | no TOC | **1** | **1** | both |
| | h371 | `LTK - LRCM v1.pdf` | mixed 18% | **1 (coll-flag)** | **1** | both |

**Findings:**

- **Form (verse/prose/mixed) is orthogonal to the outcome.** Gate-OFF segmentation is
  driven entirely by whether sections carry distinct *work-like titles*, not by verse
  content — h70 (mixed autobiography) → 1 work because its bookmarks aren't distinct
  titles; h274 (pure prose study) → 129 "works" because its chapter titles look distinct.
  The original "does multi-work need verse?" worry is fully inverted: **title shape is
  everything, verse is irrelevant.** Standalone verse works (h57, h344, h72) have no TOC →
  pass through as 1 whole-book work automatically.
- **The two modes are complementary, neither wins alone.** Gate-OFF is right for *flat
  anthologies* with distinct nav titles (h230, h4, h403, h182, h422 — 5 books gate-ON gets
  wrong by collapsing). Gate-ON is right for *nested single works* — a commentary's sa-bcad
  outline (h51) or a study's chapters (h274, h171) — where gate-OFF explodes into 56/129/90
  fake works. Both agree and are correct on clean 2-work verse (h39), no-TOC singles, and
  non-distinct-bookmark singles (8 books). Neither works on noisy-TOC anthologies (h45
  page-labels, h418 filtered titles) and the giant Lamdre cycle (h211).
- **The separator is structural depth, not verse.** Gate-OFF wins exactly when the work
  boundaries are *top-level* nav entries; gate-ON "wins" on nested books only by refusing
  everything. The one signal that distinguishes "distinct title = a real work" from
  "distinct title = a sub-heading of the work above" is **nav-hierarchy depth — G2.** With
  depth captured, a single mode (segment top-level entries, fold descendants in) would get
  the gate-OFF *and* gate-ON columns right at once: h51/h274/h171 stay 1 work while
  h403/h182 still split.

**Implication for build order:** **G2 (nav-depth) is the highest-leverage next step** — it
converts ~7 wrong rows to correct. **G3 (LLM grouping/title-cleanup)** then handles the 4
"neither" rows (h45, h418, h211, and noisy-TOC anthologies generally). Authors are sparse
in the dry run only because it runs `ladder=None`; the G3 pass fills them from "by X" /
"Teachings of X" titles.
