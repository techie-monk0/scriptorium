import Foundation
import Postilla
import Octavo

/// Reading-position transport over the catalogue's `/holding/<id>/position` endpoint (the same one
/// web/PWA use). iOS's position-of-record stays LOCAL (`CatalogueReadingStore`, restored on open); this
/// mirrors it to the server so a SECOND device can offer "resume from where you left off". Last-write-
/// wins (no `rev`); the resume prompt is advisory, never an auto-jump. Reuses `BookmarkSync`'s opaque
/// `locator`-string ↔ `Locator` mapping (PDF page number / EPUB CFI).
public struct PositionSync: Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let authorize: @Sendable (inout URLRequest) -> Void

    public init(baseURL: URL, session: URLSession = .shared,
                authorize: @escaping @Sendable (inout URLRequest) -> Void = { _ in }) {
        self.baseURL = baseURL; self.session = session; self.authorize = authorize
    }

    private func url(_ hid: Int) -> URL {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = "/holding/\(hid)/position"
        return comps.url!
    }

    /// The server's last-known position for this holding (from any device), or nil if none/unreachable.
    public func pull(publicationId: String) async -> (locator: Locator?, fraction: Double?)? {
        guard let hid = BookmarkSync.holdingId(from: publicationId) else { return nil }
        var req = URLRequest(url: url(hid)); authorize(&req)
        guard let (data, resp) = try? await session.data(for: req),
              let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              let wire = try? JSONDecoder().decode(Wire.self, from: data) else { return nil }
        return (BookmarkSync.locator(from: wire.locator, publicationId: publicationId), wire.fraction)
    }

    /// Mirror the current reading position to the server (best-effort; LWW). No-op if the pubId doesn't
    /// resolve to a holding id.
    public func push(publicationId: String, locator: Locator, fraction: Double?) async {
        guard let hid = BookmarkSync.holdingId(from: publicationId) else { return }
        var req = URLRequest(url: url(hid)); req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type"); authorize(&req)
        let body = Wire(locator: BookmarkSync.locatorString(locator),
                        fraction: fraction ?? locator.locations.progression)
        req.httpBody = try? JSONEncoder().encode(body)
        _ = try? await session.data(for: req)
    }

    private struct Wire: Codable { var locator: String?; var fraction: Double? }
}
