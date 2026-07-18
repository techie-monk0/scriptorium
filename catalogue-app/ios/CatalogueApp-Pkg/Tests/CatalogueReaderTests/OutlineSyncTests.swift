import XCTest
@testable import CatalogueReader
import CatalogueReaderWire

private final class OLMockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var routes: [String: (Int, Data)] = [:]
    nonisolated(unsafe) static var lastBody: Data?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var data = Data(); let n = 4096; var buf = [UInt8](repeating: 0, count: n)
            while stream.hasBytesAvailable {
                let r = stream.read(&buf, maxLength: n); if r > 0 { data.append(buf, count: r) } else { break }
            }
            Self.lastBody = data
        } else { Self.lastBody = request.httpBody }
        let (status, body) = Self.routes[request.url?.path ?? ""] ?? (404, Data())
        let resp = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

/// `OutlineSync` speaks the real `/sync/reader` wire for outlines (through a mocked URLSession): a push
/// serialises a `type:"outline"` op with a stable per-copy id + entries JSON; a pull decodes the newest
/// `outlines` record. The server-side round-trip is covered in `tests/system/test_reader_sync.py`.
final class OutlineSyncTests: XCTestCase {
    private func makeSync() -> OutlineSync {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [OLMockURLProtocol.self]
        return OutlineSync(baseURL: URL(string: "http://mac.local:5000")!,
                           session: URLSession(configuration: cfg),
                           now: { Date(timeIntervalSince1970: 1_700_000_000) })
    }
    override func tearDown() { OLMockURLProtocol.routes = [:]; OLMockURLProtocol.lastBody = nil; super.tearDown() }

    func testPushSendsOutlineOpWithStableIdAndEntries() async throws {
        OLMockURLProtocol.routes["/sync/reader"] =
            (200, Data("{\"rev\":4,\"applied\":[{\"id\":\"outline:holding:7\",\"rev\":4}]}".utf8))
        try await makeSync().push(publicationId: "holding:7",
                                  entries: [OutlineEntry(level: 1, title: "Intro", page: 1)])
        let sent = String(data: OLMockURLProtocol.lastBody ?? Data(), encoding: .utf8) ?? ""
        XCTAssertTrue(sent.contains("\"type\":\"outline\""))
        XCTAssertTrue(sent.contains("\"id\":\"outline:holding:7\""))    // stable per-copy id → convergence
        XCTAssertTrue(sent.contains("\"holding_id\":7"))
        XCTAssertTrue(sent.contains("Intro"))
    }

    func testPullReturnsNewestOutlineByRev() async throws {
        // two rows for the holding; the newer rev wins (wholesale)
        let body = """
        {"rev":6,"bookmarks":[],"annotations":[],"outlines":[
          {"id":"outline:holding:7","holding_id":7,"entries":"[{\\"level\\":1,\\"title\\":\\"Old\\",\\"page\\":1}]","rev":3},
          {"id":"outline:holding:7","holding_id":7,"entries":"[{\\"level\\":1,\\"title\\":\\"New\\",\\"page\\":2}]","rev":6}],
         "contract_version":2}
        """
        OLMockURLProtocol.routes["/sync/reader"] = (200, Data(body.utf8))
        let entries = try await makeSync().pull(publicationId: "holding:7")
        XCTAssertEqual(entries, [OutlineEntry(level: 1, title: "New", page: 2)])
    }

    func testPullEmptyWhenNoOutlines() async throws {
        OLMockURLProtocol.routes["/sync/reader"] =
            (200, Data("{\"rev\":0,\"bookmarks\":[],\"annotations\":[],\"outlines\":[],\"contract_version\":2}".utf8))
        let entries = try await makeSync().pull(publicationId: "holding:7")
        XCTAssertEqual(entries, [])
    }
}
