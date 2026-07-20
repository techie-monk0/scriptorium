import XCTest
@testable import CatalogueUI
import CatalogueData

/// The built-in default endpoint is injected from the `CatalogueDefaultServer` Info.plist key
/// (git-ignored `App.local.xcconfig` → `CATALOGUE_DEFAULT_SERVER`, mirroring `app_default_server`
/// in `private/local_defaults.json`). `builtinDefault` is the pure resolver behind it.
final class DefaultEndpointTests: XCTestCase {
    /// The real per-machine default server (`app_default_server` in `private/local_defaults.json`),
    /// or nil when that private config isn't present. It is a PERSONAL identifier (a MagicDNS name),
    /// so it must never be hardcoded in this tracked test — `private/` is stripped from the public
    /// mirror, and main's tracked source must carry no personal identifiers (see private/release.config).
    /// We read it at runtime instead, walking up from this source file to the repo root.
    private func configuredDefaultServer() -> String? {
        var dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        for _ in 0..<10 {
            let file = dir.appendingPathComponent("private/local_defaults.json")
            if let data = try? Data(contentsOf: file),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let server = obj["app_default_server"] as? String, !server.isEmpty {
                return server
            }
            dir = dir.deletingLastPathComponent()
        }
        return nil
    }

    /// A scheme-less MagicDNS address (as stored in the xcconfig, which can't hold "//") resolves to
    /// an http:// LAN endpoint — the shape needed to reach a plain-HTTP server over the tailnet. Uses
    /// the actual configured default (read from private/) so no personal address is baked into the test;
    /// skips in a public build where private/local_defaults.json isn't present.
    func testMagicDNSAddressBecomesHTTPLocalNetworkEndpoint() throws {
        guard let configured = configuredDefaultServer() else {
            throw XCTSkip("private/local_defaults.json not present — no configured default server to check")
        }
        // The xcconfig mirrors this value scheme-less (can't hold "//"); mirror that to exercise the
        // scheme-less → http:// promotion the app actually relies on.
        let schemeless = configured
            .replacingOccurrences(of: "http://", with: "")
            .replacingOccurrences(of: "https://", with: "")
        let e = AppModel.builtinDefault(schemeless)
        XCTAssertEqual(e.kind, .localNetwork)
        XCTAssertEqual(e.baseURL.absoluteString, "http://" + schemeless)
    }

    /// Absent (public build: key expands to empty / missing) → loopback fallback.
    func testMissingAndEmptyFallBackToLoopback() {
        for raw in [nil, "", "   "] as [String?] {
            let e = AppModel.builtinDefault(raw)
            XCTAssertEqual(e.kind, .localNetwork)
            XCTAssertEqual(e.baseURL.absoluteString, "http://127.0.0.1:8000",
                           "raw=\(String(describing: raw)) should fall back to loopback")
        }
    }

    /// An explicit https address is still honoured (inferred as a tunnel), proving the resolver
    /// defers to `ServerEndpoints.infer` rather than forcing http.
    func testExplicitHTTPSStaysTunnel() {
        let e = AppModel.builtinDefault("https://library.example")
        XCTAssertEqual(e.kind, .tunnel)
        XCTAssertEqual(e.baseURL.absoluteString, "https://library.example")
    }
}
