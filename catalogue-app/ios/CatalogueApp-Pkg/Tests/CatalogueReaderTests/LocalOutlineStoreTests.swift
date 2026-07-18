import XCTest
@testable import CatalogueReader
import CatalogueReaderWire

/// A stand-in remote `OutlineStore` that can be toggled offline (throws) — models the server. Stores
/// the whole outline per pubId (wholesale, like the server's one-row-per-copy).
private actor MockRemoteOutline: OutlineStore {
    private var online: Bool
    private var server: [String: [OutlineEntry]] = [:]
    private(set) var pushCount = 0

    init(online: Bool) { self.online = online }
    func setOnline(_ b: Bool) { online = b }
    func serverEntries(_ pub: String) -> [OutlineEntry] { server[pub] ?? [] }

    func pull(publicationId: String) async throws -> [OutlineEntry] {
        guard online else { throw URLError(.notConnectedToInternet) }
        return server[publicationId] ?? []
    }
    func push(publicationId: String, entries: [OutlineEntry]) async throws {
        guard online else { throw URLError(.notConnectedToInternet) }
        pushCount += 1
        server[publicationId] = entries
    }
}

/// The client authored-outline outbox: an outline edited offline is durable, survives relaunch, and
/// flushes on reconnect; a read-only probe reports the outbox depth; `pull` is offline-first.
final class LocalOutlineStoreTests: XCTestCase {
    private let pub = "holding:7"
    private func tempFile() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("lo-\(UUID().uuidString).json")
    }
    private let entries = [OutlineEntry(level: 1, title: "Chapter One", page: 1),
                           OutlineEntry(level: 1, title: "Chapter Two", page: 3)]

    func testPushQueuesThenBackgroundFlushReachesServer() async throws {
        let remote = MockRemoteOutline(online: true)
        let store = LocalOutlineStore(fileURL: tempFile(), remote: remote)
        // push is local-first + non-blocking (it flushes in the background so the editor's Save never
        // waits on the network); drive the reconcile directly for a deterministic assertion.
        try await store.push(publicationId: pub, entries: entries)
        await store.reconcileWithRemote(publicationId: pub)
        let onServer = await remote.serverEntries(pub)
        XCTAssertEqual(onServer, entries)
        let pending = await store.pendingWriteCount()
        XCTAssertEqual(pending, 0, "an accepted flush drains the outbox")
    }

    func testOfflineEditSurvivesRelaunchAndFlushesOnReconnect() async throws {
        let file = tempFile()
        let remote = MockRemoteOutline(online: false)          // start offline
        let store = LocalOutlineStore(fileURL: file, remote: remote)

        try await store.push(publicationId: pub, entries: entries)
        let reached0 = await remote.serverEntries(pub)
        XCTAssertTrue(reached0.isEmpty, "offline push must not reach the server")
        let queued = await store.pendingWriteCount()
        XCTAssertEqual(queued, 1, "queued while offline")

        // Relaunch: a fresh instance over the same file still has the edit, still local, still queued.
        let relaunched = LocalOutlineStore(fileURL: file, remote: remote)
        let local = try await relaunched.pull(publicationId: pub)
        XCTAssertEqual(local, entries, "offline outline persists across relaunch")
        let stillQueued = await relaunched.pendingWriteCount()
        XCTAssertEqual(stillQueued, 1)

        // A separate read-only probe over the same file sees the depth (the AppModel path).
        let probeDepth = await LocalOutlineStore(fileURL: file).pendingWriteCount()
        XCTAssertEqual(probeDepth, 1)

        // Reconnect + reconcile → the outbox flushes to the server.
        await remote.setOnline(true)
        await relaunched.reconcileWithRemote(publicationId: pub)
        let onServer = await remote.serverEntries(pub)
        XCTAssertEqual(onServer, entries, "reconnect flushes the offline edit")
        let drained = await relaunched.pendingWriteCount()
        XCTAssertEqual(drained, 0, "outbox drained")
    }

    func testReconcileAdoptsServerOutlineWhenNothingPending() async throws {
        let file = tempFile()
        let remote = MockRemoteOutline(online: true)
        try await remote.push(publicationId: pub, entries: entries)   // another device authored it
        let store = LocalOutlineStore(fileURL: file, remote: remote)

        let before = try await store.pull(publicationId: pub)
        XCTAssertEqual(before, [], "nothing local yet")
        await store.reconcileWithRemote(publicationId: pub)           // background reconcile
        let after = try await store.pull(publicationId: pub)
        XCTAssertEqual(after, entries, "adopts the server's outline")
    }

    func testLocalEditIsNotClobberedByServerWhilePending() async throws {
        let file = tempFile()
        let remote = MockRemoteOutline(online: false)
        let store = LocalOutlineStore(fileURL: file, remote: remote)
        try await store.push(publicationId: pub, entries: entries)    // local edit, offline → pending
        // Reconcile with server online but our edit still pending: our edit flushes and wins; not clobbered.
        await remote.setOnline(true)
        await store.reconcileWithRemote(publicationId: pub)
        let local = try await store.pull(publicationId: pub)
        XCTAssertEqual(local, entries)
        let onServer = await remote.serverEntries(pub)
        XCTAssertEqual(onServer, entries)
    }
}
