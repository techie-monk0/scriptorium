import XCTest
@testable import CatalogueReader
import Postilla
import Octavo

/// N1 — the testable core of ink capture (the PencilKit surface is device-verified, not here).
final class InkCaptureTests: XCTestCase {

    private func stroke() -> InkStroke {
        InkStroke(points: [InkPoint(x: 0.1, y: 0.2, pressure: 0.5, t: 0),
                           InkPoint(x: 0.3, y: 0.4, pressure: 0.7, t: 12)],
                  width: 4, color: "#ff3b30", mode: .draw)
    }

    func testBuildsInkAnnotationAtLocator() throws {
        let loc = Locator(publicationId: "holding:9", format: .pdf, locations: .init(page: 4))
        let ink = Ink(strokes: [stroke()])
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let a = try XCTUnwrap(InkCapture.annotation(ink: ink, locator: loc,
                                                    publicationId: "holding:9", rev: 7, now: now))
        XCTAssertEqual(a.kind, .ink)
        XCTAssertEqual(a.ink, ink)                       // raw strokes carried (incl. timestamps)
        XCTAssertEqual(a.locator.locations.page, 4)
        XCTAssertEqual(a.rev, 7)
        XCTAssertEqual(a.publicationId, "holding:9")
        XCTAssertNil(a.deletedAt)
    }

    /// Empty ink (a stray tap) creates no mark.
    func testEmptyInkIsDropped() {
        let loc = Locator(publicationId: "holding:9", format: .pdf, locations: .init(page: 1))
        XCTAssertNil(InkCapture.annotation(ink: Ink(strokes: []), locator: loc,
                                           publicationId: "holding:9", rev: 1, now: Date()))
        let blank = Ink(strokes: [InkStroke(points: [], width: 4, color: "#000", mode: .draw)])
        XCTAssertNil(InkCapture.annotation(ink: blank, locator: loc,
                                           publicationId: "holding:9", rev: 1, now: Date()))
    }
}
