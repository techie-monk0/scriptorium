# Person → authority resolution — diagnosis & fixes

*Session 6, 2026-06-02. Live DB: `catalogue-db/catalogue.db`.*

## TL;DR

After the authorship layer was rebuilt (`recompute_contributors.py` → **557 persons,
960 `work_contributor` links, 207 translator links**), `person_external_id` was still
**0**. Two things were true: (1) the verify/authority pass had simply never been run on
this DB, and (2) even when run, its old policy bound **almost nothing** on this corpus.
The reasons are structural, not a single bug. This documents why, the fix already
landed, and the name-matching gaps that remain.

## How authority binding is wired (recap)

The regular flow (`run_resolve` → `run_load` → promote-in-`/review`) produces persons
with `verification_status='provisional'` and `external_id` NULL. Authority binding is a
**separate, manually-run** pass — nothing auto-triggers it (see the promote.py docstring;
`person_external_id` was 0 because the pass was never run, not because anything broke).
The two passes:

- **Name-only** (`catalogue/verify.py` `verify_all --kind person`) — matches a person's
  name against Wikidata / VIAF / BDRC.
- **Work-driven joint** (`catalogue/person_work.py`) — disambiguates a person by the
  *work* they authored, via `WorkAuthorityResolver`. Binds only on strong-work + exact-name.

## The crack both passes fell into

- The joint pass needs a **classical work with a near-unique referent** (ideally a Toh
  number). Our 364 works are **modern publisher book-containers** ("How to Meditate on
  the Stages of the Path"), so it resolves nothing → `unmatched`. Measured: a 557-person
  run produced **0 auto-binds, 1 fuzzy candidate**.
- The old name-only pass **DEFERRED every work-attached person** to the joint pass (the
  homonym guard). All 557 are authors/translators → work-attached → all deferred.

So a person who is (a) work-attached AND (b) whose works are modern containers fell
through both passes — i.e. essentially everyone. Even a marquee classical author with a
perfect, reachable, auto-bindable authority record got nothing.

### The Atisha probe (the smoking gun)

`Atisha` resolves to a **hard, non-provisional** Wikidata hit `Q320150` carrying
cross-links `{wikidata:Q320150, bdrc:P3379, viaf:36928173}` — exactly what
`person_external_id` is for. But the joint pass logged him `unmatched` (his catalogue
works are modern anthologies — *The Book of Kadam*, *Wisdom of the Kadam Masters* — not
his classical *Bodhipathapradīpa* / Toh 3947), and the name-only pass **deferred** him
because he has a work edge. He also exists as **three person rows** (103 "Atisha", 481
"Atisha Dipankara", 483 "Jowo Atisha") because the spellings don't share a fold-key, and
rows 481/483 are mis-attributed to *"Atisha and Buddhism in Tibet"* — a book *about* him.

## Fix #1 — LANDED: hard matches bind even when work-attached

`catalogue/verify.py` `verify_person` now probes for a match **before** the work-edge
check and **auto-binds a HARD (non-provisional) exact-name hit even for a work-attached
person** — the verifier's own name guard makes it safe (it is not the homonym risk the
deferral was guarding against). Only a **fuzzy/absent** hit on a work-attached person is
deferred to the joint pass.

- Tests updated: the two old "work-attached ⇒ deferred" tests in
  `tests/test_person_status.py` were rewritten; **3 new tests** cover hard-match-binds,
  fuzzy-defers (author edge), fuzzy-defers (translator edge). Verify/authority suite green.
- **Validated live**: the patched name-only pass binds work-attached **modern** authors
  with Wikidata records too — e.g. `B. Alan Wallace → wikidata:Q2688724` — where before
  *every* work-attached person was deferred. (Run: `authority_pass.py`, against a copy.)

## Fix #2 — PROTOTYPE: authority-driven dedup

`authority_pass.py` `merge_by_authority`: clusters persons by **shared authority id**
(a bound person's `external_id` + harvested cross-links; the still-provisional ones are
re-probed) and merges each cluster into its hard-bound anchor via `names._merge_person`.
**Precision-first** — a cluster with no hard anchor is left for review, never merged on
name similarity alone. This is the safe way to collapse the Atisha trio: "Atisha" binds
hard (anchor, carrying `bdrc:P3379`), and the fuzzy "Atisha Dipankara"/"Jowo Atisha" both
resolve to `bdrc:P3379` → corroborated → merged into the anchor.

## Why specific famous names still don't auto-bind

None are missing from the authorities — they fail at the **name-matching** layer, for
four distinct reasons (probes: stored form → result, vs bare/personal form):

| Name (stored) | Stored-form result | Bare / personal form | Root cause |
|---|---|---|---|
| Acarya Nagarjuna | no match | `Nagarjuna` → Q171195 *provisional* | **A** + **D** |
| Acharya Kamalashila | no match | `Kamalashila` → bdr:P7641 *provisional* (no WD) | **A** |
| Jeffrey Hopkins | Q1686453, canon exact, *provisional* | — | **D** |
| Dalai Lama XIV | bdr:P1649 fuzzy *provisional* | `Tenzin Gyatso` → Q17293 **HARD** | **C** |
| Panchen Lozang Chökyi Gyaltsen | bdr:P00KG07868 *"dhondup chokyi nyima"* (wrong) | `Lobsang Chokyi Gyaltsen` → Q930398 **HARD** | **B** |

**A. Honorific/title not in the strip vocabulary.** `strip_honorifics`
(`catalogue/honorifics.py`) doesn't know `Acarya` / `Acharya` / `Arya` / `Panchen`, so the
title-prefixed string is sent verbatim and matches no authority label. One-line vocab
fix; would immediately reduce "Acarya Nagarjuna" → "Nagarjuna".

**B. Transliteration / diacritic variance.** Stored "Lo**z**ang Chökyi Gyaltsen" vs the
authority "Lo**b**sang"; the exact-name guard can't bridge z/b (and ö). Searched as
stored, BDRC's fuzzy search even returned the *wrong* incarnation — correctly provisional.
"Lobsang Chokyi Gyaltsen" hard-matches Q930398. (Also duplicated: persons 21 "Lozang" /
68 "Losang".)

**C. Entity returned, but the guard rejects it (office/ordinal naming).** This is *not* a
search miss — searching "Dalai Lama XIV" **does surface `Q17293`** (label "Tenzin Gyatso",
description "14th Dalai Lama"). It fails one step later: the **token-overlap name guard**
compares the stored "Dalai Lama XIV" against the label "Tenzin Gyatso", finds no shared
tokens, and throws the correct hit away (only fuzzy BDRC `P1649` survives → provisional →
deferred). The catalogue stores the office + Roman numeral; the authority indexes the
personal name (and `names.canonical_dalai_lama` actively rewrites Tenzin Gyatso →
"Dalai Lama XIV", erasing the string that *would* have passed the guard). **Fix: search
the person's aliases / personal-name form, not just `primary_name`** (so "Tenzin Gyatso"
is one of the strings tried), or keep a small curated office→Q-id table.

**D. Genuine homonym ambiguity.** "Jeffrey Hopkins" returns the right entity with an
exact canonical name but is flagged `provisional` (several Jeffrey Hopkins on Wikidata);
"Nagarjuna" likewise. That caution is **correct** — these should be human-confirmed. The
real bug is the confirmation path: work-attached + provisional persons are **silently
deferred with no review item**, and the joint pass that's meant to confirm them can't
(modern container works). They should queue a candidate for review instead.

**E. Organizations / corporate authors have no path.** "Padmakara Translation Group" is
Wikidata `Q2985401`, an **organization** — the person verifiers require `P31 = human (Q5)`
and reject it. There is no corporate-author entity type or verifier, so translation groups
sit forever as provisional "persons." Needs its own lane (a corporate-author entity + an
org verifier, or at least routing orgs out of the person pipeline).

## BDRC ElasticSearch — the Tibetan-name path (session 6)

`Wikidata-first` is backwards for Tibetan figures (Wikidata has poor Tibetan phonetic
coverage). The right primary source for them is **BDRC's own search**, the service behind
library.bdrc.io — *not* the `BLMP` lds-pdi template the project currently uses (which
matches literals across ALL resource types and never surfaced the person; even fed the
exact Wylie `paN chen blo bzang chos kyi rgyal mtshan` it returned his *works* + a wrong
person `P7192`, never `P719`).

**Endpoint (extracted from the SPA):** `POST https://autocomplete.bdrc.io/msearch`, index
`bdrc_prod`, Basic auth `publicquery:0Vsg1QvjLkTCzvtl` (a public read-only key shipped in
the frontend JS). There is also `POST /autosuggest` with body `{"query": "<name>"}` — but
that is a **prefix typeahead**, not the full search (it returns `[]` for full names that
aren't a label prefix, which is why it found nothing for "Lobsang Chökyi Gyaltsen").

**The phonetic→Wylie mechanism (key finding).** BDRC stores Tibetan person names in
**Wylie** (`prefLabel_bo_x_ewts`) + English; for many figures there is **no English-phonetic
label** (P719's `prefLabel_en` is itself Wylie "paN chen 04 blo bzang…", `altLabel_en` is
just "Panchen Lama 04"). So a raw phonetic English query misses them. The website bridges
this by **converting the phonetic query to Wylie** (it bundles a jsEWTS transliterator),
then matching the Wylie field — confirmed: querying `blo bzang chos kyi rgyal mtshan`
against `prefLabel_bo_x_ewts` (person-filtered) **does** surface `P719`. So
"search the phonetic spelling in BDRC and get the person" works *only with* a phonetic→Wylie
step, which we do not yet have (and the local LLMs — gemma3, qwen3 — are unreliable at Wylie:
gemma even bled a Chinese character into the output; both relabel the phonetic as "Wylie").

**Prototype (`bdrc_search.py`) results.** A fuzzy multi-field person search (`prefLabel_*`,
`altLabel_*` over `en`/`bo_x_ewts`/`iast`, `fuzziness:"AUTO:4,8"`) over the live unmatched
persons:
- **Score alone is unsafe** — confident-looking *wrong* top hits (e.g. "Khenchen Thrangu
  Rinpoche" → `gyatrul rinpoche`, "Losang Choephel Ganchenpa" → `aschoff, jurgen c.`). Same
  fuzzy-noise trap as the old BLMP disaster.
- **With a strict name guard** (every content token of the query ⊆ the returned label):
  **6 of 60 unmatched safe-auto-bind, all correct** — B. Alan Wallace→`P9927`, Lama Yeshe
  Gyamtso→`P1KG20769`, Geshe Thupten Jinpa→`P10277`, D.T. Suzuki→`P1KG13704`, Geshe Lobsang
  Tharchin→`P1AL8`, Dan Martin→`P9222`; the false positives correctly drop to *review*.

**Design conclusion.** Add a **BDRC-ES verifier as a candidate generator** (strictly better
than BLMP) gated by the **label name-guard** for auto-bind; route Tibetan/phonetic names to
it *first*, Western names to VIAF/Wikidata, and harvest cross-links. The **Wylie-only tail**
(the Panchen, etc.) still needs phonetic→Wylie conversion or human judgment → the manual
matching tool. URLs for review: `https://purl.bdrc.io/resource/<id>`.

## Open work (proposed next steps)

1. **Honorific/title vocab** — add `acarya`/`acharya`/`arya`/`panchen` (and audit the
   Tibetan/Sanskrit title set) to `strip_honorifics`. + regression test. *(easy, high yield)*
2. **Alias-based matching** — match against **all** of a person's `person_alias` rows, not
   just `primary_name`; recovers the Dalai Lama (Tenzin Gyatso alias) and transliteration
   variants. + regression test.
3. **Queue work-attached + provisional persons for review** instead of silent `deferred`,
   when the joint pass has no resolvable work — so Hopkins/Nagarjuna become actionable.
4. **Transliteration-tolerant name guard** — fold common Wylie/phonetic variants
   (Lozang/Lobsang, ö/o) in the exact-name comparison.
5. **Promote `merge_by_authority`** from prototype into `catalogue/` (it currently re-probes
   the network; consider folding the merge into the verify pass to avoid the second pass).
6. **Wire an in-app "Run verification" trigger** (still CLI-only) so the pass actually runs
   after promotion rounds — otherwise `person_external_id` stays empty by default.
7. **Promote the BDRC-ES verifier** (`bdrc_search.py`) into `catalogue/` as a `Verifier`,
   gated by the label name-guard, routed first for Tibetan/phonetic names. Replaces the
   noisy BLMP template. *(prototype done; 6/60 safe binds measured)*
8. **Phonetic→Wylie conversion** for the Wylie-only BDRC tail (the hard part — local LLMs
   unreliable; consider a deterministic phonetic→EWTS table or porting BDRC's converter).
9. **Org/corporate-author lane** (cause E) — recognize organizations and route them out of
   the person pipeline.

## Files

- `catalogue/verify.py` — `verify_person` policy change (Fix #1).
- `tests/test_person_status.py` — updated/added policy tests.
- `authority_pass.py` — name-only bind + `merge_by_authority` driver (Fix #2 prototype).
- `bdrc_search.py` — BDRC ElasticSearch person-search prototype (autocomplete.bdrc.io
  `/msearch`, index `bdrc_prod`) + guarded auto-bind runner.
- `recompute_contributors.py` — the authorship-layer rebuild this all sits on top of.
- `catalogue/honorifics.py`, `catalogue/names.py` — where fixes A–C land.
