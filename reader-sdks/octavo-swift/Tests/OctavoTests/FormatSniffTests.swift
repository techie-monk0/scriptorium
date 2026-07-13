import XCTest
@testable import Octavo

/// OS-U2 — format sniff (magic bytes + extension fallback).
final class FormatSniffTests: XCTestCase {

    func testSniffPdfByMagicBytes() {
        let data = Data("%PDF-1.7\n…rest…".utf8)
        XCTAssertEqual(FormatSniffer.sniff(data: data), .pdf)
    }

    func testSniffEpubByZipPlusMimetype() {
        // Minimal OCF prefix: PK local header + "mimetype" + media type.
        var bytes: [UInt8] = [0x50, 0x4B, 0x03, 0x04] // PK\x03\x04
        bytes += Array("....mimetypeapplication/epub+zip".utf8)
        XCTAssertEqual(FormatSniffer.sniff(data: Data(bytes)), .epub)
    }

    func testZipWithoutMimetypeIsNotEpub() {
        var bytes: [UInt8] = [0x50, 0x4B, 0x03, 0x04]
        bytes += Array("some/other/zip/contents".utf8)
        XCTAssertNil(FormatSniffer.sniff(data: Data(bytes)))
    }

    func testUnknownBytesAreNil() {
        XCTAssertNil(FormatSniffer.sniff(data: Data([0x00, 0x01, 0x02, 0x03])))
        XCTAssertNil(FormatSniffer.sniff(data: Data()))
    }

    func testExtensionFallback() {
        XCTAssertEqual(FormatSniffer.sniff(pathExtension: "PDF"), .pdf)
        XCTAssertEqual(FormatSniffer.sniff(pathExtension: "epub"), .epub)
        XCTAssertNil(FormatSniffer.sniff(pathExtension: "txt"))
    }

    /// Ambiguous (no magic) input resolves via the URL extension.
    func testBytesFirstThenUrlFallback() {
        let ambiguous = Data("not a known magic".utf8)
        let url = URL(fileURLWithPath: "/tmp/book.epub")
        XCTAssertEqual(FormatSniffer.sniff(data: ambiguous, url: url), .epub)

        let pdfBytes = Data("%PDF".utf8)
        // Bytes win over a misleading extension.
        XCTAssertEqual(
            FormatSniffer.sniff(data: pdfBytes, url: URL(fileURLWithPath: "/x.epub")),
            .pdf
        )
    }
}
