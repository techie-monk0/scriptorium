import Foundation
import Postilla
import CatalogueReaderWire

/// Reading-position **transport** over `/holding/<id>/position` (the same endpoint web/PWA use). iOS's
/// position-of-record stays LOCAL (`CatalogueReadingStore`); this mirrors it so a SECOND device can
/// offer "resume from where you left off". LWW (no `rev`); the resume prompt is advisory. The
/// `Locator ⇄ wire` mapping and route live in `CatalogueReaderWire`.
public struct PositionSync: Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let authorize: @Sendable (inout URLRequest) -> Void

    public init(baseURL: URL, session: URLSession = .shared,
                authorize: @escaping @Sendable (inout URLRequest) -> Void = { _ in }) {
        self.baseURL = baseURL; self.session = session; self.authorize = authorize
    }

    /// The server's last-known position for this holding (from any device), or nil if none/unreachable.
    public func pull(publicationId: String) async -> (locator: Locator?, fraction: Double?)? {
        guard let hid = ReaderWireCodec.holdingId(from: publicationId) else { return nil }
        var req = URLRequest(url: ReaderRoutes.position(baseURL: baseURL, holding: hid)); authorize(&req)
        guard let (data, resp) = try? await session.data(for: req),
              let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              let wire = try? JSONDecoder().decode(PositionRecord.self, from: data) else { return nil }
        return ReaderWireCodec.position(from: wire, publicationId: publicationId)
    }

    /// Mirror the current reading position to the server (best-effort; LWW). No-op if the pubId doesn't
    /// resolve to a holding id.
    public func push(publicationId: String, locator: Locator, fraction: Double?) async {
        guard let hid = ReaderWireCodec.holdingId(from: publicationId) else { return }
        var req = URLRequest(url: ReaderRoutes.position(baseURL: baseURL, holding: hid)); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type"); authorize(&req)
        req.httpBody = try? JSONEncoder().encode(ReaderWireCodec.positionRecord(locator: locator, fraction: fraction))
        _ = try? await session.data(for: req)
    }
}
