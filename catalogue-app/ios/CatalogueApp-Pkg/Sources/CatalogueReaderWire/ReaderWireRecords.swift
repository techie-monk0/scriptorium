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
    public var contract_version: Int?
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
    // common
    public var created_at: String?
    public var updated_at: String?
    public var deleted_at: String?

    public init(type: String, id: String, holding_id: Int? = nil,
                kind: String? = nil, cfi_range: String? = nil, page: Int? = nil, rect: String? = nil,
                color: String? = nil, note_text: String? = nil, ink: String? = nil,
                locator: String? = nil, fraction: Double? = nil, label: String? = nil,
                created_at: String? = nil, updated_at: String? = nil, deleted_at: String? = nil) {
        self.type = type; self.id = id; self.holding_id = holding_id
        self.kind = kind; self.cfi_range = cfi_range; self.page = page; self.rect = rect
        self.color = color; self.note_text = note_text; self.ink = ink
        self.locator = locator; self.fraction = fraction; self.label = label
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

/// GET/POST `/holding/<id>/position` body — the cross-device reading position (Shape C; LWW, no rev).
public struct PositionRecord: Codable, Sendable {
    public var locator: String?
    public var fraction: Double?
    public init(locator: String? = nil, fraction: Double? = nil) {
        self.locator = locator; self.fraction = fraction
    }
}
