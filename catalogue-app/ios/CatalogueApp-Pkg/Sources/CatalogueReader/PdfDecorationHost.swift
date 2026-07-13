#if canImport(UIKit)
import UIKit
import PDFKit
import Octavo

/// The catalogue-app's concrete octavo `DecorationHost` for PDF — renders postilla's mark decorations
/// as native `PDFAnnotation`s on the hosted `PDFView`. The base `Decoration` carries a Locator + style
/// (not a precise rect — text-anchored rects are a postilla enhancement), so a mark is drawn as a
/// page-anchored band; the round-trip (create → store → pull → render) is what this proves.
@MainActor
public final class PdfDecorationHost: DecorationHost {
    private let pdfView: PDFView
    private var drawn: [(PDFPage, PDFAnnotation)] = []

    public init(pdfView: PDFView) { self.pdfView = pdfView }

    public func apply(_ decorations: [Decoration]) {
        clear()
        guard let doc = pdfView.document else { return }
        for d in decorations {
            let index = d.locator.locations.position ?? d.locator.locations.page.map { max(0, $0 - 1) }
            guard let i = index, i >= 0, i < doc.pageCount, let page = doc.page(at: i) else { continue }
            let b = page.bounds(for: .cropBox)
            let rect = CGRect(x: b.minX + 16, y: b.maxY - 56, width: b.width - 32, height: 26)
            let ann = PDFAnnotation(bounds: rect, forType: subtype(d.style), withProperties: nil)
            ann.color = color(d.color) ?? .systemYellow
            page.addAnnotation(ann)
            drawn.append((page, ann))
        }
    }

    public func clear() {
        for (page, ann) in drawn { page.removeAnnotation(ann) }
        drawn.removeAll()
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
