import XCTest
@testable import CatalogueReader
import Postilla
import Octavo
import CatalogueReaderWire

/// Stage 1 of the unified refresh layer: the coordinator turns the stores' (already existing) background
/// reconcile into an **awaited** call plus observable `phase`/`generation`, so a landed cross-device
/// change surfaces immediately instead of on the next panel open (the "open Contents three times" bug).
///
/// Three layers, per the testing rule:
/// - **Unit** — the coordinator's phase/generation contract and its coalescing, over a fake resource.
/// - **System** — the real `RemoteReconcilable` seam on `LocalBookmarkStore` folds a server bookmark in.
/// - **E2e** — two stores sharing one server: device A adds a bookmark, device B sees it in ONE pass
///   after `coordinator.refresh` (whereas a bare `pull` returns the stale/empty snapshot first).
final class ReaderSyncCoordinatorTests: XCTestCase {

    // MARK: Fakes

    /// A reconcilable that just counts calls and can dwell, so a second refresh overlaps the first.
    private actor CountingResource: RemoteReconcilable {
        private(set) var calls = 0
        private let dwell: UInt64
        init(dwell: UInt64 = 0) { self.dwell = dwell }
        func reconcileNow(publicationId: String) async {
            calls += 1
            if dwell > 0 { try? await Task.sleep(nanoseconds: dwell) }
        }
        func callCount() -> Int { calls }
    }

    /// A stand-in remote `BookmarkStore` = "the server", shared by both device stores in the e2e test.
    private actor Server: BookmarkStore {
        private var rows: [String: Bookmark] = [:]
        func pull(publicationId: String, since rev: Int) async throws -> BookmarkPullResult {
            let ops = Array(rows.values)
            return BookmarkPullResult(rev: ops.map(\.rev).max() ?? 0, ops: ops)
        }
        func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult {
            for o in ops { rows[o.id.uuidString] = o }
            return PushResult(rev: ops.map(\.rev).max() ?? 0, applied: ops.map(\.id))
        }
    }

    private let pub = "holding:771"
    private func tempFile() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("rsc-\(UUID().uuidString).json")
    }
    private func bookmark(_ id: UUID) -> Bookmark {
        Bookmark(id: id, publicationId: pub,
                 locator: Locator(publicationId: pub, format: .pdf, locations: .init(page: 42)),
                 fraction: 0.9, label: "spot",
                 createdAt: Date(timeIntervalSince1970: 1), updatedAt: Date(timeIntervalSince1970: 1), rev: 1)
    }

    // MARK: Unit — phase / generation / coalescing

    @MainActor
    func testRefreshDrivesPhaseAndBumpsGeneration() async {
        let r1 = CountingResource(), r2 = CountingResource()
        let c = ReaderSyncCoordinator(resources: [r1, r2])
        XCTAssertEqual(c.phase, .idle)
        XCTAssertEqual(c.generation, 0)

        await c.refresh(pubId: pub)

        XCTAssertEqual(c.phase, .synced, "refresh ends in .synced")
        XCTAssertEqual(c.generation, 1, "one completed refresh bumps generation once")
        let n1 = await r1.callCount(), n2 = await r2.callCount()
        XCTAssertEqual([n1, n2], [1, 1], "every resource reconciled exactly once")

        await c.refresh(pubId: pub)
        XCTAssertEqual(c.generation, 2, "a second sequential refresh bumps again")
    }

    @MainActor
    func testEmptyResourcesIsANoOp() async {
        let c = ReaderSyncCoordinator(resources: [])
        await c.refresh(pubId: pub)
        XCTAssertEqual(c.phase, .idle, "nothing to reconcile → never enters .refreshing")
        XCTAssertEqual(c.generation, 0)
    }

    @MainActor
    func testConcurrentRefreshesCoalesce() async {
        let r = CountingResource(dwell: 60_000_000)   // 60ms so the second call overlaps the first
        let c = ReaderSyncCoordinator(resources: [r])
        // Two MainActor-inheriting tasks: the first sets `inFlight` before it suspends into the reconcile,
        // so the second — scheduled while the first still dwells — must coalesce.
        let t1 = Task { await c.refresh(pubId: pub) }
        let t2 = Task { await c.refresh(pubId: pub) }
        _ = await t1.value; _ = await t2.value
        let calls = await r.callCount()
        XCTAssertEqual(calls, 1, "an overlapping refresh coalesces into the in-flight pass")
        XCTAssertEqual(c.generation, 1, "one completed pass → one generation bump")
    }

    // MARK: Unit — the rev-gate (check → fetch only if changed)

    @MainActor
    func testRevGateSkipsFetchWhenNothingChanged() async {
        let r = CountingResource()
        let cursors = ReaderSyncCursorStore(fileURL: tempFile())
        let revs = HoldingRevs(bookmarks: 3, annotations: 2, outlines: 1)
        await cursors.record(pub, revs)                       // we've already merged these
        let c = ReaderSyncCoordinator(resources: [r],
                                      revCheck: { _ in revs },  // server reports the same revs
                                      cursors: cursors)
        await c.refresh(pubId: pub)
        let calls = await r.callCount()
        XCTAssertEqual(calls, 0, "unchanged → no reconcile / no network pull")
        XCTAssertEqual(c.generation, 0, "unchanged → nothing to repopulate")
        XCTAssertEqual(c.phase, .synced)
    }

    @MainActor
    func testRevGateFetchesAndAdvancesCursorWhenChanged() async {
        let r = CountingResource()
        let cursors = ReaderSyncCursorStore(fileURL: tempFile())   // starts empty (.zero)
        let server = HoldingRevs(bookmarks: 5, annotations: 0, outlines: 2)
        let c = ReaderSyncCoordinator(resources: [r], revCheck: { _ in server }, cursors: cursors)

        await c.refresh(pubId: pub)
        let firstCalls = await r.callCount()
        XCTAssertEqual(firstCalls, 1, "server ahead of our cursor → fetch")
        XCTAssertEqual(c.generation, 1)
        XCTAssertEqual(c.phase, .synced)
        let recorded = await cursors.lastSeen(pub)
        XCTAssertEqual(recorded, server, "cursor advances to exactly what we merged")

        // Second pass, same server revs → now a no-op (proves the cursor was persisted + honored).
        await c.refresh(pubId: pub)
        let secondCalls = await r.callCount()
        XCTAssertEqual(secondCalls, 1, "no further changes → no second reconcile")
        XCTAssertEqual(c.generation, 1)
    }

    @MainActor
    func testFailedProbeAssumesChangedAndFetches() async {
        let r = CountingResource()
        let cursors = ReaderSyncCursorStore(fileURL: tempFile())
        let c = ReaderSyncCoordinator(resources: [r], revCheck: { _ in nil }, cursors: cursors)  // offline probe
        await c.refresh(pubId: pub)
        let calls = await r.callCount()
        XCTAssertEqual(calls, 1, "a failed probe assumes changed → fetch, never go stale")
        XCTAssertEqual(c.generation, 1)
    }

    func testCursorStorePersistsAcrossInstances() async {
        let file = tempFile()
        let s1 = ReaderSyncCursorStore(fileURL: file)
        let seen0 = await s1.lastSeen(pub)
        XCTAssertEqual(seen0, .zero, "never-seen book reads as zero → first open always fetches")
        let revs = HoldingRevs(bookmarks: 1, annotations: 2, outlines: 3)
        await s1.record(pub, revs)
        let s2 = ReaderSyncCursorStore(fileURL: file)          // fresh instance, same file
        let seen1 = await s2.lastSeen(pub)
        XCTAssertEqual(seen1, revs, "cursor survives relaunch")
    }

    // MARK: System — the RemoteReconcilable seam on the real store

    func testReconcileNowSeamFoldsServerBookmarkIntoLocalStore() async throws {
        let server = Server()
        _ = try await server.push(publicationId: pub, ops: [bookmark(UUID())])   // server already has one

        let store = LocalBookmarkStore(fileURL: tempFile(), remote: server)
        let before = try await store.pull(publicationId: pub, since: 0)
        XCTAssertTrue(before.ops.isEmpty, "fresh store starts empty (local-first pull returns local)")

        await (store as any RemoteReconcilable).reconcileNow(publicationId: pub)   // the awaited seam
        let after = try await store.pull(publicationId: pub, since: 0)
        XCTAssertEqual(after.ops.count, 1, "reconcileNow folds the server's bookmark into local")
    }

    // MARK: E2e — one-pass cross-device reveal (the bug this fixes)

    func testCoordinatorRefreshRevealsAnotherDevicesBookmarkInOnePass() async throws {
        let server = Server()
        let id = UUID()

        // Device A adds a bookmark and syncs it to the server.
        let deviceA = LocalBookmarkStore(fileURL: tempFile(), remote: server)
        _ = try await deviceA.push(publicationId: pub, ops: [bookmark(id)])
        await deviceA.reconcileNow(publicationId: pub)   // deterministic flush to the server

        // Device B opens the same book. A bare local-first pull returns the STALE (empty) snapshot — this
        // is exactly the "nothing there on first open" the user hit.
        let deviceB = LocalBookmarkStore(fileURL: tempFile(), remote: server)
        let firstPull = try await deviceB.pull(publicationId: pub, since: 0)
        XCTAssertTrue(firstPull.ops.isEmpty, "reproduces the bug: first open shows nothing")

        // With the coordinator, the reconcile is awaited BEFORE the view reads — so the very next read
        // (device B's bookmark list) has the bookmark. One pass, no reopen.
        let coordinator = await ReaderSyncCoordinator(resources: [deviceB])
        await coordinator.refresh(pubId: pub)
        let revealed = try await deviceB.pull(publicationId: pub, since: 0)
        XCTAssertEqual(revealed.ops.map(\.id), [id], "device B sees A's bookmark in a single pass")

        let phase = await coordinator.phase
        let generation = await coordinator.generation
        XCTAssertEqual(phase, .synced)
        XCTAssertEqual(generation, 1)
    }
}
