#if canImport(WebKit)
import XCTest
@testable import CatalogueReader
import Octavo

/// N4 — the testable bit of `EpubDecorationHost` (the live epub.js annotation calls are device-verified).
final class EpubDecorationHostTests: XCTestCase {
    func testStyleMapsToEpubAnnotationType() {
        XCTAssertEqual(EpubDecorationHost.epubType(.highlight), "highlight")
        XCTAssertEqual(EpubDecorationHost.epubType(.underline), "underline")
        XCTAssertNil(EpubDecorationHost.epubType(.strikethrough))   // no native epub.js type
        XCTAssertNil(EpubDecorationHost.epubType(.note))            // notes are a separate surface
    }

    private typealias Mark = EpubDecorationHost.Mark

    /// The core of the display fix: re-applying the SAME mark set (which happens on every page turn)
    /// must be a no-op, so epub.js keeps its own re-injected marks instead of us churning them off.
    func testDiffUnchangedSetIsNoOp() {
        let a = Mark(type: "highlight", cfiRange: "epubcfi(/6/4!/4/2,/1:0,/1:5)", color: "#ffd54a")
        let b = Mark(type: "underline", cfiRange: "epubcfi(/6/4!/4/6,/1:0,/1:9)", color: "#ff3b30")
        let (add, remove) = EpubDecorationHost.diff(from: [a, b], to: [b, a])   // order-insensitive
        XCTAssertTrue(add.isEmpty)
        XCTAssertTrue(remove.isEmpty)
    }

    func testDiffAddsOnlyNew() {
        let a = Mark(type: "highlight", cfiRange: "cfiA", color: nil)
        let b = Mark(type: "underline", cfiRange: "cfiB", color: nil)
        let (add, remove) = EpubDecorationHost.diff(from: [a], to: [a, b])
        XCTAssertEqual(add, [b])
        XCTAssertTrue(remove.isEmpty)
    }

    func testDiffRemovesOnlyGone() {
        let a = Mark(type: "highlight", cfiRange: "cfiA", color: nil)
        let b = Mark(type: "underline", cfiRange: "cfiB", color: nil)
        let (add, remove) = EpubDecorationHost.diff(from: [a, b], to: [a])
        XCTAssertTrue(add.isEmpty)
        XCTAssertEqual(remove, [b])
    }

    func testDiffDistinguishesTypeAtSameCfi() {
        // A highlight and an underline over the SAME range are distinct marks.
        let hl = Mark(type: "highlight", cfiRange: "cfiX", color: nil)
        let ul = Mark(type: "underline", cfiRange: "cfiX", color: nil)
        let (add, remove) = EpubDecorationHost.diff(from: [hl], to: [ul])
        XCTAssertEqual(add, [ul])
        XCTAssertEqual(remove, [hl])
    }
}
#endif
