import Foundation
import CatalogueCore
import CatalogueData

/// Resolves a holding's bytes to a local file URL the reader opens (octavo's `FileSource`/PDFKit fast
/// path). Order: a cached copy → the opaque provider `storage` ref (`open_url`) → the server stream
/// `/holding/<id>/file`. The reader never learns which: the storage backend stays behind this seam
/// (kDrive doesn't leak). Bytes are cached on first fetch so reopen is zero-transfer.
public struct HoldingBytes: Sendable {
    private let endpoint: any ServerEndpoint
    private let cache: FileCache
    private let session: URLSession

    public init(endpoint: any ServerEndpoint, cache: FileCache = FileCache(), session: URLSession = .shared) {
        self.endpoint = endpoint; self.cache = cache; self.session = session
    }

    public func fileURL(for holding: Holding) async throws -> URL {
        let ext = holding.kind ?? holding.format ?? "pdf"
        if let cached = await cache.url(holdingId: holding.holdingId, ext: ext) { return cached }
        var req = URLRequest(url: remoteURL(for: holding))
        endpoint.authorize(&req)   // same auth (tunnel/NAS headers) as the metadata API
        let (data, resp) = try await session.data(for: req)
        // Never cache an error body: a 401/404/5xx (or a login/redirect HTML page) would otherwise be
        // written as `<id>.pdf` and then fail to decode as "corrupt" — poisoning the cache on reopen.
        if let http = resp as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            throw HoldingBytesError.httpStatus(http.statusCode)
        }
        guard !data.isEmpty else { throw HoldingBytesError.emptyBody }
        return try await cache.store(data, holdingId: holding.holdingId, ext: ext)
    }

    /// Drop any cached copy (e.g. a poisoned partial/error body from before response validation) so the
    /// next `fileURL(for:)` re-fetches from the server.
    public func evict(for holding: Holding) async {
        let ext = holding.kind ?? holding.format ?? "pdf"
        await cache.evict(holdingId: holding.holdingId, ext: ext)
    }

    /// The opaque provider open-url if present, else the server file stream. Tolerates either spelling
    /// of the storage key (`open_url`/`openUrl`) since the replica's snake_case is camelCased on decode.
    private func remoteURL(for holding: Holding) -> URL {
        if let storage = holding.storage,
           let raw = (storage["open_url"] ?? storage["openUrl"])?.stringValue,
           let url = URL(string: raw) {
            return url
        }
        // Build from the host ROOT with an ABSOLUTE path — not `appendingPathComponent` — so a server
        // URL that carries a prefix (e.g. the PWA's "https://…/app") doesn't leak into
        // "/app/holding/<id>/file" (→ 404). `/holding/<id>/file` lives at the root, exactly like the
        // sync/metadata endpoints, which already set their path this way (which is why those work).
        var comps = URLComponents(url: endpoint.baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = "/holding/\(holding.holdingId)/file"
        comps.query = nil
        comps.fragment = nil
        return comps.url ?? endpoint.baseURL.appendingPathComponent("holding/\(holding.holdingId)/file")
    }
}

public enum HoldingBytesError: Error, LocalizedError {
    case httpStatus(Int)
    case emptyBody

    public var errorDescription: String? {
        switch self {
        case .httpStatus(let code): return "The server returned HTTP \(code) fetching this file."
        case .emptyBody: return "The server returned an empty file."
        }
    }
}
