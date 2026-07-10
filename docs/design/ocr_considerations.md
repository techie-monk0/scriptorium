# OCR Considerations — Scanning Whole Books into the Catalogue

*Problems and mitigations for OCRing physical Buddhist books scanned in (Step 6,
§4.8 Digitizer). Written 2026-05-30.*

For this corpus, OCR is where most downstream damage originates: bad OCR silently
poisons title/author matching, search recall, and the citation/terminology
extraction that content processing depends on. The problems below are ordered
worst-and-most-domain-specific first.

---

## 1. Diacritics and scripts — the domain killer

What makes Buddhist texts different from ordinary OCR:

- **IAST diacritics get mangled systematically.** `ā ī ū ṛ ṃ ḥ ṅ ñ ṭ ḍ ṇ ś ṣ` —
  vanilla Tesseract `eng` drops or confuses these (macron → nothing, underdot →
  comma/nothing, `ś`→`s`, `ṃ`→`m`). The fix is the **Shreeshrii IAST
  traineddata** (`eng+iast`), and the hard rule **never use `san`** (the
  Devanagari model hallucinates Devanagari into romanized lines, §4.8 / v5 H1).
  Even with Shreeshrii it is imperfect on the tiniest marks.
- **The errors are silent and systematic** — the insidious part:
  `Śāntideva → Santideva`, `Nāgārjuna → Nagarjuna`, `dharmakāya → dharmakaya`.
  These often survive a dictionary check (the stripped word still looks
  plausible), so a page can score "clean" while every diacritic is wrong.
- **Scope (confirmed): romanized only — no native scripts.** Tibetan and Sanskrit
  appear **only as Latin-script IAST / Wylie**, never as dbu-can or Devanagari. So
  the whole native-script OCR problem (and the Tibetan-OCR ecosystem — BDRC /
  Monlam / Namsel — and the `san`/`bod` Tesseract models) is **out of scope**. The
  job reduces to **high-fidelity Latin OCR** that (a) preserves IAST diacritics
  exactly, (b) handles verse/footnote layout, and (c) does **not "improve" Wylie /
  IAST strings** (e.g. `bsgrubs`, `rnam par shes pa`) into English-looking words.
- **Wylie/IAST is a transliteration, not a language** — a string like `bsgrubs`
  looks like a corrupt typo to any engine with a language prior, so the prior that
  helps ordinary English *hurts* here. This is the central reason a from-scratch
  VLM transcription is risky (§8).
- **Unicode normalization.** OCR can emit decomposed (`a`+combining-macron)
  instead of precomposed `ā` — visually identical, different bytes, breaks
  matching/FTS. **NFC is the mandated first post-OCR step** (§4.8c).

## 2. The scan itself — garbage in, garbage out

The text layer is only as good as the image, and diacritics are *small*:

- **DPI.** ≥300, **400 for small diacritics** — below that, macrons/underdots
  are unrecoverable. Scan **grayscale, not bitonal** (1-bit thresholding
  destroys faint marks).
- **Binding/gutter.** Tight bindings → curved text and gutter shadow near the
  spine → errors concentrated at line starts/ends. Only destructive (cut-spine)
  scanning gives truly flat pages.
- **Bleed-through** on thin paper (common here) → phantom characters from the
  reverse side.
- **Skew/warp, uneven lighting** (phone/overhead) → baseline distortion and
  thresholding errors deskew won't fully fix.
- **Foxing/yellowing/stains** on older volumes → noise.

## 3. Layout and structure

- **Verse layout.** Indented, numbered stanzas — OCR often **flattens line
  breaks** or merges verse into prose, defeating verse-form detection
  (`verse_score` relies on consecutive short numbered lines). Hurts the
  cataloguing pipeline, not just reading.
- **Footnotes/endnotes.** Citations, Sanskrit terms, and Toh numbers often live
  in the footnotes. OCR commonly merges them into the body, scrambles order, or
  garbles superscript markers — wrecking exactly the citation-extraction data.
- **Multi-column** (dictionaries, academic works) → reading order scrambled if
  column detection fails.
- **Hyphenation** at line ends splits words → breaks FTS and matching unless
  de-hyphenated.
- **Page furniture** (running heads, folio numbers, drop caps, transliteration
  tables, diagrams/mandalas, pecha-format pages) → injected noise.

## 4. Engine choice is unresolved — and it gates everything

The **OCR bake-off is still open** (§9): Tesseract+Shreeshrii vs **Cloud Vision**
vs **Apple Vision**, scored on **macron/underdot survival**. Tesseract is only
the *provisional* default.

- Don't commit hundreds of books to an unvalidated engine. **Run the bake-off
  first** on a handful of representative pages (one IAST-dense, one
  Tibetan-mixed, one verse-heavy, one footnote-heavy); pick by diacritic
  survival.
- **Cloud Vision** is usually best on diacritics but costs money, sends contents
  to Google (privacy), and is a fork/adapter integration — not turnkey.
- **Apple Vision** is free, on-device, good on Latin+diacritics — worth testing,
  unvalidated here for underdots.
- Mechanics: `--redo-ocr` (lossless) vs `--force-ocr` (rasterizes, lossy) — moot
  for image-only scans, but the OCRmyPDF subprocess has **no timeout** (open bug
  L1), so a wedged page pins forever in a long batch.

## 5. "The scan becomes the only copy" — errors are permanent

Once a book is scanned and shelved/discarded, the PDF is the archive. An
undetected systematic diacritic error is then baked in forever.

- **Always retain the raw page images** (§4.8). The text layer can be regenerated
  with a better engine later; a poor scan cannot. This makes the engine choice
  **reversible** as long as images are kept — the key insurance.
- **Validate on diacritic survival, not just dictionary hit-rate** (§4.8c step 2
  watches systematic substitutions). A clean dictionary score can hide 100%
  macron loss.

**Nuance:** the FTS index folds diacritics (`remove_diacritics 2`), so basic
keyword *search* is fairly robust to diacritic loss (query and index both fold).
The real damage is to (a) stored/displayed fidelity, (b) **exact citation/term
extraction**, and (c) author/title matching when errors go beyond diacritics into
letter swaps (`rn→m`, `l→1`, `c→e`) — those hurt search too.

## 6. Scale, throughput, and contention

- OCR is **CPU/IO-heavy** (Tesseract + Ghostscript); whole books are hundreds of
  pages. Hundreds of physical books → **days** of OCR.
- **Don't run OCR concurrently with the LLM batches** (the 24 GB rule) — it
  competes with the resolve/content passes.
- Quality scoring (§4.8d) gates which pages get re-OCR'd, keeping cloud/recompute
  cost on the bad minority — only works if the score catches diacritic errors.

---

## Practical shortlist

1. **Bake off the engine on ~4 representative pages first** — pick by
   macron/underdot survival. This §9 decision governs the whole archive.
2. **Scan ≥400 DPI, grayscale, retain raw images, output PDF/A-2b.** Keeping
   images means you can re-OCR later when a better engine wins.
3. **No native-script handling needed** — corpus is romanized IAST/Wylie only
   (so no BDRC/Monlam/Namsel, no `san`/`bod`). The task is high-fidelity *Latin*
   OCR; see §8 for the engine choice + pipeline.
4. **NFC-normalize, then validate on diacritic survival** (not dictionary
   hit-rate); spot-check verse line breaks and footnotes, since those feed the
   catalogue pipeline.
5. **Don't co-run with the LLM batch**, and add a per-page timeout (open bug L1)
   before a big run.

**Bottom line:** the failure mode isn't "OCR doesn't work" — it's a
*plausible-looking* text layer with **silent, systematic diacritic and
footnote/verse-structure errors** that become permanent and quietly degrade exact
citation/terminology extraction. Cheap insurance: pick the engine deliberately,
scan high enough to preserve the marks, and keep the raw images so the decision
stays reversible.

---

## 7. Hardware impact (current M4 Pro / 24 GB vs new M5 Max / 128 GB)

Specs (verified against Apple tech-spec pages 121553 / 126318): current
**M4 Pro** — 12-core CPU (8 P + 4 E), 16-core GPU, **273 GB/s**, 16-core ANE,
24 GB. New **M5 Max (40-core GPU)** — 18-core CPU (6 super + 12 performance, no
efficiency tier), **40-core GPU**, **614 GB/s**, 16-core ANE, 128 GB. (Bandwidth
273 → 614 = **2.25×**; GPU cores 16 → 40 = 2.5×; plus M5's per-GPU-core neural
accelerators, ~4× peak AI compute per generation.)

OCR splits into two parts that respond to hardware very differently.

**Accuracy — the real problem — is almost hardware-independent.**
Whether `ā`/`ṭ`/`ś` survive is set by the **engine + training data + scan DPI**,
not the machine. A 128 GB Max will *not* make Tesseract read diacritics any
better. So the upgrade does little for the failure mode that actually matters.
The fixes stay: the bake-off, the right engine, ≥400 DPI grayscale, retain raw
images.

**Speed — modestly hardware-dependent, and it depends which engine:**
- **Tesseract / OCRmyPDF / Ghostscript: CPU-bound, GPU-irrelevant.** They don't
  touch the GPU and use little RAM (24 GB is plenty). Throughput scales with
  **CPU core count + per-core speed** and parallel page workers (OCR is
  embarrassingly parallel — pages are independent). Current 8 P + 4 E ≈ ~9.8
  P-equivalents; new is **18 performance-class cores (6 super + 12 perf, no
  efficiency tier)** plus an M4→M5 per-core uplift → **~2–2.3×** Tesseract
  throughput, mostly from running ~18 page-workers vs ~8–10. A solid, but not
  dramatic, win.
- **Apple Vision: ANE/GPU, on-device, already fast.** **Both machines have the
  same 16-core ANE**, so Apple-Vision OCR throughput is **~flat** across the
  upgrade (small generational bump only).
- **Cloud Vision: hardware-irrelevant.** Runs on Google's servers; your machine
  only uploads. An upgrade changes nothing here (bounded by network).

**What the current 24 GB box actually constrains:**
- Fine for OCR *memory*; the limit is CPU parallelism and the **"don't run OCR +
  LLM concurrently"** rule (they'd fight for memory and the resident model). So
  today OCR and the resolve/content passes must be **serialized**.
- No headroom to run a **local vision-LLM OCR** model alongside other work.

**The one place new hardware improves OCR *quality*: local vision-LLM OCR.**
The M5 Max's **128 GB + 40-core GPU (614 GB/s) with per-core neural accelerators**
(the M5's headline AI feature, ~4× peak AI compute vs M4) can run a strong
**vision-language model** (7B+ to a quantized 30–72B on 128 GB) to assist OCR.
A good VLM handles **verse layout, footnotes, and reading order far better than
Tesseract**, and can recover faint diacritics from context — but for *raw
transcription* of romanized text its hallucination/autocorrect risk is the catch
(§8: use it as a layout/anchor pass, not the sole transcriber). This path is
genuinely GPU+RAM gated (exactly what M5's GPU AI compute is for), so the upgrade
unlocks it. Note it
neither uses the ANE (so the identical 16-core ANE is irrelevant here) nor the
CPU cores. Trade-off: VLM OCR is slow per page and must be validated (it can
hallucinate), but on the M5 Max it's feasible at corpus scale. This extends the
plan's vision-LLM rung (§4.7) from TOC-only to full-page OCR.

**Plus a workflow win from 128 GB:** enough memory to run **OCR and the LLM
pipeline at the same time** (lifts the concurrency rule) and to fan OCR out across
many more parallel page-workers.

**Not a RAM/GPU issue — a disk issue:** retaining raw 400 DPI grayscale images
for hundreds of books is **hundreds of GB of storage**, independent of the
128 GB unified memory. Plan disk accordingly.

**Summary:** the M5 Max buys **~2–2.3× faster Tesseract** (12 → 18
performance-class cores), **~flat Apple-Vision** (same 16-core ANE), **nothing
for Cloud Vision** (server-side), removes the OCR-vs-LLM serialization (128 GB),
and — most importantly — makes the **local vision-LLM layer** (§8) viable via the
M5 GPU's neural accelerators + 128 GB. But conventional-OCR *accuracy* (the
silent-diacritic problem) is fixed by engine/DPI/retained-images, not by the
machine.

---

## 8. Engine comparison + the high-fidelity pipeline (IAST-only corpus)

Given the romanized-only scope (§1), the job is high-fidelity **Latin** OCR. Three
candidate engines, scored on what matters for an archive of transliterated text:

| Dimension | Tesseract+Shreeshrii | Apple Vision | Local VLM |
|---|---|---|---|
| Precision (never invents text) | high | **highest** (transcribes only) | **lowest** (can hallucinate) |
| Diacritic *recall* on faint marks | lowest | medium | **highest** (uses context) |
| IAST underdot fidelity | **trained for it** | **uncertain — test both modes** | varies by model |
| Wylie/transliteration (no "autocorrect") | high | high | **risk** — prior may "fix" it |
| Layout: verse, footnotes, reading order | low | low–med (boxes only) | **highest** (structured) |
| Determinism / auditability | **high** | **high** | low (sampling/drift) |
| Speed | medium | **fast (ANE)** | slow (GPU) |

**Why a VLM can beat Tesseract:** it reads the whole page with language priors and
is *instructable*, so it recovers faint diacritics from context and emits clean
structured markdown (verse/footnotes/reading order) in one pass. **Its fatal flaw
for this corpus:** the same prior can *hallucinate* fluent, invisible errors and
"correct" Wylie/IAST — and Wylie looks like a typo to any language prior, so the
prior is a *liability* here, not an asset. (The mechanism is out-of-distribution
token uncertainty — rare strings have weak predictions, so the model drifts to a
"normal" continuation — **not** attention-span exhaustion.)

**Apple Vision** is a specialized neural OCR that **cannot freely hallucinate**
(it transcribes detected glyphs), is fast and deterministic, and — with no
native-script requirement — its only real risk is **IAST underdot coverage**. Do
**not** pre-concede this: Vision applies a language model that can suppress OOD
glyphs, but that is switchable. **Test Vision in two modes:**
`.accurate` + `usesLanguageCorrection = true` vs `= false`. If underdots survive
with correction *off*, Vision is viable; if they vanish even with it off, that's a
hard character-set limit (then Tesseract+Shreeshrii wins the glyph battle).

### Recommended pipeline — "VLM as anchor", not as transcriber
Do **not** transcribe with the VLM and diff against the deterministic engine — a
raw text diff breaks on column/interlinear reading-order differences and flags the
whole page. Instead:

1. **Deterministic transcription** (Apple Vision *or* Tesseract+Shreeshrii, per
   bake-off) → raw text + **bounding boxes**, preserving diacritics. This is the
   spelling source of truth.
2. **Layout** — first try **geometry** (column/reading-order from the boxes); many
   single-column + indented-verse pages need no VLM at all.
3. **VLM only for ambiguous structure** (interlinear, footnote association,
   complex multi-column), run as an **anchored** pass: feed it the page image
   **plus the deterministic OCR text**, and constrain it to *arrange* that text
   into markdown — "preserve every character/diacritic exactly, do not correct
   spellings or Wylie." Strongest variant: have it emit the **ordering of OCR text
   blocks (by id/box)**, not re-emit characters, so rare strings are never
   regenerated.
4. **Guardrails** (this corpus): the VLM may **flag** a suspected missed diacritic
   (→ review queue), **never silently substitute** — a "you may correct it" clause
   re-opens the hallucination hole. Low temperature; always retain raw images.

This keeps the VLM's layout/recall strengths while containing the one fatal flaw
(fluent hallucination on romanized text). It "reduces" hallucination — it does not
*eliminate* it (a constrained VLM can still drop/duplicate/mis-place blocks).

### VLMs to put in the bake-off (all run locally on the 128 GB M5 Max via MLX / llama.cpp)
- **olmOCR** (Allen AI, on Qwen2-VL-7B) — built for *faithful* PDF→text with
  anti-hallucination measures; best fit for the anchor/layout role.
- **Qwen3-VL** (or 2.5-VL) — strongest general doc VLM, ~32-language OCR, robust on
  degraded scans, great structured output; a quantized 30–72B fits 128 GB.
- **dots.ocr**, **DeepSeek-OCR** — OCR-specialists worth including.
- *(No Tibetan-script models — out of scope.)*

### Bake-off metrics (same ~4 pages: underdot-dense, Wylie-heavy, numbered-verse, footnote-heavy)
1. **Diacritic CER**, split into **recall** (dropped marks) vs **precision**
   (spuriously *added* marks) — VLMs fail on precision, Vision/Tesseract on recall.
2. **Wylie corruption rate** — how often a model "fixes" a Wylie string.
3. **Layout time-to-correction** — editor clicks to fix the page structure.
Run Apple Vision (both correction modes), Tesseract+Shreeshrii, and the VLMs in the
*same* harness for an apples-to-apples call.

### Open input needed
How much of the library is **complex layout** (multi-column, interlinear,
commentary-with-embedded-verse) vs plain single-column prose+verse? That sets how
much the VLM layer is worth. Domain guess: the bulk (published translations) is
single-column prose + indented verse (geometry suffices); the minority
(dictionaries, encyclopedias, critical editions) drives the VLM need. This can be
estimated from the catalogue's own `verse_score` / structure data rather than
guessed.

---

## 9. Bake-off results — Tesseract + Shreeshrii (`eng+IAST`) vs Apple Vision (run 2026-05-30)

First head-to-head of the two **local** engines (resolves the Tesseract-vs-Vision
half of the plan's §9 open decision). **[Updated same day: Cloud Vision now
benchmarked too — see "Three-way update" at the end of this section; §9 is resolved
for the three OCR engines. Only a local VLM remains untested.]**

**Method.** Construct pages (IAST-dense, Wylie, verse, footnotes, multi-column,
mixed-script) pulled from the live corpus, rendered at **400 dpi**, OCR'd by
(a) **Tesseract 5.5.2 `-l eng+IAST`** (Shreeshrii) and (b) **Apple Vision**
(`VNRecognizeTextRequest`, `.accurate`) with `usesLanguageCorrection` both **off
and on**. Scored against ground truth wherever a clean Unicode text layer existed:
- **IAST ground truth:** *Oxford Handbook of Tantric Studies* (born-digital,
  pristine Unicode) — dense Sanskrit mantra/verse pages (290–410 diacritics each).
- **Wylie ground truth:** Hopkins, *Maps of the Profound* — its **Wylie is clean
  ASCII** (its Sanskrit extracts as a *separate* mojibake, so only Wylie is scored).
Metrics (pure-Python scorer): overall CER, base-letter CER (diacritics stripped),
diacritic **recall** & **precision** (base-letter-aligned), Wylie token exact-recall.

**Headline numbers** — IAST pages (mean of 3):

| engine | overall CER | base-letter CER | diacritic recall | diacritic precision |
|---|---|---|---|---|
| Tesseract `eng+IAST` | ~12% | ~5% | **~27%** | **~95%** |
| Apple Vision (corr. off) | ~10% | ~4% | ~22% | ~50% |
| Apple Vision (corr. on)  | ~10% | ~4% | ~21% | ~52% |

Wylie token exact-recall: **Tesseract 94–96%** vs Vision 88–89% (Hopkins clean GT);
85% vs 80% on a Wylie scan (Pabongka).

**The honest finding: neither local engine reliably preserves dense IAST
diacritics.** On the hardest pages (Sanskrit mantras packed with anusvāra/underdots)
**both** drop or mangle ~70–80% of the marks (recall 20–32%). The §9 hope for a
clean local winner on diacritic *recall* is **not met by either** — consistent with
§1 ("even with Shreeshrii it is imperfect on the tiniest marks"). High-recall
archival diacritics will need **Cloud Vision** (expected best, §8) or a VLM, and/or
the keep-the-raw-images insurance (§5).

**But the two fail very differently, and the difference is decisive for an archive:**
- **Tesseract errs by *omission*.** Diacritic **precision ~95%**: a mark it outputs
  is almost always right; when unsure it drops it or emits a placeholder. E.g.
  `oṃ → oX`, `hṛt → hXt` (anusvāra/underdot → `X`), `māyām → mayam` (macron lost) —
  yet `yonideśe tathā` ✓.
- **Apple Vision errs by *substitution*.** Its European language model forces wrong
  Latin diacritics on; precision only ~50%: `Nāgārjuna → Nägărjuna`,
  `Mūla-…-kārikā → Müla-…-kärikā`, `Tō. → Tõ.`, `ā → ä/à`, invents `é ş ț ã`; and it
  corrupts Wylie (`tsogs → isogs`, `Bodhisattva… → Bodhisastva…`).

For a permanent archive this is the crux: **a missing diacritic folds away harmlessly
in the diacritic-folded FTS (§4.5) and is recoverable by re-OCR; a *confident wrong*
diacritic is a silent corruption** that doesn't fold, survives validation, and looks
authoritative (§1, §5). So Tesseract's error profile is the safer one.
`usesLanguageCorrection` **on** makes Vision slightly worse — if Vision is ever used,
keep it **off** (confirms §8). Base-letter recognition is ~tied (base CER ~4–5%),
Apple Vision marginally ahead — its real strengths (speed, on-device, boxes) are
orthogonal to the diacritic problem that gates this corpus.

**Decision (Step 6 backend default):** keep **Tesseract + Shreeshrii (`eng+IAST`)**
as the local default — safer error profile (omission, not corruption), better Wylie
integrity, trustworthy when it emits a mark. **Do not close §9:** add **Cloud Vision**
(and optionally a VLM) to this same harness before committing the archive, because
*neither* local engine clears the diacritic-recall bar on dense Sanskrit. Retain raw
images regardless (§5) so the choice stays reversible.

**Two corpus realities surfaced by the run (update assumptions):**
1. **Born-digital text layers are not free OCR ground truth here.** *Tilopa*
   (`ā→Ɨ, ō→ǀ`, digits scrambled) and Hopkins' Sanskrit (`ā→å, ś→Ÿ`) extract as
   **mojibake** from custom fonts — `get_text()` is unusable, and Tesseract's OCR of
   the rasterized page is *cleaner Unicode than the PDF's own text layer*.
   Implication: for diacritic-faithful search/citation, **re-OCR may be warranted
   even for some born-digital PDFs**, not only scans. (The *Oxford Handbook* is the
   exception — genuinely clean Unicode — which is why it served as the IAST GT.)
2. **Native scripts exist in the corpus** (Tibetan dbu-can in *Great Exposition* /
   LTK back-matter; a CJK apparatus in the Daśabhūmika) — contradicting the
   "romanized-only" scope assumption (§1). Confirm whether the native-script minority
   needs OCR at all.

### Three-way update — Cloud Vision added (2026-05-30): §9 resolved

Ran **Google Cloud Vision** (`DOCUMENT_TEXT_DETECTION`, REST) through the same harness
on the Oxford IAST GT pages and Hopkins Wylie GT pages.

| IAST page (vs Oxford GT) | engine | overall CER | diacritic recall | diacritic precision |
|---|---|---|---|---|
| ox552 | Tesseract `eng+IAST` | 13.6% | 25% | 94% |
| ox552 | Apple Vision (off) | 13.1% | 18% | 47% |
| ox552 | **Cloud Vision** | **8.1%** | **55%** | 76% |
| ox969 | Tesseract | 11.1% | 23% | 96% |
| ox969 | Apple Vision | 8.8% | 24% | 47% |
| ox969 | **Cloud Vision** | **3.2%** | **78%** | 87% |
| ox096 | Tesseract | 11.4% | 32% | 97% |
| ox096 | Apple Vision | 8.9% | 25% | 56% |
| ox096 | **Cloud Vision** | **5.1%** | **66%** | 82% |

Wylie (Hopkins GT) token exact-recall: **Cloud Vision 94% ≈ Tesseract 94–96% > Apple
Vision 88–89%**; Cloud Vision over-diacriticizes Wylie slightly (more spurious
non-ASCII) but token integrity holds.

**Cloud Vision is the diacritic-accuracy winner — now measured, not assumed** (was
the §8/§11 expectation). Diacritic **recall 55–78%** vs Tesseract 23–32% / Apple
Vision 18–25% — **2–3× the locals**; overall CER roughly halved (3–8% vs 9–14%). It is
**not** flawless: recall isn't >90%, and **precision 76–87%** — it substitutes on
anusvāra/retroflex (`oṃ→0ž`, `ṛ→á`, `ṣ→š`, `ṅ→ē`), better than Apple Vision (~50%) but
below Tesseract (~95%). Its wrong marks are often **non-IAST glyphs** (`ž ē á "`), so a
**valid-IAST-only filter** can flag them for review/fallback — a cheap precision boost.

**§9 decision (three engines settled; a local VLM remains the only untested rung):**
- **Tesseract + Shreeshrii (`eng+IAST`) = local default** — free, private, best Wylie,
  highest diacritic *precision*; errors are recoverable omissions.
- **Cloud Vision = high-accuracy escalation** — 2–3× diacritic recall; route
  diacritic-relevant pages to it. The $300 credit covers the whole corpus (~$75–150),
  so **privacy (page images upload to Google), not cost, is the reason to route
  selectively.** Apply a valid-IAST filter to catch its anusvāra/underdot substitutions.
- **Apple Vision = dropped** — loses on every diacritic metric (substitutes European
  marks); only edge is base-letter CER + speed, irrelevant to the gating problem.
- Retain raw page images regardless (§5) — even Cloud Vision's 55–78% recall means
  re-OCR-from-image must stay possible.

### Per-page routing detector (`ocr_router.py`) — validated 2026-05-30

Validated on a 32-page stratified Oxford sample (clean GT). **Key finding:** Tesseract
diacritic recall is ~20% **corpus-wide**, *not* concentrated in a hard minority — so
route by **diacritic-relevance**, not "hard-page" detection. Signals (best first):
`tdia` (diacritics emitted by the *local* Tesseract pass — tracks true density),
romanized-Sanskrit **vocabulary** (relevance even when marks were dropped), the
`X`-garbage signature; **low Tesseract confidence flags priority** (dense pages run
~80–90 vs ~95 for plain English). **Negative result:** Tesseract↔Apple-Vision diacritic
*disagreement* is **saturated (~0.8 everywhere)** — useless as a discriminator (the two
locals almost never agree on a mark). **Operational caveat:** feed the router a **fresh
Tesseract+IAST pass, never a scan's pre-existing text layer** — the old OCR already
dropped the diacritics, so the router would under-flag scans (observed: 86% flagged on
born-digital Oxford, but <10% when fed the scans' stale layers). It correctly keeps
pure-Wylie/English pages local and routes IAST/Sanskrit pages out.

*Harness lives in `bakeoff/`:* `bakeoff.py`, `run_scored.py`, `run_threeway.py`,
`score.py`, `ocr_router.py`, `vision_ocr.swift`, `vision_gcv.py` (+ `gcv_key.txt`, gitignored).

### Diacritic restoration & trilingual glossaries — research findings, PARKED (not built)

Explored 2026-05-30 whether the corpus's *sparse, recurring* diacritic vocabulary lets
us **restore** diacritics by lexicon lookup instead of re-OCR. **Findings, then a
scope decision.**

**Prevalence:** of 413 books with text — 11% NEG (no Sanskrit), 60% STRIP (Sanskrit
present, diacritics dropped by OCR), 28% CLEAN (clean-Unicode IAST, lexicon-able). But
density is **uniformly low** (top book ~6.8 Sanskrit-tokens/1000 chars) — broad-but-thin.

**Lexicon restoration (`bakeoff/restore_diacritics.py`, `restore_entity.py`):** build
`{stripped → IAST}` and restore on diacritic-blind text. Validated on held-out Oxford:
naive = 45% precision (English homographs `are→āre`); + English-blocklist + cross-book
support = **~80% precision, ~15% recall — a ceiling that holds regardless of source**
(driven by genuine ambiguity `pada/padā` + source ligature mojibake). A *single
authority* helped only if scheme-matched: the multilingual Princeton Dictionary made it
**worse** (69%) by mixing Skt/Tib/Pāli/Japanese readings. **Conclusion:** restoration is
a **review-suggestion**, not archive-grade auto-apply; Cloud Vision stays the recall tool.

**Trilingual glossaries (`bakeoff/find_glossaries.py`, `glossary_parse.py`):** 75 books
have Skt/Tib/Eng glossaries; ~12 are clean-Unicode aligned 3-column (Knowing Illusion,
Oxford, LTK Jinpa, Treasury Bk6, Tibetan Logic). A coordinate-based parser extracts
`{English, Wylie, IAST}` triples cleanly (Knowing Illusion → 285 aligned rows). Their
**real value is the alignment**, not restoration precision — they are a ready data
source for entity-alias seeding and Wylie↔Sanskrit↔English links. The ~60 *scan*
glossaries have intact Tibetan/English but dropped Sanskrit — their glossary
page-ranges are the highest-value targeted Cloud-Vision job.

**Scope decision (checked against plan §13/§12/§8) — split by use:**

*Feeds Step 6 / §4.8 digitization (the manual-scan-and-OCR pipeline) — legitimate
refinements, integration candidates:* the per-page **router** (`ocr_router.py`) is the
§4.8d gate selecting which scanned pages get the Cloud-Vision pass; a **valid-IAST
post-filter** on Cloud-Vision output is a §4.8c step; **scan-glossary targeted
Cloud-Vision** is selective re-OCR of the highest-value pages; **diacritic restoration**
is a §4.8c **post-OCR review-suggestion** (secondary — for fresh scans the router→Cloud
Vision path gives 55–78% recall vs restoration's ~15%).

*Genuinely deferred — do NOT build now (§13 "do not build query-expansion / live
resolver beyond a stub"):* only the **glossary → `work_alias`/`person_alias` →
query-expansion** chain (Step-4 resolver + the Step-5/8 search feature §4.5 defers).
Prototypes retained in `bakeoff/`. Revisit glossary→alias mining when **Step 4** is
active and query-expansion when it is de-deferred.

## 10. Tibetan escalation phase (planned, 2026-06-20)

*Context: the production pipeline is now Surya (`ocr_pipeline/ocr_run.py`, driven by
`ocr_pipeline/reocr_run.py`). Surya nails English/IAST but **cannot read
Tibetan** — on a Tibetan page it hallucinates Indic script and loops to the token
ceiling, then `--drop-nonlatin` blanks the bogus text. Net effect on a mixed book:
the Tibetan pages come out **empty AND cost the most time** (full-page loop → repeat-
detect → block-OCR fallback → still garbage → dropped). Observed on **e75 "Luminous
Lives"**: 52 of 322 pages (16%) came out near-blank — all Tibetan — and that same
subset drove the ~2.6× fallback and the long runtime.*

**The idea:** don't make Surya the only reader. After the Surya pass, **set aside the
pages Surya couldn't read, send them to a Tibetan-capable model, and only if *that*
also fails to recognize the page, fall back to a re-OCR.** This recovers the Tibetan
text *and* is faster (the looping work moves off Surya). Crucially it **keeps Surya as
the IAST anchor** — we do not replace it (a general VLM swap risks the diacritic
fidelity Surya was chosen for; cf. §8/§9, Apple Vision botched 32% of diacritics).

### Flow (escalation, cheapest stage first)

1. **Surya pass (capped).** Normal run with `--max-decode N` (bounds the loop waste on
   unreadable pages). Produces text + the per-page record in `full/pages.jsonl`.
2. **Set aside the "Surya couldn't read it" pages.** Reliable trigger = pages whose
   Surya output is empty/near-blank (blanked by `--drop-nonlatin`, or text length below
   a small threshold), optionally unioned with the cap-hit list. **NB:** the raw
   "hit the token cap" set is *not* per-page identifiable from the llama logs
   (request↔page is not 1:1 — loop fallback + retries; see `reocr_run.py`'s
   `flag_capped_pages`, which reports counts, not page names). The **blank-output
   signal in `pages.jsonl` is the dependable one** and captures exactly the dropped-
   Tibetan pages. (Optionally pre-confirm with the OSD/`ocr_router.py` script
   detector, already validated, to label them Tibetan before routing.)
3. **Tibetan model.** Run the set-aside pages through a Tibetan-capable engine. A
   recognition/confidence gate decides:
   - **Recognized (confident Tibetan)** → accept; merge its text back into
     `pages.jsonl` / `full/` + `body/` / the PDF text layer for those pages.
   - **Not recognized** (low confidence / not actually Tibetan — e.g. a genuinely
     dense Latin/IAST page that merely hit the cap) → it wasn't a Tibetan problem;
     **fall back to re-OCR** via the existing mechanism (Surya block-OCR, or a Surya
     re-run at a higher `--max-decode`, or Cloud Vision per §9).
4. **Merge + rebuild.** Splice recovered pages into the text/JSONL and rebuild the PDF
   text layer (OCR is cached, so the rebuild is cheap).

### Engine choice (bake-off first, per §8 discipline)

Tibetan is low-resource, so **general VLMs are unreliable on it** — do not assume a
single multilingual model does English+IAST+Tibetan well. Strongest fits, most on-
target first: **Monlam AI / BDRC Tibetan OCR** (purpose-built for Tibetan Buddhist
texts), then **Google Cloud Vision** (`bo` model, cloud fallback; already wired in
§9 via `vision_gcv.py`). Pick by a small bake-off on **e75's 52 Tibetan pages** as the
test set (extract them by blank-output detection; confirm script with OSD).

### Architecture / effort

Low — both seams already exist: the pipeline is **pluggable** (`@register("name")`
backends in `ocr_pipeline/lib/`) and the **per-page script router** is built
(`ocr_router.py`, §9). The new work is: (a) a Tibetan backend wrapper, (b) the
set-aside/merge orchestration in the `reocr_run.py` driver (it already produces
`capped_pages.json` and reads `pages.jsonl`), (c) a recognition gate + the fall-back
branch. **Status: planned, not built.** First step = the e75 bake-off.
