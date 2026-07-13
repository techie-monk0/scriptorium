#if canImport(UIKit)
import UIKit
import PDFKit
import Postilla
import PostillaUI

/// A display-only `PDFAnnotation` that draws postilla freehand ink onto a PDF
/// page via the shared `InkLayerRenderer`. By living *inside* a `PDFAnnotation`,
/// it inherits PDFKit's scroll/zoom/page-transform/print handling for free — the
/// host only has to add it to the right page; PDFKit composes it at every scale.
///
/// The ink's normalized `0…1` points are page-relative with a **top-left origin
/// (y-down)**, but a PDF page's context is **bottom-left (y-up)** — so `draw`
/// flips the CTM before delegating to `InkLayerRenderer`. That flip is the one
/// PDF-specific concession; the renderer itself is format-agnostic and is reused
/// verbatim by the (future) EPUB overlay.
///
/// Subtype `.stamp` (not `.ink`) so PDFKit does not try to render its own
/// `/InkList`; we own the drawing entirely. This is a *display* annotation, not
/// the export representation (that path is `PostillaExport` / the server route).
///
/// Deliberately **not** `@MainActor`: it overrides `PDFAnnotation.draw(with:in:)`
/// (a nonisolated superclass method), and its only state is `Sendable` strokes,
/// so isolating it would create an override-isolation mismatch under Swift 6.
final class InkPdfAnnotation: PDFAnnotation {
    private let strokes: [InkStroke]

    /// `pageBounds` should be the page's `.cropBox` bounds — the canvas the ink's
    /// `0…1` coordinates are normalized against.
    init(strokes: [InkStroke], pageBounds: CGRect) {
        self.strokes = strokes
        super.init(bounds: pageBounds, forType: .stamp, withProperties: nil)
        shouldDisplay = true
        shouldPrint = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    override func draw(with box: PDFDisplayBox, in context: CGContext) {
        guard !strokes.isEmpty else { return }
        context.saveGState()
        // Flip PDF (bottom-left, y-up) → renderer space (top-left, y-down). The
        // context origin is the annotation's lower-left, so a translate by the
        // bounds height + y-scale of -1 is exact regardless of cropBox origin.
        context.translateBy(x: 0, y: bounds.height)
        context.scaleBy(x: 1, y: -1)
        InkLayerRenderer.draw(strokes, in: bounds.size, into: context)
        context.restoreGState()
    }
}
#endif
