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

        // Reconnect + pull → the outbox flushes.
        await remote.setOnline(true)
        _ = try await relaunched.pull(publicationId: pub, since: 0)
        let reached1 = await remote.serverIds()
        XCTAssertTrue(reached1.contains(id.uuidString), "reconnect flushes the offline mark to the server")

        // A subsequent pull does not re-push (outbox drained).
        let before = await remote.pushCount
        _ = try await relaunched.pull(publicationId: pub, since: 0)
        let after = await remote.pushCount
        XCTAssertEqual(before, after, "a drained outbox doesn't re-push")
    }

    func testOnlinePushReachesServerImmediately() async throws {
        let remote = MockRemoteStore(online: true)
        let store = LocalAnnotationStore(fileURL: tempFile(), remote: remote)
        let id = UUID()
        _ = try await store.push(publicationId: pub, ops: [highlight(id)])
        let reached = await remote.serverIds()
        XCTAssertTrue(reached.contains(id.uuidString))
    }
}
