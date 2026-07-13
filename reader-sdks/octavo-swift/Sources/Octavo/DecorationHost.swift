import Foundation

/// A visual decoration anchored at a `Locator` (highlight, underline, note
/// marker, …). The base SDK defines the shape; rendering is the extension's
/// (`postilla-swift`) job.
public struct Decoration: Sendable, Equatable, Identifiable {
    public enum Style: String, Sendable {
        case highlight
        case underline
        case strikethrough
        case note
    }

    public var id: String
    public var locator: Locator
    public var style: Style
    /// Optional `#rrggbb` tint; the host maps it to a platform color.
    public var color: String?
    /// Precise anchor (a mark spans a *range*, not the locator's point): an EPUB text range as an
    /// epub.js CFI range (`EpubDecorationHost` → `rendition.annotations.add`), and/or a PDF region as
    /// normalized `[x, y, w, h]` (`PdfDecorationHost` quadpoints). Either may be nil when the host
    /// falls back to a page-anchored band.
    public var cfiRange: String?
    public var region: [Double]?

    public init(id: String, locator: Locator, style: Style, color: String? = nil,
                cfiRange: String? = nil, region: [Double]? = nil) {
        self.id = id
        self.locator = locator
        self.style = style
        self.color = color
        self.cfiRange = cfiRange
        self.region = region
    }
}

/// SEAM: the extension (`postilla-swift`) plugs in here to draw decorations
/// over the engine's view. The base SDK defines the protocol only — there is no
/// built-in implementation.
@MainActor
public protocol DecorationHost: AnyObject {
    /// Render (or re-render) the given decorations.
    func apply(_ decorations: [Decoration])

    /// Remove all decorations.
    func clear()
}
