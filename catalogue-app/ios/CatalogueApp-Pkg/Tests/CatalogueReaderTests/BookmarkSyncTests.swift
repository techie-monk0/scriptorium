import XCTest
@testable import CatalogueReader
import Postilla
import Octavo

private final class BMMockURLProtocol: URLProtocol {
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

final class BookmarkSyncTests: XCTestCase {
    private let id = UUID(uuidString: "22222222-2222-2222-2222-222222222222")!

    private func makeSync() -> BookmarkSync {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [BMMockURLProtocol.self]
        return BookmarkSync(baseURL: URL(string: "http://mac.local:5000")!, session: URLSession(configuration: cfg))
    }
    override func tearDown() { BMMockURLProtocol.routes = [:]; BMMockURLProtocol.lastBody = nil; super.tearDown() }

    /// `Locator` ⇄ the legacy opaque `locator` string (page number / CFI).
    func testLocatorStringRoundTrips() {
        let pdf = Locator(publicationId: "holding:7", format: .pdf, locations: .init(page: 42))
        XCTAssertEqual(BookmarkSync.locatorString(pdf), "42")
        XCTAssertEqual(BookmarkSync.locator(from: "42", publicationId: "holding:7")?.locations.page, 42)
        let epub = Locator(publicationId: "holding:7", format: .epub, locations: .init(cfi: "epubcfi(/6/4!/4)"))
        XCTAssertEqual(BookmarkSync.locatorString(epub), "epubcfi(/6/4!/4)")
        XCTAssertEqual(BookmarkSync.locator(from: "epubcfi(/6/4!/4)", publicationId: "holding:7")?.locations.cfi,
                       "epubcfi(/6/4!/4)")
    }

    func testPullMapsLegacyBookmarks() async throws {
        let body = """
        {"rev":3,"annotations":[],"bookmarks":[
          {"id":"\(id.uuidString)","holding_id":7,"locator":"42","fraction":0.5,"label":"spot",
           "created_at":"2026-06-29T10:00:00Z","updated_at":"2026-06-29T10:00:00Z","rev":3}]}
        """
        BMMockURLProtocol.routes["/sync/reader"] = (200, Data(body.utf8))
        let res = try await makeSync().pull(publicationId: "holding:7", since: 0)
        XCTAssertEqual(res.rev, 3)
        let b = try XCTUnwrap(res.ops.first)
        XCTAssertEqual(b.label, "spot")
        XCTAssertEqual(b.fraction, 0.5)
        XCTAssertEqual(b.locator?.locations.page, 42)
    }

    func testPushSendsLegacyBookmarkOp() async throws {
        BMMockURLProtocol.routes["/sync/reader"] =
            (200, Data("{\"rev\":4,\"applied\":[{\"id\":\"\(id.uuidString)\",\"rev\":4}]}".utf8))
        let t = Date(timeIntervalSince1970: 1_700_000_000)
        let bm = Bookmark(id: id, publicationId: "holding:7",
                          locator: Locator(publicationId: "holding:7", format: .pdf, locations: .init(page: 42)),
                          fraction: 0.5, label: "spot", createdAt: t, updatedAt: t, rev: 3)
        let res = try await makeSync().push(publicationId: "holding:7", ops: [bm])
        XCTAssertEqual(res.applied, [id])
        let sent = String(data: BMMockURLProtocol.lastBody ?? Data(), encoding: .utf8) ?? ""
        XCTAssertTrue(sent.contains("\"type\":\"bookmark\""))
        XCTAssertTrue(sent.contains("\"holding_id\":7"))
        XCTAssertTrue(sent.contains("\"locator\":\"42\""))
        XCTAssertTrue(sent.contains("\"label\":\"spot\""))
    }
}
