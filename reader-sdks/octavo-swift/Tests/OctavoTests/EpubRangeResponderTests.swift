import XCTest
@testable import OctavoEPUB

/// N3 — the EPUB byte-serving range math (the verifiable core of the `Source`-backed scheme handler).
final class EpubRangeResponderTests: XCTestCase {

    func testNoRangeServesFull() {
        XCTAssertNil(EpubRangeResponder.parse(rangeHeader: nil, total: 1000))
        XCTAssertNil(EpubRangeResponder.parse(rangeHeader: "garbage", total: 1000))
        XCTAssertNil(EpubRangeResponder.parse(rangeHeader: "bytes=10-20", total: 0))
    }

    func testClosedRange() {
        XCTAssertEqual(EpubRangeResponder.parse(rangeHeader: "bytes=100-199", total: 1000), 100..<200)
    }

    func testZeroZeroIsOneByte() {
        XCTAssertEqual(EpubRangeResponder.parse(rangeHeader: "bytes=0-0", total: 1000), 0..<1)
    }

    func testOpenEndedRunsToEOF() {
        XCTAssertEqual(EpubRangeResponder.parse(rangeHeader: "bytes=900-", total: 1000), 900..<1000)
    }

    func testEndClampedPastEOF() {
        XCTAssertEqual(EpubRangeResponder.parse(rangeHeader: "bytes=900-9999", total: 1000), 900..<1000)
    }

    func testSuffixRangeIsLastNBytes() {
        XCTAssertEqual(EpubRangeResponder.parse(rangeHeader: "bytes=-50", total: 1000), 950..<1000)
        XCTAssertEqual(EpubRangeResponder.parse(rangeHeader: "bytes=-5000", total: 1000), 0..<1000)
    }

    func testUnsatisfiableIsNil() {
        XCTAssertNil(EpubRangeResponder.parse(rangeHeader: "bytes=2000-3000", total: 1000))
        XCTAssertNil(EpubRangeResponder.parse(rangeHeader: "bytes=500-100", total: 1000))   // end<start
    }

    func testPartialHeaders() {
        let h = EpubRangeResponder.headers(contentType: "application/epub+zip", total: 1000,
                                           served: 100..<200, partial: true)
        XCTAssertEqual(h["Content-Range"], "bytes 100-199/1000")
        XCTAssertEqual(h["Content-Length"], "100")
        XCTAssertEqual(h["Accept-Ranges"], "bytes")
        XCTAssertEqual(h["Content-Type"], "application/epub+zip")
    }

    func testFullHeadersOmitContentRange() {
        let h = EpubRangeResponder.headers(contentType: "application/epub+zip", total: 1000,
                                           served: 0..<1000, partial: false)
        XCTAssertNil(h["Content-Range"])
        XCTAssertEqual(h["Content-Length"], "1000")
    }
}
