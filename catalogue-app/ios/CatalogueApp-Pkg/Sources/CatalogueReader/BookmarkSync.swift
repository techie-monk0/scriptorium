import Foundation
import Postilla
import CatalogueReaderWire

/// The catalogue-app's `BookmarkStore` **transport** over `/sync/reader` — the bookmark sibling of
/// `ReaderSync`. HTTP + auth only; the `Bookmark ⇄ wire` mapping, routes, and contract check live in
/// the neutral `CatalogueReaderWire` layer.
public struct BookmarkSync: BookmarkStore, Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let authorize: @Sendable (inout URLRequest) -> Void

    public init(baseURL: URL, session: URLSession = .shared,
                authorize: @escaping @Sendable (inout URLRequest) -> Void = { _ in }) {
        self.baseURL = baseURL
        self.session = session
        self.authorize = authorize
    }

    public func pull(publicationId: String, since rev: Int) async throws -> BookmarkPullResult {
        let hid = ReaderWireCodec.holdingId(from: publicationId)
        var req = URLRequest(url: ReaderRoutes.syncPull(baseURL: baseURL, holding: hid, since: rev))
        authorize(&req)
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let wire = try JSONDecoder().decode(ReaderPullResponse.self, from: data)
        ReaderSyncContract.check(wire.contract_version)
        return BookmarkPullResult(
            rev: wire.rev,
            ops: (wire.bookmarks ?? []).compactMap { ReaderWireCodec.bookmark(from: $0, publicationId: publicationId) }
        )
    }

    public func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult {
        let hid = ReaderWireCodec.holdingId(from: publicationId)
        var req = URLRequest(url: ReaderRoutes.syncPush(baseURL: baseURL))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&req)
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes]
        req.httpBody = try enc.encode(ReaderPushRequest(ops: ops.map { ReaderWireCodec.op(from: $0, holdingId: hid) }))
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let r = try JSONDecoder().decode(ReaderPushResponse.self, from: data)
        ReaderSyncContract.check(r.contract_version)
        return PushResult(rev: r.rev,
                          applied: r.applied.compactMap { $0.rev != nil ? UUID(uuidString: $0.id) : nil })
    }

    private static func check(_ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
    }
}
