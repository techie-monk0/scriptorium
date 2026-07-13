import Foundation
import CatalogueCore

/// Caches the device-local replica (`GET /api/v1/replica`) to disk with ETag conditional refresh — the
/// native analogue of the PWA loading the replica into IndexedDB. Search/Browse/Detail are served from
/// the cached copy (`ReplicaData`); `refresh()` re-fetches and 304s when unchanged.
public actor ReplicaStore {
    private let api: CatalogueAPI?
    private let fileURL: URL
    private let etagURL: URL
    private var memo: Replica?

    public init(api: CatalogueAPI? = nil, directory: URL? = nil) {
        self.api = api
        let dir = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("catalogue-app", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        self.fileURL = dir.appendingPathComponent("replica.json")
        self.etagURL = dir.appendingPathComponent("replica.etag")
    }

    /// The cached replica (memoized; lazily loaded from disk).
    public func cached() -> Replica? {
        if let memo { return memo }
        guard let data = try? Data(contentsOf: fileURL) else { return nil }
        memo = try? CatalogueJSON.decode(Replica.self, from: data)
        return memo
    }

    /// Persist a replica (used by `refresh` and by tests to seed the cache without a server).
    public func store(_ replica: Replica, etag: String? = nil) {
        memo = replica
        if let data = try? CatalogueJSON.encoder.encode(replica) { try? data.write(to: fileURL) }
        if let etag { try? Data(etag.utf8).write(to: etagURL) }
    }

    /// Conditional refresh: send the saved ETag; on 200 store the new replica, on 304 keep the cache.
    @discardableResult
    public func refresh() async throws -> Replica? {
        try await revalidate().replica
    }

    /// Conditional refresh that also reports whether the payload actually changed (200 vs 304) — the
    /// signal the `SyncEngine` needs to bump its data revision only when there's new data.
    @discardableResult
    public func revalidate() async throws -> (changed: Bool, replica: Replica?) {
        guard let api else { return (false, cached()) }
        let etag = (try? Data(contentsOf: etagURL)).flatMap { String(data: $0, encoding: .utf8) }
        let (replica, newEtag) = try await api.replica(ifNoneMatch: etag)
        if let replica { store(replica, etag: newEtag); return (true, replica) }
        return (false, cached())
    }
}
