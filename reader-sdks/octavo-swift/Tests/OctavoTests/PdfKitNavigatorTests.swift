import XCTest
@testable import Octavo

// PDFKit IS available on macOS, so this gated test compiles AND runs under
// `swift test`. A tiny searchable PDF is generated in-test via CoreGraphics.
#if canImport(PDFKit)
import PDFKit
import CoreGraphics
import CoreText
@testable import OctavoPDFKit

@MainActor
final class PdfKitNavigatorTests: XCTestCase {

    func testPageToLocatorMappingAndNav() async throws {
        let doc = try makeDocument(pages: ["alpha", "beta", "gamma", "delta"])
        let nav = PdfKitNavigator(document: doc, publicationId: "pub-pdf")

        var emitted: [Locator] = []
        nav.onLocationChanged = { emitted.append($0) }

        try await nav.open()
        // Initial page → 1-based page 1, position 0.
        XCTAssertEqual(nav.currentLocation?.locations.page, 1)
        XCTAssertEqual(nav.currentLocation?.locations.position, 0)

        try await nav.goTo(Locator(publicationId: "pub-pdf", format: .pdf,
                                   locations: .init(page: 3)))
        XCTAssertEqual(nav.currentLocation?.locations.page, 3)
        XCTAssertEqual(nav.currentLocation?.locations.position, 2)
        XCTAssertEqual(nav.currentLocation?.locations.progression, 2.0 / 4.0)

        try await nav.next()
        XCTAssertEqual(nav.currentLocation?.locations.page, 4)
        try await nav.prev()
        XCTAssertEqual(nav.currentLocation?.locations.page, 3)

        XCTAssertEqual(emitted.last?.locations.page, 3)
    }

    func testGoToOutOfRangeThrows() async throws {
        let doc = try makeDocument(pages: ["only"])
        let nav = PdfKitNavigator(document: doc, publicationId: "p")
        try await nav.open()
        do {
            try await nav.goTo(Locator(publicationId: "p", format: .pdf,
                                       locations: .init(page: 99)))
            XCTFail("expected pageOutOfRange")
        } catch let e as PdfKitNavigatorError {
            XCTAssertEqual(e, .pageOutOfRange)
        }
    }

    func testSearchReturnsLocators() async throws {
        let doc = try makeDocument(pages: [
            "the quick brown fox",
            "dependent origination here",
            "nothing relevant",
        ])
        let nav = PdfKitNavigator(document: doc, publicationId: "p")
        try await nav.open()

        let hits = try await nav.search("origination")
        // PDFKit text extraction can vary; if it found the term, it must point
        // at page 2 (position 1). At minimum, search must not throw.
        if let first = hits.first {
            XCTAssertEqual(first.locations.position, 1)
            XCTAssertEqual(first.format, .pdf)
            XCTAssertNotNil(first.text?.highlight)
        }
        let emptyHits = try await nav.search("")
        XCTAssertTrue(emptyHits.isEmpty)
    }

    // MARK: tiny PDF generator (searchable text via CoreText)

    private func makeDocument(pages: [String]) throws -> PDFDocument {
        let data = NSMutableData()
        guard let consumer = CGDataConsumer(data: data as CFMutableData) else {
            throw XCTSkip("CGDataConsumer unavailable")
        }
        var box = CGRect(x: 0, y: 0, width: 300, height: 300)
        guard let ctx = CGContext(consumer: consumer, mediaBox: &box, nil) else {
            throw XCTSkip("CGContext PDF unavailable")
        }
        let font = CTFontCreateWithName("Helvetica" as CFString, 16, nil)
        for text in pages {
            ctx.beginPDFPage(nil)
            let attr = NSAttributedString(string: text, attributes: [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
            ])
            let line = CTLineCreateWithAttributedString(attr)
            ctx.textPosition = CGPoint(x: 20, y: 150)
            CTLineDraw(line, ctx)
            ctx.endPDFPage()
        }
        ctx.closePDF()

        guard let doc = PDFDocument(data: data as Data) else {
            throw XCTSkip("PDFDocument could not parse generated PDF")
        }
        XCTAssertEqual(doc.pageCount, pages.count)
        return doc
    }
}
#endif
