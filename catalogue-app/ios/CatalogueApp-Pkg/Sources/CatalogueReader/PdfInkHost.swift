#if canImport(UIKit)
import UIKit
import PDFKit
import os
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
    /// Live annotations keyed by `InkRegion.id` (the annotation's uuid). Keying by id lets `render` be
    /// **differential** — see `render(_:)` for why that matters.
    private var drawn: [String: (page: PDFPage, annotation: PDFAnnotation)] = [:]

    /// `renderer` is the swappable ink engine (default: `FreehandInkRenderer`).
    public init(pdfView: PDFView, renderer: any InkRenderer = FreehandInkRenderer()) {
        self.pdfView = pdfView
        self.renderer = renderer
    }

    /// (Re-)render the ink `regions` (non-`.fixedPage` placements belong to the EPUB overlay host).
    ///
    /// **Differential, not clear-and-rebuild.** The reader re-renders the full mark set on every change
    /// (add, erase, undo, and the post-push server reconcile). If each render removed *all* ink
    /// annotations and re-added them, the reconcile after drawing a stroke would remove-then-re-add the
    /// just-drawn ink — and PDFView does not reliably repaint that churn for custom `.stamp` annotations,
    /// so the fresh stroke would blank out the instant the pencil lifts. Instead we keep annotations keyed
    /// by region id: unchanged regions stay put (the reconcile is a no-op for them), only genuinely-new
    /// regions are added and only removed regions are taken away.
    public func render(_ regions: [InkRegion]) {
        guard let doc = pdfView.document else { clear(); return }
        let wanted = regions.filter { $0.placement == .fixedPage && !$0.strokes.isEmpty }
        let wantedIds = Set(wanted.map(\.id))

        // Remove annotations whose region is gone (an erase or an undo of a draw).
        var removedAny = false
        var removedCount = 0
        for (id, entry) in drawn where !wantedIds.contains(id) {
            hide(entry)
            drawn[id] = nil
            removedAny = true
            removedCount += 1
        }
        if removedCount > 0 { ReaderLog.annotations.info("PdfInkHost.render REMOVING=\(removedCount) (wanted=\(wanted.count))") }
        // Add annotations for regions we aren't already showing (a fresh draw, or a redo).
        for region in wanted where drawn[region.id] == nil {
            let index = region.anchor.locations.position
                ?? region.anchor.locations.page.map { max(0, $0 - 1) }
            guard let i = index, i >= 0, i < doc.pageCount, let page = doc.page(at: i) else { continue }
            let ann = InkPdfAnnotation(strokes: region.strokes, pageBounds: page.bounds(for: .cropBox),
                                       renderer: renderer)
            page.addAnnotation(ann)
            drawn[region.id] = (page, ann)
        }
        // A removal (erase/undo) needs a reliable re-composite: `setNeedsDisplay` leaves the erased stroke
        // baked in the page's cached tile. `layoutDocumentView` re-tiles and clears it — but an ERASE
        // happens mid-PencilKit-gesture, where an inline re-tile is ignored until the interaction ends
        // (why the erase only showed after a later refresh/tab-switch). Defer it to the next runloop so it
        // runs after the gesture event completes. Only on removal — a pure add repaints via `addAnnotation`.
        if removedAny {
            let view = pdfView
            ReaderLog.annotations.info("🧹 PdfInkHost: removed=\(removedCount) → scheduling redraw (layoutDocumentView)")
            DispatchQueue.main.async {
                view.layoutDocumentView()
                view.setNeedsDisplay(view.bounds)
                ReaderLog.annotations.info("🧹 PdfInkHost: redraw EXECUTED (layoutDocumentView + setNeedsDisplay)")
            }
        }
        ReaderLog.annotations.info("PdfInkHost.render: wanted=\(wanted.count) drawn=\(self.drawn.count) removedAny=\(removedAny) removed=\(removedCount)")
    }

    public func clear() {
        guard !drawn.isEmpty else { return }
        for entry in drawn.values { hide(entry) }
        drawn.removeAll()
        pdfView.layoutDocumentView()   // reliable re-composite (setNeedsDisplay leaves erased ink baked in)
    }

    /// Remove an ink annotation so it actually disappears. `removeAnnotation` alone updates the page model
    /// but does NOT reliably repaint a custom-drawn (`.stamp`) annotation — PDFView keeps it baked in the
    /// page's cached bitmap, so erased/undone ink would linger until the page re-tiles. Flipping
    /// `shouldDisplay` posts an annotation-changed notification that forces a re-composite without it.
    private func hide(_ entry: (page: PDFPage, annotation: PDFAnnotation)) {
        entry.annotation.shouldDisplay = false
        entry.page.removeAnnotation(entry.annotation)
    }
}
#endif
