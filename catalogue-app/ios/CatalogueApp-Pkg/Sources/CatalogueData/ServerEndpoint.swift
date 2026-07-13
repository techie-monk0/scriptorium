import Foundation

/// How the app reaches the catalogue server — an **abstract strategy** with interchangeable
/// implementations (LAN, Cloudflare tunnel, NAS, anything you decide later). It supplies the base URL
/// the API/reader build on plus an `authorize` hook to decorate every request (Access service tokens,
/// basic auth, …). A new backend is a new conformer — nothing above this layer changes. (Mirrors the
/// repo's client-supplied-strategy + protocol-agnostic-executor convention.)
public protocol ServerEndpoint: Sendable {
    var kind: ServerEndpointKind { get }
    /// Human label for Settings (e.g. "Tunnel — library.example").
    var label: String { get }
    /// The root the API resolves `/api/v1/*` and `/holding/<id>/file` against.
    var baseURL: URL { get }
    /// Decorate an outgoing request (default: no-op). Tunnels/NAS add auth headers here.
    func authorize(_ request: inout URLRequest)
    /// A persistable description (stored in prefs; rebuilt via `make()`).
    var descriptor: ServerEndpointDescriptor { get }
}

public extension ServerEndpoint {
    func authorize(_ request: inout URLRequest) {}
}

public enum ServerEndpointKind: String, Codable, Sendable, CaseIterable {
    case localNetwork, tunnel, nas, direct
    public var title: String {
        switch self {
        case .localNetwork: return "Local network"
        case .tunnel: return "Cloudflare tunnel"
        case .nas: return "NAS"
        case .direct: return "Direct URL"
        }
    }
}

/// Codable, persistable form of any endpoint — `prefs` stores this; `make()` reconstructs the
/// concrete strategy. Adding a backend = a new `kind` + a case here.
public struct ServerEndpointDescriptor: Codable, Sendable, Equatable {
    public var kind: ServerEndpointKind
    public var url: String
    /// Auth headers (e.g. `CF-Access-Client-Id`/`-Secret` for a tunnel behind Access; `Authorization`
    /// for a NAS reverse-proxy). Kept generic so any scheme drops in without a model change.
    public var headers: [String: String]

    public init(kind: ServerEndpointKind, url: String, headers: [String: String] = [:]) {
        self.kind = kind; self.url = url; self.headers = headers
    }

    public func make() -> (any ServerEndpoint)? {
        guard let u = normalizeServerURL(url) else { return nil }
        switch kind {
        case .localNetwork: return LocalNetworkEndpoint(baseURL: u)
        case .tunnel: return TunnelEndpoint(baseURL: u, headers: headers)
        case .nas: return NASEndpoint(baseURL: u, headers: headers)
        case .direct: return DirectEndpoint(baseURL: u, headers: headers)
        }
    }
}

// ── concrete strategies ───────────────────────────────────────────────────────

/// Plain HTTP on the LAN (same Wi-Fi as the Mac). No auth; reachable only on-network.
public struct LocalNetworkEndpoint: ServerEndpoint {
    public let baseURL: URL
    public init(baseURL: URL) { self.baseURL = baseURL }
    public var kind: ServerEndpointKind { .localNetwork }
    public var label: String { "Local network — \(baseURL.host ?? baseURL.absoluteString)" }
    public var descriptor: ServerEndpointDescriptor { .init(kind: .localNetwork, url: baseURL.absoluteString) }
}

/// Reach the server from anywhere over a Cloudflare tunnel (HTTPS). `headers` carry Access service
/// tokens when the hostname is behind Cloudflare Access.
public struct TunnelEndpoint: ServerEndpoint {
    public let baseURL: URL
    public let headers: [String: String]
    public init(baseURL: URL, headers: [String: String] = [:]) { self.baseURL = baseURL; self.headers = headers }
    public var kind: ServerEndpointKind { .tunnel }
    public var label: String { "Tunnel — \(baseURL.host ?? baseURL.absoluteString)" }
    public func authorize(_ request: inout URLRequest) { headers.forEach { request.setValue($1, forHTTPHeaderField: $0) } }
    public var descriptor: ServerEndpointDescriptor { .init(kind: .tunnel, url: baseURL.absoluteString, headers: headers) }
}

/// A NAS / self-hosted reverse proxy. Same shape as a tunnel (URL + optional auth headers); separate
/// kind so Settings can label it and future NAS-specific behaviour has a home.
public struct NASEndpoint: ServerEndpoint {
    public let baseURL: URL
    public let headers: [String: String]
    public init(baseURL: URL, headers: [String: String] = [:]) { self.baseURL = baseURL; self.headers = headers }
    public var kind: ServerEndpointKind { .nas }
    public var label: String { "NAS — \(baseURL.host ?? baseURL.absoluteString)" }
    public func authorize(_ request: inout URLRequest) { headers.forEach { request.setValue($1, forHTTPHeaderField: $0) } }
    public var descriptor: ServerEndpointDescriptor { .init(kind: .nas, url: baseURL.absoluteString, headers: headers) }
}

/// Escape hatch — an arbitrary base URL with optional headers.
public struct DirectEndpoint: ServerEndpoint {
    public let baseURL: URL
    public let headers: [String: String]
    public init(baseURL: URL, headers: [String: String] = [:]) { self.baseURL = baseURL; self.headers = headers }
    public var kind: ServerEndpointKind { .direct }
    public var label: String { baseURL.host ?? baseURL.absoluteString }
    public func authorize(_ request: inout URLRequest) { headers.forEach { request.setValue($1, forHTTPHeaderField: $0) } }
    public var descriptor: ServerEndpointDescriptor { .init(kind: .direct, url: baseURL.absoluteString, headers: headers) }
}

public enum ServerEndpoints {
    /// Infer a sensible default strategy from a typed address: `https://…` → tunnel, otherwise LAN.
    /// (Settings can still override the kind explicitly.)
    public static func infer(from text: String) -> (any ServerEndpoint)? {
        guard let u = normalizeServerURL(text) else { return nil }
        return u.scheme == "https" ? TunnelEndpoint(baseURL: u) : LocalNetworkEndpoint(baseURL: u)
    }
}

/// Normalize a user-typed address (default scheme `http://`, trim a trailing slash, require a host).
public func normalizeServerURL(_ text: String) -> URL? {
    var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !s.isEmpty else { return nil }
    if !s.contains("://") { s = "http://" + s }
    while s.hasSuffix("/") { s.removeLast() }
    guard let u = URL(string: s), u.host != nil else { return nil }
    return u
}
