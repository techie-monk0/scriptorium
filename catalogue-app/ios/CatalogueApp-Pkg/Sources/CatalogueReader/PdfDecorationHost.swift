#if canImport(UIKit)
import UIKit
import PDFKit
import os
import Octavo
import Postilla

/// The catalogue-app's concrete octavo `DecorationHost` for PDF — renders postilla's mark decorations
/// as native `PDFAnnotation`s on the hosted `PDFView`. A text mark carries **per-line quads**
/// (`Decoration.quads`, normalized top-left) → one annotation per line placed at its rect; a note
/// carries a point (`region`) → a text-marker icon; a mark with neither falls back to a page band.
@MainActor
public final class PdfDecorationHost: DecorationHost {
    private let pdfView: PDFView
    private var drawn: [(PDFPage, PDFAnnotation)] = []

    public init(pdfView: PDFView) { self.pdfView = pdfView }

    public func apply(_ decorations: [Decoration]) {
        clear()
        defer {
            nudgeRedraw()
            ReaderLog.annotations.info("PdfDecorationHost.apply: decorations=\(decorations.count) drawn=\(self.drawn.count) doc=\(self.pdfView.document != nil)")
        }
        guard let doc = pdfView.document else { return }
        for d in decorations {
            let index = d.locator.locations.position ?? d.locator.locations.page.map { max(0, $0 - 1) }
            guard let i = index, i >= 0, i < doc.pageCount, let page = doc.page(at: i) else { continue }
            let b = page.bounds(for: .cropBox)

            if d.style == .note {
                let pt = d.region ?? [0.5, 0.5]
                let px = b.minX + (pt.first ?? 0.5) * b.width
                let py = b.minY + b.height - (pt.count > 1 ? pt[1] : 0.5) * b.height
                add(page, CGRect(x: px - 8, y: py - 8, width: 16, height: 16), .text, d.color)
                continue
            }

            if let quads = d.quads, !quads.isEmpty {
                // One native annotation per line rect — renders reliably (bounds-filled) without relying
                // on quadrilateralPoints coordinate conventions. Convert normalized top-left → page space.
                for q in quads {
                    guard let r = PageGeometry.pageRect(quad: q, pageMinX: b.minX, pageMinY: b.minY,
                                                        pageWidth: b.width, pageHeight: b.height) else { continue }
                    add(page, CGRect(x: r.x, y: r.y, width: r.w, height: r.h), subtype(d.style), d.color)
                }
                continue
            }

            // Fallback: page-anchored band (legacy mark with no quads).
            add(page, CGRect(x: b.minX + 16, y: b.maxY - 56, width: b.width - 32, height: 26),
                subtype(d.style), d.color)
        }
    }

    private func add(_ page: PDFPage, _ rect: CGRect, _ type: PDFAnnotationSubtype, _ hex: String?) {
        let ann = PDFAnnotation(bounds: rect, forType: type, withProperties: nil)
        ann.color = color(hex) ?? .systemYellow
        page.addAnnotation(ann)
        drawn.append((page, ann))
    }

    public func clear() {
        if !drawn.isEmpty { ReaderLog.annotations.info("PdfDecorationHost.clear removing=\(self.drawn.count)") }
        // `removeAnnotation` alone updates the page model but PDFView keeps the mark baked in the page's
        // cached tile, so an undone/erased highlight lingers until a re-tile. Flipping `shouldDisplay`
        // posts an annotation-changed notification that forces a re-composite without it (the same proven
        // trick `PdfInkHost` uses for ink).
        for (page, ann) in drawn { ann.shouldDisplay = false; page.removeAnnotation(ann) }
        drawn.removeAll()
    }

    /// PDFKit does not reliably repaint after a `PDFPage` annotation add/remove that isn't tied to a user
    /// gesture — `setNeedsDisplay` leaves the cached page tile stale, so an undone highlight lingers and
    /// marks re-added on a fresh (tab-switched) view don't show until a scroll. `layoutDocumentView`
    /// re-tiles the document and reliably repaints the annotation change. Position-preserving, and this
    /// only runs on mark add/remove/pull (never during live ink drawing), so there's no jank.
    private func nudgeRedraw() {
        pdfView.layoutDocumentView()
    }

    private func subtype(_ style: Decoration.Style) -> PDFAnnotationSubtype {
        switch style {
        case .highlight: return .highlight
        case .underline: return .underline
        case .strikethrough: return .strikeOut
        case .note: return .text
        }
    }

    private func color(_ hex: String?) -> UIColor? {
        guard var s = hex else { return nil }
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        return UIColor(red: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                       blue: CGFloat(v & 0xff) / 255, alpha: 0.4)
    }
}
#endif
