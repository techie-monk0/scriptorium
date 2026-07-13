import XCTest
@testable import Postilla

/// PS-U5 — Offline op-queue order / flush / relaunch survival.
final class OpQueueTests: XCTestCase {

    /// Ops created offline queue, then flush in order on reconnect.
    func testQueueFlushesInOrder() async throws {
        let store = InMemoryAnnotationStore()
        let engine = SyncEngine(publicationId: Fix.pub, store: store)

        // Three offline edits (distinct ids, increasing updatedAt).
        await engine.localUpsert(Fix.highlight(1, updatedAt: 10))
        await engine.localUpsert(Fix.highlight(2, updatedAt: 20))
        await engine.localUpsert(Fix.highlight(3, updatedAt: 30))

        let pendingBefore = await engine.pendingOps()
        XCTAssertEqual(pendingBefore.map(\.id), [Fix.uuid(1), Fix.uuid(2), Fix.uuid(3)])

        _ = try await engine.flush()

        // Store received them in queue order.
        let order = await store.appliedOrder
        XCTAssertEqual(order, [Fix.uuid(1), Fix.uuid(2), Fix.uuid(3)])

        // Queue drained; all three are live.
        let pendingAfter = await engine.pendingOps()
        XCTAssertTrue(pendingAfter.isEmpty)
        let live = await engine.annotations
        XCTAssertEqual(live.count, 3)
    }

    /// Ops survive relaunch: snapshot the queue, rebuild the engine, flush.
    func testQueueSurvivesRelaunch() async throws {
        let store = InMemoryAnnotationStore()

        // Session 1 — go offline, make edits, persist the queue.
        let s1 = SyncEngine(publicationId: Fix.pub, store: store)
        await s1.localUpsert(Fix.highlight(1, updatedAt: 10))
        await s1.localUpsert(Fix.highlight(2, updatedAt: 20))
        let saved = await s1.pendingOps()

        // Session 2 — relaunch with the restored queue, then reconnect/flush.
        let s2 = SyncEngine(publicationId: Fix.pub, store: store, pending: saved)
        _ = try await s2.flush()

        let order = await store.appliedOrder
        XCTAssertEqual(order, [Fix.uuid(1), Fix.uuid(2)])
        let live = await s2.annotations
        XCTAssertEqual(live.count, 2)
    }

    /// A second client pulls the first client's pushed marks (round-trip).
    func testSecondClientPullsMarks() async throws {
        let store = InMemoryAnnotationStore()

        let clientA = SyncEngine(publicationId: Fix.pub, store: store)
        await clientA.localUpsert(Fix.highlight(1, updatedAt: 10, color: "#f00"))
        _ = try await clientA.flush()

        let clientB = SyncEngine(publicationId: Fix.pub, store: store)
        _ = try await clientB.refresh()

        let live = await clientB.annotations
        XCTAssertEqual(live.map(\.id), [Fix.uuid(1)])
        XCTAssertEqual(live.first?.color, "#f00")
    }

    /// A local delete propagates as a tombstone through flush/refresh.
    func testDeletePropagates() async throws {
        let store = InMemoryAnnotationStore()
        let a = SyncEngine(publicationId: Fix.pub, store: store)
        await a.localUpsert(Fix.highlight(1, updatedAt: 10))
        _ = try await a.flush()
        await a.localDelete(id: Fix.uuid(1), at: Fix.date(99))
        _ = try await a.flush()

        let b = SyncEngine(publicationId: Fix.pub, store: store)
        _ = try await b.refresh()
        let live = await b.annotations
        XCTAssertTrue(live.isEmpty)
    }
}
