import Foundation
import Postilla
import Octavo
import CatalogueCore

/// Local-first bookmark store — the durable **offline outbox** for bookmarks, mirroring
/// `LocalAnnotationStore`. Every bookmark is persisted to a JSON file *before* the network and its id is
/// queued in an outbox, so a bookmark made **offline** survives relaunch and is flushed to the server
/// when it's reachable again (on the next pull). It best-effort mirrors to/from a remote `BookmarkStore`
/// (the server `/sync/reader`, i.e. `BookmarkSync`) with LWW merge (`BookmarkMerge`).
///
/// Previously this persisted bookmarks locally but only *best-effort* mirrored on each push — a bookmark
/// created offline was never re-pushed on reconnect. Now it self-heals like annotations and position do.
public actor LocalBookmarkStore: BookmarkStore, OutboxProbe {
    private let fileURL: URL
    private let remote: (any BookmarkStore)?
    private var state: State?

    struct State: Codable {
        var bookmarks: [String: [String: Bookmark]] = [:]   // pubId → (uuid → Bookmark)
        var pending: [String: Set<String>] = [:]            // pubId → un-acked uuids (the outbox)
    }

    public init(fileURL: URL? = nil, remote: (any BookmarkStore)? = nil) {
        self.fileURL = fileURL ?? Self.defaultURL()
        self.remote = remote
    }

    private static func defaultURL() -> URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("bookmarks.json")
    }

    private func loaded() -> State {
        if let s = state { return s }
        let data = try? Data(contentsOf: fileURL)
        // New (outbox) format first; fall back to the pre-outbox `[pubId: [uuid: Bookmark]]` shape so an
        // upgrade doesn't drop bookmarks saved by the old store.
        let s: State
        if let data, let decoded = try? JSONDecoder().decode(State.self, from: data) {
            s = decoded
        } else if let data, let legacy = try? JSONDecoder().decode([String: [String: Bookmark]].self, from: data) {
            s = State(bookmarks: legacy, pending: [:])
        } else {
            s = State()
        }
        state = s
        return s
    }

    private func persist(_ s: State) {
        state = s
        if let data = try? JSONEncoder().encode(s) { try? data.write(to: fileURL, options: .atomic) }
    }

    private func merge(_ incoming: [Bookmark], into pub: inout [String: Bookmark]) {
        for b in incoming {
            let key = b.id.uuidString
            if let existing = pub[key] {
                if BookmarkMerge.wins(b, over: existing) { pub[key] = b }
            } else {
                pub[key] = b
            }
        }
    }

    public func pull(publicationId: String, since rev: Int) async throws -> BookmarkPullResult {
        let s = loaded()
        // Return LOCAL bookmarks immediately (so opening the bookmark list never waits on a slow server);
        // reconcile the server in the background. Mirrors `LocalAnnotationStore`.
        if remote != nil {
            Task { await self.reconcileWithRemote(publicationId: publicationId) }
        }
        let ops = Array((s.bookmarks[publicationId] ?? [:]).values)
        return BookmarkPullResult(rev: ops.map(\.rev).max() ?? 0, ops: ops)
    }

    public func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult {
        var s = loaded()
        var pub = s.bookmarks[publicationId] ?? [:]
        merge(ops, into: &pub)
        s.bookmarks[publicationId] = pub
        s.pending[publicationId, default: []].formUnion(ops.map { $0.id.uuidString })
        persist(s)                                          // durable + queued BEFORE the network
        if remote != nil {                                  // flush in the background — never block the caller
            Task { await self.reconcileWithRemote(publicationId: publicationId) }
        }
        let live = s.bookmarks[publicationId] ?? [:]
        return PushResult(rev: live.values.map(\.rev).max() ?? 0, applied: ops.map(\.id))
    }

    /// Background reconcile: fold in the server's bookmarks and flush the outbox. Never blocks a
    /// `pull`/`push`; internal so a test can drive it deterministically.
    func reconcileWithRemote(publicationId: String) async {
        var s = loaded()
        var pub = s.bookmarks[publicationId] ?? [:]
        if let remote, let r = try? await remote.pull(publicationId: publicationId, since: 0) {
            merge(r.ops, into: &pub); s.bookmarks[publicationId] = pub; persist(s)
        }
        _ = await flush(publicationId: publicationId, s)
    }

    /// Push the publication's outbox to the server (idempotent LWW) and drop the ids it accepts. A
    /// failure (offline / server down) is swallowed — the ids stay queued and retry on the next pull.
    private func flush(publicationId: String, _ state: State) async -> State {
        guard let remote,
              let ids = state.pending[publicationId], !ids.isEmpty,
              let pub = state.bookmarks[publicationId] else { return state }
        let ops = ids.compactMap { pub[$0] }
        guard let result = try? await remote.push(publicationId: publicationId, ops: ops) else { return state }
        var s = state
        s.pending[publicationId]?.subtract(result.applied.map { $0.uuidString })
        if s.pending[publicationId]?.isEmpty == true { s.pending[publicationId] = nil }
        persist(s)
        return s
    }

    /// Total un-acked bookmarks across every publication — the outbox depth folded into the freshness
    /// chip. Read **fresh from disk** (not the memo) so a read-only probe instance separate from the
    /// reader's writing instance sees ops the reader just queued (each `push` persists before returning).
    public func pendingWriteCount() -> Int {
        let s = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode(State.self, from: $0) } ?? state ?? State()
        return s.pending.values.reduce(0) { $0 + $1.count }
    }
}
