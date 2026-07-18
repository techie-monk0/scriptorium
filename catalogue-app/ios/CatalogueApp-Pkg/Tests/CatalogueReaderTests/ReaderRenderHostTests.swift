import XCTest
#if canImport(UIKit)
import PDFKit
import CoreGraphics
import Octavo
import Postilla
import PostillaRender
@testable import CatalogueReader

/// The render path that bugs 1 & 3 are about: given the marks the store returns on (re)open, does the
/// render layer actually put annotations on the PDF pages? These fire in CI (real PDFKit on the
/// simulator) — so a "mark not rendered on reopen" bug is caught at the model level, distinct from a
/// PDFKit *repaint* bug (which only shows visually and needs on-device logs).
@MainActor
final class ReaderRenderHostTests: XCTestCase {

    private func makePdf(pages: Int) throws -> PDFDocument {
        let data = NSMutableData()
        guard let consumer = CGDataConsumer(data: data as CFMutableData) else { throw XCTSkip("no consumer") }
        var box = CGRect(x: 0, y: 0, width: 300, height: 400)
        guard let ctx = CGContext(consumer: consumer, mediaBox: &box, nil) else { throw XCTSkip("no ctx") }
        for _ in 0..<pages { ctx.beginPDFPage(nil); ctx.endPDFPage() }
        ctx.closePDF()
        guard let doc = PDFDocument(data: data as Data) else { throw XCTSkip("no doc") }
        return doc
    }

    private func highlight(page: Int) -> Annotation {
        Annotation(publicationId: "p", kind: .highlight,
                   locator: Locator(publicationId: "p", format: .pdf, locations: .init(page: page)),
                   quads: [[0.1, 0.1, 0.6, 0.05]], color: "#ffd54a",
                   createdAt: Date(timeIntervalSince1970: 0), updatedAt: Date(timeIntervalSince1970: 0), rev: 1)
    }

    private func inkMark(page: Int) -> Annotation {
        let stroke = InkStroke(points: [InkPoint(x: 0.2, y: 0.2), InkPoint(x: 0.5, y: 0.35)],
                               width: 3, color: "#1565c0")
        return Annotation(publicationId: "p", kind: .ink,
                          locator: Locator(publicationId: "p", format: .pdf, locations: .init(page: page)),
                          ink: Ink(strokes: [stroke]),
                          createdAt: Date(timeIntervalSince1970: 0), updatedAt: Date(timeIntervalSince1970: 0), rev: 1)
    }

    // MARK: text marks (highlight / underline) — bug 1 & 3

    func testHighlightIsAddedToPageAndRemovedOnErase() throws {
        let doc = try makePdf(pages: 3)
        let pdfView = PDFView(); pdfView.document = doc
        let host = PdfDecorationHost(pdfView: pdfView)         // retained for the test (see note below)
        let layer = CompositeRenderLayer(decorations: host, ink: PdfInkHost(pdfView: pdfView), inkPlacement: .fixedPage)

        layer.render([highlight(page: 1)])
        XCTAssertEqual(doc.page(at: 0)?.annotations.count, 1, "highlight must be on page 1")

        // Undo / erase → the reduced set must remove the annotation from the page model.
        layer.render([])
        XCTAssertEqual(doc.page(at: 0)?.annotations.count, 0, "removed highlight must leave no annotation")
        withExtendedLifetime(host) {}
    }

    func testMarksReRenderFromEmptyHostLikeAFreshTabMount() throws {
        // A fresh tab mount = a brand-new host with empty state receiving the persisted marks.
        let doc = try makePdf(pages: 3)
        let pdfView = PDFView(); pdfView.document = doc
        let host = PdfDecorationHost(pdfView: pdfView)
        let fresh = CompositeRenderLayer(decorations: host, ink: PdfInkHost(pdfView: pdfView), inkPlacement: .fixedPage)
        fresh.render([highlight(page: 1), inkMark(page: 2)])
        XCTAssertEqual(doc.page(at: 0)?.annotations.count, 1, "text mark restored on mount")
        XCTAssertEqual(doc.page(at: 1)?.annotations.count, 1, "ink restored on mount")
        withExtendedLifetime(host) {}
    }

    /// Guard the fragility the tests above exposed: `CompositeRenderLayer` references its `DecorationHost`
    /// only weakly (through `MarkOverlay`), so if nothing else retains it, text marks silently stop
    /// rendering. The reader keeps it in `@State`; this documents/locks that requirement.
    func testDecorationHostMustBeRetainedByCaller() throws {
        let doc = try makePdf(pages: 1)
        let pdfView = PDFView(); pdfView.document = doc
        let layer = CompositeRenderLayer(decorations: PdfDecorationHost(pdfView: pdfView),   // NOT retained
                                         ink: PdfInkHost(pdfView: pdfView), inkPlacement: .fixedPage)
        layer.render([highlight(page: 1)])
        XCTAssertEqual(doc.page(at: 0)?.annotations.count, 0,
                       "an un-retained DecorationHost is deallocated → marks don't render (must be retained)")
    }

    // MARK: ink — differential add / no-op / remove

    func testInkAddsIsDifferentialAndRemoves() throws {
        let doc = try makePdf(pages: 3)
        let pdfView = PDFView(); pdfView.document = doc
        let ink = PdfInkHost(pdfView: pdfView)
        let region = try XCTUnwrap(inkMark(page: 1).inkRegion(placement: .fixedPage))

        ink.render([region])
        XCTAssertEqual(doc.page(at: 0)?.annotations.count, 1, "ink added to page 1")

        // The reconcile after drawing re-renders the SAME set — differential, so no churn / no duplicate.
        ink.render([region])
        XCTAssertEqual(doc.page(at: 0)?.annotations.count, 1, "re-render of the same ink is a no-op")

        ink.render([])
        XCTAssertEqual(doc.page(at: 0)?.annotations.count, 0, "erased ink removed")
    }
}
#endif
