import XCTest
import Octavo
@testable import Postilla

/// PS-U1 — Sync LWW merge (the correctness core).
final class SyncMergeTests: XCTestCase {

    /// Divergent streams, same UUID, different `updatedAt` → last write wins.
    func testLastWriteWins() {
        let early = Fix.highlight(1, updatedAt: 100, color: "#aaa")
        let late = Fix.highlight(1, updatedAt: 200, color: "#bbb")

        // Either order of arrival yields the later write (commutative).
        let a = AnnotationMerge.merge(into: [:], ops: [early, late])
        let b = AnnotationMerge.merge(into: [:], ops: [late, early])
        XCTAssertEqual(a[Fix.uuid(1)]?.color, "#bbb")
        XCTAssertEqual(b[Fix.uuid(1)]?.color, "#bbb")
        XCTAssertEqual(a, b)
    }

    /// A tombstone removes a mark from the live view.
    func testTombstoneRemoves() {
        let live = Fix.highlight(1, updatedAt: 100)
        let stone = Fix.highlight(1, updatedAt: 200, deleted: 200)
        let state = AnnotationMerge.merge(into: [:], ops: [live, stone])

        XCTAssertTrue(state[Fix.uuid(1)]?.isTombstone == true) // kept as tombstone
        XCTAssertTrue(AnnotationMerge.live(state).isEmpty)     // gone from live view
    }

    /// An older op never resurrects a newer tombstone.
    func testStaleEditCannotResurrectTombstone() {
        let stone = Fix.highlight(1, updatedAt: 200, deleted: 200)
        let staleEdit = Fix.highlight(1, updatedAt: 150, color: "#ccc")
        let state = AnnotationMerge.merge(into: [:], ops: [stone, staleEdit])
        XCTAssertTrue(state[Fix.uuid(1)]?.isTombstone == true)
        XCTAssertTrue(AnnotationMerge.live(state).isEmpty)
    }

    /// Idempotent re-apply is a no-op.
    func testIdempotentReapply() {
        let ops = [
            Fix.highlight(1, updatedAt: 100),
            Fix.highlight(2, updatedAt: 100),
        ]
        let once = AnnotationMerge.merge(into: [:], ops: ops)
        let twice = AnnotationMerge.merge(into: once, ops: ops)
        let thrice = AnnotationMerge.merge(into: twice, ops: ops + ops)
        XCTAssertEqual(once, twice)
        XCTAssertEqual(twice, thrice)
    }

    /// `pull(since:)` returns only newer rows, including tombstones, scoped.
    func testPullSinceReturnsOnlyNewerInclTombstones() async throws {
        let store = InMemoryAnnotationStore()

        // rev 1
        _ = try await store.push(publicationId: Fix.pub, ops: [Fix.highlight(1, updatedAt: 100)])
        let afterFirst = try await store.pull(publicationId: Fix.pub, since: 0)
        let revAfterFirst = afterFirst.rev

        // rev 2: a tombstone for a different id
        _ = try await store.push(
            publicationId: Fix.pub,
            ops: [Fix.highlight(2, updatedAt: 200, deleted: 200)]
        )

        // Pull since the first rev → only the newer (tombstone) row.
        let delta = try await store.pull(publicationId: Fix.pub, since: revAfterFirst)
        XCTAssertEqual(delta.ops.count, 1)
        XCTAssertEqual(delta.ops.first?.id, Fix.uuid(2))
        XCTAssertTrue(delta.ops.first?.isTombstone == true)

        // Pull since 0 → both rows.
        let full = try await store.pull(publicationId: Fix.pub, since: 0)
        XCTAssertEqual(full.ops.count, 2)
    }

    /// Publication scoping: opening one book does not pull another's marks.
    func testPublicationScopedPull() async throws {
        let store = InMemoryAnnotationStore()
        _ = try await store.push(publicationId: "book-A", ops: [Fix.highlight(1, updatedAt: 100, pub: "book-A")])
        _ = try await store.push(publicationId: "book-B", ops: [Fix.highlight(2, updatedAt: 100, pub: "book-B")])

        let a = try await store.pull(publicationId: "book-A", since: 0)
        XCTAssertEqual(a.ops.map(\.id), [Fix.uuid(1)])
    }
}
