import XCTest
@testable import Octavo

/// OS-U1 — Locator round-trip + a fixed JSON golden (byte-parity with the web
/// binding's canonical JSON for the same Locator).
final class LocatorTests: XCTestCase {

    /// Canonical JSON for a PDF locator, sorted keys, slashes unescaped.
    private let pdfGolden =
        #"{"format":"pdf","locations":{"page":42,"progression":0.5},"publicationId":"pub-1"}"#

    /// Canonical JSON for an EPUB locator carrying text context.
    private let epubGolden =
        #"{"format":"epub","locations":{"cfi":"epubcfi(/6/4!/4/2)","progression":0.25},"publicationId":"book-2","text":{"after":" world","before":"hello ","highlight":"beautiful"}}"#

    func testPdfLocatorGoldenEncode() throws {
        let loc = Locator(
            publicationId: "pub-1",
            format: .pdf,
            locations: .init(page: 42, progression: 0.5)
        )
        let json = String(decoding: try loc.jsonData(), as: UTF8.self)
        XCTAssertEqual(json, pdfGolden)
    }

    func testEpubLocatorGoldenEncode() throws {
        let loc = Locator(
            publicationId: "book-2",
            format: .epub,
            locations: .init(cfi: "epubcfi(/6/4!/4/2)", progression: 0.25),
            text: .init(before: "hello ", highlight: "beautiful", after: " world")
        )
        let json = String(decoding: try loc.jsonData(), as: UTF8.self)
        XCTAssertEqual(json, epubGolden)
    }

    func testGoldenDecodeRoundTrip() throws {
        for golden in [pdfGolden, epubGolden] {
            let decoded = try Locator.from(jsonData: Data(golden.utf8))
            let reencoded = String(decoding: try decoded.jsonData(), as: UTF8.self)
            XCTAssertEqual(reencoded, golden, "round-trip must be byte-stable")
        }
    }

    func testAllShapesRoundTripLosslessly() throws {
        let shapes: [Locator] = [
            Locator(publicationId: "a", format: .pdf,
                    locations: .init(page: 1)),
            Locator(publicationId: "b", format: .pdf,
                    locations: .init(page: 7, progression: 0.33, position: 6)),
            Locator(publicationId: "c", format: .epub,
                    locations: .init(cfi: "epubcfi(/2)")),
            Locator(publicationId: "d", format: .epub,
                    locations: .init(cfi: "epubcfi(/4)", progression: 1.0),
                    text: .init(before: "x", highlight: "y", after: "z")),
        ]
        for loc in shapes {
            let decoded = try Locator.from(jsonData: try loc.jsonData())
            XCTAssertEqual(decoded, loc)
        }
    }

    /// nil optionals must be omitted from JSON (web parity).
    func testNilFieldsOmitted() throws {
        let loc = Locator(publicationId: "x", format: .pdf, locations: .init(page: 3))
        let json = String(decoding: try loc.jsonData(), as: UTF8.self)
        XCTAssertFalse(json.contains("cfi"))
        XCTAssertFalse(json.contains("text"))
        XCTAssertFalse(json.contains("position"))
        XCTAssertFalse(json.contains("progression"))
    }
}
