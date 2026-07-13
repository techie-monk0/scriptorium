# postilla-swift

The Swift binding of **postilla** — the annotation and handwriting layer that sits
on top of the base reader SDK, **octavo**. Octavo reads, paginates, and searches a
book and exposes two seams (a `Locator` for "where in the text" and a
`DecorationHost` for "draw something there"). Postilla turns those seams into the
full annotation stack: highlights, underline/strikeout, notes, freehand
handwriting, an offline-first sync-of-record, and a recognition (ink → text) port.

The package ships **two products**, and the split is the important thing to
understand before you use it.

## The two products

### `Postilla` — the model and logic core

Pure Swift with no platform dependencies (no UIKit, no PencilKit, no CoreGraphics
drawing). It is the "what an annotation *is* and how it syncs" layer:

- the annotation and ink **data models**,
- the **`AnnotationStore` / `Recognizer` ports** (protocols a host implements to
  supply its own backend or recognizer),
- the offline **`SyncEngine`** (last-write-wins merge, op-queue, tombstones),
- bookmarks.

Because it touches no platform APIs, it compiles and its logic runs anywhere —
including on macOS under plain `swift test`. Import this alone if all you need is
the model and sync (for example, a server-side or headless component).

### `PostillaRender` — the capture and rendering engine

This is **not a set of UI views.** There is no `View`, `UIView`, or
`UIViewController` in it. It is a CoreGraphics-based engine plus a few protocol
**seams** that a host app wires up:

- **capture** — turns Apple Pencil / touch input into the neutral ink model
  (`InkCanvas` / `InkSampler`; the only PencilKit code lives here, behind
  `#if canImport`),
- **render** — turns stored ink strokes into pixels (`FreehandRenderer`,
  `InkLayerRenderer`) and maps annotations onto octavo decorations
  (`Decorations`, `MarkOverlay`),
- **seams** — the `InkHost` and `InkRegionResolver` protocols a host implements to
  say *where* on the current layout a stroke should be drawn.

It was previously called `PostillaUI`; the name was misleading because it holds no
UI. `PostillaRender` names what it actually does: it renders (and captures), and
leaves the actual views to the host app.

### How they relate

```
Postilla         model + sync   (no platform deps)          ← import for logic only
   ▲
   │ depends on
PostillaRender   capture + render engine + seams (CoreGraphics)  ← import to draw ink/marks
```

`PostillaRender` depends on `Postilla` (and on `Octavo` for the `Locator` /
`Decoration` contract). You never import `PostillaRender` *instead of* `Postilla` —
importing `PostillaRender` gives you both.

| | `Postilla` | `PostillaRender` |
|---|---|---|
| Role | annotation model + sync-of-record | capture + rendering engine |
| Platform deps | none | CoreGraphics; PencilKit behind `#if canImport` |
| Contains views? | no | no — it draws into a context the host owns |
| Key types | `Annotation`, `Ink`, `SyncEngine`, `AnnotationStore` | `FreehandRenderer`, `InkLayerRenderer`, `MarkOverlay`, `InkHost` |
| Import it when | you only need model/sync | you need to capture pen input or draw ink/marks |

## Using it from an app

A host app links `PostillaRender` and supplies the thin platform glue:

1. Implement a `DecorationHost` (draw highlights/underlines) and an
   `InkHost` / `InkRegionResolver` (hand the engine a rect + a `CGContext`).
2. Feed pen input through `InkCanvas` to get neutral `InkStroke`s.
3. Call `InkLayerRenderer.draw(...)` from your view's draw pass; drive marks
   through `MarkOverlay`.

The catalogue app is the reference host — see
`catalogue-app/ios/CatalogueApp-Pkg/Sources/CatalogueReader/` (`PdfInkHost.swift`,
`PdfDecorationHost.swift`, `PencilKitInkCanvas.swift`), which are exactly these
adapters against PDFKit. Another app writes its own equivalents.

## Build and test

```
swift build        # builds Postilla + PostillaRender
swift test         # runs the pure unit tests (macOS)
```

The renderer is deliberately CoreGraphics-only and the PencilKit capture is
`#if canImport`-guarded, so the whole package builds and tests on macOS even
though capture only does anything on iOS.

## Technical details

**Canonical-ink rule.** PencilKit's `PKDrawing` / `PKStroke` are used only as an
*input* signal — they are immediately converted to raw `[x, y, pressure]` samples
in normalized `0…1` page space and never persisted. Everything is drawn back
through `FreehandRenderer` (a deterministic perfect-freehand-style port), never
through PencilKit's native ink, so the same stored stroke renders pixel-stable on
web, on device, and in an exported/flattened PDF. That determinism is what the
render-parity tests assert.

**The render path is format-agnostic.** `InkLayerRenderer` is the single place
strokes become pixels; every host (PDF today, EPUB, flattened export) goes through
it. A host only has to (a) pick the canvas size and (b) put its `CGContext` into
the renderer's coordinate space (top-left origin, y-down; a y-up PDF page flips the
CTM before calling in). `InkRegionResolver` is the per-format/mode seam that maps a
region's content anchor to an on-screen rect; the renderer never learns the format
and the resolver never learns how strokes are shaped.

**Marks vs. ink take different routes.** Highlight / underline / strikeout / note
render as octavo `Decoration`s via `Decorations` + `MarkOverlay` (anchored at a
`Locator`, carrying a `cfiRange` for EPUB text or a `region` rect for a PDF).
Freehand ink does *not* go through the `DecorationHost`; it renders through the
`FreehandRenderer` / `InkHost` overlay path. `Decorations.style(for:)` returns
`nil` for the `ink` kind for exactly this reason.

**Sync.** `SyncEngine` is publication-scoped, offline-first, last-write-wins with
UUID ids and tombstones. It queues ops locally and flushes on reconnect; conflict
resolution is by `updated_at` / `rev`. The `AnnotationStore` port
(`pull(since:) -> {rev, ops}` / `push(ops) -> {rev}`) is what a host implements to
back it with a real server.
