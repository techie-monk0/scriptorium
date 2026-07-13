import Foundation

/// The correctness core: idempotent last-write-wins merge keyed by `UUID`.
///
/// Pure and synchronous so PS-U1 can exercise it directly. Tombstones are kept
/// in the merged map (so `pull(since:)` can re-emit them); the *live* view
/// filters them out.
public enum AnnotationMerge {

    /// Does `incoming` beat `existing` under LWW?
    ///
    /// Primary key is `updatedAt`; ties break on `rev`, then on `id` so the
    /// result is deterministic regardless of input order (commutative merge).
    public static func wins(_ incoming: Annotation, over existing: Annotation) -> Bool {
        if incoming.updatedAt != existing.updatedAt {
            return incoming.updatedAt > existing.updatedAt
        }
        if incoming.rev != existing.rev {
            return incoming.rev > existing.rev
        }
        return incoming.id.uuidString > existing.id.uuidString
    }

    /// Fold `ops` into `state` (a `id -> winning annotation` map). Idempotent:
    /// re-applying the same ops is a no-op. Order-independent.
    public static func merge(
        into state: [UUID: Annotation],
        ops: [Annotation]
    ) -> [UUID: Annotation] {
        var s = state
        for op in ops {
            if let cur = s[op.id] {
                if wins(op, over: cur) { s[op.id] = op }
            } else {
                s[op.id] = op
            }
        }
        return s
    }

    /// The live (non-tombstone) annotations from a merged state, sorted by
    /// `createdAt` then `id` for a stable order.
    public static func live(_ state: [UUID: Annotation]) -> [Annotation] {
        state.values
            .filter { !$0.isTombstone }
            .sorted {
                $0.createdAt != $1.createdAt
                    ? $0.createdAt < $1.createdAt
                    : $0.id.uuidString < $1.id.uuidString
            }
    }
}

/// Offline-first sync for a single publication.
///
/// Holds a local merged state + an ordered op-queue. Local edits merge
/// immediately and enqueue; `flush()` pushes the queue then pulls newer ops on
/// reconnect. The queue can be snapshotted/restored across relaunch (PS-U5).
///
/// An `actor` for safe concurrent access; the merge itself is pure
/// (`AnnotationMerge`).
public actor SyncEngine {
    public let publicationId: String
    private let store: any AnnotationStore
    private var state: [UUID: Annotation]
    private var queue: [Annotation]
    public private(set) var lastRev: Int

    public init(
        publicationId: String,
        store: any AnnotationStore,
        lastRev: Int = 0,
        pending: [Annotation] = [],
        state: [Annotation] = []
    ) {
        self.publicationId = publicationId
        self.store = store
        self.lastRev = lastRev
        self.queue = pending
        // Local truth = restored merged state + not-yet-flushed queued ops.
        self.state = AnnotationMerge.merge(into: [:], ops: state + pending)
    }

    /// Live (non-deleted) annotations for this publication.
    public var annotations: [Annotation] { AnnotationMerge.live(state) }

    /// The pending op-queue snapshot — persist this to survive relaunch.
    public func pendingOps() -> [Annotation] { queue }

    /// Create/update a mark locally: merge now, queue for the next flush.
    /// Ignores ops for a different publication (defensive scoping).
    @discardableResult
    public func localUpsert(_ annotation: Annotation) -> Bool {
        guard annotation.publicationId == publicationId else { return false }
        state = AnnotationMerge.merge(into: state, ops: [annotation])
        queue.append(annotation)
        return true
    }

    /// Tombstone a mark locally (LWW delete). No-op if unknown.
    @discardableResult
    public func localDelete(id: UUID, at: Date = Date()) -> Bool {
        guard let cur = state[id] else { return false }
        let stone = cur.tombstoned(at: at)
        state = AnnotationMerge.merge(into: state, ops: [stone])
        queue.append(stone)
        return true
    }

    /// Push the queued ops (in order) then pull anything newer. On success the
    /// queue is cleared. Returns the new `lastRev`.
    @discardableResult
    public func flush() async throws -> Int {
        if !queue.isEmpty {
            let pushed = try await store.push(publicationId: publicationId, ops: queue)
            lastRev = max(lastRev, pushed.rev)
            queue.removeAll()
        }
        let pulled = try await store.pull(publicationId: publicationId, since: lastRev)
        state = AnnotationMerge.merge(into: state, ops: pulled.ops)
        lastRev = max(lastRev, pulled.rev)
        return lastRev
    }

    /// Pull-only refresh (no local ops to push).
    @discardableResult
    public func refresh() async throws -> Int {
        let pulled = try await store.pull(publicationId: publicationId, since: lastRev)
        state = AnnotationMerge.merge(into: state, ops: pulled.ops)
        lastRev = max(lastRev, pulled.rev)
        return lastRev
    }
}
