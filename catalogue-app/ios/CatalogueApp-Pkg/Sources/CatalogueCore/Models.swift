import Foundation

// Codable mirrors of the server's `/api/v1/*` JSON + replica rows. Property names are camelCase; the
// decoder uses `.convertFromSnakeCase` (see `CatalogueJSON`), so `edition_id` ⇄ `editionId` etc. with
// no hand-written CodingKeys. Unknown wire fields are tolerated (Swift ignores keys it doesn't model);
// optionals absorb missing fields. Shapes traced 1:1 to: routes/api.py, services/export_replica.py,
// services/subject_tree.py, services/library.py.

/// A lossless, opaque JSON value — used for the provider-specific `storage` ref on a holding, which
/// the client must carry but never interpret (the kDrive-doesn't-leak seam).
public enum JSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int.self) { self = .int(v) }
        else if let v = try? c.decode(Double.self) { self = .double(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { throw DecodingError.dataCorruptedError(in: c, debugDescription: "unrepresentable JSON") }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .int(let v): try c.encode(v)
        case .double(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
}

// ── /api/v1/health ──────────────────────────────────────────────────────────
/// Capability probe — lets a client hide what the signed-in identity can't do (server still enforces).
public struct Health: Codable, Equatable, Sendable {
    public var ok: Bool
    public var service: String?
    public var api: Int?
    public var role: String?
    public var canEdit: Bool?
    public var canDownload: Bool?
    // App-version handshake (see AppBuildContract). `appBuild` = the build the server is running;
    // `serverStale` = the server is behind its own code on disk (restart pending). Optional/additive,
    // so an older server that omits them just reads as nil (no drift signalled).
    public var appBuild: String?
    public var serverStale: Bool?
}

// ── /api/v1/library  → browser rows (the metadata "Search"/browse list) ───────
/// `services/library.browser_row` — the master-list row shape.
public struct BrowserRow: Codable, Equatable, Sendable, Identifiable {
    public var id: Int
    public var title: String
    public var displayTitle: String?
    public var subtitle: String?
    public var done: Bool?
    public var holdingId: Int?
    public var hasFile: Bool
    public var fileExt: String?
}

public struct LibraryResponse: Codable, Equatable, Sendable {
    public var q: String?
    public var rows: [BrowserRow]
}

// ── /api/v1/content  → full-text hits grouped by edition ──────────────────────
public struct ContentBook: Codable, Equatable, Sendable, Identifiable {
    public var eid: Int
    public var title: String
    public var authors: [String]
    public var snippets: [String]
    public var id: Int { eid }
}

public struct ContentResponse: Codable, Equatable, Sendable {
    public var q: String?
    public var books: [ContentBook]
    public var available: Bool
    public init(q: String? = nil, books: [ContentBook], available: Bool) {
        self.q = q; self.books = books; self.available = available
    }
}

// ── replica row / /api/v1/edition/<eid>  (services/export_replica.edition_row) ─
/// One openable copy of an edition. `kind` (pdf/epub) is the reader-dispatch key; `storage` is the
/// opaque provider ref (nil → stream from the server via `/holding/<id>/file`).
public struct Holding: Codable, Equatable, Sendable, Identifiable {
    public var holdingId: Int
    public var format: String?
    public var kind: String?
    public var hasFile: Bool
    public var storage: JSONValue?
    public var id: Int { holdingId }
}

public struct EditionRow: Codable, Equatable, Sendable, Identifiable {
    public var editionId: Int
    public var title: String
    public var displayTitle: String?
    public var subtitle: String?
    public var volume: String?
    public var publisher: String?
    public var year: Int?
    public var coverUrl: String?
    public var spineUrl: String?
    public var authors: [String]
    public var translators: [String]
    public var isbns: [String]
    public var subjects: [String]
    public var workTitles: [String]
    public var works: [WorkRef]? = nil                    // contained works + their folded all-alias search blob
    public var holdings: [Holding]
    public var searchText: String?
    public var connections: [EditionConnection]? = nil    // FRBR siblings (other editions of the works)
    // v4 home-rail primitives (optional → tolerant of older schema-3 replicas + the detail fixture).
    public var dateAdded: String? = nil          // earliest holding.date_added — the "Recently added" key
    public var series: [String]? = nil           // series subject names — groups the home "Series" rail
    // v5: the edition's Buddhist tradition (a `tradition.name`) — shown on the book detail.
    public var tradition: String? = nil
    public var id: Int { editionId }
}

// ── /api/v1/wishlist  (catalogue.contracts.WishlistItem) ──────────────────────
/// One wishlist item as carried by the wishlist payload — the input to `wishlistVM`. The resolved
/// snapshot fields are optional so an `unresolved`/`ambiguous` item (the resolver couldn't identify
/// the book) decodes fine; the raw inputs are kept so it can be fixed later. `convertFromSnakeCase`
/// maps `raw_isbn`⇄`rawIsbn` etc.; unknown wire keys (rev/addedAt/…) are tolerated.
public struct WishlistItemRow: Codable, Equatable, Sendable, Identifiable {
    public var id: Int
    public var source: String
    public var status: String
    public var rawIsbn: String? = nil
    public var rawTitle: String? = nil
    public var rawAuthor: String? = nil
    public var title: String? = nil
    public var authors: [String] = []
    public var publisher: String? = nil
    public var year: Int? = nil
    public var isbn: String? = nil
    public var coverUrl: String? = nil
    public var candidates: [JSONValue] = []
    public var matchedEditionId: Int? = nil
}

/// The wishlist payload (`GET /api/v1/wishlist`) — `items` + a schema tag.
public struct WishlistPayload: Codable, Equatable, Sendable {
    public var items: [WishlistItemRow]
    public var schema: Int? = nil
}

// ── /api/v1/starred  (the starred-edition ids behind the Starred rail + highlighted covers) ──
/// The `GET /api/v1/starred` list (and the envelope each toggle write returns). `editions` is the
/// shared starred set the client holds as `starredIds` and feeds to `homeVM`.
public struct StarredPayload: Codable, Equatable, Sendable {
    public var editions: [Int]
    public var schema: Int? = nil
    public init(editions: [Int] = [], schema: Int? = nil) { self.editions = editions; self.schema = schema }
}

/// The `POST /api/v1/wishlist` response envelope. `added` false with `owned`/`duplicate` true means
/// the server declined to add it (already in the catalogue, or already on the wishlist).
public struct WishlistAddResponse: Codable, Equatable, Sendable {
    public var item: WishlistItemRow?
    public var added: Bool?
    public var owned: Bool?
    public var duplicate: Bool?
    public init(item: WishlistItemRow? = nil, added: Bool? = nil,
                owned: Bool? = nil, duplicate: Bool? = nil) {
        self.item = item; self.added = added; self.owned = owned; self.duplicate = duplicate
    }
}

/// A contained work — its display `title` plus a folded blob of ALL its aliases (for Work search).
public struct WorkRef: Codable, Equatable, Sendable {
    public var title: String
    public var search: String?
}

/// A cross-link to another edition (a Connections entry) — the same `{eid,title}` the replica carries.
public struct EditionConnection: Codable, Equatable, Sendable, Identifiable {
    public var eid: Int
    public var title: String
    public var id: Int { eid }
}

public struct Replica: Codable, Equatable, Sendable {
    public var schemaVersion: Int
    public var exportedAt: String?
    public var provider: String?
    public var count: Int
    public var editions: [EditionRow]
    // v4: the topic hierarchy, so the client builds home SUBJECT rails itself (ids + is_protected).
    public var subjectForest: [SubjectNode]? = nil
}

// ── /api/v1/subjects  (services/subject_tree.subject_forest) ──────────────────
public struct SubjectNode: Codable, Equatable, Sendable, Identifiable {
    public var id: Int
    public var name: String
    public var leafLabel: String
    public var depth: Int
    public var parentId: Int?
    public var hasChildren: Bool
    public var isProtected: Bool
    public var nWorks: Int?
    public var nEditions: Int?
    public var nBooksDirect: Int?
    public var nBooksTotal: Int?
}

public struct SubjectsResponse: Codable, Equatable, Sendable {
    public var kind: String
    public var tree: [SubjectNode]
}

// ── /api/v1/subject/<sid>  (services/subject_tree.subject_page) ───────────────
public struct SubjectRef: Codable, Equatable, Sendable, Identifiable {
    public var id: Int
    public var name: String
    public var kind: String?
    public var leafLabel: String?
}

public struct Crumb: Codable, Equatable, Sendable, Identifiable {
    public var id: Int
    public var name: String
    public var leafLabel: String?
}

/// A home/shelf tile — `services/library._home_card` (also the element of `subject_page.books`).
public struct Card: Codable, Equatable, Sendable, Identifiable {
    public var eid: Int
    public var title: String
    public var displayTitle: String?
    public var by: String?
    public var holdingId: Int?
    public var hasFile: Bool?
    public var coverUrl: String?
    public var spineUrl: String?
    // homeVM tags: `starred` paints the cover highlight from one source; `badge` is 'New' on a newly
    // added book in the Recent rail. Optional → cards from other sources (search/browse fixtures) omit
    // them, keeping those goldens unchanged. The cover overlay reads live state from AppModel anyway.
    public var starred: Bool?
    public var badge: String?
    public var id: Int { eid }

    public init(eid: Int, title: String, displayTitle: String? = nil, by: String? = nil,
                holdingId: Int? = nil, hasFile: Bool? = nil, coverUrl: String? = nil, spineUrl: String? = nil,
                starred: Bool? = nil, badge: String? = nil) {
        self.eid = eid; self.title = title; self.displayTitle = displayTitle; self.by = by
        self.holdingId = holdingId; self.hasFile = hasFile; self.coverUrl = coverUrl; self.spineUrl = spineUrl
        self.starred = starred; self.badge = badge
    }
}

public struct SubjectPage: Codable, Equatable, Sendable {
    public var subject: SubjectRef
    public var crumbs: [Crumb]
    public var children: [SubjectNode]
    public var books: [Card]
    public var nBooks: Int
}
