import Foundation
import CatalogueCore

/// The offline content-search facade (`{available, status, load, enable, disable, search}`) the screens
/// program against — so Content search works the same whether served live or from a downloaded FTS5
/// index. The concrete SQLite-FTS5 engine (mirroring the server's `match_fts` semantics — NFKD
/// normalize, whole-query phrase, `ORDER BY bm25`) is a substantial component fetched from
/// `/api/v1/content-index`; this ships the seam plus a `NoContentIndex` default so the app degrades
/// gracefully (Content is live-only until the index is enabled). See ios_native_plan.md §6 / U11.
public enum ContentIndexStatus: String, Sendable { case absent, disabled, loading, ready }

public protocol ContentIndex: Sendable {
    var status: ContentIndexStatus { get }
    var available: Bool { get }
    func enable() async throws
    func disable() async
    func search(_ q: String) async throws -> [ContentBook]
}

public extension ContentIndex {
    var available: Bool { status == .ready }
}

/// The default: no offline index present. Content search reports unavailable offline (the live
/// `/api/v1/content` path serves it when online).
public struct NoContentIndex: ContentIndex {
    public init() {}
    public var status: ContentIndexStatus { .absent }
    public func enable() async throws { throw AdapterUnsupported("offline content index (SQLite FTS5) not built yet") }
    public func disable() async {}
    public func search(_ q: String) async throws -> [ContentBook] { [] }
}
