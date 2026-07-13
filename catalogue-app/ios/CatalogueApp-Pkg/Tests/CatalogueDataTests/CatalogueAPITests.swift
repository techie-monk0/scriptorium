import XCTest
@testable import CatalogueData
import CatalogueCore

/// A `URLProtocol` that answers each request from a routing table — so the live `DataPort` mapping is
/// tested deterministically with no server (the seeded-server harness is T3/system, deferred here).
final class MockURLProtocol: URLProtocol {
    /// path → (status, headers, body). Set per-test. `nonisolated(unsafe)` is fine: tests run serially.
    nonisolated(unsafe) static var routes: [String: (Int, [String: String], Data)] = [:]
    /// Captured request headers for assertions (e.g. If-None-Match).
    nonisolated(unsafe) static var lastHeaders: [String: String] = [:]

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        let path = request.url?.path ?? ""
        Self.lastHeaders = request.allHTTPHeaderFields ?? [:]
        let (status, headers, body) = Self.routes[path] ?? (404, [:], Data())
        let resp = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

final class CatalogueAPITests: XCTestCase {
    private func makeAPI() -> CatalogueAPI {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockURLProtocol.self]
        return CatalogueAPI(baseURL: URL(string: "http://mac.local:5000")!, session: URLSession(configuration: cfg))
    }
    override func tearDown() { MockURLProtocol.routes = [:]; MockURLProtocol.lastHeaders = [:]; super.tearDown() }

    func testSearchMapsLibraryRowsToCardsLikeWebAdapter() async throws {
        let body = Data(#"""
        {"q":"bodhi","rows":[{"id":42,"title":"Bodhicaryāvatāra","display_title":"Bodhicaryāvatāra",
         "subtitle":"Śāntideva · 1w · reviewed","done":true,"holding_id":7,"has_file":true,"file_ext":"pdf"}]}
        """#.utf8)
        MockURLProtocol.routes["/api/v1/library"] = (200, ["Content-Type": "application/json"], body)
        let cards = try await makeAPI().search("bodhi")
        XCTAssertEqual(cards.count, 1)
        let c = cards[0]
        XCTAssertEqual(c.eid, 42)
        XCTAssertEqual(c.title, "Bodhicaryāvatāra")
        XCTAssertEqual(c.by, "Śāntideva · 1w · reviewed")     // `by` = the row subtitle (web parity)
        XCTAssertEqual(c.coverUrl, "/edition/42/cover.jpg")    // deterministic art handle
        XCTAssertEqual(c.spineUrl, "/edition/42/spine.svg")
        XCTAssertEqual(c.holdingId, 7)
    }

    func testDetailDecodesEdition() async throws {
        let body = Data(#"{"edition_id":42,"title":"t","authors":["a"],"translators":[],"isbns":[],"subjects":[],"work_titles":[],"holdings":[]}"#.utf8)
        MockURLProtocol.routes["/api/v1/edition/42"] = (200, [:], body)
        let row = try await makeAPI().detail(42)
        XCTAssertEqual(row?.editionId, 42)
        XCTAssertEqual(row?.authors, ["a"])
    }

    func testDetail404ReturnsNilNotError() async throws {
        MockURLProtocol.routes["/api/v1/edition/999"] = (404, [:], Data())
        let row = try await makeAPI().detail(999)
        XCTAssertNil(row)
    }

    func testReplica304KeepsCachedCopy() async throws {
        MockURLProtocol.routes["/api/v1/replica"] = (304, ["ETag": "\"abc\""], Data())
        let (rep, etag) = try await makeAPI().replica(ifNoneMatch: "\"abc\"")
        XCTAssertNil(rep)                              // 304 → no body, caller keeps its cache
        XCTAssertEqual(etag, "\"abc\"")
        XCTAssertEqual(MockURLProtocol.lastHeaders["If-None-Match"], "\"abc\"")
    }

    func testReplica200DecodesAndReturnsEtag() async throws {
        let body = Data(#"{"schema_version":3,"count":0,"editions":[]}"#.utf8)
        MockURLProtocol.routes["/api/v1/replica"] = (200, ["ETag": "\"v2\""], body)
        let (rep, etag) = try await makeAPI().replica(ifNoneMatch: nil)
        XCTAssertEqual(rep?.schemaVersion, 3)
        XCTAssertEqual(etag, "\"v2\"")
    }

    func testContentDecodes() async throws {
        let body = Data(#"{"q":"e","books":[{"eid":1,"title":"t","authors":[],"snippets":["s"]}],"available":true}"#.utf8)
        MockURLProtocol.routes["/api/v1/content"] = (200, [:], body)
        let doc = try await makeAPI().content("e")
        XCTAssertTrue(doc.available)
        XCTAssertEqual(doc.books.first?.snippets, ["s"])
    }

    // ── cookie login ──────────────────────────────────────────────────────────
    private func makeAPI(onUnauthorized: (@Sendable () async -> Bool)?) -> CatalogueAPI {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockURLProtocol.self]
        return CatalogueAPI(endpoint: DirectEndpoint(baseURL: URL(string: "http://mac.local:5000")!),
                            session: URLSession(configuration: cfg), onUnauthorized: onUnauthorized)
    }

    func testLoginSucceedsWhenServerAcceptsCredential() async throws {
        // A good credential → 302 to `next` (followed by URLSession) → gated 200. login() must not throw.
        MockURLProtocol.routes["/login"] = (302, ["Location": "/api/v1/health"], Data())
        MockURLProtocol.routes["/api/v1/health"] = (200, [:], Data(#"{"ok":true}"#.utf8))
        try await makeAPI().login(username: "me", password: "pw")
    }

    func testLoginThrows401OnWrongCredential() async throws {
        MockURLProtocol.routes["/login"] = (401, [:], Data("nope".utf8))   // form re-renders 401
        do { try await makeAPI().login(username: "me", password: "bad"); XCTFail("expected 401") }
        catch let e as APIError { XCTAssertEqual(e.status, 401) }
    }

    func testUnauthorizedRequestRetriesAfterReauth() async throws {
        // First hit is 401; the onUnauthorized hook "re-auths" (flips the route to 200) and returns true,
        // so the request retries once and succeeds — the transparent session-refresh path.
        MockURLProtocol.routes["/api/v1/health"] = (401, [:], Data())
        let api = makeAPI(onUnauthorized: {
            MockURLProtocol.routes["/api/v1/health"] = (200, [:], Data(#"{"ok":true}"#.utf8))
            return true
        })
        let h = try await api.health()
        XCTAssertTrue(h.ok)
    }
}
