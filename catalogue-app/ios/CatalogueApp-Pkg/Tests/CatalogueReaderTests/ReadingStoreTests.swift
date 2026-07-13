import XCTest
@testable import CatalogueReader
import Octavo

/// U8 — the catalogue-app's concrete octavo `ReadingStore`. Persists/restores a `Locator` per
/// publication, orders `recent(n)` by last-touched, and survives a relaunch (reload from disk). (The
/// port *contract* is also covered upstream by octavo's OS-U4 against MemoryReadingStore.)
final class ReadingStoreTests: XCTestCase {
    private func tempDir() -> URL {
        let d = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }
    private func pdfLocator(_ pub: String, page: Int) -> Locator {
        Locator(publicationId: pub, format: .pdf, locations: .init(page: page, progression: Double(page) / 100))
    }

    func testSetGetRoundTrip() async throws {
        let store = CatalogueReadingStore(directory: tempDir())
        let before = try await store.getPosition("holding:7")
        XCTAssertNil(before)
        try await store.setPosition("holding:7", pdfLocator("holding:7", page: 42))
        let got = try await store.getPosition("holding:7")
        XCTAssertEqual(got?.locations.page, 42)
        XCTAssertEqual(got?.format, .pdf)
    }

    func testRecentOrdersByMostRecentlyTouched() async throws {
        let store = CatalogueReadingStore(directory: tempDir())
        try await store.setPosition("a", pdfLocator("a", page: 1))
        try await store.setPosition("b", pdfLocator("b", page: 2))
        try await store.setPosition("c", pdfLocator("c", page: 3))
        try await store.setPosition("a", pdfLocator("a", page: 9))   // touch "a" again → most recent
        let recent = try await store.recent(2)
        XCTAssertEqual(recent.first?.publicationId, "a")
        XCTAssertEqual(recent.first?.locations.page, 9)
        XCTAssertEqual(recent.count, 2)
    }

    func testPersistsAcrossInstances() async throws {
        let dir = tempDir()
        let store = CatalogueReadingStore(directory: dir)
        try await store.setPosition("holding:7", pdfLocator("holding:7", page: 100))
        // a fresh store over the same directory restores the saved position (relaunch)
        let reopened = CatalogueReadingStore(directory: dir)
        let restored = try await reopened.getPosition("holding:7")
        XCTAssertEqual(restored?.locations.page, 100)
    }
}
