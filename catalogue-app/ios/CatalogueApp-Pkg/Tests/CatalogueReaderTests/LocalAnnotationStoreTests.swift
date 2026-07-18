import XCTest
@testable import CatalogueReader
import Postilla
import Octavo

/// A stand-in remote `AnnotationStore` that can be toggled offline (throws) — models the server.
private actor MockRemoteStore: AnnotationStore {
    private var online: Bool
    private var server: [String: Annotation] = [:]
    private(set) var pushCount = 0

    init(online: Bool) { self.online = online }
    func setOnline(_ b: Bool) { online = b }
    func serverIds() -> Set<String> { Set(server.keys) }

    func pull(publicationId: String, since rev: Int) async throws -> PullResult {
        guard online else { throw URLError(.notConnectedToInternet) }
        let ops = Array(server.values)
        return PullResult(rev: ops.map(\.rev).max() ?? 0, ops: ops)
    }

    func push(publicationId: String, ops: [Annotation]) async throws -> PushResult {
        guard online else { throw URLError(.notConnectedToInternet) }
        pushCount += 1
        for o in ops { server[o.id.uuidString] = o }
        return PushResult(rev: ops.map(\.rev).max() ?? 0, applied: ops.map { $0.id })
    }
}

/// PS-U5 (host side): a mark made offline is durable, survives relaunch, and flushes on reconnect.
final class LocalAnnotationStoreTests: XCTestCase {
    private let pub = "holding:7"

    private func tempFile() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("la-\(UUID().uuidString).json")
    }
    private func highlight(_ id: UUID) -> Annotation {
        Annotation(id: id, publicationId: pub, kind: .highlight,
                   locator: Locator(publicationId: pub, format: .pdf, locations: .init(page: 1)),
                   quads: [[0.1, 0.2, 0.3, 0.04]], color: "#ffd54a",
                   createdAt: Date(timeIntervalSince1970: 1), updatedAt: Date(timeIntervalSince1970: 1), rev: 1)
    }

    func testOfflineMarkSurvivesRelaunchAndFlushesOnReconnect() async throws {
        let file = tempFile()
        let remote = MockRemoteStore(online: false)                 // start offline
        let id = UUID()

        // Draw offline.
        let store = LocalAnnotationStore(fileURL: file, remote: remote)
        _ = try await store.push(publicationId: pub, ops: [highlight(id)])
        let reached0 = await remote.serverIds()
        XCTAssertTrue(reached0.isEmpty, "offline push must not reach the server")

        // Relaunch (fresh instance, same file) — the mark is still there, still offline.
        let relaunched = LocalAnnotationStore(fileURL: file, remote: remote)
        let local = try await relaunched.pull(publicationId: pub, since: 0)
        XCTAssertEqual(local.ops.map { $0.id }, [id], "offline mark persists across relaunch")

        // Reconnect + reconcile → the outbox flushes. (`pull` now kicks reconcile in the background so a
        // slow server never blocks the render; drive it directly here for a deterministic assertion.)
        await remote.setOnline(true)
        await relaunched.reconcileWithRemote(publicationId: pub)
        let reached1 = await remote.serverIds()
        XCTAssertTrue(reached1.contains(id.uuidString), "reconnect flushes the offline mark to the server")

        // A subsequent reconcile does not re-push (outbox drained).
        let before = await remote.pushCount
        await relaunched.reconcileWithRemote(publicationId: pub)
        let after = await remote.pushCount
        XCTAssertEqual(before, after, "a drained outbox doesn't re-push")
    }

    /// A remote whose calls hang — proves neither `push` nor `pull` ever awaits the network.
    private actor HangingRemote: AnnotationStore {
        func pull(publicationId: String, since rev: Int) async throws -> PullResult {
            try? await Task.sleep(nanoseconds: 60_000_000_000); return PullResult(rev: 0, ops: [])
        }
        func push(publicationId: String, ops: [Annotation]) async throws -> PushResult {
            try? await Task.sleep(nanoseconds: 60_000_000_000); return PushResult(rev: 0, applied: [])
        }
    }

    /// The root fix: an annotation op (add/erase/undo all call `push`) must return promptly off a durable
    /// LOCAL write, never blocking the reader on a slow/timing-out server — the bug that stalled the
    /// optimistic repaint for tens of seconds. (Would take 60s if `push`/`pull` awaited the remote.)
    func testPushAndPullNeverBlockOnSlowRemote() async throws {
        let store = LocalAnnotationStore(fileURL: tempFile(), remote: HangingRemote())
        let id = UUID()
        _ = try await store.push(publicationId: pub, ops: [highlight(id)])
        let local = try await store.pull(publicationId: pub, since: 0)
        XCTAssertEqual(local.ops.map { $0.id }, [id], "op persists + reads locally without waiting for the server")
    }

    func testOnlinePushQueuesLocallyAndFlushesToServer() async throws {
        let remote = MockRemoteStore(online: true)
        let store = LocalAnnotationStore(fileURL: tempFile(), remote: remote)
        let id = UUID()
        // Push returns after the LOCAL, durable write (it never blocks on the network); the server flush
        // runs in the background — drive it deterministically here.
        _ = try await store.push(publicationId: pub, ops: [highlight(id)])
        await store.reconcileWithRemote(publicationId: pub)
        let reached = await remote.serverIds()
        XCTAssertTrue(reached.contains(id.uuidString))
    }

    /// The `OutboxProbe` surface the freshness chip reads: pending depth counts queued-offline ops and
    /// drops to zero once they flush. A SEPARATE probe instance over the same file (the composition
    /// root's read-only probe, distinct from the reader's writer) must see the same count.
    func testPendingWriteCountReflectsOutboxAndProbeSeesIt() async throws {
        let file = tempFile()
        let remote = MockRemoteStore(online: false)                 // offline → ops queue
        let store = LocalAnnotationStore(fileURL: file, remote: remote)

        _ = try await store.push(publicationId: pub, ops: [highlight(UUID()), highlight(UUID())])
        let queued = await store.pendingWriteCount()
        XCTAssertEqual(queued, 2, "two offline marks are queued in the outbox")

        // A fresh, remote-less probe over the same file (what AppModel owns) reads the depth off disk.
        let probe = LocalAnnotationStore(fileURL: file)
        let seen = await probe.pendingWriteCount()
        XCTAssertEqual(seen, 2, "a separate probe instance sees the same outbox depth from disk")

        // Reconnect + reconcile flushes the outbox → depth returns to zero.
        await remote.setOnline(true)
        await store.reconcileWithRemote(publicationId: pub)
        let drained = await store.pendingWriteCount()
        XCTAssertEqual(drained, 0, "flushed outbox reports zero pending")
        let seenAfter = await LocalAnnotationStore(fileURL: file).pendingWriteCount()
        XCTAssertEqual(seenAfter, 0, "the probe reflects the drained outbox too")
    }
}
