import XCTest
@testable import CatalogueReader
import Postilla
import Octavo

/// A stand-in remote `BookmarkStore` that can be toggled offline (throws) — models the server.
private actor MockRemoteBookmarks: BookmarkStore {
    private var online: Bool
    private var server: [String: Bookmark] = [:]
    private(set) var pushCount = 0

    init(online: Bool) { self.online = online }
    func setOnline(_ b: Bool) { online = b }
    func serverIds() -> Set<String> { Set(server.keys) }

    func pull(publicationId: String, since rev: Int) async throws -> BookmarkPullResult {
        guard online else { throw URLError(.notConnectedToInternet) }
        let ops = Array(server.values)
        return BookmarkPullResult(rev: ops.map(\.rev).max() ?? 0, ops: ops)
    }

    func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult {
        guard online else { throw URLError(.notConnectedToInternet) }
        pushCount += 1
        for o in ops { server[o.id.uuidString] = o }
        return PushResult(rev: ops.map(\.rev).max() ?? 0, applied: ops.map(\.id))
    }
}

/// Bookmarks now get the same durable-outbox treatment as annotations: made offline → survive relaunch →
/// flush on reconnect; a read-only probe reports the outbox depth; the legacy on-disk format still loads.
final class LocalBookmarkStoreTests: XCTestCase {
    private let pub = "holding:7"

    private func tempFile() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("lb-\(UUID().uuidString).json")
    }
    private func bookmark(_ id: UUID) -> Bookmark {
        Bookmark(id: id, publicationId: pub,
                 locator: Locator(publicationId: pub, format: .pdf, locations: .init(page: 12)),
                 fraction: 0.3, label: "spot",
                 createdAt: Date(timeIntervalSince1970: 1), updatedAt: Date(timeIntervalSince1970: 1), rev: 1)
    }

    func testOfflineBookmarkSurvivesRelaunchAndFlushesOnReconnect() async throws {
        let file = tempFile()
        let remote = MockRemoteBookmarks(online: false)             // start offline
        let id = UUID()

        let store = LocalBookmarkStore(fileURL: file, remote: remote)
        _ = try await store.push(publicationId: pub, ops: [bookmark(id)])
        let reached0 = await remote.serverIds()
        XCTAssertTrue(reached0.isEmpty, "offline push must not reach the server")

        // Relaunch (fresh instance, same file) — the bookmark and its outbox entry persist.
        let relaunched = LocalBookmarkStore(fileURL: file, remote: remote)
        let local = try await relaunched.pull(publicationId: pub, since: 0)
        XCTAssertEqual(local.ops.map(\.id), [id], "offline bookmark persists across relaunch")
        let queued = await relaunched.pendingWriteCount()
        XCTAssertEqual(queued, 1, "still queued while offline")

        // Reconnect + reconcile → the outbox flushes. (`pull` now kicks reconcile in the background so a
        // slow server never blocks opening the bookmark list; drive it directly here for determinism.)
        await remote.setOnline(true)
        await relaunched.reconcileWithRemote(publicationId: pub)
        let reached1 = await remote.serverIds()
        XCTAssertTrue(reached1.contains(id.uuidString), "reconnect flushes the offline bookmark")
        let drained = await relaunched.pendingWriteCount()
        XCTAssertEqual(drained, 0, "outbox drains after flush")

        // A subsequent reconcile does not re-push.
        let before = await remote.pushCount
        await relaunched.reconcileWithRemote(publicationId: pub)
        let after = await remote.pushCount
        XCTAssertEqual(before, after, "a drained outbox doesn't re-push")
    }

    /// A `bookmarks.json` written by the pre-outbox store (`[pubId: [uuid: Bookmark]]`) must still load,
    /// so upgrading doesn't drop existing bookmarks.
    func testLegacyFormatMigrates() async throws {
        let file = tempFile()
        let id = UUID()
        let legacy: [String: [String: Bookmark]] = [pub: [id.uuidString: bookmark(id)]]
        try JSONEncoder().encode(legacy).write(to: file)

        let store = LocalBookmarkStore(fileURL: file)               // no remote
        let loaded = try await store.pull(publicationId: pub, since: 0)
        XCTAssertEqual(loaded.ops.map(\.id), [id], "legacy bookmarks survive the format upgrade")
        let pending = await store.pendingWriteCount()
        XCTAssertEqual(pending, 0, "migrated bookmarks aren't spuriously re-queued")
    }
}
