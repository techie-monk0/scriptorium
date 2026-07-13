import Foundation

/// A reference, in-memory `AnnotationStore` — the LWW source-of-record contract
/// with no transport. Useful for tests and demos; an integrator swaps in their
/// own networked store.
///
/// Each accepted op gets a monotonically increasing server `rev`. `pull(since:)`
/// returns ops whose rev is greater than the requested one, including
/// tombstones, scoped by publication.
public actor InMemoryAnnotationStore: AnnotationStore {
    /// One stored row: the annotation plus the server rev it was assigned.
    private struct Row {
        var annotation: Annotation
        var serverRev: Int
    }

    private var rows: [UUID: Row] = [:]
    private var clock: Int
    /// Order in which ids were accepted by `push` (for flush-order assertions).
    public private(set) var appliedOrder: [UUID] = []

    public init(startRev: Int = 0) {
        self.clock = startRev
    }

    public func pull(publicationId: String, since rev: Int) async throws -> PullResult {
        let ops = rows.values
            .filter { $0.annotation.publicationId == publicationId && $0.serverRev > rev }
            .sorted { $0.serverRev < $1.serverRev }
            .map { $0.annotation }
        return PullResult(rev: clock, ops: ops)
    }

    public func push(publicationId: String, ops: [Annotation]) async throws -> PushResult {
        var applied: [UUID] = []
        for op in ops {
            guard op.publicationId == publicationId else { continue }
            let existing = rows[op.id]?.annotation
            // Idempotent LWW: only accept (and bump rev) when the op wins.
            if existing == nil || AnnotationMerge.wins(op, over: existing!) {
                clock += 1
                rows[op.id] = Row(annotation: op, serverRev: clock)
                applied.append(op.id)
                appliedOrder.append(op.id)
            }
        }
        return PushResult(rev: clock, applied: applied)
    }
}
