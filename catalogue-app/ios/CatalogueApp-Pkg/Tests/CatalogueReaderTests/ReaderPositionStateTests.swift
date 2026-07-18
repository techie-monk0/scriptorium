import XCTest
import Octavo
@testable import CatalogueReader

/// Position-domain persistence beyond the current page: Kindle-style "furthest read" (advances forward
/// only) and the per-document back/jump history (bounded, survives reopen).
final class ReaderPositionStateTests: XCTestCase {
    private func tempDir() -> URL {
        let d = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    private func loc(_ progression: Double, page: Int) -> Locator {
        Locator(publicationId: "p", format: .pdf, locations: .init(page: page, progression: progression))
    }

    func testFurthestAdvancesForwardOnly() async throws {
        let store = CatalogueReadingStore(directory: tempDir())
        try await store.setPosition("p", loc(0.2, page: 20))
        var f = try await store.furthest("p")
        XCTAssertEqual(f?.locations.page, 20)

        try await store.setPosition("p", loc(0.5, page: 50))     // moved ahead
        f = try await store.furthest("p")
        XCTAssertEqual(f?.locations.page, 50)

        try await store.setPosition("p", loc(0.3, page: 30))     // scrolled BACK
        f = try await store.furthest("p")
        XCTAssertEqual(f?.locations.page, 50)                    // furthest holds at 50
        let cur = try await store.getPosition("p")
        XCTAssertEqual(cur?.locations.page, 30)                  // current tracks the latest
    }

    func testFurthestPersistsAcrossInstances() async throws {
        let dir = tempDir()
        let store = CatalogueReadingStore(directory: dir)
        try await store.setPosition("p", loc(0.7, page: 70))
        let reopened = CatalogueReadingStore(directory: dir)
        let f = try await reopened.furthest("p")
        XCTAssertEqual(f?.locations.page, 70)
    }

    func testHistoryRoundTripsAndIsBounded() async throws {
        let dir = tempDir()
        let store = ReaderHistoryStore(directory: dir, limit: 3)
        let stack = (1...5).map { loc(Double($0) / 10.0, page: $0 * 10) }
        await store.set("p", stack)

        let got = await store.get("p")
        XCTAssertEqual(got.count, 3)                              // bounded to limit
        XCTAssertEqual(got.map { $0.locations.page }, [30, 40, 50])   // most-recent kept

        let reopened = ReaderHistoryStore(directory: dir, limit: 3)
        let reread = await reopened.get("p")
        XCTAssertEqual(reread.map { $0.locations.page }, [30, 40, 50])  // survives reopen
    }

    func testHistoryIsPerDocument() async throws {
        let store = ReaderHistoryStore(directory: tempDir())
        await store.set("a", [loc(0.1, page: 1)])
        await store.set("b", [loc(0.2, page: 2)])
        let a = await store.get("a"), b = await store.get("b")
        XCTAssertEqual(a.map { $0.locations.page }, [1])
        XCTAssertEqual(b.map { $0.locations.page }, [2])
    }
}
