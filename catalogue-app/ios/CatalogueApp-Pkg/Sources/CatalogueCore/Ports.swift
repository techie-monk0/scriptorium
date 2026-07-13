import Foundation

// The Tier-2 ADAPTER PROTOCOL — each surface (web, PWA, native) supplies one `Platform`. The view
// models in `ViewModels.swift` are written against these ports only, so the exact same presenter logic
// runs on iOS as on the web. Mirrors the `adapter = { data, nav, prefs, isOffline }` object in
// library-core.js. CatalogueAPI (live) + the replica store (offline) implement `DataPort`.

/// Raised by an adapter for a capability it doesn't provide (e.g. the live API has no grouped browse;
/// browse is served from the replica). The view models translate it into an error/offline view-model
/// rather than letting it escape — exactly like the JS `catch`.
public struct AdapterUnsupported: Error, Sendable { public let what: String; public init(_ what: String) { self.what = what } }

// ── browse / suggest wire shapes (the live `/find`-style grouped results) ─────
public struct BrowseHit: Codable, Equatable, Sendable {
    public var type: String?
    public var label: String
    public var sublabel: String?
    public var url: String?
    public init(type: String? = nil, label: String, sublabel: String? = nil, url: String? = nil) {
        self.type = type; self.label = label; self.sublabel = sublabel; self.url = url
    }
}

public struct BrowseGroup: Codable, Equatable, Sendable {
    public var key: String?
    public var label: String
    public var labelPlural: String?
    public var count: Int?
    public var hits: [BrowseHit]
    public init(key: String? = nil, label: String, labelPlural: String? = nil, count: Int? = nil, hits: [BrowseHit]) {
        self.key = key; self.label = label; self.labelPlural = labelPlural; self.count = count; self.hits = hits
    }
}

public struct BrowseDoc: Codable, Equatable, Sendable {
    public var groups: [BrowseGroup]
    public init(groups: [BrowseGroup]) { self.groups = groups }
}

public struct Suggestion: Codable, Equatable, Sendable {
    public var type: String?
    public var label: String
    public var sublabel: String?
    public var url: String?
    public init(type: String? = nil, label: String, sublabel: String? = nil, url: String? = nil) {
        self.type = type; self.label = label; self.sublabel = sublabel; self.url = url
    }
}

// ── ports ─────────────────────────────────────────────────────────────────────
/// Data access (live JSON or replica). `browse`/`suggest` default to "unsupported" so an adapter that
/// only does live search/content/detail (like the web one) need not implement them.
public protocol DataPort: Sendable {
    func search(_ q: String) async throws -> [Card]
    func content(_ q: String) async throws -> ContentResponse
    func detail(_ eid: Int) async throws -> EditionRow?
    func browse(_ q: String, only: String?) async throws -> BrowseDoc
    func suggest(_ q: String) async throws -> [Suggestion]
}
public extension DataPort {
    func browse(_ q: String, only: String?) async throws -> BrowseDoc { throw AdapterUnsupported("browse") }
    func suggest(_ q: String) async throws -> [Suggestion] { throw AdapterUnsupported("suggest") }
}

/// Navigation mapping — `ref → native route`. `NativeNav` is the concrete impl.
public protocol NavPort: Sendable {
    func hrefFor(_ ref: Ref?) -> String?
}
extension NativeNav: NavPort {}

/// Device prefs (the renderer applies them; the neutral keys/semantics are shared).
public protocol PrefsPort: AnyObject, Sendable {
    func get(_ key: String) -> String?
    func set(_ key: String, _ value: String)
    func remove(_ key: String)
}

/// The bundle a surface supplies to the presenter. `isOffline()` selects live vs. replica paths.
public protocol Platform: Sendable {
    var data: DataPort { get }
    var nav: NavPort { get }
    var prefs: PrefsPort { get }
    func isOffline() -> Bool
}
