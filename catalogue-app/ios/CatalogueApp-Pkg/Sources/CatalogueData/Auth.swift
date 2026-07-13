import Foundation
import Security

/// Cookie-session auth for the catalogue server (the iOS analogue of the PWA's `/login` flow).
///
/// The server's default gate is a signed, timed cookie (`lib_auth`) set by a same-origin `/login`
/// form — NOT HTTP Basic and NOT Cloudflare Access (see `webui/auth.py`). So the app authenticates
/// exactly the way the browser PWA does: POST the credential once to `/login`; `URLSession`'s shared
/// `HTTPCookieStorage` then stores the cookie (persistent — 90-day max-age) and auto-attaches it to
/// every subsequent same-host request, including `/api/v1/*`. Nothing per-request lives here — the
/// `ServerEndpoint.authorize` hook stays a no-op for cookie mode.
///
/// This mirrors the repo's client-supplied-strategy convention: `CatalogueAPI` owns transport and,
/// on a 401, delegates re-auth to an injected `onUnauthorized` hook (wired to `AuthSession` below),
/// so the login protocol lives beside — not inside — the request path.

/// A username/password pair. Persisted in the keychain (see `Keychain`) so the app can re-establish
/// a session silently when the cookie expires (or after a reinstall).
public struct Credentials: Codable, Sendable, Equatable {
    public let username: String
    public let password: String
    public init(username: String, password: String) {
        self.username = username
        self.password = password
    }
}

/// POST `username`/`password` to the server's `/login`, establishing the session cookie in `session`'s
/// cookie storage. Throws `APIError(status: 401)` on a wrong credential (the form re-renders 401) and
/// `APIError` for any other non-success. On success the cookie is already stored — nothing is returned.
///
/// `next=/api/v1/health` makes a successful login redirect (302, followed by `URLSession`) to a cheap
/// gated endpoint, so a good credential round-trips end-to-end (cookie set → gated 200) in one call.
public func cookieLogin(endpoint: any ServerEndpoint, session: URLSession,
                        username: String, password: String) async throws {
    var comps = URLComponents(url: endpoint.baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
    comps.path = "/login"
    guard let url = comps.url else { throw APIError(status: -1) }
    var req = URLRequest(url: url)
    req.httpMethod = "POST"
    endpoint.authorize(&req)   // carry any tunnel/NAS headers on the login POST too
    req.setValue("application/x-www-form-urlencoded; charset=utf-8", forHTTPHeaderField: "Content-Type")
    let body = "username=\(formURLEncode(username))&password=\(formURLEncode(password))&next=%2Fapi%2Fv1%2Fhealth"
    req.httpBody = body.data(using: .utf8)
    let (_, resp) = try await session.data(for: req)
    guard let http = resp as? HTTPURLResponse else { throw APIError(status: -1) }
    guard http.statusCode != 401 else { throw APIError(status: 401) }               // wrong username/password
    guard (200..<400).contains(http.statusCode) else { throw APIError(status: http.statusCode) }
}

/// Percent-encode one `application/x-www-form-urlencoded` field. Only RFC 3986 unreserved characters
/// pass through, so `+`, `&`, `=`, and spaces in a password are encoded (space → `%20`, which Flask
/// decodes the same as `+`) rather than corrupting the form.
private func formURLEncode(_ s: String) -> String {
    var allowed = CharacterSet.alphanumerics
    allowed.insert(charactersIn: "-._~")
    return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
}

/// Holds the credential for one endpoint and (re)establishes its cookie session. The single-flight
/// `reauthenticate()` is what `CatalogueAPI` calls on a 401: concurrent 401s (search + replica + …)
/// collapse to ONE `/login` round-trip, then each request retries. An `actor` so that coalescing is
/// race-free without a lock.
public actor AuthSession {
    private let endpoint: any ServerEndpoint
    private let session: URLSession
    private var credentials: Credentials?
    private var inFlight: Task<Bool, Never>?

    public init(endpoint: any ServerEndpoint, session: URLSession = .shared, credentials: Credentials? = nil) {
        self.endpoint = endpoint
        self.session = session
        self.credentials = credentials
    }

    public var hasCredentials: Bool { credentials != nil }

    /// Log in with an explicit credential (the Settings form). Stores it for silent re-auth on success.
    public func logIn(_ c: Credentials) async throws {
        try await cookieLogin(endpoint: endpoint, session: session, username: c.username, password: c.password)
        credentials = c
    }

    /// Forget the credential (sign-out); the caller also clears the stored cookie + keychain entry.
    public func clear() { credentials = nil }

    /// Silent re-auth from the stored credential, used on a 401. Single-flight: a second caller awaits
    /// the first attempt instead of firing its own. Returns whether a fresh session was established.
    public func reauthenticate() async -> Bool {
        if let inFlight { return await inFlight.value }
        guard let c = credentials else { return false }
        let ep = endpoint, sess = session
        let task = Task { () -> Bool in
            (try? await cookieLogin(endpoint: ep, session: sess, username: c.username, password: c.password)) != nil
        }
        inFlight = task
        let ok = await task.value
        inFlight = nil
        return ok
    }
}

/// Keychain-backed credential store, keyed by server host (so a LAN address and the tunnel keep
/// separate logins). Reads never prompt and treat "not found"/errors as `nil`, so it's safe to call
/// eagerly at startup. Generic-password items; accessible after first unlock so background sync works.
public enum Keychain {
    // Derived from the app's bundle id so nothing hardcodes a personal identifier and
    // each build (or fork) gets its own keychain namespace automatically. Falls back to
    // the public default id when there's no host bundle (e.g. unit tests).
    private static let service = (Bundle.main.bundleIdentifier ?? "app.scriptorium.reader") + ".login"

    public static func save(_ c: Credentials, account: String) {
        guard let data = try? JSONEncoder().encode(c) else { return }
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: account]
        SecItemDelete(base as CFDictionary)                    // replace any existing item
        var add = base
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    public static func load(account: String) -> Credentials? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: account,
                                    kSecReturnData as String: true,
                                    kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(Credentials.self, from: data)
    }

    public static func delete(account: String) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service,
                                    kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
    }
}
