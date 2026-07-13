import Foundation

// Tier-2 presenter — a 1:1 Swift port of `library-core.js`. Given a `Platform` adapter, turns raw data
// into neutral view-models for the features (Search / Browse / Content / Detail / Settings / Nav). No
// SwiftUI/UIKit; errors and offline are encoded as FIELDS on the view-model (never thrown), exactly
// like the JS `catch` branches — so a SwiftUI renderer consumes the same shapes the web/PWA do.

public struct Option: Equatable, Sendable { public let value: String; public let label: String }
public let THEME_OPTIONS: [Option] = [.init(value: "auto", label: "🖥 Auto"),
                                      .init(value: "light", label: "☀ Light"),
                                      .init(value: "dark", label: "🌙 Dark")]
public let SHELF_OPTIONS: [Option] = [.init(value: "spine", label: "Spines"),
                                      .init(value: "cover", label: "Covers")]

// ── view-model outputs ────────────────────────────────────────────────────────
public struct SearchVM: Equatable, Sendable {
    public var q: String
    public var cards: [Card]
    public var empty: Bool
    public var offline: Bool = false
    public var error: String? = nil
}

public struct BrowseHitVM: Equatable, Sendable {
    public var type: String
    public var label: String
    public var sublabel: String
    public var ref: Ref?
}
public struct BrowseGroupVM: Equatable, Sendable {
    public var key: String?
    public var label: String
    public var labelPlural: String
    public var count: Int
    public var hits: [BrowseHitVM]
}
public struct BrowseVM: Equatable, Sendable {
    public var q: String
    public var only: String?
    public var groups: [BrowseGroupVM]
    public var empty: Bool
    public var offline: Bool = false
    public var error: String? = nil
}

public struct ContentCardVM: Equatable, Sendable, Identifiable {
    public var eid: Int
    public var title: String
    public var authors: [String]
    public var snippets: [String]
    public var ref: Ref
    public var id: Int { eid }
}
public struct ContentVM: Equatable, Sendable {
    public var q: String
    public var books: [ContentCardVM]
    public var empty: Bool
    public var available: Bool
    public var offline: Bool = false
    public var error: String? = nil
}

public struct DetailVM: Equatable, Sendable {
    public var eid: Int
    public var missing: Bool = false
    public var offline: Bool = false
    public var error: String? = nil
    public var title: String = ""
    public var by: String = ""
    public var authors: [String] = []
    public var translators: [String] = []
    public var subjects: [String] = []
    public var isbns: [String] = []
    public var publisher: String? = nil
    public var year: Int? = nil
    public var tradition: String? = nil                  // the edition's Buddhist tradition (display)
    public var workTitles: [String] = []
    public var connections: [EditionConnection] = []     // other editions of the contained works
    public var coverUrl: String = ""
    public var holdings: [Holding] = []
    public var ref: Ref? = nil
}

public struct SettingsVM: Equatable, Sendable {
    public var theme: String
    public var shelfArt: String
    public var seriesCoverStyle: String          // active SeriesCover style key (SERIES_COVER_STYLES)
    public var shelfTitles: Bool                 // show the title below each cover on a Shelf (default off)
    public var themeOptions: [Option]
    public var shelfOptions: [Option]
    public var seriesCoverStyles: [SeriesCoverStyleSpec]
}

public struct NavItem: Equatable, Sendable {
    public var key: String
    public var label: String
    public var icon: String
    public var href: String?
    public var active: Bool
}
public struct NavVM: Equatable, Sendable { public var items: [NavItem] }

// ── Home shelves ───────────────────────────────────────────────────────────────
/// A "box-set" the renderer collapses into one tile + a volume drawer.
public struct HomeSet: Equatable, Sendable, Identifiable {
    public var name: String
    public var count: Int
    public var cards: [Card]
    public var id: String { name }
}
/// One home rail. `kind` ∈ recent|added|subject|series. `cards` carries recent/added/subject
/// tiles; `sets` carries the series rail; `id` is the subject id (subject rails) for navigation.
public struct HomeRail: Equatable, Sendable, Identifiable {
    public var kind: String
    public var title: String
    public var id: Int?
    public var count: Int?
    public var cards: [Card]
    public var sets: [HomeSet]
    // Stable identity for ForEach (subject rails share kind/title space → key by id when present).
    public var identity: String { "\(kind):\(id.map(String.init) ?? title)" }
}
public struct HomeVM: Equatable, Sendable { public var rails: [HomeRail]; public var empty: Bool }

// ── Wishlist ──────────────────────────────────────────────────────────────────
/// One wishlist tile. Mirrors `library-core.js` `_wishlistCard`. `badge` flags an item the resolver
/// couldn't fully identify (e.g. "Add details", "Choose edition") so nothing is silently dropped.
public struct WishlistCard: Equatable, Sendable, Identifiable {
    public var id: Int
    public var title: String
    public var by: String
    public var year: Int?
    public var publisher: String?
    public var isbn: String?
    public var status: String
    public var badge: String
    public var coverUrl: String?
    public var candidateCount: Int
    public var matchedEditionId: Int?
}
/// A wishlist group (`kind` = the resolution status). The renderer paints one section per group.
public struct WishlistGroup: Equatable, Sendable, Identifiable {
    public var kind: String
    public var title: String
    public var cards: [WishlistCard]
    public var id: String { kind }
}
/// The wishlist screen composed from the cached `/api/v1/wishlist` payload (mirrors `wishlistVM`).
/// `count` = still-wanted items (everything not yet acquired).
public struct WishlistVM: Equatable, Sendable {
    public var groups: [WishlistGroup]
    public var count: Int
    public var empty: Bool
}

/// A user intent on the wishlist — input to the shared `wishlistRequest` mapper.
public enum WishlistAction: Equatable, Sendable {
    case list
    case add(body: [String: JSONValue])
    case remove(id: Int)
    case pick(id: Int, index: Int)
    case confirm(id: Int, editionId: Int)
    case decline(id: Int)
}

/// The backend request a `WishlistAction` maps to. A surface's adapter EXECUTES this (method/path/
/// body) — it never hardcodes the endpoint, so web/PWA/iOS issue identical requests.
public struct WishlistRequest: Equatable, Sendable {
    public var method: String
    public var path: String
    public var body: [String: JSONValue]?
}

/// A star toggle intent — input to the shared `starredRequest` mapper. The cover star button reads the
/// current state from the cached starred set and fires `.star`/`.unstar`.
public enum StarredAction: Equatable, Sendable {
    case list
    case star(eid: Int)
    case unstar(eid: Int)
}

/// The backend request a `StarredAction` maps to (same {method,path,body} shape as `WishlistRequest`),
/// executed by the surface's adapter so no endpoint is hardcoded.
public struct StarredRequest: Equatable, Sendable {
    public var method: String
    public var path: String
    public var body: [String: JSONValue]?
}

// ── Subject page ────────────────────────────────────────────────────────────────
public struct SubjectCrumb: Equatable, Sendable, Identifiable { public var name: String; public var label: String; public var id: String { name } }
public struct SubjectChild: Equatable, Sendable, Identifiable {
    public var name: String; public var leaf: String; public var books: [Card]; public var id: String { name }
}
/// One subject's page composed from the replica (mirrors `library-core.js` subjectVM + the server `/subject`).
public struct SubjectVM: Equatable, Sendable {
    public var name: String; public var leaf: String; public var count: Int
    public var crumbs: [SubjectCrumb]; public var children: [SubjectChild]
    public var books: [Card]; public var leftover: [Card]
}

/// A nav menu item the surface supplies (the renderer presents it however it likes).
public struct NavMenuItem: Equatable, Sendable, Codable {
    public var key: String
    public var label: String
    public var icon: String?
    public var href: String?
    public var `protocol`: String?
    public init(key: String, label: String, icon: String? = nil, href: String? = nil, protocol p: String? = nil) {
        self.key = key; self.label = label; self.icon = icon; self.href = href; self.protocol = p
    }
}

// ── the presenter ─────────────────────────────────────────────────────────────
public enum LibraryCore {
    private static func trim(_ s: String?) -> String { (s ?? "").trimmingCharacters(in: .whitespacesAndNewlines) }

    public static func searchVM(_ p: Platform, _ q: String?) async -> SearchVM {
        let q = trim(q)
        do {
            let cards = try await p.data.search(q)
            return SearchVM(q: q, cards: cards, empty: cards.isEmpty)
        } catch {
            if p.isOffline() { return SearchVM(q: q, cards: [], empty: true, offline: true) }
            return SearchVM(q: q, cards: [], empty: true, error: String(describing: error))
        }
    }

    public static func browseVM(_ p: Platform, _ q: String?, only: String? = nil) async -> BrowseVM {
        let q = trim(q)
        let only = only
        if q.isEmpty { return BrowseVM(q: q, only: only, groups: [], empty: true) }
        do {
            let doc = try await p.data.browse(q, only: only)
            let groups = doc.groups.map { g -> BrowseGroupVM in
                BrowseGroupVM(
                    key: g.key, label: g.label, labelPlural: g.labelPlural ?? g.label,
                    count: g.count ?? g.hits.count,
                    hits: g.hits.map { h in
                        BrowseHitVM(type: h.type ?? g.label, label: h.label,
                                    sublabel: h.sublabel ?? "", ref: refFromUrl(h.url))
                    })
            }
            let total = groups.reduce(0) { $0 + $1.hits.count }
            return BrowseVM(q: q, only: only, groups: groups, empty: total == 0)
        } catch {
            if p.isOffline() { return BrowseVM(q: q, only: only, groups: [], empty: true, offline: true) }
            return BrowseVM(q: q, only: only, groups: [], empty: true, error: String(describing: error))
        }
    }

    public static func contentVM(_ p: Platform, _ q: String?) async -> ContentVM {
        let q = trim(q)
        if q.isEmpty { return ContentVM(q: q, books: [], empty: true, available: true) }
        do {
            let doc = try await p.data.content(q)
            let books = doc.books.map { b in
                ContentCardVM(eid: b.eid, title: b.title, authors: b.authors, snippets: b.snippets, ref: editionRef(b.eid))
            }
            return ContentVM(q: q, books: books, empty: books.isEmpty, available: doc.available)
        } catch {
            // Content search is the one live-only feature: offline w/o a local index → not available.
            if p.isOffline() { return ContentVM(q: q, books: [], empty: true, available: false, offline: true) }
            return ContentVM(q: q, books: [], empty: true, available: true, error: String(describing: error))
        }
    }

    public static func detailVM(_ p: Platform, _ eid: Int) async -> DetailVM {
        do {
            guard let e = try await p.data.detail(eid) else { return DetailVM(eid: eid, missing: true) }
            let authors = e.authors
            let by = authors.isEmpty ? "no author" : authors.joined(separator: ", ")
            return DetailVM(
                eid: eid, title: e.displayTitle ?? (e.title.isEmpty ? "edition #\(eid)" : e.title),
                by: by, authors: authors, translators: e.translators, subjects: e.subjects,
                isbns: e.isbns, publisher: e.publisher, year: e.year, tradition: e.tradition,
                workTitles: e.workTitles,
                connections: e.connections ?? [],
                coverUrl: e.coverUrl ?? artFor(eid).coverUrl,
                holdings: e.holdings.filter { $0.hasFile }, ref: editionRef(eid))
        } catch {
            if p.isOffline() { return DetailVM(eid: eid, offline: true) }
            return DetailVM(eid: eid, error: String(describing: error))
        }
    }

    // ── Home shelves (PURE) — a 1:1 port of `library-core.js` homeVM ──────────────
    // Computed from data the client already holds (cached replica + local recently-opened
    // history), so it's pure, not an async fetch. The single composition of "which rails, in
    // what order" for every surface; the SwiftUI renderer only paints it.
    static func volumeSortKey(_ vol: String?) -> (Int, Int, String) {
        let s = (vol ?? "").trimmingCharacters(in: .whitespaces)
        if s.isEmpty { return (1, 1 << 30, "") }
        let lead = String(s.prefix { $0.isASCII && $0.isNumber })
        if let n = Int(lead) { return (0, n, s.lowercased()) }     // leading int → 2 < 10
        return (0, 1 << 30, s.lowercased())                       // no leading int → sorts after ints
    }

    /// A home/shelf card from an edition row (the `_homeCard` shape shared by home + subject pages).
    /// `starred` (set from the client's starred set) + `badge` ('New' on a newly-added Recent card)
    /// mirror the JS `mk()` tagging.
    static func homeCard(_ e: EditionRow, starred: Set<Int> = [], badge: String = "") -> Card {
        let art = artFor(e.editionId)
        return Card(eid: e.editionId,
                    title: e.displayTitle ?? (e.title.isEmpty ? "edition #\(e.editionId)" : e.title),
                    by: e.authors.joined(separator: ", "),
                    coverUrl: e.coverUrl ?? art.coverUrl, spineUrl: e.spineUrl ?? art.spineUrl,
                    starred: starred.contains(e.editionId), badge: badge)
    }

    /// `homeVM` also takes `starredIds` (the client's `/api/v1/starred` set — a sibling input like
    /// `recentIds`, NOT in the replica): it drives the Starred rail and tags every card's `starred`.
    // Parse a catalogue timestamp to epoch-ms. Handles date-only ('2024-01-10'), SQLite
    // 'YYYY-MM-DD HH:MM:SS' (UTC, space, no zone), and ISO with a 'T'/zone. nil if unparseable.
    private static func epochMs(_ s: String?) -> Double? {
        guard let s = s, !s.isEmpty else { return nil }
        var t = s.contains("T") ? s : s.replacingOccurrences(of: " ", with: "T")
        if t.count <= 10 {
            t += "T00:00:00Z"
        } else if !(t.hasSuffix("Z") || t.contains("+")
                    || t.range(of: #"-\d\d:\d\d$"#, options: .regularExpression) != nil) {
            t += "Z"   // assume UTC when zoneless
        }
        let f1 = ISO8601DateFormatter(); f1.formatOptions = [.withInternetDateTime]
        if let d = f1.date(from: t) { return d.timeIntervalSince1970 * 1000 }
        let f2 = ISO8601DateFormatter(); f2.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f2.date(from: t) { return d.timeIntervalSince1970 * 1000 }
        return nil
    }

    public static func homeVM(_ replica: Replica, recentIds: [Int], starredIds: [Int] = [],
                              perRow: Int = 40, recent: Int = 24, recentDays: Int = 30) -> HomeVM {
        // "Recently added" is bounded to a recency WINDOW (default 30 days), measured from
        // replica.exportedAt, so old never-read books drop out of Recent over time. No/unparseable
        // exportedAt → no window (include all). Mirrors library-core.js homeVM.
        let addedCutoff: Double? = epochMs(replica.exportedAt).map { $0 - Double(recentDays) * 86_400_000 }
        let editions = replica.editions
        let forest = replica.subjectForest ?? []
        var byId = [Int: EditionRow](minimumCapacity: editions.count)
        for e in editions { byId[e.editionId] = e }

        let starredSet = Set(starredIds)
        // mk = a home card tagged with starred state + an optional 'New' badge (Recent rail only).
        func mk(_ e: EditionRow, _ badge: String = "") -> Card { homeCard(e, starred: starredSet, badge: badge) }
        // date_added DESC, eid DESC tiebreak; missing date sorts last (empty string is least).
        let byAdded = editions.sorted { a, b in
            let da = a.dateAdded ?? "", db = b.dateAdded ?? ""
            return da != db ? da > db : a.editionId > b.editionId
        }

        var rails: [HomeRail] = []

        // 1. Recent — recently OPENED first (no badge), then newest-ADDED not already shown ('New'),
        //    falling back to newest-added (all 'New') before anything is opened.
        var seen = Set<Int>(); var recentCards: [Card] = []
        for id in recentIds where !seen.contains(id) {
            if let row = byId[id] { seen.insert(id); recentCards.append(mk(row, "")) }
        }
        for row in byAdded {
            if recentCards.count >= recent { break }
            if seen.contains(row.editionId) { continue }
            if let cut = addedCutoff, let da = epochMs(row.dateAdded), da < cut { continue }   // outside window
            seen.insert(row.editionId); recentCards.append(mk(row, "New"))
        }
        recentCards = Array(recentCards.prefix(recent))
        if !recentCards.isEmpty {
            rails.append(HomeRail(kind: "recent", title: "Recent", id: nil, count: nil,
                                  cards: recentCards, sets: []))
        }
        // 2. Starred — the curated favourites, newest-starred first; non-live editions skipped.
        let starredCards = starredIds.compactMap { byId[$0] }.map { mk($0, "") }
        if !starredCards.isEmpty {
            rails.append(HomeRail(kind: "starred", title: "Starred", id: nil, count: nil,
                                  cards: starredCards, sets: []))
        }
        // 3. Subject shelves — top-level topics rolled up by name; fuller first, protected sunk.
        struct Agg { let node: SubjectNode; let label: String; let members: [EditionRow] }
        var subj: [Agg] = forest.filter { $0.depth == 0 }.map { n in
            let label = n.leafLabel.isEmpty ? n.name : n.leafLabel
            let members = editions
                .filter { e in e.subjects.contains { subjectTopLevel($0) == label } }
                .sorted { $0.editionId > $1.editionId }
            return Agg(node: n, label: label, members: members)
        }.filter { !$0.members.isEmpty }
        subj.sort { a, b in
            let pa = a.node.isProtected ? 1 : 0, pb = b.node.isProtected ? 1 : 0
            if pa != pb { return pa < pb }
            if a.members.count != b.members.count { return a.members.count > b.members.count }
            return a.label.lowercased() < b.label.lowercased()
        }
        for r in subj {
            rails.append(HomeRail(kind: "subject", title: r.label, id: r.node.id, count: r.members.count,
                                  cards: r.members.prefix(perRow).map { mk($0) }, sets: []))
        }
        // 4. Series — group by name, each set ordered by volume; one rail of sets.
        var bag = [String: [EditionRow]](); var order: [String] = []
        for e in editions {
            for name in (e.series ?? []) {
                if bag[name] == nil { bag[name] = []; order.append(name) }
                bag[name]!.append(e)
            }
        }
        order.sort { $0.lowercased() < $1.lowercased() }
        let sets = order.map { name -> HomeSet in
            let rows = (bag[name] ?? []).sorted { a, b in
                let ka = volumeSortKey(a.volume), kb = volumeSortKey(b.volume)
                return ka != kb ? ka < kb : a.editionId < b.editionId
            }
            return HomeSet(name: name, count: rows.count, cards: rows.prefix(perRow).map { mk($0) })
        }
        if !sets.isEmpty { rails.append(HomeRail(kind: "series", title: "Series", id: nil, count: nil, cards: [], sets: sets)) }

        return HomeVM(rails: rails, empty: rails.isEmpty)
    }

    // ── Wishlist VM — 1:1 port of library-core.js wishlistVM ────────────────────────
    /// Fixed attention-first group order + badges, mirroring `WISHLIST_GROUPS`/`WISHLIST_BADGES`.
    static let wishlistGroups: [(status: String, title: String)] = [
        ("ambiguous", "Choose an edition"), ("suspected", "Might already be in your library"),
        ("unresolved", "Needs details"), ("resolved", "Wishlist"),
        ("owned", "Already in your library"), ("acquired", "Acquired"),
    ]
    static let wishlistBadges: [String: String] = [
        "ambiguous": "Choose edition", "suspected": "Confirm match", "unresolved": "Add details",
        "resolved": "", "owned": "Already owned", "acquired": "Acquired",
    ]

    static func wishlistCard(_ it: WishlistItemRow) -> WishlistCard {
        let isbn = it.isbn ?? it.rawIsbn
        let title = it.title ?? it.rawTitle ?? (isbn.map { "ISBN \($0)" } ?? "Untitled")
        let authors = !it.authors.isEmpty ? it.authors : (it.rawAuthor.map { [$0] } ?? [])
        let cover = it.coverUrl ?? isbn.map { "https://covers.openlibrary.org/b/isbn/\($0)-L.jpg" }
        return WishlistCard(
            id: it.id, title: title, by: authors.joined(separator: ", "),
            year: it.year, publisher: it.publisher, isbn: isbn, status: it.status,
            badge: wishlistBadges[it.status] ?? "", coverUrl: cover,
            candidateCount: it.candidates.count, matchedEditionId: it.matchedEditionId)
    }

    /// Compose the wishlist screen from the cached payload. Groups by status (fixed order); a book
    /// the resolver couldn't identify surfaces in its `unresolved`/`ambiguous` group, never dropped.
    public static func wishlistVM(_ wishlist: WishlistPayload) -> WishlistVM {
        let items = wishlist.items
        let groups: [WishlistGroup] = wishlistGroups.compactMap { gdef in
            let cards = items.filter { $0.status == gdef.status }.map(wishlistCard)
            return cards.isEmpty ? nil : WishlistGroup(kind: gdef.status, title: gdef.title, cards: cards)
        }
        let count = items.filter { $0.status != "acquired" }.count
        return WishlistVM(groups: groups, count: count, empty: items.isEmpty)
    }

    // ── Wishlist COMMAND path — 1:1 port of library-core.js (shared, golden-locked) ──
    /// `wishlistRequest` maps a user intent → the backend request; the iOS adapter (CatalogueAPI)
    /// EXECUTES this, so it never hardcodes a `/api/v1/...` endpoint. `wishlistAddMessage` maps the
    /// add response → the one-wording-everywhere user message. Mirrors `LibraryCore` JS exactly.
    public static func wishlistRequest(_ action: WishlistAction) -> WishlistRequest {
        let base = "/api/v1/wishlist"
        switch action {
        case .list:
            return WishlistRequest(method: "GET", path: base, body: nil)
        case .add(let body):
            return WishlistRequest(method: "POST", path: base, body: body)
        case .remove(let id):
            return WishlistRequest(method: "DELETE", path: "\(base)/\(id)", body: nil)
        case .pick(let id, let index):
            return WishlistRequest(method: "PATCH", path: "\(base)/\(id)", body: ["pick": .int(index)])
        case .confirm(let id, let editionId):
            return WishlistRequest(method: "PATCH", path: "\(base)/\(id)",
                                   body: ["confirm_owned": .int(editionId)])
        case .decline(let id):
            return WishlistRequest(method: "PATCH", path: "\(base)/\(id)",
                                   body: ["decline_suspected": .bool(true)])
        }
    }

    public static func wishlistAddMessage(_ resp: WishlistAddResponse) -> String {
        if resp.owned == true { return "You already own this — not added." }
        if resp.duplicate == true { return "Already on your wishlist." }
        switch resp.item?.status {
        case "suspected":  return "Added — you might already own this; confirm below."
        case "unresolved": return "Added — needs details (couldn’t identify it)."
        case "ambiguous":  return "Added — choose the right edition below."
        default:           return "Added to wishlist."
        }
    }

    // ── Starred COMMAND path — 1:1 port of library-core.js starredRequest ────────────
    /// Maps a star toggle intent → the backend request; the iOS adapter (CatalogueAPI) EXECUTES it,
    /// so it never hardcodes a `/api/v1/...` endpoint. Mirrors `LibraryCore.starredRequest` JS exactly.
    public static func starredRequest(_ action: StarredAction) -> StarredRequest {
        let base = "/api/v1/starred"
        switch action {
        case .list:
            return StarredRequest(method: "GET", path: base, body: nil)
        case .star(let eid):
            return StarredRequest(method: "POST", path: base, body: ["edition_id": .int(eid)])
        case .unstar(let eid):
            return StarredRequest(method: "DELETE", path: "\(base)/\(eid)", body: nil)
        }
    }

    // ── Replica Search / Browse (the shared MATCHER) — 1:1 port of library-core.js ──
    // Match = fold(query) ⊂ the row's server-built `search_text`, both re-folded with the parity-tested
    // `fold` so the query and haystack normalise identically. People/Subjects match folded names. The
    // single offline matcher so native and PWA agree exactly (no per-client re-derived blob).
    private static func replicaRows(_ r: Replica) -> [EditionRow] { r.editions.reversed() }   // newest-first
    // Folded haystack: prefer the server-built `search_text`; fall back to a derived blob (thin/test rows).
    private static func derivedBlob(_ e: EditionRow) -> String {
        ([e.title, e.displayTitle, e.subtitle, e.publisher].compactMap { $0 }
         + e.authors + e.translators + e.isbns + e.subjects + e.workTitles).joined(separator: " ")
    }
    private static func hay(_ e: EditionRow) -> String { nameKey(e.searchText ?? derivedBlob(e)) }
    private static func searchCard(_ e: EditionRow) -> Card {
        let art = artFor(e.editionId)
        return Card(eid: e.editionId, title: e.title, displayTitle: e.displayTitle, by: e.authors.first ?? "",
                    holdingId: e.holdings.first?.holdingId, hasFile: e.holdings.contains { $0.hasFile },
                    coverUrl: e.coverUrl ?? art.coverUrl, spineUrl: e.spineUrl ?? art.spineUrl)
    }
    // Term-AND: every whitespace-separated query term must be a substring (same rule as the PWA).
    private static func terms(_ q: String) -> [String] { nameKey(trim(q)).split(whereSeparator: { $0.isWhitespace }).map(String.init) }
    private static func allIn(_ hay: String, _ terms: [String]) -> Bool { terms.allSatisfy { hay.contains($0) } }
    // Matches by text OR by edition NUMBER (eid) when the whole query is digits.
    private static func matchRow(_ e: EditionRow, _ terms: [String]) -> Bool {
        if allIn(hay(e), terms) { return true }
        return terms.count == 1 && terms[0].allSatisfy { $0.isASCII && $0.isNumber } && String(e.editionId) == terms[0]
    }
    private static func distinctMatching(_ values: [String], _ terms: [String]) -> [String] {
        var seen = Set<String>(); var out: [String] = []
        for v in values { let k = nameKey(v); if (terms.isEmpty || allIn(k, terms)) && seen.insert(k).inserted { out.append(v) } }
        return out.sorted()
    }

    public static func searchReplica(_ replica: Replica, _ q: String) -> [Card] {
        let ts = terms(q)
        return replicaRows(replica).filter { ts.isEmpty || matchRow($0, ts) }.map(searchCard)
    }

    /// Typeahead suggestions — top book matches shaped as `Suggestion`. Shared so PWA + native agree.
    public static func suggestReplica(_ replica: Replica, _ q: String) -> [Suggestion] {
        searchReplica(replica, q).prefix(8).map {
            Suggestion(type: "Book", label: $0.displayTitle ?? $0.title, sublabel: $0.by, url: "/library?eid=\($0.eid)")
        }
    }

    public static func browseReplica(_ replica: Replica, _ q: String, only: String? = nil) -> BrowseDoc {
        let ts = terms(q)
        var groups: [BrowseGroup] = []
        let books = replicaRows(replica).filter { ts.isEmpty || matchRow($0, ts) }
        if (only == nil || only == "editions"), !books.isEmpty {
            groups.append(BrowseGroup(key: "editions", label: "Book", labelPlural: "Books", count: books.count,
                hits: books.map { BrowseHit(type: "Book", label: $0.displayTitle ?? $0.title, url: "/library?eid=\($0.editionId)") }))
        }
        // Works: match each work's folded all-alias blob (any spelling), display the canonical title.
        var workBlobs: [String: String] = [:]
        for e in replica.editions {
            for w in (e.works ?? []) { workBlobs[w.title, default: ""] += " " + nameKey(w.search ?? w.title) }
        }
        let works = workBlobs.keys.filter { ts.isEmpty || allIn(workBlobs[$0]!, ts) }.sorted()
        if (only == nil || only == "works"), !works.isEmpty {
            groups.append(BrowseGroup(key: "works", label: "Work", labelPlural: "Works", count: works.count,
                hits: works.map { BrowseHit(type: "Work", label: $0, url: nil) }))
        }
        let people = distinctMatching(replica.editions.flatMap { $0.authors + $0.translators }, ts)
        if (only == nil || only == "people"), !people.isEmpty {
            groups.append(BrowseGroup(key: "people", label: "Person", labelPlural: "People", count: people.count,
                hits: people.map { BrowseHit(type: "Person", label: $0, url: nil) }))
        }
        var sidMap: [String: Int] = [:]
        for n in (replica.subjectForest ?? []) { sidMap[n.name] = n.id }
        let subjects = distinctMatching(replica.editions.flatMap { $0.subjects }, ts)
        if (only == nil || only == "subjects"), !subjects.isEmpty {
            groups.append(BrowseGroup(key: "subjects", label: "Subject", labelPlural: "Subjects", count: subjects.count,
                hits: subjects.map { BrowseHit(type: "Subject", label: $0, url: sidMap[$0].map { "/subject/\($0)" }) }))
        }
        return BrowseDoc(groups: groups)
    }

    /// The subject page (PURE) — a 1:1 port of `library-core.js` subjectVM. Composes one subject's
    /// crumbs, child sub-subjects (each with its books), descendant-inclusive books, and the leftover
    /// books under no child, all from the cached replica.
    public static func subjectVM(_ replica: Replica, _ name: String) -> SubjectVM {
        let eds = replica.editions.filter { e in e.subjects.contains { isUnderSubject($0, name) } }
        let segs = name.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        let crumbs = segs.indices.map { i in SubjectCrumb(name: segs[0...i].joined(separator: "/"), label: segs[i]) }
        var seen = Set<String>(); var childNames: [String] = []
        for e in eds {
            for s in e.subjects where s.hasPrefix(name + "/") {
                let rest = String(s.dropFirst(name.count + 1))
                let c = name + "/" + String(rest.prefix(while: { $0 != "/" }))
                if seen.insert(c).inserted { childNames.append(c) }
            }
        }
        childNames.sort()
        let children = childNames.map { c -> SubjectChild in
            let books = eds.filter { e in e.subjects.contains { isUnderSubject($0, c) } }.map { homeCard($0) }
            return SubjectChild(name: c, leaf: c.split(separator: "/").last.map(String.init) ?? c, books: books)
        }
        var covered = Set<Int>()
        for ch in children { for b in ch.books { covered.insert(b.eid) } }
        let leftover = eds.filter { !covered.contains($0.editionId) }.map { homeCard($0) }
        return SubjectVM(name: name, leaf: segs.last ?? name, count: eds.count,
                         crumbs: crumbs, children: children, books: eds.map { homeCard($0) }, leftover: leftover)
    }

    public static func settingsVM(_ p: Platform) -> SettingsVM {
        let theme0 = p.prefs.get("theme")
        let theme = (theme0 == "light" || theme0 == "dark") ? theme0! : "auto"
        let shelf = p.prefs.get("shelfArt") == "spine" ? "spine" : "cover"
        let set0 = p.prefs.get("setStyle")
        let setStyle = SERIES_COVER_STYLES.contains { $0.key == set0 } ? set0! : SERIES_COVER_DEFAULT
        let shelfTitles = p.prefs.get("shelfTitles") == "on"
        return SettingsVM(theme: theme, shelfArt: shelf, seriesCoverStyle: setStyle, shelfTitles: shelfTitles,
                          themeOptions: THEME_OPTIONS, shelfOptions: SHELF_OPTIONS,
                          seriesCoverStyles: SERIES_COVER_STYLES)
    }

    public static func navVM(_ items: [NavMenuItem], activeKey: String?, ctx: ProtocolContext) -> NavVM {
        NavVM(items: items
            .filter { protocolVisible($0.protocol ?? "default", ctx) }
            .map { NavItem(key: $0.key, label: $0.label, icon: $0.icon ?? "", href: $0.href,
                           active: activeKey != nil && $0.key == activeKey) })
    }
}
