import Foundation

/// The catalogue `/sync/reader` + `/holding/<id>/position` **wire records**, exactly as the server
/// serialises them (snake_case; `rect`/`ink` as JSON *strings*; `holding_id` ints). These are the
/// neutral, transport-free description of the bytes on the wire — a `URLSession` (iOS), an OkHttp
/// (Android), or a `fetch` (web) all read/write these same shapes. Pair with `ReaderWireCodec`, which
/// maps them to/from the postilla model, and the `reader-wire-goldens.json` contract.

/// GET `/sync/reader` → both marks and bookmarks for the scope, plus the advertised contract version.
public struct ReaderPullResponse: Decodable, Sendable {
    public var rev: Int
    public var annotations: [AnnotationRecord]?
    public var bookmarks: [BookmarkRecord]?
    public var outlines: [OutlineRecord]?      // authored PDF outlines (wire contract v2)
    public var contract_version: Int?
}

/// GET `/sync/reader/rev` → the max rev per resource for one copy (the cheap change-probe). A reader
/// stores the last set it merged and compares — if any is higher, that resource changed elsewhere.
public struct ReaderRevResponse: Decodable, Sendable {
    public var bookmarks_rev: Int
    public var annotations_rev: Int
    public var outlines_rev: Int
    public var contract_version: Int?
}

/// The neutral value: max rev per resource for one copy. `Codable` so a client can persist the last
/// set it merged (its per-book sync cursor) and diff the next probe against it.
public struct HoldingRevs: Codable, Equatable, Sendable {
    public var bookmarks: Int
    public var annotations: Int
    public var outlines: Int
    public init(bookmarks: Int = 0, annotations: Int = 0, outlines: Int = 0) {
        self.bookmarks = bookmarks; self.annotations = annotations; self.outlines = outlines
    }
    public static let zero = HoldingRevs()
    /// True if the server has a newer rev for ANY resource than what we last merged (`seen`).
    public func hasChanges(since seen: HoldingRevs) -> Bool {
        bookmarks > seen.bookmarks || annotations > seen.annotations || outlines > seen.outlines
    }
}

/// One authored table-of-contents entry: a heading at `level` (>=1) pointing at 1-based `page`. The
/// neutral shape shared by the wire, the local store, and the editor UI.
public struct OutlineEntry: Codable, Equatable, Sendable {
    public var level: Int
    public var title: String
    public var page: Int
    public init(level: Int = 1, title: String, page: Int) {
        self.level = level; self.title = title; self.page = page
    }
}

/// POST `/sync/reader` body — a batch of upsert ops (annotations and/or bookmarks), discriminated by
/// `type`. One op type carries all fields; unset ones are omitted on encode (`encodeIfPresent`), so an
/// annotation op and a bookmark op serialise to exactly their own field set.
public struct ReaderPushRequest: Encodable, Sendable {
    public var ops: [ReaderWireOp]
    public init(ops: [ReaderWireOp]) { self.ops = ops }
}

public struct ReaderPushResponse: Decodable, Sendable {
    public var rev: Int
    public var applied: [Applied]
    public var contract_version: Int?
    public struct Applied: Decodable, Sendable { public var id: String; public var rev: Int? }
}

/// One upsert op. `type` ∈ {"annotation","bookmark"}. Annotation fields and bookmark fields coexist;
/// the codec sets only those for the op's kind, and nil optionals are dropped on encode.
public struct ReaderWireOp: Encodable, Equatable, Sendable {
    public var type: String
    public var id: String
    public var holding_id: Int?
    // annotation
    public var kind: String?
    public var cfi_range: String?
    public var page: Int?
    public var rect: String?
    public var color: String?
    public var note_text: String?
    public var ink: String?
    // bookmark
    public var locator: String?
    public var fraction: Double?
    public var label: String?
    // outline (entries is a JSON string of [OutlineEntry], mirroring how rect/ink ride as JSON strings)
    public var entries: String?
    // common
    public var created_at: String?
    public var updated_at: String?
    public var deleted_at: String?

    public init(type: String, id: String, holding_id: Int? = nil,
                kind: String? = nil, cfi_range: String? = nil, page: Int? = nil, rect: String? = nil,
                color: String? = nil, note_text: String? = nil, ink: String? = nil,
                locator: String? = nil, fraction: Double? = nil, label: String? = nil,
                entries: String? = nil,
                created_at: String? = nil, updated_at: String? = nil, deleted_at: String? = nil) {
        self.type = type; self.id = id; self.holding_id = holding_id
        self.kind = kind; self.cfi_range = cfi_range; self.page = page; self.rect = rect
        self.color = color; self.note_text = note_text; self.ink = ink
        self.locator = locator; self.fraction = fraction; self.label = label
        self.entries = entries
        self.created_at = created_at; self.updated_at = updated_at; self.deleted_at = deleted_at
    }
}

/// One annotation row as the store serialises it (`content_hash` is server-side, ignored here).
public struct AnnotationRecord: Decodable, Sendable {
    public var id: String
    public var holding_id: Int?
    public var kind: String?
    public var cfi_range: String?
    public var page: Int?
    public var rect: String?
    public var color: String?
    public var note_text: String?
    public var ink: String?
    public var created_at: String?
    public var updated_at: String?
    public var deleted_at: String?
    public var rev: Int?
}

/// One bookmark row.
public struct BookmarkRecord: Decodable, Sendable {
    public var id: String
    public var holding_id: Int?
    public var locator: String?
    public var fraction: Double?
    public var label: String?
    public var created_at: String?
    public var updated_at: String?
    public var deleted_at: String?
    public var rev: Int?
}

/// One authored-outline row as the store serialises it (`entries` is a JSON string of `[OutlineEntry]`).
public struct OutlineRecord: Decodable, Sendable {
    public var id: String
    public var holding_id: Int?
    public var entries: String?
    public var created_at: String?
    public var updated_at: String?
    public var deleted_at: String?
    public var rev: Int?
}

/// GET/POST `/holding/<id>/position` body — the cross-device reading position (Shape C; LWW, no rev).
public struct PositionRecord: Codable, Sendable {
    public var locator: String?
    public var fraction: Double?
    public init(locator: String? = nil, fraction: Double? = nil) {
        self.locator = locator; self.fraction = fraction
    }
}
