import Foundation
import Postilla
import CatalogueReaderWire

/// The catalogue-app's `AnnotationStore` **transport** over `/sync/reader`. It now does only HTTP +
/// auth: the `Annotation ⇄ wire` translation and the route/contract knowledge live in the neutral
/// `CatalogueReaderWire` layer (`ReaderWireCodec` / `ReaderRoutes` / `ReaderSyncContract`), so a
/// non-iOS frontend reuses that mapping and only swaps this URLSession shell. LWW/merge correctness
/// lives in postilla's SyncEngine.
public struct ReaderSync: AnnotationStore, Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let authorize: @Sendable (inout URLRequest) -> Void

    /// `authorize` decorates every request with the endpoint's auth (tunnel/NAS headers) so the
    /// reader's marks sync through the same gate as the metadata API.
    public init(baseURL: URL, session: URLSession = .shared,
                authorize: @escaping @Sendable (inout URLRequest) -> Void = { _ in }) {
        self.baseURL = baseURL
        self.session = session
        self.authorize = authorize
    }

    public func pull(publicationId: String, since rev: Int) async throws -> PullResult {
        let hid = ReaderWireCodec.holdingId(from: publicationId)
        var req = URLRequest(url: ReaderRoutes.syncPull(baseURL: baseURL, holding: hid, since: rev))
        authorize(&req)
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let wire = try JSONDecoder().decode(ReaderPullResponse.self, from: data)
        ReaderSyncContract.check(wire.contract_version)
        return PullResult(
            rev: wire.rev,
            ops: (wire.annotations ?? []).compactMap { ReaderWireCodec.annotation(from: $0, publicationId: publicationId) }
        )
    }

    public func push(publicationId: String, ops: [Annotation]) async throws -> PushResult {
        let hid = ReaderWireCodec.holdingId(from: publicationId)
        var req = URLRequest(url: ReaderRoutes.syncPush(baseURL: baseURL))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&req)
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes]   // keep CFIs (epubcfi(/6/4…)) un-escaped
        req.httpBody = try enc.encode(ReaderPushRequest(ops: ops.map { ReaderWireCodec.op(from: $0, holdingId: hid) }))
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let r = try JSONDecoder().decode(ReaderPushResponse.self, from: data)
        ReaderSyncContract.check(r.contract_version)
        // `applied` carries {id, rev} for accepted ops, {id, skipped} for dropped ones.
        return PushResult(rev: r.rev,
                          applied: r.applied.compactMap { $0.rev != nil ? UUID(uuidString: $0.id) : nil })
    }

    private static func check(_ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
    }
}
