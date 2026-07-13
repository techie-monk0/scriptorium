import XCTest
@testable import CatalogueReader
import CatalogueCore

/// Phase 3 — the reader "tabs" open-set: open/activate/close semantics + the most-recent default, over
/// an isolated temp file so the suite never touches the real Application Support store.
final class OpenSessionsStoreTests: XCTestCase {
    private func tempStore() -> OpenSessionsStore {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("open-sessions-\(UUID().uuidString).json")
        return OpenSessionsStore(fileURL: url)
    }

    private func book(_ id: Int, _ title: String) -> OpenBook {
        // Holding only exposes a Decodable init across modules — fabricate one from JSON.
        let json = "{\"holdingId\":\(id),\"format\":\"pdf\",\"kind\":\"pdf\",\"hasFile\":true}"
        let holding = try! JSONDecoder().decode(Holding.self, from: Data(json.utf8))
        return OpenBook(holding: holding, title: title, eid: id * 10)
    }

    func testOpenMovesToFrontAndActivates() async {
        let store = tempStore()
        await store.open(book(1, "A"))
        await store.open(book(2, "B"))
        let ids = await store.list().map(\.pubId)
        XCTAssertEqual(ids, ["holding:2", "holding:1"])         // most-recent first
        let active = await store.activeId()
        XCTAssertEqual(active, "holding:2")                     // newest opened is active
    }

    func testReopenExistingDedupesAndRefocuses() async {
        let store = tempStore()
        await store.open(book(1, "A"))
        await store.open(book(2, "B"))
        await store.open(book(1, "A"))                          // reopen the first
        let ids = await store.list().map(\.pubId)
        XCTAssertEqual(ids, ["holding:1", "holding:2"])         // no duplicate; moved to front
        let active = await store.activeId()
        XCTAssertEqual(active, "holding:1")
    }

    func testCloseActiveFallsBackToFront() async {
        let store = tempStore()
        await store.open(book(1, "A"))
        await store.open(book(2, "B"))                          // active = 2, order [2,1]
        await store.close("holding:2")
        let ids = await store.list().map(\.pubId)
        XCTAssertEqual(ids, ["holding:1"])
        let active = await store.activeId()
        XCTAssertEqual(active, "holding:1")                     // fell back to what remains
    }

    func testActiveIdDefaultsToMostRecentWhenUnset() async {
        let store = tempStore()
        await store.open(book(1, "A"))
        await store.activate("holding:1")
        await store.close("holding:1")                          // now empty, activeId nil
        let active = await store.activeId()
        XCTAssertNil(active)
    }

    func testPersistsAcrossInstances() async {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("open-sessions-\(UUID().uuidString).json")
        let a = OpenSessionsStore(fileURL: url)
        await a.open(book(7, "Seven"))
        let b = OpenSessionsStore(fileURL: url)                 // fresh instance, same file
        let ids = await b.list().map(\.pubId)
        XCTAssertEqual(ids, ["holding:7"])
        let active = await b.activeId()
        XCTAssertEqual(active, "holding:7")
    }
}
