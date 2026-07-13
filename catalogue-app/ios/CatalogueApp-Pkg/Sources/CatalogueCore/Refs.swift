import Foundation

/// A toolkit-agnostic navigation reference — the neutral target a hit/card points at, mapped by each
/// platform to its own navigation. 1:1 with `library-core.js` (`editionRef`/`refFromUrl`). A `subject`
/// may carry an id (canonical `/subject/<id>`) or just a query name (fall back to a filtered search).
public enum Ref: Equatable, Sendable {
    case edition(Int)
    case work(Int)
    case person(Int)
    case subject(id: Int?, q: String?)
    case url(String)
}

public func editionRef(_ eid: Int) -> Ref { .edition(eid) }

/// Parse a server-emitted web URL (from `/find` hits) into a neutral `Ref`, so a non-web UI maps it to
/// its own navigation instead of following a URL. Mirrors `refFromUrl` exactly, including precedence.
public func refFromUrl(_ url: String?) -> Ref? {
    guard let url, !url.isEmpty else { return nil }
    func firstInt(_ pattern: String, _ s: String) -> Int? {
        guard let re = try? NSRegularExpression(pattern: pattern),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)),
              m.numberOfRanges > 1, let r = Range(m.range(at: 1), in: s) else { return nil }
        return Int(s[r])
    }
    if let id = firstInt(#"[?&]eid=(\d+)"#, url) { return .edition(id) }
    if let id = firstInt(#"^/edition/(\d+)"#, url) { return .edition(id) }
    if let id = firstInt(#"^/work/(\d+)"#, url) { return .work(id) }
    if let id = firstInt(#"^/person/(\d+)"#, url) { return .person(id) }
    if let id = firstInt(#"^/subject/(\d+)"#, url) { return .subject(id: id, q: nil) }
    return .url(url)
}

/// Deterministic art handles for an edition (the server route is `/edition/<id>/cover.jpg|spine.svg`).
public struct Art: Equatable, Sendable {
    public let coverUrl: String
    public let spineUrl: String
}
public func artFor(_ eid: Int) -> Art {
    Art(coverUrl: "/edition/\(eid)/cover.jpg", spineUrl: "/edition/\(eid)/spine.svg")
}

// ── Subject-hierarchy helpers (the '/' path is the hierarchy — one shared rule) ──
public func subjectTopLevel(_ name: String?) -> String { String(name ?? "").split(separator: "/", omittingEmptySubsequences: false).first.map(String.init) ?? "" }
public func isUnderSubject(_ name: String?, _ ancestor: String?) -> Bool {
    let n = name ?? "", a = ancestor ?? ""
    return n == a || n.hasPrefix(a + "/")
}

/// A native app route — the iOS analogue of the web URL `hrefFor` returns. `work`/`person` have no
/// native destination (mirrors the PWA adapter, which returns null for them).
public enum Route: Equatable, Sendable {
    case detail(eid: Int)
    case subject(id: Int)
    case search(subject: String)
    case external(String)

    /// A stable path string (for deep links / parity asserts).
    public var path: String {
        switch self {
        case .detail(let eid): return "/library?eid=\(eid)"
        case .subject(let id): return "/subject/\(id)"
        case .search(let s): return "/search?subject=\(s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s)"
        case .external(let u): return u
        }
    }
}

public extension Ref {
    /// The native route for this ref, or nil when there is no native destination (work/person).
    var route: Route? {
        switch self {
        case .edition(let id): return .detail(eid: id)
        case .work, .person: return nil
        case .subject(let id, let q):
            if let id { return .subject(id: id) }
            return .search(subject: q ?? "")
        case .url(let u): return .external(u)
        }
    }
}

/// The native `nav` port mapping — `hrefFor(ref)` returns a route path or nil (work/person → nil).
public struct NativeNav: Sendable {
    public init() {}
    public func hrefFor(_ ref: Ref?) -> String? { ref?.route?.path }
    public func route(_ ref: Ref?) -> Route? { ref?.route }
    /// Where a holding's "Read" control points when there is no in-app reader (parity with web).
    public func readHref(holdingId: Int?) -> String { holdingId.map { "/holding/\($0)/file" } ?? "#" }
}
