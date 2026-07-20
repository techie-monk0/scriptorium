import Foundation
import CatalogueReaderWire

/// The **change-probe** transport over `GET /sync/reader/rev` — the sibling of `ReaderSync`/
/// `BookmarkSync`/`OutlineSync`, but it fetches only the max rev per resource for one copy (no rows),
/// so the reader can ask "did this book's bookmarks/outline/annotations change on another device?"
/// cheaply and only do the full pull when the answer is yes. HTTP + auth only; the route + wire shape
/// live in the neutral `CatalogueReaderWire` layer.
public struct ReaderRevCheck: Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let authorize: @Sendable (inout URLRequest) -> Void

    public init(baseURL: URL, session: URLSession = .shared,
                authorize: @escaping @Sendable (inout URLRequest) -> Void = { _ in }) {
        self.baseURL = baseURL
        self.session = session
        self.authorize = authorize
    }

    /// The server's current max rev per resource for `publicationId`, or throws (offline / not a
    /// holding-scoped id / non-2xx) so the caller can treat a failed probe as "assume changed".
    public func revs(publicationId: String) async throws -> HoldingRevs {
        guard let hid = ReaderWireCodec.holdingId(from: publicationId) else { throw URLError(.badURL) }
        var req = URLRequest(url: ReaderRoutes.syncRev(baseURL: baseURL, holding: hid))
        authorize(&req)
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        let wire = try JSONDecoder().decode(ReaderRevResponse.self, from: data)
        ReaderSyncContract.check(wire.contract_version)
        return HoldingRevs(bookmarks: wire.bookmarks_rev,
                           annotations: wire.annotations_rev,
                           outlines: wire.outlines_rev)
    }
}
