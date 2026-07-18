import Foundation
import CatalogueReaderWire

/// Reads/writes a publication's authored PDF outline (the whole table-of-contents as a unit). The
/// reader authors against this ABSTRACTION; the adapters are `OutlineSync` (server transport) and
/// `LocalOutlineStore` (durable, offline-first). Unlike marks/bookmarks (one row per item), an outline
/// is a single document per copy — `push` replaces it wholesale (LWW), `pull` returns the current set.
public protocol OutlineStore: Sendable {
    func pull(publicationId: String) async throws -> [OutlineEntry]
    func push(publicationId: String, entries: [OutlineEntry]) async throws
}

/// The catalogue-app's `OutlineStore` **transport** over `/sync/reader` — the outline sibling of
/// `BookmarkSync`. HTTP + auth only; the `[OutlineEntry] ⇄ wire` mapping, routes, and contract check
/// live in the neutral `CatalogueReaderWire` layer. Wholesale per copy, keyed by a stable per-copy id
/// so two devices editing the same copy's outline converge on one server row.
public struct OutlineSync: OutlineStore, Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let authorize: @Sendable (inout URLRequest) -> Void
    private let now: @Sendable () -> Date

    public init(baseURL: URL, session: URLSession = .shared,
                authorize: @escaping @Sendable (inout URLRequest) -> Void = { _ in },
                now: @escaping @Sendable () -> Date = { Date() }) {
        self.baseURL = baseURL; self.session = session; self.authorize = authorize; self.now = now
    }

    /// The stable per-copy outline id both server and client use, so LWW converges on one row.
    public static func outlineId(_ publicationId: String) -> String {
        if let hid = ReaderWireCodec.holdingId(from: publicationId) { return "outline:holding:\(hid)" }
        return "outline:\(publicationId)"
    }

    public func pull(publicationId: String) async throws -> [OutlineEntry] {
        let hid = ReaderWireCodec.holdingId(from: publicationId)
        var req = URLRequest(url: ReaderRoutes.syncPull(baseURL: baseURL, holding: hid, since: 0))
        authorize(&req)
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let wire = try JSONDecoder().decode(ReaderPullResponse.self, from: data)
        ReaderSyncContract.check(wire.contract_version)
        // Newest row wins (wholesale); a tombstone → empty (entries(from:) returns [] for a tombstone).
        guard let newest = (wire.outlines ?? []).max(by: { ($0.rev ?? 0) < ($1.rev ?? 0) }) else { return [] }
        return ReaderWireCodec.entries(from: newest)
    }

    public func push(publicationId: String, entries: [OutlineEntry]) async throws {
        let hid = ReaderWireCodec.holdingId(from: publicationId)
        var req = URLRequest(url: ReaderRoutes.syncPush(baseURL: baseURL))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&req)
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes]
        let op = ReaderWireCodec.outlineOp(entries: entries, id: Self.outlineId(publicationId),
                                           holdingId: hid, updatedAt: now())
        req.httpBody = try enc.encode(ReaderPushRequest(ops: [op]))
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let r = try JSONDecoder().decode(ReaderPushResponse.self, from: data)
        ReaderSyncContract.check(r.contract_version)
    }

    private static func check(_ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
    }
}
