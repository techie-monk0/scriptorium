import XCTest
@testable import Octavo
@testable import OctavoAdapters

/// OS-U4 — ReadingStore conformance (contract test over MemoryReadingStore).
final class ReadingStoreTests: XCTestCase {

    private func loc(_ id: String, page: Int) -> Locator {
        Locator(publicationId: id, format: .pdf, locations: .init(page: page))
    }

    func testSetGetRoundTrip() async throws {
        let store = MemoryReadingStore()
        let l = loc("pub-1", page: 12)
        try await store.setPosition("pub-1", l)
        let got = try await store.getPosition("pub-1")
        XCTAssertEqual(got, l)
    }

    func testMissingRecordTolerated() async throws {
        let store = MemoryReadingStore()
        let got = try await store.getPosition("never-seen")
        XCTAssertNil(got)
    }

    func testSetOverwrites() async throws {
        let store = MemoryReadingStore()
        try await store.setPosition("pub-1", loc("pub-1", page: 1))
        try await store.setPosition("pub-1", loc("pub-1", page: 99))
        let got = try await store.getPosition("pub-1")
        XCTAssertEqual(got?.locations.page, 99)
    }

    func testRecentOrdersByRecency() async throws {
        let store = MemoryReadingStore()
        try await store.setPosition("a", loc("a", page: 1))
        try await store.setPosition("b", loc("b", page: 1))
        try await store.setPosition("c", loc("c", page: 1))
        // Re-touch "a" so it becomes most-recent.
        try await store.setPosition("a", loc("a", page: 2))

        let recent = try await store.recent(3)
        XCTAssertEqual(recent.map(\.publicationId), ["a", "c", "b"])
        XCTAssertEqual(recent.first?.locations.page, 2)
    }

    func testRecentRespectsLimit() async throws {
        let store = MemoryReadingStore()
        for i in 0..<5 { try await store.setPosition("p\(i)", loc("p\(i)", page: i)) }
        let two = try await store.recent(2)
        let zero = try await store.recent(0)
        XCTAssertEqual(two.count, 2)
        XCTAssertEqual(zero.count, 0)
    }
}
