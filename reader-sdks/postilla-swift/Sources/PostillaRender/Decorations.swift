import Foundation
import Octavo
import Postilla

/// Maps a Postilla `Annotation` onto an Octavo `Decoration` anchored at its
/// `Locator`. CoreGraphics/Foundation only — no UIKit — so it compiles on macOS.
///
/// `ink` annotations have **no** `Decoration` style (they render through
/// `FreehandRenderer` over the input/overlay seam, not the DecorationHost), so
/// the mapping returns `nil` for them.
public enum Decorations {

    /// The Octavo decoration style for a mark kind, or `nil` when the kind does
    /// not render as a host decoration (`note` placeholder, `ink`).
    public static func style(for kind: AnnotationKind) -> Decoration.Style? {
        switch kind {
        case .highlight: return .highlight
        case .underline: return .underline
        case .strikeout: return .strikethrough
        case .note: return .note
        case .ink: return nil
        }
    }

    /// Build a `Decoration` for an annotation, or `nil` for ink / tombstones. Carries the precise
    /// anchor (`cfiRange` for EPUB text, `region` for a PDF rect) so the host can place the mark
    /// exactly instead of on a page band.
    public static func decoration(for annotation: Annotation) -> Decoration? {
        guard !annotation.isTombstone, let style = style(for: annotation.kind) else {
            return nil
        }
        return Decoration(
            id: annotation.id.uuidString,
            locator: annotation.locator,
            style: style,
            color: annotation.color,
            cfiRange: annotation.cfiRange,
            region: annotation.region
        )
    }

    /// Map a batch, dropping ink/tombstones.
    public static func decorations(for annotations: [Annotation]) -> [Decoration] {
        annotations.compactMap(decoration(for:))
    }
}
