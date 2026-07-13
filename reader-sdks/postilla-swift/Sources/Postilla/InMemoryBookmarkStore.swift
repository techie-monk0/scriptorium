import Foundation

/// A reference, in-memory `BookmarkStore` — the LWW source-of-record contract with no transport
/// (sibling of `InMemoryAnnotationStore`). Each accepted op gets a monotonic server `rev`;
/// `pull(since:)` returns rows whose rev is greater, incl. tombstones, scoped by publication.
public actor InMemoryBookmarkStore: BookmarkStore {
    private struct Row { var bookmark: Bookmark; var serverRev: Int }
    private var rows: [UUID: Row] = [:]
    private var clock: Int

    public init(startRev: Int = 0) { self.clock = startRev }

    public func pull(publicationId: String, since rev: Int) async throws -> BookmarkPullResult {
        let ops = rows.values
            .filter { $0.bookmark.publicationId == publicationId && $0.serverRev > rev }
            .sorted { $0.serverRev < $1.serverRev }
            .map { $0.bookmark }
        return BookmarkPullResult(rev: clock, ops: ops)
    }

    public func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult {
        var applied: [UUID] = []
        for op in ops {
            guard op.publicationId == publicationId else { continue }
            let existing = rows[op.id]?.bookmark
            if existing == nil || BookmarkMerge.wins(op, over: existing!) {
                clock += 1
                rows[op.id] = Row(bookmark: op, serverRev: clock)
                applied.append(op.id)
            }
        }
        return PushResult(rev: clock, applied: applied)
    }
}
