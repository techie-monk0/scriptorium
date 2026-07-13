import Foundation
import Postilla
import Octavo

/// The catalogue-app's `AnnotationStore` adapter over the catalogue's existing `/sync/reader`
/// endpoint. It speaks the server's **legacy record shape** — `{rev, annotations:[…]}` on pull,
/// `{ops:[{type:"annotation", …}]}` on push, with snake_case fields, `holding_id` ints, and
/// `rect`/`ink` carried as JSON strings — and maps it to/from the postilla `Annotation`
/// value-contract (reader plan **N0b**). The `AnnotationStore` PORT and the server route are
/// unchanged; this mapping is the adapter's private job. LWW/merge correctness lives in postilla's
/// SyncEngine; this is transport + translation.
///
/// Anchoring round-trips losslessly via `Annotation.cfiRange` (↔ `cfi_range`) and `Annotation.region`
/// (↔ `rect`). The Locator `format` is **inferred** (`cfi_range` ⇒ epub, else pdf); EPUB *ink*
/// (spine-index in `page`, no `cfi_range`) therefore reads back as `.pdf` — a documented best-effort
/// caveat until a format column exists.
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

    // MARK: AnnotationStore

    public func pull(publicationId: String, since rev: Int) async throws -> PullResult {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = "/sync/reader"
        var items = [URLQueryItem(name: "since", value: String(rev))]
        if let hid = Self.holdingId(from: publicationId) {
            // `?holding&since` → this book's deltas incl. tombstones (holding-scoped, not the world).
            items.append(URLQueryItem(name: "holding", value: String(hid)))
        }
        comps.queryItems = items
        var req = URLRequest(url: comps.url!)
        authorize(&req)
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let wire = try JSONDecoder().decode(LegacyPull.self, from: data)
        return PullResult(rev: wire.rev,
                          ops: wire.annotations.compactMap { $0.toAnnotation(publicationId: publicationId) })
    }

    public func push(publicationId: String, ops: [Annotation]) async throws -> PushResult {
        let hid = Self.holdingId(from: publicationId)
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = "/sync/reader"
        var req = URLRequest(url: comps.url!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        authorize(&req)
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes]   // keep CFIs (epubcfi(/6/4…)) un-escaped
        req.httpBody = try enc.encode(LegacyPush(ops: ops.map { LegacyOp(from: $0, holdingId: hid) }))
        let (data, resp) = try await session.data(for: req)
        try Self.check(resp)
        let r = try JSONDecoder().decode(LegacyPushResp.self, from: data)
        // `applied` carries {id, rev} for accepted ops, {id, skipped} for dropped ones.
        return PushResult(rev: r.rev,
                          applied: r.applied.compactMap { $0.rev != nil ? UUID(uuidString: $0.id) : nil })
    }

    // MARK: Legacy /sync/reader wire shapes

    private struct LegacyPull: Decodable { var rev: Int; var annotations: [LegacyAnnotation] }
    private struct LegacyPush: Encodable { var ops: [LegacyOp] }
    private struct LegacyPushResp: Decodable { var rev: Int; var applied: [Applied] }
    private struct Applied: Decodable { var id: String; var rev: Int? }

    /// One annotation record as the catalogue store serialises it (snake_case; `rect`/`ink` are
    /// JSON strings; `content_hash` is server-side and ignored on the client).
    private struct LegacyAnnotation: Decodable {
        var id: String
        var holding_id: Int?
        var kind: String?
        var cfi_range: String?
        var page: Int?
        var rect: String?
        var color: String?
        var note_text: String?
        var ink: String?
        var created_at: String?
        var updated_at: String?
        var deleted_at: String?
        var rev: Int?

        func toAnnotation(publicationId: String) -> Annotation? {
            guard let uuid = UUID(uuidString: id),
                  let raw = kind, let kind = AnnotationKind(rawValue: raw) else { return nil }
            let format: Locator.Format = (cfi_range != nil) ? .epub : .pdf
            let loc = Locator(publicationId: publicationId, format: format,
                              locations: .init(page: page))
            return Annotation(
                id: uuid, publicationId: publicationId, kind: kind, locator: loc,
                cfiRange: cfi_range,
                region: rect.flatMap(ReaderSync.doubles(fromJSON:)),
                color: color, noteText: note_text,
                ink: ink.flatMap { try? Ink.from(jsonData: Data($0.utf8)) },
                createdAt: ReaderSync.date(created_at) ?? Date(timeIntervalSince1970: 0),
                updatedAt: ReaderSync.date(updated_at) ?? Date(timeIntervalSince1970: 0),
                deletedAt: ReaderSync.date(deleted_at),
                rev: rev ?? 0)
        }
    }

    /// One push op — `LegacyAnnotation` plus the `type` discriminator the route switches on.
    private struct LegacyOp: Encodable {
        var type = "annotation"
        var id: String
        var holding_id: Int?
        var kind: String?
        var cfi_range: String?
        var page: Int?
        var rect: String?
        var color: String?
        var note_text: String?
        var ink: String?
        var created_at: String?
        var updated_at: String?
        var deleted_at: String?

        init(from a: Annotation, holdingId: Int?) {
            id = a.id.uuidString
            holding_id = holdingId
            kind = a.kind.rawValue
            cfi_range = a.cfiRange
            page = a.locator.locations.page
            rect = a.region.flatMap(ReaderSync.json(fromDoubles:))
            color = a.color
            note_text = a.noteText
            ink = a.ink.flatMap { try? String(decoding: $0.canonicalJSONData(), as: UTF8.self) }
            created_at = ReaderSync.iso(a.createdAt)
            updated_at = ReaderSync.iso(a.updatedAt)
            deleted_at = a.deletedAt.map(ReaderSync.iso)
        }
    }

    // MARK: Mapping helpers

    /// "holding:<id>" → <id> (also tolerates a bare int).
    private static func holdingId(from publicationId: String) -> Int? {
        if let n = Int(publicationId) { return n }
        guard let colon = publicationId.lastIndex(of: ":") else { return nil }
        return Int(publicationId[publicationId.index(after: colon)...])
    }

    private static func doubles(fromJSON s: String) -> [Double]? {
        try? JSONDecoder().decode([Double].self, from: Data(s.utf8))
    }
    private static func json(fromDoubles d: [Double]) -> String? {
        (try? JSONEncoder().encode(d)).map { String(decoding: $0, as: UTF8.self) }
    }
    private static func iso(_ date: Date) -> String {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]
        return f.string(from: date)
    }
    private static func date(_ s: String?) -> Date? {
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
