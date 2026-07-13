import Foundation

/// The single unit of "where am I / take me here / anchor a mark here".
///
/// A `Locator` is the heart of the octavo contract (`octavo.md` §3). It is the
/// same shape across every binding (web/Swift/Kotlin) so that a bookmark, a
/// search hit, a citation, or a highlight made on one platform resolves on
/// another. JSON is byte-parity with the web binding (see `OS-U1`).
///
/// Shape:
/// ```json
/// { "publicationId": "...", "format": "pdf"|"epub",
///   "locations": { "page"?, "cfi"?, "progression"?, "position"? },
///   "text"?: { "before"?, "highlight"?, "after"? } }
/// ```
public struct Locator: Codable, Equatable, Hashable, Sendable {

    /// The publication format. PDF uses `page`; EPUB uses `cfi`; `progression`
    /// is the universal fallback.
    public enum Format: String, Codable, Sendable {
        case pdf
        case epub
    }

    /// Format-tagged location. All fields optional; `progression` survives
    /// re-pagination and is the cross-format fallback.
    public struct Locations: Codable, Equatable, Hashable, Sendable {
        /// 1-based human page number (PDF).
        public var page: Int?
        /// EPUB Canonical Fragment Identifier.
        public var cfi: String?
        /// Reading progress in the publication, `0.0...1.0`.
        public var progression: Double?
        /// Engine-specific ordinal position (e.g. PDF page index).
        public var position: Int?

        public init(
            page: Int? = nil,
            cfi: String? = nil,
            progression: Double? = nil,
            position: Int? = nil
        ) {
            self.page = page
            self.cfi = cfi
            self.progression = progression
            self.position = position
        }
    }

    /// Surrounding text context — makes a locator survive re-pagination and
    /// carries the matched snippet for a search hit.
    public struct Text: Codable, Equatable, Hashable, Sendable {
        public var before: String?
        public var highlight: String?
        public var after: String?

        public init(
            before: String? = nil,
            highlight: String? = nil,
            after: String? = nil
        ) {
            self.before = before
            self.highlight = highlight
            self.after = after
        }
    }

    public var publicationId: String
    public var format: Format
    public var locations: Locations
    public var text: Text?

    public init(
        publicationId: String,
        format: Format,
        locations: Locations,
        text: Text? = nil
    ) {
        self.publicationId = publicationId
        self.format = format
        self.locations = locations
        self.text = text
    }
}

extension Locator {
    /// Encode with deterministic (sorted) keys for byte-parity goldens.
    public func jsonData() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(self)
    }

    /// Decode from the canonical contract JSON.
    public static func from(jsonData data: Data) throws -> Locator {
        try JSONDecoder().decode(Locator.self, from: data)
    }
}
