import Foundation
import Postilla
import Octavo
import CatalogueCore

/// Local-first annotation store — the durable **offline outbox** for marks/ink. Every op is persisted to
/// a JSON file *before* the network, and its id is queued in an outbox; a mark made **offline** therefore
/// survives relaunch and is flushed to the server when it's reachable again (on the next pull). It
/// best-effort mirrors to/from a remote `AnnotationStore` (the catalogue `/sync/reader`, i.e.
/// `ReaderSync`) with LWW merge (`AnnotationMerge`).
///
/// The annotation sibling of `LocalBookmarkStore`, plus a real outbox: previously the reader pushed
/// straight to the server and kept nothing locally, so an offline mark was lost on relaunch and never
/// retried. Now it's durable and self-heals on reconnect. Pure Foundation → unit-testable headlessly.
public actor LocalAnnotationStore: AnnotationStore, OutboxProbe {
    private let fileURL: URL
    private let remote: (any AnnotationStore)?
    private var state: State?

    struct State: Codable {
        var marks: [String: [String: Annotation]] = [:]   // pubId → (uuid → Annotation)
        var pending: [String: Set<String>] = [:]          // pubId → un-acked uuids (the outbox)
    }

    public init(fileURL: URL? = nil, remote: (any AnnotationStore)? = nil) {
        self.fileURL = fileURL ?? Self.defaultURL()
        self.remote = remote
    }

    private static func defaultURL() -> URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("annotations.json")
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

    private func merge(_ incoming: [Annotation], into pub: inout [String: Annotation]) {
        for a in incoming {
            let key = a.id.uuidString
            if let existing = pub[key] {
                if AnnotationMerge.wins(a, over: existing) { pub[key] = a }
            } else {
                pub[key] = a
            }
        }
    }

    public func pull(publicationId: String, since rev: Int) async throws -> PullResult {
        let s = loaded()
        // Offline-first: return LOCAL marks IMMEDIATELY so a slow/unreachable server can never delay the
        // reader's render (a hanging `remote.pull` was why marks only appeared minutes later, when a poll's
        // network call finally completed). The server reconcile — fold in other devices' marks + flush the
        // outbox — runs in the BACKGROUND; its result lands in the file and shows up on the next pull.
        if remote != nil {
            Task { await self.reconcileWithRemote(publicationId: publicationId) }
        }
        let ops = Array((s.marks[publicationId] ?? [:]).values)
        return PullResult(rev: ops.map(\.rev).max() ?? 0, ops: ops)
    }

    /// Background reconcile: merge the server's marks into the local store and flush the outbox. Never
    /// blocks a `pull`; failures are swallowed and retried on the next call. Internal (not private) so a
    /// test can drive it deterministically — mirrors `LocalOutlineStore`.
    func reconcileWithRemote(publicationId: String) async {
        var s = loaded()
        var pub = s.marks[publicationId] ?? [:]
        if let remote, let r = try? await remote.pull(publicationId: publicationId, since: 0) {
            merge(r.ops, into: &pub)
            s.marks[publicationId] = pub
            persist(s)
        }
        _ = await flush(publicationId: publicationId, s)
    }

    public func push(publicationId: String, ops: [Annotation]) async throws -> PushResult {
        var s = loaded()
        var pub = s.marks[publicationId] ?? [:]
        merge(ops, into: &pub)
        s.marks[publicationId] = pub
        s.pending[publicationId, default: []].formUnion(ops.map { $0.id.uuidString })
        persist(s)                                        // durable + queued BEFORE the network
        // Flush to the server in the BACKGROUND. `flush` awaits `remote.push`, which on a slow/timing-out
        // server takes tens of seconds — and EVERY caller awaits `push` (add, erase, undo, redo, note…),
        // so a synchronous flush blocked each of those (and the optimistic repaint behind it) until the
        // timeout. The op is already durable + queued locally, so the reader never needs to wait; the
        // outbox flushes best-effort in the background (mirrors `pull`'s background reconcile).
        if remote != nil {
            Task { await self.reconcileWithRemote(publicationId: publicationId) }
        }
        let live = s.marks[publicationId] ?? [:]
        return PushResult(rev: live.values.map(\.rev).max() ?? 0, applied: ops.map { $0.id })
    }

    /// Total un-acked ops across every publication — the outbox depth the freshness chip shows as
    /// "N unsynced". Read **fresh from disk** (not the memo) so a read-only probe instance — the one the
    /// composition root owns, separate from the reader's writing instance — sees ops the reader just
    /// queued (each `push` persists before returning, so the file is always current).
    public func pendingWriteCount() -> Int {
        let s = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode(State.self, from: $0) } ?? state ?? State()
        return s.pending.values.reduce(0) { $0 + $1.count }
    }

    /// Push the publication's outbox to the server (idempotent LWW) and drop the ids it accepts. A
    /// failure (offline / server down) is swallowed — the ids stay queued and retry on the next pull.
    private func flush(publicationId: String, _ state: State) async -> State {
        guard let remote,
              let ids = state.pending[publicationId], !ids.isEmpty,
              let pub = state.marks[publicationId] else { return state }
        let ops = ids.compactMap { pub[$0] }
        guard let result = try? await remote.push(publicationId: publicationId, ops: ops) else { return state }
        var s = state
        s.pending[publicationId]?.subtract(result.applied.map { $0.uuidString })
        if s.pending[publicationId]?.isEmpty == true { s.pending[publicationId] = nil }
        persist(s)
        return s
    }
}
