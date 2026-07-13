import Foundation

/// On-demand per-holding byte cache (mirrors the server `FileStore`): the reader asks for a holding's
/// bytes; if not present locally they're fetched once from the opaque `storage` ref or
/// `/holding/<id>/file` and cached here, giving the zero-transfer fast path on reopen.
public actor FileCache {
    private let dir: URL

    public init(directory: URL? = nil) {
        let base = directory ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("catalogue-app/holdings", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        self.dir = base
    }

    private func path(_ holdingId: Int, ext: String) -> URL {
        dir.appendingPathComponent("\(holdingId).\(ext.isEmpty ? "bin" : ext)")
    }

    /// The cached file URL for a holding, or nil if not cached.
    public func url(holdingId: Int, ext: String) -> URL? {
        let u = path(holdingId, ext: ext)
        return FileManager.default.fileExists(atPath: u.path) ? u : nil
    }

    public func isCached(holdingId: Int, ext: String) -> Bool { url(holdingId: holdingId, ext: ext) != nil }

    /// Store bytes for a holding and return the on-disk URL (the reader opens a FileSource over it).
    @discardableResult
    public func store(_ data: Data, holdingId: Int, ext: String) throws -> URL {
        let u = path(holdingId, ext: ext)
        try data.write(to: u, options: .atomic)
        return u
    }

    public func evict(holdingId: Int, ext: String) {
        try? FileManager.default.removeItem(at: path(holdingId, ext: ext))
    }
}
