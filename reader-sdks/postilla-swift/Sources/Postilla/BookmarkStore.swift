import Foundation

/// Result of a bookmark `pull` — the server `rev` and every bookmark (incl. tombstones) newer than
/// the requested `since`.
public struct BookmarkPullResult: Sendable, Equatable {
    public var rev: Int
    public var ops: [Bookmark]
    public init(rev: Int, ops: [Bookmark]) { self.rev = rev; self.ops = ops }
}

/// PORT — the bookmark sync-of-record seam (sibling of `AnnotationStore`). Reuses `PushResult`.
/// Same contract: `pull(since:)` returns rows with `rev > since` incl. tombstones; `push` is an
/// idempotent LWW upsert keyed by `Bookmark.id`; both publication-scoped. No transport knowledge.
public protocol BookmarkStore: Sendable {
    func pull(publicationId: String, since rev: Int) async throws -> BookmarkPullResult
    func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult
}

/// Idempotent LWW for bookmarks — mirrors `AnnotationMerge` (key `updatedAt`, then `rev`, then `id`).
public enum BookmarkMerge {
    public static func wins(_ incoming: Bookmark, over existing: Bookmark) -> Bool {
        if incoming.updatedAt != existing.updatedAt { return incoming.updatedAt > existing.updatedAt }
        if incoming.rev != existing.rev { return incoming.rev > existing.rev }
        return incoming.id.uuidString > existing.id.uuidString
    }
}
