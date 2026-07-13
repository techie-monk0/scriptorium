import XCTest
import ReaderContract
import PostillaRender
@testable import Postilla

/// PS-U3 — Annotation ⇄ Locator anchoring.
final class AnchoringTests: XCTestCase {

    /// highlight/underline/strikeout/note map to a host decoration at the same
    /// Locator and resolve back to it.
    func testTextMarksAnchorAndResolveBack() throws {
        for kind in [AnnotationKind.highlight, .underline, .strikeout, .note] {
            let loc = Fix.locator(page: 7)
            let ann = Annotation(
                id: Fix.uuid(1),
                publicationId: Fix.pub,
                kind: kind,
                locator: loc,
                color: "#abc",
                noteText: kind == .note ? "hi" : nil,
                createdAt: Fix.date(0),
                updatedAt: Fix.date(0)
            )
            let dec = try XCTUnwrap(Decorations.decoration(for: ann), "\(kind) should map")
            XCTAssertEqual(dec.locator, loc, "anchor preserved for \(kind)")
            XCTAssertEqual(dec.id, ann.id.uuidString)
        }
    }

    /// A mark's precise anchor (cfiRange / region) survives Annotation → Decoration so the host can
    /// place it exactly (EPUB CFI range / PDF rect), not on a page band.
    func testDecorationCarriesPreciseAnchor() throws {
        let ann = Annotation(
            id: Fix.uuid(9), publicationId: Fix.pub, kind: .underline,
            locator: Fix.locator(page: 4),
            cfiRange: "epubcfi(/6/4!/4/2,/1:0,/1:9)", region: [0.1, 0.2, 0.5, 0.03],
            createdAt: Fix.date(0), updatedAt: Fix.date(0))
        let dec = try XCTUnwrap(Decorations.decoration(for: ann))
        XCTAssertEqual(dec.cfiRange, "epubcfi(/6/4!/4/2,/1:0,/1:9)")
        XCTAssertEqual(dec.region, [0.1, 0.2, 0.5, 0.03])
        XCTAssertEqual(dec.style, .underline)
    }

    /// Kind ⇄ style mapping is exactly the contract.
    func testStyleMapping() {
        XCTAssertEqual(Decorations.style(for: .highlight), .highlight)
        XCTAssertEqual(Decorations.style(for: .underline), .underline)
        XCTAssertEqual(Decorations.style(for: .strikeout), .strikethrough)
        XCTAssertEqual(Decorations.style(for: .note), .note)
        XCTAssertNil(Decorations.style(for: .ink)) // ink renders via FreehandRenderer
    }

    /// Ink anchors at its Locator (page / spine-index) but is NOT a host
    /// decoration.
    func testInkHasNoDecorationButKeepsLocator() {
        let ann = Annotation(
            id: Fix.uuid(2),
            publicationId: Fix.pub,
            kind: .ink,
            locator: Fix.locator(page: 12),
            ink: Ink(strokes: [Fix.inkStroke()]),
            createdAt: Fix.date(0),
            updatedAt: Fix.date(0)
        )
        XCTAssertNil(Decorations.decoration(for: ann))
        XCTAssertEqual(ann.locator.locations.page, 12)
    }

    /// Tombstones never produce a decoration.
    func testTombstoneHasNoDecoration() {
        let ann = Fix.highlight(3, updatedAt: 10, deleted: 10)
        XCTAssertNil(Decorations.decoration(for: ann))
    }

    /// `cfiRange` + `region` carry a passage range / a precise box that the
    /// point-only `locator` can't (the N0b losslessness fix); they round-trip and
    /// are **omitted on the wire when nil** so existing annotation JSON is unchanged.
    func testRangeAndRegionRoundTripAndOmitWhenNil() throws {
        let enc = JSONEncoder(); enc.outputFormatting = [.sortedKeys]; enc.dateEncodingStrategy = .iso8601
        let dec = JSONDecoder(); dec.dateDecodingStrategy = .iso8601

        // nil case: keys absent (back-compat).
        let plain = Fix.highlight(1, updatedAt: 0)
        let plainJSON = String(decoding: try enc.encode(plain), as: UTF8.self)
        XCTAssertFalse(plainJSON.contains("cfiRange"))
        XCTAssertFalse(plainJSON.contains("region"))

        // populated case: round-trips.
        let marked = Annotation(
            id: Fix.uuid(2), publicationId: Fix.pub, kind: .highlight,
            locator: Fix.locator(page: 7),
            cfiRange: "epubcfi(/6/4!/4/2,/1:0,/1:8)",
            region: [0.1, 0.2, 0.3, 0.05],
            createdAt: Fix.date(0), updatedAt: Fix.date(0))
        let back = try dec.decode(Annotation.self, from: try enc.encode(marked))
        XCTAssertEqual(back, marked)
        XCTAssertEqual(back.cfiRange, "epubcfi(/6/4!/4/2,/1:0,/1:8)")
        XCTAssertEqual(back.region, [0.1, 0.2, 0.3, 0.05])
    }
}
