import Foundation
import Postilla
import Octavo

/// The catalogue-app's `BookmarkStore` adapter over the catalogue's `/sync/reader` endpoint — the
/// bookmark sibling of `ReaderSync`. Speaks the legacy `{rev, bookmarks:[…]}` (pull) /
/// `{ops:[{type:"bookmark", …}]}` (push) shape and maps it to/from the postilla `Bookmark`. The
/// server already owns bookmarks (the `bookmark` table); this is the iOS transport. The opaque
/// `locator` string (a PDF page number or an EPUB CFI) maps to/from a `Locator`.
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
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = "/sync/reader"
        var items = [URLQueryItem(name: "since", value: String(rev))]
        if let hid = Self.holdingId(from: publicationId) {
            items.append(URLQueryItem(name: "holding", value: String(hid)))
        }
        comps.queryItems = items
        var req = URLRequest(url: comps.url!)
        authorize(&req)
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let wire = try JSONDecoder().decode(LegacyPull.self, from: data)
        ReaderSyncContract.check(wire.contract_version)
        return BookmarkPullResult(rev: wire.rev,
                                  ops: wire.bookmarks.compactMap { $0.toBookmark(publicationId: publicationId) })
    }

    public func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult {
        let hid = Self.holdingId(from: publicationId)
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = "/sync/reader"
        var req = URLRequest(url: comps.url!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&req)
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes]
        req.httpBody = try enc.encode(LegacyPush(ops: ops.map { LegacyOp(from: $0, holdingId: hid) }))
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let r = try JSONDecoder().decode(LegacyPushResp.self, from: data)
        ReaderSyncContract.check(r.contract_version)
        return PushResult(rev: r.rev,
                          applied: r.applied.compactMap { $0.rev != nil ? UUID(uuidString: $0.id) : nil })
    }

    // MARK: Legacy wire

    private struct LegacyPull: Decodable { var rev: Int; var bookmarks: [LegacyBookmark]; var contract_version: Int? }
    private struct LegacyPush: Encodable { var ops: [LegacyOp] }
    private struct LegacyPushResp: Decodable { var rev: Int; var applied: [Applied]; var contract_version: Int? }
    private struct Applied: Decodable { var id: String; var rev: Int? }

    private struct LegacyBookmark: Decodable {
        var id: String
        var holding_id: Int?
        var locator: String?
        var fraction: Double?
        var label: String?
        var created_at: String?
        var updated_at: String?
        var deleted_at: String?
        var rev: Int?

        func toBookmark(publicationId: String) -> Bookmark? {
            guard let uuid = UUID(uuidString: id) else { return nil }
            return Bookmark(
                id: uuid, publicationId: publicationId,
                locator: BookmarkSync.locator(from: locator, publicationId: publicationId),
                fraction: fraction, label: label,
                createdAt: BookmarkSync.date(created_at) ?? Date(timeIntervalSince1970: 0),
                updatedAt: BookmarkSync.date(updated_at) ?? Date(timeIntervalSince1970: 0),
                deletedAt: BookmarkSync.date(deleted_at),
                rev: rev ?? 0)
        }
    }

    private struct LegacyOp: Encodable {
        var type = "bookmark"
        var id: String
        var holding_id: Int?
        var locator: String?
        var fraction: Double?
        var label: String?
        var created_at: String?
        var updated_at: String?
        var deleted_at: String?

        init(from b: Bookmark, holdingId: Int?) {
            id = b.id.uuidString
            holding_id = holdingId
            locator = BookmarkSync.locatorString(b.locator)
            fraction = b.fraction
            label = b.label
            created_at = BookmarkSync.iso(b.createdAt)
            updated_at = BookmarkSync.iso(b.updatedAt)
            deleted_at = b.deletedAt.map(BookmarkSync.iso)
        }
    }

    // MARK: Mapping helpers (internal so they're unit-testable via @testable)

    static func holdingId(from publicationId: String) -> Int? {
        if let n = Int(publicationId) { return n }
        guard let colon = publicationId.lastIndex(of: ":") else { return nil }
        return Int(publicationId[publicationId.index(after: colon)...])
    }

    /// postilla `Locator` → the legacy opaque `locator` string (PDF page number, else EPUB CFI).
    static func locatorString(_ loc: Locator?) -> String? {
        guard let loc else { return nil }
        if let page = loc.locations.page { return String(page) }
        return loc.locations.cfi
    }

    /// legacy `locator` string → `Locator` (an Int ⇒ a PDF page, else an EPUB CFI).
    static func locator(from s: String?, publicationId: String) -> Locator? {
        guard let s, !s.isEmpty else { return nil }
        if let page = Int(s) {
            return Locator(publicationId: publicationId, format: .pdf, locations: .init(page: page))
        }
        return Locator(publicationId: publicationId, format: .epub, locations: .init(cfi: s))
    }

    static func iso(_ d: Date) -> String {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }
    static func date(_ s: String?) -> Date? {
        guard let s, !s.isEmpty else { return nil }
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    private static func check(_ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
    }
}
