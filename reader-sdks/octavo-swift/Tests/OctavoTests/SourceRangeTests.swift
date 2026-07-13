import XCTest
@testable import Octavo
@testable import OctavoAdapters

/// OS-U3 — Source range math. FileSource over a temp file (exact byte windows),
/// HttpRangeSource header/chunk math, and a HttpRangeSource integration test
/// against a local URLProtocol mock (chunking + reassembly).
final class SourceRangeTests: XCTestCase {

    // MARK: FileSource

    func testFileSourceExactByteWindows() async throws {
        let payload = Data((0..<256).map { UInt8($0) })
        let url = try writeTemp(payload)
        defer { try? FileManager.default.removeItem(at: url) }

        let source = FileSource(url: url)
        let len = try await source.length()
        XCTAssertEqual(len, 256)
        let ct = try await source.contentType()
        XCTAssertNil(ct)

        // Whole-file.
        let all = try await source.readAll()
        XCTAssertEqual(all, payload)
        // Sub-window [10, 20).
        let mid = try await source.read(range: 10..<20)
        XCTAssertEqual(mid, payload.subdata(in: 10..<20))
        // Tail window clamps at EOF.
        let tail = try await source.read(range: 250..<300)
        XCTAssertEqual(tail, payload.subdata(in: 250..<256))
        // Empty range.
        let empty = try await source.read(range: 5..<5)
        XCTAssertEqual(empty, Data())
    }

    func testFileSourceContentTypeFromExtension() async throws {
        let url = try writeTemp(Data("%PDF".utf8), ext: "pdf")
        defer { try? FileManager.default.removeItem(at: url) }
        let ct = try await FileSource(url: url).contentType()
        XCTAssertEqual(ct, "application/pdf")
    }

    // MARK: HttpRangeSource — pure math

    func testRangeHeaderIsInclusive() {
        XCTAssertEqual(HttpRangeSource.rangeHeader(for: 0..<1), "bytes=0-0")
        XCTAssertEqual(HttpRangeSource.rangeHeader(for: 0..<1024), "bytes=0-1023")
        XCTAssertEqual(HttpRangeSource.rangeHeader(for: 100..<200), "bytes=100-199")
    }

    func testChunkRangesHonor1MBChunking() {
        let oneMB = HttpRangeSource.defaultChunkSize
        let chunks = HttpRangeSource.chunkRanges(for: 0..<(oneMB * 2 + 5),
                                                 chunkSize: oneMB)
        XCTAssertEqual(chunks.count, 3)
        XCTAssertEqual(chunks[0], 0..<oneMB)
        XCTAssertEqual(chunks[1], oneMB..<(oneMB * 2))
        XCTAssertEqual(chunks[2], (oneMB * 2)..<(oneMB * 2 + 5))
        // Reassembled coverage is exact and contiguous.
        XCTAssertEqual(chunks.first?.lowerBound, 0)
        XCTAssertEqual(chunks.last?.upperBound, oneMB * 2 + 5)
    }

    func testChunkRangesEmpty() {
        XCTAssertTrue(HttpRangeSource.chunkRanges(for: 7..<7, chunkSize: 10).isEmpty)
    }

    func testParseContentRangeTotal() {
        XCTAssertEqual(
            HttpRangeSource.totalLength(fromContentRange: "bytes 0-0/4096"), 4096)
        XCTAssertNil(
            HttpRangeSource.totalLength(fromContentRange: "bytes 0-0/*"))
    }

    // MARK: HttpRangeSource — integration over a URLProtocol mock

    func testHttpRangeSourceReassemblesAcrossChunks() async throws {
        let payload = Data((0..<4096).map { UInt8($0 & 0xFF) })
        MockRangeURLProtocol.payload = payload
        MockRangeURLProtocol.requestedRanges = []

        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockRangeURLProtocol.self]
        let session = URLSession(configuration: config)

        let source = HttpRangeSource(
            url: URL(string: "https://example.test/holding/1/file")!,
            chunkSize: 1024,
            session: session
        )

        // length via Content-Range probe (mock serves no Content-Length on HEAD).
        let len = try await source.length()
        XCTAssertEqual(len, 4096)

        // Full read split into 4 chunks of 1024.
        let data = try await source.read(range: 0..<4096)
        XCTAssertEqual(data, payload)
        XCTAssertEqual(MockRangeURLProtocol.requestedRanges.suffix(4),
                       ["bytes=0-1023", "bytes=1024-2047",
                        "bytes=2048-3071", "bytes=3072-4095"])

        // Partial window across a chunk boundary reassembles exactly.
        let mid = try await source.read(range: 1000..<2050)
        XCTAssertEqual(mid, payload.subdata(in: 1000..<2050))
    }

    // MARK: helpers

    private func writeTemp(_ data: Data, ext: String = "bin") throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension(ext)
        try data.write(to: url)
        return url
    }
}

/// A URLProtocol that serves a fixed payload honoring `Range:` requests, with a
/// `Content-Range` total — enough to exercise HttpRangeSource end-to-end.
final class MockRangeURLProtocol: URLProtocol {
    nonisolated(unsafe) static var payload = Data()
    nonisolated(unsafe) static var requestedRanges: [String] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let total = Self.payload.count
        let rangeHeader = request.value(forHTTPHeaderField: "Range")

        if let rangeHeader {
            Self.requestedRanges.append(rangeHeader)
            // Parse "bytes=start-end" (inclusive).
            let spec = rangeHeader.replacingOccurrences(of: "bytes=", with: "")
            let parts = spec.split(separator: "-", omittingEmptySubsequences: false)
            let start = Int(parts.first ?? "") ?? 0
            let end = Int(parts.count > 1 ? parts[1] : "") ?? (total - 1)
            let clampedEnd = min(end, total - 1)
            let slice = Self.payload.subdata(in: start..<(clampedEnd + 1))

            let headers = [
                "Content-Range": "bytes \(start)-\(clampedEnd)/\(total)",
                "Content-Length": "\(slice.count)",
            ]
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 206,
                httpVersion: "HTTP/1.1", headerFields: headers)!
            client?.urlProtocol(self, didReceive: response,
                                cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: slice)
        } else {
            // Whole-resource GET / HEAD.
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Length": "\(total)"])!
            client?.urlProtocol(self, didReceive: response,
                                cacheStoragePolicy: .notAllowed)
            if request.httpMethod != "HEAD" {
                client?.urlProtocol(self, didLoad: Self.payload)
            }
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
