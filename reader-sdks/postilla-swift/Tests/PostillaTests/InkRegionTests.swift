import XCTest
import ReaderContract
@testable import Postilla

/// N2 — `Annotation` → `InkRegion` projection (the placement-layer's typed view over the row).
final class InkRegionTests: XCTestCase {

    private func inkAnnotation(strokes: [InkStroke]) -> Annotation {
        Annotation(id: Fix.uuid(1), publicationId: Fix.pub, kind: .ink,
                   locator: Fix.locator(page: 5), ink: Ink(strokes: strokes),
                   createdAt: Fix.date(0), updatedAt: Fix.date(0))
    }

    func testInkAnnotationProjectsToFixedPageRegion() throws {
        let a = inkAnnotation(strokes: [Fix.inkStroke()])
        let r = try XCTUnwrap(a.inkRegion())
        XCTAssertEqual(r.id, a.id.uuidString)
        XCTAssertEqual(r.placement, .fixedPage)             // default = PDF / frozen
        XCTAssertEqual(r.anchor.locations.page, 5)          // anchor preserved
        XCTAssertEqual(r.strokes, a.ink?.strokes)           // raw strokes carried
    }

    func testPlacementOverrideForEpub() throws {
        let r = try XCTUnwrap(inkAnnotation(strokes: [Fix.inkStroke()]).inkRegion(placement: .inlineBox(aspect: 0.5)))
        XCTAssertEqual(r.placement, .inlineBox(aspect: 0.5))
    }

    func testNonInkOrEmptyProjectsToNil() {
        // a highlight is not ink
        XCTAssertNil(Fix.highlight(2, updatedAt: 0).inkRegion())
        // ink with no strokes
        XCTAssertNil(inkAnnotation(strokes: []).inkRegion())
    }
}
