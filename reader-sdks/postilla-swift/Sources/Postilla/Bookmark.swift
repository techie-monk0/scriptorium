import Foundation
import Octavo

/// A reading bookmark — the bookmark sibling of `Annotation` (same offline-first shape: client-minted
/// `UUID`, `updatedAt`+`rev` LWW, `deletedAt` tombstone). `locator` is where it points (a PDF page /
/// EPUB CFI); `fraction` is 0…1 progress (for a progress-bar dot); `label` is the user title.
public struct Bookmark: Codable, Equatable, Sendable, Identifiable {
    public var id: UUID
    public var publicationId: String
    public var locator: Locator?
    public var fraction: Double?
    public var label: String?
    public var createdAt: Date
    public var updatedAt: Date
    public var deletedAt: Date?
    public var rev: Int

    public init(
        id: UUID = UUID(),
        publicationId: String,
        locator: Locator? = nil,
        fraction: Double? = nil,
        label: String? = nil,
        createdAt: Date,
        updatedAt: Date,
        deletedAt: Date? = nil,
        rev: Int = 0
    ) {
        self.id = id
        self.publicationId = publicationId
        self.locator = locator
        self.fraction = fraction
        self.label = label
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.deletedAt = deletedAt
        self.rev = rev
    }

    public var isTombstone: Bool { deletedAt != nil }

    /// A copy marked deleted at `at`, bumping `updatedAt` so the tombstone wins LWW.
    public func tombstoned(at: Date) -> Bookmark {
        var copy = self
        copy.deletedAt = at
        copy.updatedAt = at
        return copy
    }
}
