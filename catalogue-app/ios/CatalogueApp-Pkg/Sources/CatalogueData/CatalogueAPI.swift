import Foundation
import CatalogueCore

/// The live `DataPort` over `/api/v1/*` (the iOS analogue of `library-web.js`). URLSession client,
/// base URL = the Mac's LAN address. It maps `/api/v1/library` rows → neutral `Card`s the same way the
/// web adapter does (`by` = the row subtitle; art handles are deterministic `/edition/<id>/…`). The
/// storage backend never leaks: a holding's bytes come from its opaque `storage` ref else
/// `/holding/<id>/file` — neither is the engine's concern here.
public struct CatalogueAPI: DataPort, Sendable {
    /// The reachability strategy (LAN / tunnel / NAS / …). Supplies the base URL and authorizes each
    /// request — so the client is identical regardless of how the server is reached.
    public let endpoint: any ServerEndpoint
    public var baseURL: URL { endpoint.baseURL }
    private let session: URLSession
    /// Called when a request comes back 401 (the cookie session expired/absent). Returns `true` if it
    /// re-established a session — then the request is retried ONCE. Wired to `AuthSession` by `AppModel`;
    /// `nil` for anonymous/test clients (a 401 then surfaces as `APIError(401)` as before).
    private let onUnauthorized: (@Sendable () async -> Bool)?

    public init(endpoint: any ServerEndpoint, session: URLSession = .shared,
                onUnauthorized: (@Sendable () async -> Bool)? = nil) {
        self.endpoint = endpoint
        self.session = session
        self.onUnauthorized = onUnauthorized
    }

    /// Convenience: a `DirectEndpoint` over a bare URL (used by tests and simple setups).
    public init(baseURL: URL, session: URLSession = .shared) {
        self.init(endpoint: DirectEndpoint(baseURL: baseURL), session: session)
    }

    /// Establish a cookie session against this endpoint (the Settings sign-in). Delegates to the shared
    /// `cookieLogin`; on success the cookie is in `session`'s storage and every call is authenticated.
    public func login(username: String, password: String) async throws {
        try await cookieLogin(endpoint: endpoint, session: session, username: username, password: password)
    }

    // ── DataPort (live search / content / detail) ─────────────────────────────
    public func search(_ q: String) async throws -> [Card] {
        let doc: LibraryResponse = try await get("/api/v1/library", [URLQueryItem(name: "q", value: q)])
        return doc.rows.map { r in
            let art = artFor(r.id)
            return Card(eid: r.id, title: r.title, displayTitle: r.displayTitle,
                        by: r.subtitle ?? "", holdingId: r.holdingId, hasFile: r.hasFile,
                        coverUrl: art.coverUrl, spineUrl: art.spineUrl)
        }
    }

    public func content(_ q: String) async throws -> ContentResponse {
        try await get("/api/v1/content", [URLQueryItem(name: "q", value: q)])
    }

    public func detail(_ eid: Int) async throws -> EditionRow? {
        do { return try await get("/api/v1/edition/\(eid)", []) as EditionRow }
        catch let e as APIError where e.status == 404 { return nil }
    }

    // ── raw fetchers (replica / subjects / health) the offline store + screens use ─
    public func health() async throws -> Health { try await get("/api/v1/health", []) }

    public func subjects(kind: String? = nil, q: String? = nil) async throws -> SubjectsResponse {
        var items: [URLQueryItem] = []
        if let kind { items.append(URLQueryItem(name: "kind", value: kind)) }
        if let q, !q.isEmpty { items.append(URLQueryItem(name: "q", value: q)) }
        return try await get("/api/v1/subjects", items)
    }

    public func subject(_ sid: Int) async throws -> SubjectPage? {
        do { return try await get("/api/v1/subject/\(sid)", []) as SubjectPage }
        catch let e as APIError where e.status == 404 { return nil }
    }

    /// The replica with conditional-GET. Returns `(nil, etag)` on a 304 (caller keeps its cached copy),
    /// else `(replica, etag)`.
    public func replica(ifNoneMatch etag: String? = nil) async throws -> (replica: Replica?, etag: String?) {
        let (data, http) = try await fetch("/api/v1/replica", [], ifNoneMatch: etag)
        let newEtag = http.value(forHTTPHeaderField: "ETag")
        if http.statusCode == 304 { return (nil, newEtag ?? etag) }
        guard (200..<300).contains(http.statusCode) else { throw APIError(status: http.statusCode) }
        return (try CatalogueJSON.decode(Replica.self, from: data), newEtag)
    }

    // ── wishlist (books wanted but not yet owned) ───────────────────────────────
    // This is a pure ADAPTER: every wishlist call routes through `LibraryCore.wishlistRequest`
    // (the shared Tier-2 intent→request mapper) and `wishlistExec` executes the descriptor. No
    // endpoint is hardcoded here, so iOS issues byte-identical requests to web/PWA.

    /// Execute a shared `WishlistAction` and return the response bytes.
    @discardableResult
    public func wishlistExec(_ action: WishlistAction) async throws -> Data {
        let req = LibraryCore.wishlistRequest(action)
        if req.method == "GET" { return try await fetch(req.path, [], ifNoneMatch: nil).0 }
        return try await send(req.path, method: req.method, jsonAny: req.body.map(jsonObject))
    }

    /// The shared wishlist list (`.list`). The caller caches it for offline display.
    public func wishlist() async throws -> WishlistPayload {
        try CatalogueJSON.decode(WishlistPayload.self, from: try await wishlistExec(.list))
    }

    /// Add a wanted book (`.add`). Body is built by the caller (the shared add-body shape); the
    /// server resolves the edition and returns the created item (`status` flags an unidentified book).
    @discardableResult
    public func addWishlist(body: [String: JSONValue]) async throws -> WishlistAddResponse {
        let data = try await wishlistExec(.add(body: body))
        return (try? CatalogueJSON.decode(WishlistAddResponse.self, from: data)) ?? WishlistAddResponse()
    }

    /// Mutating actions (remove / pick / confirm / decline) — fire-and-forget through the mapper.
    public func wishlistAct(_ action: WishlistAction) async throws { _ = try await wishlistExec(action) }

    // ── starred editions (the Starred rail + highlighted covers) ─────────────────
    // Same pure-adapter shape as wishlist: every call routes through `LibraryCore.starredRequest`.

    /// Execute a shared `StarredAction` and return the response bytes.
    @discardableResult
    public func starredExec(_ action: StarredAction) async throws -> Data {
        let req = LibraryCore.starredRequest(action)
        if req.method == "GET" { return try await fetch(req.path, [], ifNoneMatch: nil).0 }
        return try await send(req.path, method: req.method, jsonAny: req.body.map(jsonObject))
    }

    /// The shared starred-edition list (`.list`). The caller holds it as `starredIds`.
    public func starred() async throws -> StarredPayload {
        try CatalogueJSON.decode(StarredPayload.self, from: try await starredExec(.list))
    }

    /// Toggle a star (`.star`/`.unstar`); each write returns the fresh list so the caller refreshes
    /// its in-memory set in one round-trip.
    @discardableResult
    public func setStarred(_ eid: Int, _ on: Bool) async throws -> StarredPayload {
        let data = try await starredExec(on ? .star(eid: eid) : .unstar(eid: eid))
        return (try? CatalogueJSON.decode(StarredPayload.self, from: data)) ?? StarredPayload()
    }

    /// Turn the shared descriptor's `[String: JSONValue]` body into a JSONSerialization-ready dict.
    private func jsonObject(_ body: [String: JSONValue]) -> [String: Any] {
        body.mapValues(jsonScalar)
    }
    private func jsonScalar(_ v: JSONValue) -> Any {
        switch v {
        case .null: return NSNull()
        case .bool(let b): return b
        case .int(let i): return i
        case .double(let d): return d
        case .string(let s): return s
        case .array(let a): return a.map(jsonScalar)
        case .object(let o): return o.mapValues(jsonScalar)
        }
    }

    // ── transport ─────────────────────────────────────────────────────────────
    private func get<T: Decodable>(_ path: String, _ items: [URLQueryItem]) async throws -> T {
        let (data, http) = try await fetch(path, items, ifNoneMatch: nil)
        guard (200..<300).contains(http.statusCode) else { throw APIError(status: http.statusCode) }
        return try CatalogueJSON.decode(T.self, from: data)
    }

    private func fetch(_ path: String, _ items: [URLQueryItem], ifNoneMatch: String?) async throws -> (Data, HTTPURLResponse) {
        let first = try await rawFetch(path, items, ifNoneMatch: ifNoneMatch)
        if first.1.statusCode == 401, let onUnauthorized, await onUnauthorized() {
            return try await rawFetch(path, items, ifNoneMatch: ifNoneMatch)   // session re-established → retry once
        }
        return first
    }

    private func rawFetch(_ path: String, _ items: [URLQueryItem], ifNoneMatch: String?) async throws -> (Data, HTTPURLResponse) {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = path
        comps.queryItems = items.isEmpty ? nil : items
        guard let url = comps.url else { throw APIError(status: -1) }
        var req = URLRequest(url: url)
        endpoint.authorize(&req)   // tunnel/NAS auth headers, etc.
        if let ifNoneMatch { req.setValue(ifNoneMatch, forHTTPHeaderField: "If-None-Match") }
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw APIError(status: -1) }
        return (data, http)
    }

    /// POST/PATCH/DELETE a JSON body and return the response bytes (2xx only, else `APIError`).
    private func send(_ path: String, method: String, json: [String: String]?) async throws -> Data {
        try await send(path, method: method, jsonAny: json.map { $0 as [String: Any] })
    }

    private func send(_ path: String, method: String, jsonAny: [String: Any]?) async throws -> Data {
        let first = try await rawSend(path, method: method, jsonAny: jsonAny)
        if first.1 == 401, let onUnauthorized, await onUnauthorized() {
            let retry = try await rawSend(path, method: method, jsonAny: jsonAny)   // re-authed → retry once
            guard (200..<300).contains(retry.1) else { throw APIError(status: retry.1) }
            return retry.0
        }
        guard (200..<300).contains(first.1) else { throw APIError(status: first.1) }
        return first.0
    }

    private func rawSend(_ path: String, method: String, jsonAny: [String: Any]?) async throws -> (Data, Int) {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = path
        guard let url = comps.url else { throw APIError(status: -1) }
        var req = URLRequest(url: url)
        req.httpMethod = method
        endpoint.authorize(&req)
        if let jsonAny {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: jsonAny)
        }
        let (data, resp) = try await session.data(for: req)
        return (data, (resp as? HTTPURLResponse)?.statusCode ?? -1)
    }
}


/// A non-2xx HTTP status surfaced as an error so callers can branch (e.g. `detail` maps 404 → nil).
public struct APIError: Error, Equatable, Sendable {
    public let status: Int
    public init(status: Int) { self.status = status }
}
