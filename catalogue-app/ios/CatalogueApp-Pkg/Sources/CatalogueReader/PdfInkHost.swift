#if canImport(UIKit)
import UIKit
import PDFKit
import Postilla
import PostillaRender

/// The PDF adapter of the `InkHost` port (`PostillaRender`) — renders **fixed-page** `InkRegion`s onto a
/// hosted `PDFView` as native `PDFAnnotation`s, so PDFKit gives transform/zoom/print for free. The ink
/// sibling of `PdfDecorationHost` (text-anchored marks). Ink is deliberately not an octavo
/// `Decoration` — it needs its raw stroke payload — so it renders here, each region wrapped in an
/// `InkPdfAnnotation`. The structured record, not the file, is the source of truth, so the same ink
/// reaches web/export. (EPUB's `InkHost` adapter, N4, hosts inline/detached regions via an overlay.)
@MainActor
public final class PdfInkHost: InkHost {
    private let pdfView: PDFView
    private let renderer: any InkRenderer
    private var drawn: [(PDFPage, PDFAnnotation)] = []

    /// `renderer` is the swappable ink engine (default: `FreehandInkRenderer`).
    public init(pdfView: PDFView, renderer: any InkRenderer = FreehandInkRenderer()) {
        self.pdfView = pdfView
        self.renderer = renderer
    }

    /// (Re-)render the ink `regions` (non-`.fixedPage` placements are skipped — they belong to the
    /// EPUB overlay host). Idempotent: clears the prior set first. Map annotations via
    /// `Annotation.inkRegion()`.
    public func render(_ regions: [InkRegion]) {
        clear()
        guard let doc = pdfView.document else { return }
        for region in regions where region.placement == .fixedPage {
            guard !region.strokes.isEmpty else { continue }
            let index = region.anchor.locations.position
                ?? region.anchor.locations.page.map { max(0, $0 - 1) }
            guard let i = index, i >= 0, i < doc.pageCount,
                  let page = doc.page(at: i) else { continue }
            let ann = InkPdfAnnotation(strokes: region.strokes, pageBounds: page.bounds(for: .cropBox),
                                       renderer: renderer)
            page.addAnnotation(ann)
            drawn.append((page, ann))
        }
    }

    public func clear() {
        guard !drawn.isEmpty else { return }
        for (page, ann) in drawn {
            // `removeAnnotation` alone updates the page model but does NOT reliably repaint a custom-drawn
            // (`.stamp`) annotation: PDFView keeps it baked in the page's cached bitmap, so erased/undone
            // ink lingers on screen until the page is re-tiled (e.g. scrolled away and back). Flipping
            // `shouldDisplay` posts an annotation-changed notification that forces PDFView to re-composite
            // the page WITHOUT this ink. (Native marks — `.highlight`/`.underline` — don't need this.)
            ann.shouldDisplay = false
            page.removeAnnotation(ann)
        }
        drawn.removeAll()
        pdfView.setNeedsDisplay()
    }
}
#endif
