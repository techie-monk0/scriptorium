import Foundation
import CatalogueCore

/// A `DataPort` served entirely from a cached `Replica` — the offline path (and, like the PWA, the
/// fast path for metadata Search/Browse even when online). Matching mirrors the server's replica fold
/// (NFKD + casefold + whitespace-collapse) so accent/case/spacing don't affect lookup. Content search
/// is the one live-only feature → `available:false` here (until an offline content index, step 5 cont.).
public struct ReplicaData: DataPort, Sendable {
    public let replica: Replica
    public init(_ replica: Replica) { self.replica = replica }

    /// Metadata Search — the shared `LibraryCore.searchReplica` matcher over the cached replica, so
    /// native and PWA agree exactly (matches the row's server-built, fold-normalised `search_text`).
    public func search(_ q: String) async throws -> [Card] { LibraryCore.searchReplica(replica, q) }

    public func detail(_ eid: Int) async throws -> EditionRow? {
        replica.editions.first { $0.editionId == eid }
    }

    public func content(_ q: String) async throws -> ContentResponse {
        ContentResponse(q: q, books: [], available: false)   // in-book text isn't in the replica
    }

    /// Type-grouped Browse — the shared `LibraryCore.browseReplica` matcher (Books / People / Subjects),
    /// so native and PWA group identically.
    public func browse(_ q: String, only: String?) async throws -> BrowseDoc { LibraryCore.browseReplica(replica, q, only: only) }

    public func suggest(_ q: String) async throws -> [Suggestion] { LibraryCore.suggestReplica(replica, q) }

}

/// Picks the replica for metadata (Search/Browse/Detail — fast + offline-safe, like the PWA) and the
/// live API for full-text Content (the one feature not in the replica). Falls back gracefully: no
/// replica cached → live; offline → replica for metadata, Content reports unavailable.
public struct OfflineFirstData: DataPort, Sendable {
    private let live: CatalogueAPI
    private let replicaProvider: @Sendable () -> Replica?
    private let offline: @Sendable () -> Bool

    public init(live: CatalogueAPI, replica: @escaping @Sendable () -> Replica?, isOffline: @escaping @Sendable () -> Bool = { false }) {
        self.live = live; self.replicaProvider = replica; self.offline = isOffline
    }

    public func search(_ q: String) async throws -> [Card] {
        if let r = replicaProvider() { return try await ReplicaData(r).search(q) }
        return try await live.search(q)
    }
    public func browse(_ q: String, only: String?) async throws -> BrowseDoc {
        if let r = replicaProvider() { return try await ReplicaData(r).browse(q, only: only) }
        return try await live.browse(q, only: only)   // live has none → AdapterUnsupported (handled by VM)
    }
    public func content(_ q: String) async throws -> ContentResponse {
        if offline() { return ContentResponse(q: q, books: [], available: false) }
        return try await live.content(q)
    }
    public func detail(_ eid: Int) async throws -> EditionRow? {
        if let r = replicaProvider(), let row = try await ReplicaData(r).detail(eid) { return row }
        return offline() ? nil : try await live.detail(eid)
    }
}
