import Foundation
import Postilla

/// The neutral **server ⇄ model** translation for the catalogue reader — the abstraction layer between
/// the wire and postilla's `Annotation`/`Bookmark`/`Locator`. It is pure (Foundation only: no
/// URLSession, no UIKit), so it runs under `swift test` and is golden-locked
/// (`reader-wire-goldens.json`); a non-iOS frontend (Android/Kotlin, web/JS) reimplements this one
/// mapping against the same goldens instead of re-deriving it from the transport code.
///
/// It owns every wire quirk: snake_case, `holding_id` int parsing, `rect`/`ink` as JSON strings, the
/// opaque bookmark `locator` string, ISO-8601 dates, and Locator `format` inference. The transports
/// (`ReaderSync`/`BookmarkSync`/`PositionSync`) keep only HTTP + auth and call through here.
///
/// `rect` is polymorphic by kind, matching the web reader (`overlay.js`) + server export
/// (`annotate_export.py`): a **text mark** (highlight/underline/strikeout) carries per-line quads
/// `[[x,y,w,h],…]` (normalized 0…1, top-left); a **note** carries a single point `[x,y]`; **ink** uses
/// the `ink` field, not `rect`.
public enum ReaderWireCodec {

    // MARK: annotations

    /// `Annotation` → push op (`type:"annotation"`).
    public static func op(from a: Annotation, holdingId: Int?) -> ReaderWireOp {
        ReaderWireOp(
            type: "annotation",
            id: a.id.uuidString,
            holding_id: holdingId,
            kind: a.kind.rawValue,
            cfi_range: a.cfiRange,
            page: a.locator.locations.page,
            rect: rectJSON(for: a),
            color: a.color,
            note_text: a.noteText,
            ink: a.ink.flatMap { try? String(decoding: $0.canonicalJSONData(), as: UTF8.self) },
            created_at: iso(a.createdAt),
            updated_at: iso(a.updatedAt),
            deleted_at: a.deletedAt.map(iso)
        )
    }

    /// Pulled annotation record → `Annotation` (nil for an unparseable id/kind). `format` is inferred:
    /// a `cfi_range` ⇒ EPUB, else PDF (documented best-effort until a format column exists).
    public static func annotation(from r: AnnotationRecord, publicationId: String) -> Annotation? {
        guard let uuid = UUID(uuidString: r.id),
              let raw = r.kind, let kind = AnnotationKind(rawValue: raw) else { return nil }
        let format: Locator.Format = (r.cfi_range != nil) ? .epub : .pdf
        let loc = Locator(publicationId: publicationId, format: format, locations: .init(page: r.page))
        // `rect` is per-kind: quads for text marks, a point for a note.
        let isTextMark = (kind == .highlight || kind == .underline || kind == .strikeout)
        return Annotation(
            id: uuid, publicationId: publicationId, kind: kind, locator: loc,
            cfiRange: r.cfi_range,
            quads: isTextMark ? r.rect.flatMap(quads(fromJSON:)) : nil,
            region: (kind == .note) ? r.rect.flatMap(doubles(fromJSON:)) : nil,
            color: r.color, noteText: r.note_text,
            ink: r.ink.flatMap { try? Ink.from(jsonData: Data($0.utf8)) },
            createdAt: date(r.created_at) ?? Date(timeIntervalSince1970: 0),
            updatedAt: date(r.updated_at) ?? Date(timeIntervalSince1970: 0),
            deletedAt: date(r.deleted_at),
            rev: r.rev ?? 0)
    }

    // MARK: bookmarks

    /// `Bookmark` → push op (`type:"bookmark"`).
    public static func op(from b: Bookmark, holdingId: Int?) -> ReaderWireOp {
        ReaderWireOp(
            type: "bookmark",
            id: b.id.uuidString,
            holding_id: holdingId,
            locator: locatorString(b.locator),
            fraction: b.fraction,
            label: b.label,
            created_at: iso(b.createdAt),
            updated_at: iso(b.updatedAt),
            deleted_at: b.deletedAt.map(iso)
        )
    }

    public static func bookmark(from r: BookmarkRecord, publicationId: String) -> Bookmark? {
        guard let uuid = UUID(uuidString: r.id) else { return nil }
        return Bookmark(
            id: uuid, publicationId: publicationId,
            locator: locator(from: r.locator, publicationId: publicationId),
            fraction: r.fraction, label: r.label,
            createdAt: date(r.created_at) ?? Date(timeIntervalSince1970: 0),
            updatedAt: date(r.updated_at) ?? Date(timeIntervalSince1970: 0),
            deletedAt: date(r.deleted_at),
            rev: r.rev ?? 0)
    }

    // MARK: outlines (wholesale per copy, JSON-string `entries`)

    /// `[OutlineEntry]` → push op (`type:"outline"`). The whole outline is one op keyed by a stable
    /// per-copy `id`; `entries` rides as a JSON string (like `rect`/`ink`).
    public static func outlineOp(entries: [OutlineEntry], id: String, holdingId: Int?,
                                 createdAt: Date? = nil, updatedAt: Date, deletedAt: Date? = nil) -> ReaderWireOp {
        ReaderWireOp(type: "outline", id: id, holding_id: holdingId,
                     entries: entriesJSON(entries),
                     created_at: createdAt.map(iso), updated_at: iso(updatedAt),
                     deleted_at: deletedAt.map(iso))
    }

    /// An outline pull row → its entries (empty on a tombstone / bad payload).
    public static func entries(from r: OutlineRecord) -> [OutlineEntry] {
        guard r.deleted_at == nil, let s = r.entries, let data = s.data(using: .utf8),
              let arr = try? JSONDecoder().decode([OutlineEntry].self, from: data) else { return [] }
        return arr
    }

    /// `[OutlineEntry]` → the JSON string the wire carries in `entries`.
    public static func entriesJSON(_ entries: [OutlineEntry]) -> String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes]
        guard let data = try? enc.encode(entries), let s = String(data: data, encoding: .utf8) else { return "[]" }
        return s
    }

    // MARK: reading position (Shape C)

    public static func positionRecord(locator: Locator, fraction: Double?) -> PositionRecord {
        PositionRecord(locator: locatorString(locator), fraction: fraction ?? locator.locations.progression)
    }

    public static func position(from r: PositionRecord, publicationId: String) -> (locator: Locator?, fraction: Double?) {
        (locator(from: r.locator, publicationId: publicationId), r.fraction)
    }

    // MARK: shared mapping helpers (the wire quirks, in one place)

    /// "holding:<id>" → <id> (also tolerates a bare int).
    public static func holdingId(from publicationId: String) -> Int? {
        if let n = Int(publicationId) { return n }
        guard let colon = publicationId.lastIndex(of: ":") else { return nil }
        return Int(publicationId[publicationId.index(after: colon)...])
    }

    /// postilla `Locator` → opaque bookmark/position `locator` string (PDF page number, else EPUB CFI).
    public static func locatorString(_ loc: Locator?) -> String? {
        guard let loc else { return nil }
        if let page = loc.locations.page { return String(page) }
        return loc.locations.cfi
    }

    /// opaque `locator` string → `Locator` (an Int ⇒ a PDF page, else an EPUB CFI).
    public static func locator(from s: String?, publicationId: String) -> Locator? {
        guard let s, !s.isEmpty else { return nil }
        if let page = Int(s) {
            return Locator(publicationId: publicationId, format: .pdf, locations: .init(page: page))
        }
        return Locator(publicationId: publicationId, format: .epub, locations: .init(cfi: s))
    }

    /// The wire `rect` for an annotation, per kind: text-mark quads `[[x,y,w,h],…]`, note point
    /// `[x,y]`, or nil (ink / no anchor).
    public static func rectJSON(for a: Annotation) -> String? {
        switch a.kind {
        case .highlight, .underline, .strikeout: return a.quads.flatMap(json(fromQuads:))
        case .note: return a.region.flatMap(json(fromDoubles:))
        case .ink: return nil
        }
    }

    public static func doubles(fromJSON s: String) -> [Double]? {
        try? JSONDecoder().decode([Double].self, from: Data(s.utf8))
    }
    public static func json(fromDoubles d: [Double]) -> String? {
        (try? JSONEncoder().encode(d)).map { String(decoding: $0, as: UTF8.self) }
    }
    public static func quads(fromJSON s: String) -> [[Double]]? {
        try? JSONDecoder().decode([[Double]].self, from: Data(s.utf8))
    }
    public static func json(fromQuads q: [[Double]]) -> String? {
        (try? JSONEncoder().encode(q)).map { String(decoding: $0, as: UTF8.self) }
    }
    public static func iso(_ date: Date) -> String {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]
        return f.string(from: date)
    }
    public static func date(_ s: String?) -> Date? {
        guard let s, !s.isEmpty else { return nil }
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }
}
