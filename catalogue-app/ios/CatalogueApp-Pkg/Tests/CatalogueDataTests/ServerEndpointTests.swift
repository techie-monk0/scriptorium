import XCTest
@testable import CatalogueData

/// The abstract server-reachability layer: address normalization, kind inference, descriptor
/// persistence round-trip, and request authorization (tunnel/NAS headers).
final class ServerEndpointTests: XCTestCase {
    func testNormalizeAddsSchemeAndTrimsSlash() {
        XCTAssertEqual(normalizeServerURL("192.168.1.10:8000")?.absoluteString, "http://192.168.1.10:8000")
        XCTAssertEqual(normalizeServerURL("https://library.example/")?.absoluteString, "https://library.example")
        XCTAssertNil(normalizeServerURL(""))
        XCTAssertNil(normalizeServerURL("   "))
    }

    func testInferPicksTunnelForHTTPSElseLAN() {
        XCTAssertEqual(ServerEndpoints.infer(from: "https://library.example")?.kind, .tunnel)
        XCTAssertEqual(ServerEndpoints.infer(from: "192.168.1.10:8000")?.kind, .localNetwork)
        XCTAssertEqual(ServerEndpoints.infer(from: "http://nas.local:8000")?.kind, .localNetwork)
    }

    func testDescriptorRoundTripRebuildsEndpoint() throws {
        let original = TunnelEndpoint(baseURL: URL(string: "https://library.example")!,
                                      headers: ["CF-Access-Client-Id": "abc", "CF-Access-Client-Secret": "xyz"])
        let data = try JSONEncoder().encode(original.descriptor)
        let descriptor = try JSONDecoder().decode(ServerEndpointDescriptor.self, from: data)
        let rebuilt = try XCTUnwrap(descriptor.make())
        XCTAssertEqual(rebuilt.kind, .tunnel)
        XCTAssertEqual(rebuilt.baseURL.absoluteString, "https://library.example")
    }

    func testAuthorizeAddsHeadersForTunnelAndNAS() {
        for endpoint in [
            TunnelEndpoint(baseURL: URL(string: "https://x")!, headers: ["CF-Access-Client-Id": "id"]) as any ServerEndpoint,
            NASEndpoint(baseURL: URL(string: "http://nas")!, headers: ["Authorization": "Bearer t"]),
        ] {
            var req = URLRequest(url: endpoint.baseURL)
            endpoint.authorize(&req)
            XCTAssertFalse(req.allHTTPHeaderFields?.isEmpty ?? true, "\(endpoint.kind) should add headers")
        }
        // LAN adds nothing.
        var req = URLRequest(url: URL(string: "http://lan:8000")!)
        LocalNetworkEndpoint(baseURL: URL(string: "http://lan:8000")!).authorize(&req)
        XCTAssertNil(req.allHTTPHeaderFields?["Authorization"])
    }

    func testCatalogueAPIUsesEndpointBaseURL() {
        let api = CatalogueAPI(endpoint: TunnelEndpoint(baseURL: URL(string: "https://library.example")!))
        XCTAssertEqual(api.baseURL.absoluteString, "https://library.example")
        // the bare-URL convenience wraps a DirectEndpoint
        XCTAssertEqual(CatalogueAPI(baseURL: URL(string: "http://x:1")!).endpoint.kind, .direct)
    }
}
