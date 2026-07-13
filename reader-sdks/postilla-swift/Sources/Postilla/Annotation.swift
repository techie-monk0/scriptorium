import Foundation
import ReaderContract

/// The kinds of mark a reader can make. `ink` carries freehand handwriting;
/// the rest are text-anchored marks.
public enum AnnotationKind: String, Codable, Sendable, CaseIterable {
    case highlight
    case underline
    case strikeout
    case note
    case ink
}

/// The unit of annotation — the sync-of-record row (`postilla.md` §3). It is
/// **structured data**, not file bytes, so a mark made in any binding shows up
/// in all of them and an LLM layer can read it directly.
///
/// Keyed by a client-minted `UUID` (never a recycled int) so two offline
/// devices can't collide. `updatedAt` + `rev` drive last-write-wins; a non-nil
/// `deletedAt` is a tombstone.
public struct Annotation: Codable, Equatable, Sendable, Identifiable {
    public var id: UUID
    public var publicationId: String
    public var kind: AnnotationKind
    public var locator: Locator
    /// EPUB text range — a highlight/underline/strikeout/note *over a passage* —
    /// as an epub.js CFI range. Mirrors the catalogue store's `cfi_range`; the
    /// point-only `locator` can't express a range.
    public var cfiRange: String?
    /// PDF region as normalized `[x, y, w, h]` (0…1, page-relative). Mirrors the
    /// catalogue store's `rect`; lets a text mark or ink carry a precise box that
    /// `locator` (a point/page) alone can't. (Also the §5 precise-anchor.)
    public var region: [Double]?
    public var color: String?
    public var noteText: String?
    public var ink: Ink?
    public var createdAt: Date
    public var updatedAt: Date
    public var deletedAt: Date?
    public var rev: Int
    /// Advisory recognizer output (HWR/shape). Never load-bearing: a bad result
    /// must never mutate `ink`. See `Recognizer` and PS-U4.
    public var recognizedText: String?

    public init(
        id: UUID = UUID(),
        publicationId: String,
        kind: AnnotationKind,
        locator: Locator,
        cfiRange: String? = nil,
        region: [Double]? = nil,
        color: String? = nil,
        noteText: String? = nil,
        ink: Ink? = nil,
        createdAt: Date,
        updatedAt: Date,
        deletedAt: Date? = nil,
        rev: Int = 0,
        recognizedText: String? = nil
    ) {
        self.id = id
        self.publicationId = publicationId
        self.kind = kind
        self.locator = locator
        self.cfiRange = cfiRange
        self.region = region
        self.color = color
        self.noteText = noteText
        self.ink = ink
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.deletedAt = deletedAt
        self.rev = rev
        self.recognizedText = recognizedText
    }

    /// A tombstone is an annotation whose `deletedAt` is set.
    public var isTombstone: Bool { deletedAt != nil }

    /// A copy marked deleted at `at`, bumping `updatedAt` so the tombstone wins
    /// LWW over the live row it replaces.
    public func tombstoned(at: Date) -> Annotation {
        var copy = self
        copy.deletedAt = at
        copy.updatedAt = at
        return copy
    }
}
