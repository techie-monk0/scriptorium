import Foundation

/// A publication opened by the engine. Format dispatch (`pdf` vs `epub`) is the
/// SDK's job, not the integrator's — `FormatSniffer` resolves it from bytes
/// and/or a URL.
public struct Publication: Sendable {
    public let format: Locator.Format
    /// Free-form metadata (title/author/…); the base SDK does not model a
    /// catalogue. Integrators layer their own.
    public let metadata: [String: String]

    public init(format: Locator.Format, metadata: [String: String] = [:]) {
        self.format = format
        self.metadata = metadata
    }
}

/// Magic-byte / extension format sniffing (`OS-U2`).
public enum FormatSniffer {

    /// `%PDF` in ASCII.
    private static let pdfMagic: [UInt8] = [0x25, 0x50, 0x44, 0x46]
    /// Local file header signature of a ZIP container (`PK\u{03}\u{04}`).
    private static let zipMagic: [UInt8] = [0x50, 0x4B, 0x03, 0x04]
    /// The OCF media type that must appear in an EPUB's first (stored) entry.
    private static let epubMimetype = Array("application/epub+zip".utf8)

    /// Sniff format from the leading bytes of a resource.
    ///
    /// - `%PDF…`               → `.pdf`
    /// - ZIP whose first entry is the uncompressed `mimetype`
    ///   `application/epub+zip` → `.epub`
    public static func sniff(data: Data) -> Locator.Format? {
        let bytes = [UInt8](data.prefix(256))
        if bytes.starts(with: pdfMagic) { return .pdf }
        if bytes.starts(with: zipMagic), contains(bytes, epubMimetype) {
            return .epub
        }
        return nil
    }

    /// Sniff from a file extension when magic bytes are unavailable/ambiguous.
    public static func sniff(pathExtension ext: String) -> Locator.Format? {
        switch ext.lowercased() {
        case "pdf": return .pdf
        case "epub": return .epub
        default: return nil
        }
    }

    /// Resolve format from bytes first, falling back to a URL's extension.
    public static func sniff(data: Data?, url: URL?) -> Locator.Format? {
        if let data, let f = sniff(data: data) { return f }
        if let ext = url?.pathExtension, let f = sniff(pathExtension: ext) {
            return f
        }
        return nil
    }

    private static func contains(_ haystack: [UInt8], _ needle: [UInt8]) -> Bool {
        guard !needle.isEmpty, haystack.count >= needle.count else { return false }
        for start in 0...(haystack.count - needle.count) {
            if Array(haystack[start..<start + needle.count]) == needle { return true }
        }
        return false
    }
}
