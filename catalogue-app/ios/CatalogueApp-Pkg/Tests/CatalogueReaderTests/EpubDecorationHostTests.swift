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
}
#endif
