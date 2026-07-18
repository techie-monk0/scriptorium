import Foundation
import CatalogueCore
import CatalogueReaderWire

/// Local-first authored-outline store — the durable **offline outbox** for a copy's TOC, the outline
/// sibling of `LocalAnnotationStore`/`LocalBookmarkStore`. The whole outline is persisted to a JSON file
/// *before* the network and the copy's id is queued in an outbox, so an outline edited **offline**
/// survives relaunch and flushes on reconnect. Best-effort mirrors to/from a remote `OutlineStore`
/// (`OutlineSync` over `/sync/reader`). Wholesale per copy (LWW), not a per-entry merge.
///
/// Offline-first read: `pull` returns the LOCAL outline immediately and reconciles with the server in
/// the background (flush the outbox, then adopt server truth when nothing local is pending) — matching
/// `LocalAnnotationStore`, so a slow/unreachable server never delays the editor.
public actor LocalOutlineStore: OutlineStore, OutboxProbe {
    private let fileURL: URL
    private let remote: (any OutlineStore)?
    private let now: @Sendable () -> Date
    private var state: State?

    struct State: Codable {
        var outlines: [String: Stored] = [:]   // pubId → the whole outline
        var pending: Set<String> = []          // pubIds with an un-acked local edit (the outbox)
    }
    struct Stored: Codable { var entries: [OutlineEntry]; var updatedAt: Date }

    public init(fileURL: URL? = nil, remote: (any OutlineStore)? = nil,
                now: @escaping @Sendable () -> Date = { Date() }) {
        self.fileURL = fileURL ?? Self.defaultURL()
        self.remote = remote
        self.now = now
    }

    private static func defaultURL() -> URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("outlines.json")
    }

    private func loaded() -> State {
        if let s = state { return s }
        let s = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode(State.self, from: $0) } ?? State()
        state = s
        return s
    }

    private func persist(_ s: State) {
        state = s
        if let data = try? JSONEncoder().encode(s) { try? data.write(to: fileURL, options: .atomic) }
    }

    public func pull(publicationId: String) async throws -> [OutlineEntry] {
        let s = loaded()
        if remote != nil {
            Task { await self.reconcileWithRemote(publicationId: publicationId) }
        }
        return s.outlines[publicationId]?.entries ?? []
    }

    /// Background reconcile: flush the outbox (the server LWW-merges our edit), then — only when nothing
    /// local is still pending — adopt the server's outline as the local copy. Never blocks a `pull`.
    /// Internal (not private) so tests can await it deterministically instead of racing the `Task`.
    func reconcileWithRemote(publicationId: String) async {
        var s = await flush(publicationId: publicationId, loaded())
        guard let remote, !s.pending.contains(publicationId) else { return }
        if let entries = try? await remote.pull(publicationId: publicationId) {
            s.outlines[publicationId] = Stored(entries: entries, updatedAt: now())
            persist(s)
        }
    }

    public func push(publicationId: String, entries: [OutlineEntry]) async throws {
        var s = loaded()
        s.outlines[publicationId] = Stored(entries: entries, updatedAt: now())
        s.pending.insert(publicationId)
        persist(s)                                         // durable + queued BEFORE the network
        // Flush in the BACKGROUND — never block the caller on the network (a slow/unreachable server
        // would otherwise hang the editor's Save for the whole URLSession timeout). Mirrors
        // `LocalBookmarkStore`/`LocalAnnotationStore`.
        if remote != nil {
            Task { await self.reconcileWithRemote(publicationId: publicationId) }
        }
    }

    /// Number of copies whose outline edit hasn't reached the server yet — folded into the "N unsynced"
    /// chip. Read **fresh from disk** so a separate probe instance sees what the reader just queued.
    public func pendingWriteCount() -> Int {
        let s = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode(State.self, from: $0) } ?? state ?? State()
        return s.pending.count
    }

    /// Push the copy's queued outline to the server (idempotent wholesale LWW) and drop it from the
    /// outbox on success. A failure (offline / server down) is swallowed — it stays queued and retries.
    private func flush(publicationId: String, _ state: State) async -> State {
        guard let remote, state.pending.contains(publicationId),
              let stored = state.outlines[publicationId] else { return state }
        guard (try? await remote.push(publicationId: publicationId, entries: stored.entries)) != nil else {
            return state
        }
        var s = state
        s.pending.remove(publicationId)
        persist(s)
        return s
    }
}
