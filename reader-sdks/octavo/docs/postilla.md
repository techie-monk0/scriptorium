# Reader SDK — annotations / handwriting / recognition extension (plan)

Package **`postilla`** (Italian/Latin: a marginal note added to a text) — an **extension** built on
the base Reader SDK **`octavo`** (`octavo.md`). The base SDK reads, paginates, searches, and exposes a **Locator** + a
**DecorationHost** seam; this extension turns that seam into the full annotation stack: highlights,
underline/strikeout, notes, **freehand handwriting**, a **structured sync-of-record** (not baked
into files), **portable export**, **recognition** (ink→text, ink→shape, search-in-ink), and the
**integration hooks** that let an LLM/research layer "open the text at a location."

This is a near-direct productization of what the web reader already does (`handwriting_TODO.md`,
landed `2acb341`) — generalized off the catalogue and split into a clean, pluggable layer.

---

## 1. Why a separate extension (not part of the base)

- Many integrators want **read-only** (a docs viewer) — they shouldn't pay for ink/recognition.
- Annotation **storage/sync** and **recognition** are exactly the parts integrators most want to
  swap (their backend, their recognizer, their export targets) → they belong behind ports, opt-in.
- It keeps the base SDK's stability surface small. The extension can iterate faster.

The extension plugs into the base via `DecorationHost` (render marks at Locators) + the input/overlay
seam (capture pen/touch). Nothing here reaches around the base contract.

---

## 2. Layers

```
postilla-core     neutral models + logic (no platform deps)
  ├─ Annotation model    {id(UUID), kind, locator, color, note?, ink?, rev, deleted_at, …}
  ├─ Ink model           strokes:[{points:[[x,y,pressure],…], width, color, mode}]  (page/CFI-relative)
  ├─ AnnotationStore PORT pull(since)->{rev,ops} · push(ops)->{rev}   (LWW + tombstones + UUID)
  ├─ Sync engine         offline op-queue, last-write-wins merge, holding/publication-scoped
  └─ Recognizer PORT      recognize(ink|region) -> {text|shape|...}   (integrator/platform supplies)

postilla-web      capture+render: pdf.js text-layer marks, epub.js CFI marks,
                        perfect-freehand ink, palm rejection (extract overlay.js)
postilla-swift    PDFKit annotations + PencilKit overlay; perfect-freehand-Swift renderer
postilla-kotlin   pdfium/WebView overlay; perfect-freehand-Kotlin renderer

postilla-export   portability: PDF flatten (PyMuPDF/native), W3C Web Annotation JSON (EPUB)
postilla-recognize-*       optional recognizer adapters (Apple Vision/PencilKit, ML Kit Digital Ink, cloud)
```

---

## 3. Annotation model & sync-of-record (generalize `/sync/reader`)

Lift the existing schema verbatim — it's already neutral and tested:

```
{ id, publicationId, kind, locator, color, note_text?, ink?,
  created_at, updated_at, deleted_at, rev }
kind ∈ { highlight, underline, strikeout, note, ink }
```

- **Anchoring:** highlight/underline/strikeout → `locator` (PDF: page + rect[] in 0..1; EPUB:
  `cfi_range`). note → locator + `note_text`. ink → `locator.page` (PDF) or spine-index (EPUB) +
  `ink` JSON, points 0..1.
- **Sync engine (PORT `AnnotationStore`):** `pull(since=rev) -> {rev, ops}` (incl. tombstones),
  `push(ops) -> {rev, applied}`. Idempotent **LWW** upsert keyed by client **UUID** (no recycled
  ints → offline devices can't collide). Offline op-queue flushes on reconnect (this is exactly PWA
  TODO #1, generalized). Scope by publication/holding so opening a book doesn't pull the world.
- The **store is the source of truth**, not the file bytes — so marks made in any binding appear in
  all of them, and an LLM layer can read structured marks (e.g. "summarize my highlights"). The
  reference server is the catalogue's `ReaderStateStore` (SQLite ABC); any integrator drops in their
  own.

---

## 4. Handwriting — the canonical-ink rule

Ink fidelity across web ⇄ native ⇄ exported-PDF is the feature that justifies owning the surface, so
it gets one hard rule:

> **`perfect-freehand` is the canonical renderer on every platform. Store raw input points; render
> via perfect-freehand; never persist a platform's native stroke object.**

- **Capture:**
  - Web: pointer events, pen-vs-touch discrimination, ±700ms palm-rejection grace (extract from
    `reader-core.js`).
  - iOS: PencilKit (or raw `UITouch`) for Apple-Pencil pressure/tilt + palm rejection — used as an
    **input** layer only; convert `PKStrokePoint(location, force)` → `[x,y,pressure]`.
  - Android: stylus `MotionEvent` (pressure) similarly.
- **Store:** the raw `[x,y,pressure]` points (existing schema, no change).
- **Render:** a **perfect-freehand port** per platform (MIT; JS exists, Swift/Kotlin ports exist or
  are small) so a stroke looks pixel-identical on web, on device, and in the flattened PDF (the
  server export already uses the PyPI `perfect-freehand`). Do **not** render with PencilKit's native
  ink if cross-platform parity matters — it will diverge from web + export.
- **EPUB ink is best-effort** and reflow-fragile by nature; the SDK adopts the catalogue's planned
  fix (TODO #2): anchor each stroke to **block-CFI + block-relative coords**, re-place on
  resize/font-change. PDF ink (fixed pages) is solid.

---

## 5. Recognition (the new surface area)

A **`Recognizer` PORT** — `recognize(input) -> result` — so the SDK ships the seam and reference
adapters, never a hard dependency. Inputs: an ink stroke set, or a page region (for OCR).

| Capability | Reference adapter(s) | Honest state |
|---|---|---|
| **Handwriting → text (HWR)** | Apple **PencilKit/Vision** (on-device, iOS); **Google ML Kit Digital Ink** (on-device, free, many languages, cross-platform-ish); cloud (MyScript) as a commercial plug | Good for Latin scripts. **Devanagari/Sanskrit (your library!) is weak** in open engines — ML Kit has some Indic coverage but accuracy is low; treat as experimental, not load-bearing. |
| **Ink → shape** (clean arrows/boxes) | geometry heuristics (`$1`/`$P` recognizers) or ML Kit shape | Tractable; matches TODO #6 backlog (`kind:'shape'`). |
| **Printed text / scanned-PDF OCR → text layer** | Tesseract / Apple Vision / the catalogue's existing `scan_ocr` | Mature for print; gives a selectable text layer over image-only PDFs → enables search + LLM on scans. |
| **Search within handwriting** | index HWR output into the same FTS the base SDK search uses | Only as good as HWR; English notes searchable, Sanskrit not yet. |

Recognition output attaches to the annotation (`recognized_text` field) or produces a derived text
layer; it's **always advisory** (the raw ink stays the source of truth, so a bad recognition never
destroys the stroke).

---

## 6. Integration hooks — the reason for all of this

The features you actually want (content-search jump, "LLM opens the text at the passage", note
backlinks, translation compare) are **not** annotation types — they're three generic seams the
extension exposes on top of the base Locator model:

1. **Selection → text + Locator:** `reader.selection() -> { text, locator }`. Lets any layer (an LLM
   "ask about this paragraph", a dictionary, a citation grabber) act on what the user selected.
2. **Navigate-to-Locator + ephemeral decoration:** `reader.goTo(locator)` + a transient highlight —
   so "the model cites page 42 ¶3" *scrolls there and pins it*. This is the exact thing third-party
   handoff can never do, exposed as one call.
3. **Annotation stream:** the structured store (no PDF parsing) so an LLM can read "my highlights/
   notes" as data and write back (e.g. auto-summary as a `note`).

These three are documented as the **public extension points an AI/research integrator builds on** —
the SDK provides the seam, not the LLM. (Your catalogue is the first to wire an LLM into them.)

---

## 7. Export / portability

- **PDF (works today):** flattened **copy** (`annotated.pdf`, original untouched) + **write-in-place**
  (localhost-only), via PyMuPDF: highlight/underline/strikeout → standard annots, note → `/Text`,
  **ink → faithful filled vector path from the same perfect-freehand outline** (pressure preserved).
  Generalize `annotate_export.py` into `postilla-export` (server-side or native PDFKit
  equivalent). Note: this is a backend dep (PyMuPDF) — keep it an optional sub-package.
- **EPUB (TODO):** export marks as **W3C / Readium Web Annotation JSON** (CFI selectors) — importable
  by Thorium. Text marks port cleanly; ink rides as a `octavo:ink` extension (best-effort). No
  embedded-annotation standard exists for EPUB, so EPUB marks stay ecosystem-portable, not
  baked-into-the-file.

---

## 8. Tests

- **Unit (`postilla-core`):** sync LWW merge (divergent streams, tombstones, idempotent
  re-apply, `since` filtering); ink codec round-trip (raw points lossless, 0..1 coords); recognizer
  port contract (mock recognizer); annotation ⇄ Locator anchoring.
- **Cross-platform ink parity:** render the same stored stroke on web/Swift/Kotlin + flatten to PDF →
  compare outlines within tolerance (the "canonical-ink rule" guard).
- **System:** create→persist→reload per kind, per format, per binding (Playwright/XCUITest), incl. a
  **canvas-actually-painted pixel guard** (already exists on web) and an **offline-queue → reconnect
  flush** test.
- **Recognition:** golden ink samples → expected text for the *supported* scripts; an explicit
  **xfail corpus for Devanagari** so the weakness is tracked, not hidden.
- **Export:** flattened PDF re-opens with marks present (extend `test_annotate_export.py`); W3C JSON
  validates + round-trips into Thorium (manual/contract test).
- **Perf:** ink capture latency, render cost with many strokes/highlights on a page, sync push/pull
  payload size (scoped, not whole-table).

---

## 9. Milestones (track the base SDK)

1. **A1 — annotate-core + sync engine** extracted & decoupled (model, ports, LWW, op-queue).
2. **A2 — web binding** (`overlay.js` → `postilla-web`); catalogue web reader dogfoods it.
3. **A3 — export** sub-package (PDF flatten today; W3C-EPUB).
4. **A4 — iOS binding** (PDFKit + PencilKit input + perfect-freehand-Swift render) — parity tests.
5. **A5 — recognition** seam + first adapters (Apple Vision / ML Kit); ship as clearly-experimental.
6. **A6 — integration hooks** documented (selection / goTo+pin / annotation-stream) — the LLM-ready API.

---

## 10. Honest caveats

- **Cross-platform parity is the expensive promise.** "Render identically everywhere" means a
  perfect-freehand port + a parity test per platform; it's real, recurring work.
- **EPUB handwriting stays best-effort** (reflow) on every engine — not solvable, only mitigated.
- **Handwriting recognition for Sanskrit/Devanagari is not there** in open engines today; ship it as
  experimental and don't build features that assume it works.
- **Recognition adds a heavy/optional dependency surface** (Vision/ML Kit/cloud) — keep every
  recognizer behind the port so the core stays light and license-clean.
- Same repo-placement logic as the base SDK: **decouple in-repo now, publish at v1** — the extension
  publishes as `@octavo/postilla` alongside `@octavo/core`.
