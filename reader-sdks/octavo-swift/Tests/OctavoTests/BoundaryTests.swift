import XCTest

/// §4 boundary enforcement — the "host-free" guarantee.
/// No catalogue route literal may appear in `Sources/`; those must arrive
/// through an injected `Source`/adapter, never as a constant in the engine.
final class BoundaryTests: XCTestCase {

    private static let forbidden = ["/api/v1", "/holding/", "/sync/reader"]

    func testNoHardcodedHostRoutes() throws {
        let sources = packageRoot().appendingPathComponent("Sources")
        let fm = FileManager.default
        guard let enumerator = fm.enumerator(
            at: sources,
            includingPropertiesForKeys: nil
        ) else {
            return XCTFail("could not enumerate \(sources.path)")
        }

        var scanned = 0
        for case let url as URL in enumerator where url.pathExtension == "swift" {
            scanned += 1
            let text = try String(contentsOf: url, encoding: .utf8)
            for literal in Self.forbidden {
                XCTAssertFalse(
                    text.contains(literal),
                    "Forbidden host route '\(literal)' found in \(url.lastPathComponent)"
                )
            }
        }
        XCTAssertGreaterThan(scanned, 0, "expected to scan source files")
    }

    /// `Sources/.../*.swift` -> package root (… / Tests / OctavoTests / file).
    private func packageRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // OctavoTests/
            .deletingLastPathComponent()   // Tests/
            .deletingLastPathComponent()   // package root
    }
}
