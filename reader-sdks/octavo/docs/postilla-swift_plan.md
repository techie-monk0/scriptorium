# postilla-swift — iOS annotation/handwriting binding (implementation & test plan)

The Swift binding of **`postilla`** (`postilla.md`): the SwiftPM package the **`catalogue-app`**
(`../../../catalogue-app/docs/ios_native_plan.md`, impl step 7) hosts for in-app annotation, handwriting,
and export. It depends only on **`reader-contract`** (the shared `Locator` / `Decoration` /
**`DecorationHost`** seam) — **not** on `octavo-swift` — and plugs into that seam + the input/overlay
layer. `octavo-swift` is the reference reader that implements the same contract; postilla never
reaches into it, so the two are independent siblings.

It mirrors **`postilla-core`** in Swift (annotation model, ink model, the `AnnotationStore` +
`Recognizer` ports, the offline-first LWW sync engine) and adds the Apple capture/render stack:
**PencilKit input → raw `[x,y,pressure]` → a perfect-freehand-Swift renderer**. Cross-platform ink
parity (web ⇄ iOS ⇄ exported PDF) is the headline guarantee.

**Build/run caveat (every task):** authored in-repo, **built/run/tested in Xcode on a Mac**; the
agent environment cannot build it. `xcodebuild test` runs locally.

---

## 1. Package layout

SwiftPM package, product/import root **`Postilla`**; depends on `ReaderContract` (re-exported, so
`import Postilla` still surfaces `Locator`/`Decoration`). Sibling of `catalogue-app`,
not under it.

```
postilla-swift/                     # → SwiftPM package "Postilla", deps: ReaderContract (not Octavo)
  Package.swift
  Sources/
    Postilla/                       # mirror of postilla-core, in Swift (no UIKit here)
      Annotation.swift              # {id(UUID), publicationId, kind, locator, color, note?, ink?,
                                    #  created_at, updated_at, deleted_at, rev}; kind ∈ {highlight,
                                    #  underline, strikeout, note, ink}
      Ink.swift                     # strokes:[{points:[[x,y,pressure]], width, color, mode}] (0..1)
      AnnotationStore.swift         # PORT: pull(since:rev)->{rev,ops} · push(ops)->{rev}  (LWW + UUID + tombstones)
      SyncEngine.swift              # offline op-queue, LWW merge, publication-scoped; flush on reconnect
      Recognizer.swift              # PORT: recognize(ink|region)->{text|shape|…}  (advisory)
    PostillaRender/                     # capture + render (UIKit/PencilKit) — plugs DecorationHost
      InkCanvas.swift               # PencilKit/UITouch capture; pen-vs-touch + ±700ms palm rejection
      FreehandRenderer.swift        # perfect-freehand-Swift outline → filled path (the canonical render)
      MarkOverlay.swift             # highlight/underline/strikeout/note over Octavo.DecorationHost
      Decorations.swift             # Annotation -> Octavo decoration mapping (anchors at Locator)
    PostillaExport/                 # portability (optional sub-target)
      PdfFlatten.swift              # PDFKit annots + ink-as-vector-path; flattened COPY
      WebAnnotationJSON.swift       # W3C/Readium Web Annotation JSON (EPUB), ink as octavo:ink ext
    PostillaRecognizeVision/        # optional adapter — Apple Vision/PencilKit on-device HWR/shape
  Tests/
    PostillaTests/                  # unit (pure)
    PostillaParityTests/            # ink-render parity vs web + flattened PDF
    PostillaPerfTests/
  Examples/PostillaDemo/            # annotate-a-page embed on top of OctavoDemo
  Fixtures/                         # golden ink strokes + expected render/recognition + sync streams
```

`PostillaExport` and `PostillaRecognizeVision` are separate targets so the core stays light and
license-clean (PyMuPDF/Vision/ML Kit never forced on an integrator).

---

## 2. Scope — reuse vs build

| Piece | Source of truth | Swift action |
|---|---|---|
| Annotation + Ink model | `postilla.md` §3–4 (verbatim, already neutral) | **Mirror** as Codable; byte-parity with web JSON |
| `AnnotationStore` port + LWW sync engine | catalogue `/sync/reader` + `ReaderStateStore` | **Mirror** ports; reimplement offline op-queue + LWW |
| Ink capture | new — PencilKit/`UITouch` | **Build** `InkCanvas`: `PKStrokePoint(location,force)` → `[x,y,pressure]`, palm rejection |
| Ink render | **perfect-freehand** (canonical, MIT) | **Build/port** `FreehandRenderer` (Swift port) — *not* PencilKit native ink |
| Mark render | `octavo-swift` `DecorationHost` | **Build** `MarkOverlay` mapping Annotation→decoration at Locator |
| PDF export | catalogue `annotate_export.py` (PyMuPDF) | **Build** `PdfFlatten` (native PDFKit) **or** call server `annotated.pdf` |
| Recognition | new — Apple Vision/ML Kit | **Build** `Recognizer` port + one Vision adapter (clearly experimental) |

---

## 3. The canonical-ink rule (the load-bearing constraint)

> **`perfect-freehand` is the canonical renderer on every platform. Store raw input points; render
> via perfect-freehand; never persist a platform's native stroke object.**

- **Capture** PencilKit (Apple-Pencil pressure/tilt) or raw `UITouch` as an **input layer only**;
  convert to `[x,y,pressure]`, 0..1 page/CFI-relative; honor the ±700ms palm-rejection grace.
- **Store** the raw points (existing schema, no change) — `PKDrawing` is **never** persisted.
- **Render** via `FreehandRenderer` so a stroke is pixel-identical on web, on device, and in the
  flattened PDF (the server export already uses the PyPI `perfect-freehand`).
- **EPUB ink** stays best-effort (reflow): anchor each stroke to **block-CFI + block-relative coords**,
  re-place on resize/font-change. PDF ink (fixed pages) is solid.

---

## 4. Integration hooks (mirror `postilla.md` §6 — the LLM-ready seams)

1. **`reader.selection() -> (text, Locator)`** — any layer (LLM "ask about this paragraph",
   dictionary, citation grab) acts on the selection.
2. **`reader.goTo(locator)` + ephemeral decoration** — "the model cites p.42 ¶3" scrolls there and
   pins a transient highlight (the thing third-party handoff can never do).
3. **Annotation stream** — the structured store (no PDF parsing) so an LLM reads "my highlights/notes"
   as data and writes back (auto-summary as a `note`).

These are exposed as `Postilla` API on top of `Octavo`; the SDK provides the seam, not the LLM.

---

## 5. Test plan

### 5.1 Unit (`PostillaTests` — pure)
- **PS-U1 Sync LWW merge.** Divergent op streams (same UUID, different `updated_at`) → last-write-wins;
  tombstone (`deleted_at`) removes a mark; `pull(since:rev)` returns only newer rows incl. tombstones;
  idempotent re-apply is a no-op. (= `catalogue-app` **U9**, now owned here.)
- **PS-U2 Ink/coord codec.** PDF rect `[[x,y,w,h]]` and ink `{strokes:[{points:[[x,y,pressure]],
  width,color,mode}]}` encode/decode losslessly; coords stay 0..1; **byte-parity with web JSON**.
  (= `catalogue-app` **U10**.)
- **PS-U3 Annotation ⇄ Locator anchoring.** highlight/underline/strikeout → rect[]@page or
  `cfi_range`; note → locator+text; ink → page/spine-index; resolves back to the same Locator.
- **PS-U4 Recognizer port.** Mock recognizer satisfies the port contract; recognition output attaches
  as `recognized_text` and is **advisory** (a bad result never mutates the raw ink).
- **PS-U5 Offline op-queue.** Ops created offline queue, survive relaunch, flush in order on reconnect;
  publication-scoped pull doesn't fetch the world.

### 5.2 Ink parity (`PostillaParityTests`)
- **PS-Parity.** Render the **same stored stroke** via `FreehandRenderer` and flatten to PDF; compare
  outlines to the web `perfect-freehand` render within tolerance. The canonical-ink-rule guard —
  this is what makes native ink round-trip to web and into export.

### 5.3 System (`PostillaTests`/Example via XCUITest)
- **PS-S1 Round-trip (headline).** Open a PDF (via OctavoDemo) → add a highlight + a PencilKit ink
  stroke → `push` succeeds → `pull(since:)` from a second client (or relaunch) → marks reappear at the
  same page-relative coords. (= `catalogue-app` **S5**.)
- **PS-S2 Per-kind/per-format.** create→persist→reload for each kind, PDF and EPUB, incl. a
  **canvas-actually-painted pixel guard** and an **offline-queue → reconnect flush** test.
  (Feeds `catalogue-app` **S8**.)
- **PS-S3 Export.** Flattened PDF re-opens with marks present (ink as faithful filled vector path);
  W3C JSON validates + round-trips (contract test). (Extends server `test_annotate_export.py`.)

### 5.4 Recognition (`PostillaTests`)
- **PS-R1.** Golden ink samples → expected text for **supported** scripts (Latin); an explicit
  **xfail corpus for Devanagari/Sanskrit** so the known weakness is tracked, not hidden.

### 5.5 Performance (`PostillaPerfTests` — on device)
- **PS-PF1 Ink capture latency** (PencilKit input sampled), **PS-PF2 render cost** with many
  strokes/highlights on a page (pan/zoom holds 60fps), **PS-PF3 sync payload size** (scoped pull/push,
  not whole-table). (= `catalogue-app` **P7**.)

> **Test ownership note:** `catalogue-app` **U9, U10, S5, P7** (and the EPUB-mark half of **S8**) move
> *into this package*; `catalogue-app` keeps host-integration only. Update `ios_native_plan.md` §7 to
> reference these when this lands.

---

## 6. Milestones (track `postilla.md` A1/A4 + `catalogue-app` step 7)

1. **PS-M1 — core + sync engine.** `Postilla` target: model, ports, LWW, offline op-queue;
   PS-U1–U5 green. Pure, no UIKit.
2. **PS-M2 — capture + render.** `PostillaRender`: `InkCanvas` (PencilKit input + palm rejection) +
   `FreehandRenderer` (perfect-freehand-Swift) + `MarkOverlay` over `DecorationHost`; PS-S1/S2.
3. **PS-M3 — ink parity gate.** `PostillaParityTests` green vs web + flattened PDF (PS-Parity).
4. **PS-M4 — export.** `PostillaExport` (PDF flatten today; W3C-EPUB JSON); PS-S3.
5. **PS-M5 — recognition seam.** `Recognizer` port + Vision adapter, shipped **clearly
   experimental**; PS-U4/PS-R1.
6. **PS-M6 — integration hooks** (§4) documented + demoed — the LLM-ready API; `catalogue-app` wires
   its LLM/research layer onto them.

---

## 7. Open decisions

- **perfect-freehand-Swift:** adopt an existing Swift port vs. write a small one. Either way it is
  **the** renderer — parity tests gate it. (The JS + PyPI versions already exist; the Swift port is
  small but must match outlines.)
- **PDF export path:** native PDFKit flatten (no server dep, works offline) vs. call the server's
  `GET /holding/<id>/annotated.pdf` (reuses tested PyMuPDF, but needs network). Lean native for
  offline; keep the server route as the portable fallback.
- **First recognizer:** Apple Vision/PencilKit (on-device, iOS-native) vs. ML Kit Digital Ink
  (cross-platform-ish). Vision first for iOS; **Devanagari stays xfail** regardless.
- **EPUB ink:** ship best-effort block-CFI anchoring in v1, or defer EPUB ink and ship EPUB text
  marks only first. Lean text-marks-first; ink behind a flag.
