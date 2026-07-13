import XCTest
import ReaderContract
@testable import Postilla

/// Bookmark sync — LWW source-of-record contract (sibling of the annotation sync tests).
final class BookmarkStoreTests: XCTestCase {

    private func bm(_ n: Int, updatedAt: TimeInterval, rev: Int = 0,
                    deleted: TimeInterval? = nil, label: String = "spot") -> Bookmark {
        Bookmark(id: Fix.uuid(n), publicationId: Fix.pub,
                 locator: Fix.locator(), fraction: 0.5, label: label,
                 createdAt: Fix.date(0), updatedAt: Fix.date(updatedAt),
                 deletedAt: deleted.map(Fix.date), rev: rev)
    }

    func testPushPullRoundTrip() async throws {
        let store = InMemoryBookmarkStore()
        let r = try await store.push(publicationId: Fix.pub, ops: [bm(1, updatedAt: 10)])
        XCTAssertEqual(r.applied, [Fix.uuid(1)])
        let pulled = try await store.pull(publicationId: Fix.pub, since: 0)
        XCTAssertEqual(pulled.ops.map(\.id), [Fix.uuid(1)])
        XCTAssertEqual(pulled.ops.first?.label, "spot")
        XCTAssertEqual(pulled.ops.first?.fraction, 0.5)
    }

    func testLastWriteWinsAndTombstoneReturned() async throws {
        let store = InMemoryBookmarkStore()
        _ = try await store.push(publicationId: Fix.pub, ops: [bm(1, updatedAt: 10, label: "old")])
        _ = try await store.push(publicationId: Fix.pub, ops: [bm(1, updatedAt: 20, label: "new")])
        _ = try await store.push(publicationId: Fix.pub, ops: [bm(1, updatedAt: 5, label: "stale")]) // rejected
        let pulled = try await store.pull(publicationId: Fix.pub, since: 0)
        XCTAssertEqual(pulled.ops.first?.label, "new")

        _ = try await store.push(publicationId: Fix.pub, ops: [bm(1, updatedAt: 30, deleted: 30)])
        let after = try await store.pull(publicationId: Fix.pub, since: 0)
        XCTAssertEqual(after.ops.first?.isTombstone, true)   // tombstone propagates
    }

    func testMergeOrdering() {
        XCTAssertTrue(BookmarkMerge.wins(bm(1, updatedAt: 20), over: bm(1, updatedAt: 10)))
        XCTAssertFalse(BookmarkMerge.wins(bm(1, updatedAt: 5), over: bm(1, updatedAt: 10)))
    }
}
