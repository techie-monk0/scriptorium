import XCTest
@testable import CatalogueReader
import Postilla
import Octavo

private final class MockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var routes: [String: (Int, Data)] = [:]
    nonisolated(unsafe) static var lastBody: Data?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        if let stream = request.httpBodyStream { Self.lastBody = Data(reading: stream) }
        else { Self.lastBody = request.httpBody }
        let (status, body) = Self.routes[request.url?.path ?? ""] ?? (404, Data())
        let resp = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

private extension Data {
    init(reading stream: InputStream) {
        self.init(); stream.open(); defer { stream.close() }
        let n = 4096; var buf = [UInt8](repeating: 0, count: n)
        while stream.hasBytesAvailable { let r = stream.read(&buf, maxLength: n); if r > 0 { append(buf, count: r) } else { break } }
    }
}

/// Validates the postilla `AnnotationStore` HTTP transport (`/sync/reader`) without a live server. The
/// LWW/merge correctness itself is covered by postilla's PS-U1; this checks pull/push wire decode.
final class ReaderSyncTests: XCTestCase {
    private func makeSync() -> ReaderSync {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockURLProtocol.self]
        return ReaderSync(baseURL: URL(string: "http://mac.local:5000")!, session: URLSession(configuration: cfg))
    }
    override func tearDown() { MockURLProtocol.routes = [:]; MockURLProtocol.lastBody = nil; super.tearDown() }

    private let sampleId = UUID(uuidString: "11111111-1111-1111-1111-111111111111")!

    private func sampleAnnotation() -> Annotation {
        let loc = Locator(publicationId: "holding:7", format: .pdf, locations: .init(page: 3))
        let t = Date(timeIntervalSince1970: 1_700_000_000)   // 2023-11-14T22:13:20Z
        let ink = Ink(strokes: [InkStroke(points: [InkPoint(x: 0.1, y: 0.2, pressure: 0.5, t: 0)],
                                          width: 4, color: "#ff0000", mode: .draw)])
        return Annotation(id: sampleId, publicationId: "holding:7", kind: .highlight, locator: loc,
                          cfiRange: "epubcfi(/6/4!/4)", region: [0.1, 0.2, 0.3, 0.05],
                          color: "#ffd54a", ink: ink, createdAt: t, updatedAt: t, rev: 5)
    }

    /// PULL maps the catalogue's legacy `{rev, annotations:[…]}` record → postilla `Annotation`,
    /// recovering cfiRange/region and inferring the Locator format.
    func testPullMapsLegacyAnnotations() async throws {
        let body = """
        {"rev":5,"bookmarks":[],"annotations":[
          {"id":"\(sampleId.uuidString)","holding_id":7,"kind":"highlight","page":3,
           "rect":"[0.1,0.2,0.3,0.05]","color":"#ffd54a","note_text":"key",
           "created_at":"2023-11-14T22:13:20Z","updated_at":"2023-11-14T22:13:20Z","rev":5}]}
        """
        MockURLProtocol.routes["/sync/reader"] = (200, Data(body.utf8))
        let result = try await makeSync().pull(publicationId: "holding:7", since: 0)
        XCTAssertEqual(result.rev, 5)
        let a = try XCTUnwrap(result.ops.first)
        XCTAssertEqual(a.kind, .highlight)
        XCTAssertEqual(a.locator.locations.page, 3)
        XCTAssertEqual(a.locator.format, .pdf)            // no cfi_range → pdf inferred
        XCTAssertEqual(a.region, [0.1, 0.2, 0.3, 0.05])   // rect JSON-string → [Double]
        XCTAssertEqual(a.noteText, "key")
    }

    /// PUSH emits the legacy `{ops:[{type:"annotation", …snake_case…}]}` with rect/ink/cfi_range
    /// serialised, and decodes the `applied:[{id,rev}]` result.
    func testPushSendsLegacyOpsAndDecodesApplied() async throws {
        MockURLProtocol.routes["/sync/reader"] =
            (200, Data("{\"rev\":6,\"applied\":[{\"id\":\"\(sampleId.uuidString)\",\"rev\":6}]}".utf8))
        let result = try await makeSync().push(publicationId: "holding:7", ops: [sampleAnnotation()])
        XCTAssertEqual(result.rev, 6)
        XCTAssertEqual(result.applied, [sampleId])
        let sent = String(data: MockURLProtocol.lastBody ?? Data(), encoding: .utf8) ?? ""
        XCTAssertTrue(sent.contains("\"type\":\"annotation\""))
        XCTAssertTrue(sent.contains("\"holding_id\":7"))
        XCTAssertTrue(sent.contains("\"page\":3"))
        XCTAssertTrue(sent.contains("highlight"))
        XCTAssertTrue(sent.contains("\"cfi_range\":\"epubcfi(/6/4!/4)\""))
        XCTAssertTrue(sent.contains("\"rect\":"))         // region → JSON string
        XCTAssertTrue(sent.contains("\"ink\":"))          // Ink struct → JSON string
    }

    /// A `{id, skipped}` applied entry (server dropped the op) is not reported accepted.
    func testSkippedOpNotReportedApplied() async throws {
        MockURLProtocol.routes["/sync/reader"] =
            (200, Data("{\"rev\":7,\"applied\":[{\"id\":\"\(sampleId.uuidString)\",\"skipped\":true}]}".utf8))
        let result = try await makeSync().push(publicationId: "holding:7", ops: [sampleAnnotation()])
        XCTAssertEqual(result.applied, [])
    }
}
