import XCTest

/// CONSTRAINT GUARD — the SDK ships ports, not transports. No HTTP/URL literals
/// (`/api/v1`, `/sync/reader`) may appear anywhere under `Sources/`; a concrete
/// HTTP `AnnotationStore` is the integrator's, not ours.
final class NoTransportLiteralsTests: XCTestCase {

    /// Walk up from this test file to the package root (the dir holding
    /// `Package.swift`).
    private func packageRoot() -> URL {
        var dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        let fm = FileManager.default
        for _ in 0..<8 {
            if fm.fileExists(atPath: dir.appendingPathComponent("Package.swift").path) {
                return dir
            }
            dir = dir.deletingLastPathComponent()
        }
        return dir
    }

    func testNoTransportLiteralsInSources() throws {
        let sources = packageRoot().appendingPathComponent("Sources")
        let fm = FileManager.default
        guard let walker = fm.enumerator(at: sources, includingPropertiesForKeys: nil) else {
            return XCTFail("Sources/ not found at \(sources.path)")
        }

        let forbidden = ["/api/v1", "/sync/reader"]
        var scanned = 0
        for case let url as URL in walker where url.pathExtension == "swift" {
            scanned += 1
            let text = try String(contentsOf: url, encoding: .utf8)
            for needle in forbidden {
                XCTAssertFalse(
                    text.contains(needle),
                    "Forbidden transport literal \(needle) found in \(url.lastPathComponent)"
                )
            }
        }
        XCTAssertGreaterThan(scanned, 0, "expected to scan some Swift sources")
    }
}
